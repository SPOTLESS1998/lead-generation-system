"""Offline tests for the appointment-reminder timing logic (core/reminders.py)
and the list/reminder state helpers (core/state.py).

NO network. Run:  venv/bin/python tests/test_reminders.py
"""

import os
import sys
import tempfile

os.environ["UNSUB_SECRET"] = "test-secret-123"
os.environ.setdefault("SMTP_USER", "safe-inbox@example.com")
os.environ.setdefault("SMTP_PASS", "dummy-pass")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from datetime import datetime, timedelta          # noqa: E402
from zoneinfo import ZoneInfo                      # noqa: E402
from core import state, reminders                  # noqa: E402

TZ = "Africa/Lagos"
CLIENT = "demo"

tmpdb = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
conn = state.connect(tmpdb)

P = 0; F = 0
def ok(name, cond):
    global P, F
    if cond: P += 1; print(f"  ✅ {name}")
    else:    F += 1; print(f"  ❌ {name}")

print("\n--- 1. list_appointments joins booking → lead -----------------")
state.upsert_lead(conn, CLIENT,
    {"email": "buyer@acme.com", "first_name": "Ada", "last_name": "Obi",
     "title": "COO", "company_name": "Acme Foods", "website_url": "https://acme.com"},
    niche="WhatsApp order-taking bot", status="meeting_booked")
state.record_booking(conn, CLIENT, "buyer@acme.com", "evt_1", "2026-08-24T10:00")
appts = state.list_appointments(conn, CLIENT)
ok("one appointment returned", len(appts) == 1)
a = appts[0]
ok("carries first name", a["first_name"] == "Ada")
ok("carries company", a["company_name"] == "Acme Foods")
ok("carries service (=niche)", a["service"] == "WhatsApp order-taking bot")
ok("carries contact email", a["lead_email"] == "buyer@acme.com")
ok("carries start time", a["starts_at"] == "2026-08-24T10:00")
ok("carries booking id", isinstance(a["id"], int))

print("\n--- 2. parse_start handles the stored formats -----------------")
z = ZoneInfo(TZ)
ok("T separator",   reminders.parse_start("2026-08-24T10:00", TZ) == datetime(2026, 8, 24, 10, 0, tzinfo=z))
ok("space separator", reminders.parse_start("2026-08-24 10:00", TZ) == datetime(2026, 8, 24, 10, 0, tzinfo=z))
ok("with seconds",  reminders.parse_start("2026-08-24T10:00:30", TZ) == datetime(2026, 8, 24, 10, 0, 30, tzinfo=z))
ok("empty → None",  reminders.parse_start("", TZ) is None)
ok("garbage → None", reminders.parse_start("not-a-date", TZ) is None)
ok("bad tz still parses (UTC fallback)", reminders.parse_start("2026-08-24T10:00", "Not/AZone") is not None)

print("\n--- 3. due_intervals windows (most-urgent-first) --------------")
start = datetime(2026, 8, 24, 10, 0, tzinfo=z)
at = lambda m: start - timedelta(minutes=m)
ok("T-90 → []",        reminders.due_intervals(start, at(90)) == [])
ok("T-60 → [60]",      reminders.due_intervals(start, at(60)) == [60])
ok("T-45 → [60]",      reminders.due_intervals(start, at(45)) == [60])
ok("T-30 → [60,30]",   reminders.due_intervals(start, at(30)) == [60, 30])
ok("T-20 → [60,30]",   reminders.due_intervals(start, at(20)) == [60, 30])
ok("T-15 → [60,30,15]", reminders.due_intervals(start, at(15)) == [60, 30, 15])
ok("T-5 → [60,30,15]", reminders.due_intervals(start, at(5)) == [60, 30, 15])
ok("at start → []",    reminders.due_intervals(start, start) == [])
ok("after start → []", reminders.due_intervals(start, start + timedelta(minutes=1)) == [])
ok("None start → []",  reminders.due_intervals(None, start) == [])

print("\n--- 4. minutes_until ------------------------------------------")
ok("T-45 → 45",     reminders.minutes_until(start, at(45)) == 45)
ok("past → negative", reminders.minutes_until(start, start + timedelta(minutes=10)) == -10)

print("\n--- 5. reminder_sent / record_reminder idempotency ------------")
bid = a["id"]
ok("not sent initially", not state.reminder_sent(conn, CLIENT, bid, 60))
state.record_reminder(conn, CLIENT, bid, 60)
ok("sent after record", state.reminder_sent(conn, CLIENT, bid, 60))
state.record_reminder(conn, CLIENT, bid, 60)  # duplicate → INSERT OR IGNORE
cnt = conn.execute("SELECT COUNT(*) c FROM reminders WHERE client=? AND booking_id=? AND minutes=?",
                   (CLIENT, bid, 60)).fetchone()["c"]
ok("duplicate ignored (still 1 row)", cnt == 1)
ok("other interval independent", not state.reminder_sent(conn, CLIENT, bid, 30))

conn.close()
os.unlink(tmpdb)
print(f"\n=========== {P} passed, {F} failed ===========")
sys.exit(1 if F else 0)
