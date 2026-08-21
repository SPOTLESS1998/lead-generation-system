"""Offline test for the booked-appointments dashboard page (GET /appointments
in scripts/approval_server.py).

Uses Flask's test_client against a throwaway DB — NO network, no real sends.
Run:  venv/bin/python tests/test_appointments.py
"""

import os
import sys
import tempfile

os.environ["UNSUB_SECRET"] = "test-secret-123"
os.environ.setdefault("SMTP_USER", "safe-inbox@example.com")
os.environ.setdefault("SMTP_PASS", "dummy-pass")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("NVIDIA_API_KEY", "x")
os.environ["CLIENT"] = "demo"        # the /appointments route resolves via config.active_client()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from datetime import datetime, timedelta          # noqa: E402
from zoneinfo import ZoneInfo                      # noqa: E402
from core import config, state                     # noqa: E402
import scripts.approval_server as A                # noqa: E402

CLIENT = "demo"
TZ = "Africa/Lagos"
cfg = config.load_client(CLIENT)

# Redirect the DB to a throwaway temp file; seed the server's cfg cache + tz.
tmpdb = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
cfg["paths"]["db"] = tmpdb
cfg["timezone"] = TZ
A._CFG_CACHE[CLIENT] = cfg
conn = state.connect(tmpdb)   # creates schema

z = ZoneInfo(TZ)
now = datetime.now(z)
fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M")

P = 0; F = 0
def ok(name, cond):
    global P, F
    if cond: P += 1; print(f"  ✅ {name}")
    else:    F += 1; print(f"  ❌ {name}")

def seed(email, first, last, company, service, starts_at, sent_minutes):
    state.upsert_lead(conn, CLIENT,
        {"email": email, "first_name": first, "last_name": last,
         "title": "COO", "company_name": company, "website_url": "https://x.co"},
        niche=service, status="meeting_booked")
    state.record_booking(conn, CLIENT, email, f"evt_{email}", starts_at)
    bid = None
    for row in state.list_appointments(conn, CLIENT):
        if row["lead_email"] == email:
            bid = row["id"]; break
    assert bid is not None, "seed failed"
    for m in sent_minutes:
        state.record_reminder(conn, CLIENT, bid, m)
    return bid

# Upcoming meeting ~45 min out: the 60-min reminder has fired; 30 & 15 have not.
# Company carries HTML to prove escaping.
seed("buyer@acme.com", "Ada", "Obi", "Acme <script>Foods",
     "WhatsApp order bot", fmt(now + timedelta(minutes=45)), sent_minutes=[60])
# Past meeting 2h ago: all three reminders fired.
seed("old@acme.com", "Bem", "Uzo", "Past Corp",
     "Voice agent", fmt(now - timedelta(minutes=120)), sent_minutes=[60, 30, 15])
conn.close()

client = A.app.test_client()

print("\n--- 1. /appointments renders the curated list -----------------")
r = client.get("/appointments")
body = r.get_data(as_text=True)
ok("returns 200", r.status_code == 200)
ok("has Upcoming section", "📅 Upcoming" in body)
ok("has Past section", "✅ Past" in body)
ok("count line: 1 upcoming, 1 past", "1 upcoming, 1 past" in body)
ok("back link to approval queue", 'href="/"' in body)

print("\n--- 2. appointment details are shown --------------------------")
ok("upcoming client name", "Ada Obi" in body)
ok("upcoming service (=niche)", "WhatsApp order bot" in body)
ok("contact as mailto link", 'href="mailto:buyer@acme.com"' in body)
ok("friendly time carries tz", f"({TZ})" in body)
ok("past client name", "Bem Uzo" in body)
ok("past service", "Voice agent" in body)

print("\n--- 3. reminder pills reflect what has fired ------------------")
ok("upcoming: 60-min shows fired (✓)", "60m ✓" in body)
ok("upcoming: 30-min shows not-yet (○)", "30m ○" in body)
ok("upcoming: 15-min shows not-yet (○)", "15m ○" in body)
ok("past: 15-min shows fired (✓)", "15m ✓" in body)

print("\n--- 4. every field is HTML-escaped ----------------------------")
ok("company HTML escaped", "Acme &lt;script&gt;Foods" in body)
ok("no raw <script> injected", "<script>" not in body)

print("\n--- 5. empty state for a client with no bookings --------------")
# Point the cache at a fresh empty DB and re-request.
empty_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
state.connect(empty_db).close()
cfg2 = dict(cfg); cfg2["paths"] = dict(cfg["paths"]); cfg2["paths"]["db"] = empty_db
A._CFG_CACHE[CLIENT] = cfg2
r2 = client.get("/appointments")
ok("empty list still 200", r2.status_code == 200)
ok("says no meetings booked", "No meetings booked yet" in r2.get_data(as_text=True))

os.unlink(tmpdb); os.unlink(empty_db)
print(f"\n=========== {P} passed, {F} failed ===========")
sys.exit(1 if F else 0)
