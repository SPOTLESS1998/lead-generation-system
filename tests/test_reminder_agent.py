"""Offline tests for the appointment reminder agent (scripts/reminder_agent.py).

The three side-effecting seams — SMTP send, macOS osascript, and the AI gateway —
are monkeypatched, so NO network / no desktop popups fire. Run:
    venv/bin/python tests/test_reminder_agent.py
"""

import os
import sys
import tempfile

os.environ["UNSUB_SECRET"] = "test-secret-123"
os.environ.setdefault("SMTP_USER", "safe-inbox@example.com")
os.environ.setdefault("SMTP_PASS", "dummy-pass")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("NVIDIA_API_KEY", "x")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from datetime import datetime, timedelta          # noqa: E402
from zoneinfo import ZoneInfo                      # noqa: E402
from core import config, state                     # noqa: E402
import scripts.reminder_agent as RA                # noqa: E402

CLIENT = "demo"
TZ = "Africa/Lagos"
cfg = config.load_client(CLIENT)
tmpdb = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
cfg["paths"]["db"] = tmpdb
cfg["timezone"] = TZ
cfg["reminders"] = {"enabled": True, "lead_times_min": [60, 30, 15], "email": True, "desktop": True}
conn = state.connect(tmpdb)

z = ZoneInfo(TZ)
SMTP_USER = os.environ["SMTP_USER"]

P = 0; F = 0
def ok(name, cond):
    global P, F
    if cond: P += 1; print(f"  ✅ {name}")
    else:    F += 1; print(f"  ❌ {name}")

# ---- fakes for the three side-effecting seams -----------------------------
smtp_calls = []
osascript_calls = []

class _Done:
    def __init__(self, rc): self.returncode = rc

def smtp_ok(host, port, user, password, from_addr, to_addr, msg_string, **kw):
    smtp_calls.append({"from": from_addr, "to": to_addr, "user": user, "msg": msg_string})

def smtp_fail(*a, **k):
    raise RuntimeError("smtp down")

def run_ok(args, **kw):
    osascript_calls.append(args); return _Done(0)

def run_fail(args, **kw):
    osascript_calls.append(args); return _Done(1)

def gen_ok(cfg, prompt):
    return ("Your quick intro call is coming right up.", "fake")

def gen_fail(cfg, prompt):
    raise RuntimeError("all providers down")

def patch(smtp=smtp_ok, run=run_ok, gen=gen_ok):
    RA.sender.smtp_deliver = smtp
    RA.subprocess.run = run
    RA.generate = gen

def seed(email, first, company, service, starts_at, niche_status="meeting_booked"):
    state.upsert_lead(conn, CLIENT,
        {"email": email, "first_name": first, "last_name": "Test",
         "title": "COO", "company_name": company, "website_url": "https://x.co"},
        niche=service, status=niche_status)
    state.record_booking(conn, CLIENT, email, f"evt_{email}", starts_at)
    for row in state.list_appointments(conn, CLIENT):
        if row["lead_email"] == email:
            return row
    raise AssertionError("seed failed")

start_str = "2026-08-24T10:00"
start = datetime(2026, 8, 24, 10, 0, tzinfo=z)
at = lambda m: start - timedelta(minutes=m)

print("\n--- A. _compose_line: AI line, then template fallback ---------")
patch(gen=gen_ok)
ctxA = {"name": "Ada", "company": "Acme", "service": "WhatsApp bot", "minutes": 30, "when": "soon"}
ok("uses AI line when gateway works", RA._compose_line(cfg, ctxA) == "Your quick intro call is coming right up.")
patch(gen=gen_fail)
line = RA._compose_line(cfg, ctxA)
ok("template fallback on AI failure", line.startswith("Heads-up") and "WhatsApp bot" in line and "30 minutes" in line)

print("\n--- B. fire once + idempotency (T-45 → 60, then quiet) --------")
patch()  # all good
r1 = seed("buyer@acme.com", "Ada", "Acme Foods", "WhatsApp order bot", start_str)
n0 = len(smtp_calls)
fired = RA.process_appointment(cfg, conn, r1, at(45), TZ)
ok("T-45 fires the 60-min reminder", fired == 60)
ok("exactly one email sent", len(smtp_calls) - n0 == 1)
ok("60-min recorded", state.reminder_sent(conn, CLIENT, r1["id"], 60))
ok("30-min NOT yet recorded", not state.reminder_sent(conn, CLIENT, r1["id"], 30))
n1 = len(smtp_calls)
ok("T-44 fires nothing (60 already sent)", RA.process_appointment(cfg, conn, r1, at(44), TZ) is None)
ok("no extra email on quiet pass", len(smtp_calls) == n1)
ok("T-20 fires the 30-min reminder", RA.process_appointment(cfg, conn, r1, at(20), TZ) == 30)
ok("30-min now recorded", state.reminder_sent(conn, CLIENT, r1["id"], 30))
ok("15-min still not recorded", not state.reminder_sent(conn, CLIENT, r1["id"], 15))

print("\n--- C. catch-up/supersede (first-ever pass at T-5) ------------")
patch()
r2 = seed("late@acme.com", "Bto", "Late Corp", "Voice agent", start_str)
n0 = len(smtp_calls)
fired = RA.process_appointment(cfg, conn, r2, at(5), TZ)
ok("fires ONE ping, the most-urgent (15)", fired == 15)
ok("exactly one email — no burst of three", len(smtp_calls) - n0 == 1)
ok("60 superseded (recorded)", state.reminder_sent(conn, CLIENT, r2["id"], 60))
ok("30 superseded (recorded)", state.reminder_sent(conn, CLIENT, r2["id"], 30))
ok("15 recorded", state.reminder_sent(conn, CLIENT, r2["id"], 15))
ok("later pass at T-4 stays silent (no stale 60)", RA.process_appointment(cfg, conn, r2, at(4), TZ) is None)

print("\n--- D. both channels + per-channel toggles --------------------")
patch()
r3 = seed("chan@acme.com", "Cy", "Chan Ltd", "Chatbot", start_str)
so, oo = len(smtp_calls), len(osascript_calls)
RA.process_appointment(cfg, conn, r3, at(45), TZ)
import email as emaillib
from email.header import make_header, decode_header
_subj = str(make_header(decode_header(emaillib.message_from_string(smtp_calls[-1]["msg"])["Subject"])))
ok("email went to operator (from==to==SMTP_USER)",
   smtp_calls[-1]["from"] == SMTP_USER and smtp_calls[-1]["to"] == SMTP_USER)
ok("subject carries the minutes + service", "60 min" in _subj and "Chatbot" in _subj)
ok("desktop banner fired (osascript called)", len(osascript_calls) - oo == 1)
ok("osascript invoked as 'osascript -e ...'", osascript_calls[-1][0] == "osascript" and osascript_calls[-1][1] == "-e")

# email-only
cfg["reminders"]["desktop"] = False
r4 = seed("emailonly@acme.com", "Dee", "EO Ltd", "RAG", start_str)
so, oo = len(smtp_calls), len(osascript_calls)
RA.process_appointment(cfg, conn, r4, at(45), TZ)
ok("email-only: email sent", len(smtp_calls) - so == 1)
ok("email-only: NO desktop call", len(osascript_calls) - oo == 0)
ok("email-only still records (won't repeat)", state.reminder_sent(conn, CLIENT, r4["id"], 60))

# desktop-only
cfg["reminders"] = {"enabled": True, "lead_times_min": [60, 30, 15], "email": False, "desktop": True}
r5 = seed("desktoponly@acme.com", "Eff", "DO Ltd", "OCR", start_str)
so, oo = len(smtp_calls), len(osascript_calls)
RA.process_appointment(cfg, conn, r5, at(45), TZ)
ok("desktop-only: NO email", len(smtp_calls) - so == 0)
ok("desktop-only: desktop fired", len(osascript_calls) - oo == 1)
cfg["reminders"] = {"enabled": True, "lead_times_min": [60, 30, 15], "email": True, "desktop": True}

print("\n--- E. edges: past meeting, and failure → retry ---------------")
patch()
r6 = seed("past@acme.com", "Gee", "Past Inc", "Bot", start_str)
so = len(smtp_calls)
ok("started/past → no reminder", RA.process_appointment(cfg, conn, r6, start + timedelta(minutes=10), TZ) is None)
ok("no email for a past meeting", len(smtp_calls) == so)

# both channels fail → not recorded → retried and succeeds next pass
r7 = seed("retry@acme.com", "Haw", "Retry Co", "Voice", start_str)
patch(smtp=smtp_fail, run=run_fail)
ok("both channels fail → returns None", RA.process_appointment(cfg, conn, r7, at(45), TZ) is None)
ok("failure recorded nothing (will retry)", not state.reminder_sent(conn, CLIENT, r7["id"], 60))
patch(smtp=smtp_ok, run=run_ok)
ok("retry succeeds on next pass", RA.process_appointment(cfg, conn, r7, at(44), TZ) == 60)
ok("now recorded after successful retry", state.reminder_sent(conn, CLIENT, r7["id"], 60))

print("\n--- F. print_upcoming renders without error ------------------")
try:
    RA.print_upcoming(cfg, conn, at(120), TZ)
    ok("print_upcoming ran cleanly", True)
except Exception as e:
    ok(f"print_upcoming ran cleanly ({e})", False)

conn.close()
os.unlink(tmpdb)
print(f"\n=========== {P} passed, {F} failed ===========")
sys.exit(1 if F else 0)
