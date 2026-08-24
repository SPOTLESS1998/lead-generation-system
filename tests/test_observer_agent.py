"""Offline tests for the observer agent (scripts/observer_agent.py).

The observer is the WATCHER: each pass it flags new problems (error events past
its watermark + stalled leads) into faults, resolves faults whose problem has
cleared, and hands the rest to the healer. This exercises that whole loop with
the two side-effecting seams (the AI gateway + escalation email) mocked, so NO
network happens. Run:  venv/bin/python tests/test_observer_agent.py

Events/leads are inserted with controlled ids + timestamps via direct SQL so the
watermark and recovery logic are deterministic (no sleeps, no clock races).
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
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from core import config, state, ai, healer         # noqa: E402
import observer_agent as observer                   # noqa: E402

base_cfg = config.load_client("demo")
tmpdb = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
base_cfg["paths"]["db"] = tmpdb
conn = state.connect(tmpdb)

# Deterministic healing: force the AI diagnosis to be unavailable so decide_action
# falls back to the kind's safe default (transient->retry, stall->requeue, ...).
def ai_down(cfg, prompt, **kw):
    raise RuntimeError("all providers down")
ai.generate_json = ai_down
# Capture (and neutralize) any operator-escalation email.
smtp_calls = []
healer.sender.smtp_deliver = lambda *a, **k: smtp_calls.append(a)

P = 0; F = 0
def ok(name, cond):
    global P, F
    if cond: P += 1; print(f"  ✅ {name}")
    else:    F += 1; print(f"  ❌ {name}")

def cfg_for(client):
    c = dict(base_cfg)
    c["client"] = client
    return c

def ins_event(client, step, status, subject=None, error=None,
              created_at="2026-08-24T12:00:00+00:00"):
    cur = conn.execute(
        "INSERT INTO pipeline_events (client, step, subject, status, attempt, error, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)", (client, step, subject, status, error, created_at))
    conn.commit()
    return cur.lastrowid

def ins_lead(client, email, status, created_at):
    conn.execute("INSERT INTO leads (client, email, status, created_at) VALUES (?, ?, ?, ?)",
                 (client, email, status, created_at))
    conn.commit()

def open_fault_for(client, subject):
    for f in state.open_faults(conn, client, limit=100):
        if f["subject"] == subject:
            return f
    return None

print("\n--- A. cold start ignores backlog; new errors get flagged ------")
COLD = "obs_cold"
e1 = ins_event(COLD, "draft", "error", "old1@x.com", "timed out")
e2 = ins_event(COLD, "draft", "error", "old2@x.com", "timed out")
opened = observer.flag_error_faults(cfg_for(COLD), conn)
ok("first run flags none of the historical backlog", opened == 0)
ok("watermark seeded to the latest event", state.get_observer_watermark(conn, COLD) == e2)
ok("no faults opened on cold start", state.fault_counts(conn, COLD) == {})
ins_event(COLD, "draft", "error", "new@x.com", "timed out")   # arrives AFTER we start watching
opened = observer.flag_error_faults(cfg_for(COLD), conn)
ok("an error after cold-start IS flagged", opened == 1)

print("\n--- B. error event -> classified fault, linked, idempotent -----")
MAIN = "obs_main"
state.set_observer_watermark(conn, MAIN, 0)   # scan everything for this client
eb1 = ins_event(MAIN, "draft", "error", "b@x.com", "Request timed out after 60s")
opened = observer.flag_error_faults(cfg_for(MAIN), conn)
fb = open_fault_for(MAIN, "b@x.com")
ok("one fault opened from the error", opened == 1 and fb is not None)
ok("classified from the error text (transient)", fb["kind"] == "transient")
ok("fault linked back to the triggering event", fb["event_id"] == eb1)
ok("watermark advanced to the scanned event", state.get_observer_watermark(conn, MAIN) == eb1)
ok("re-scan with no new events flags nothing", observer.flag_error_faults(cfg_for(MAIN), conn) == 0)
ins_event(MAIN, "draft", "error", "b@x.com", "timed out AGAIN")   # same step+subject, still open
observer.flag_error_faults(cfg_for(MAIN), conn)
same = [f for f in state.open_faults(conn, MAIN) if f["subject"] == "b@x.com"]
ok("a repeat error dedups into the one open fault", len(same) == 1)

print("\n--- C. error fault auto-resolves once the step succeeds --------")
fid_b = fb["id"]
ins_event(MAIN, "draft", "ok", "b@x.com", created_at="2099-01-01T00:00:00+00:00")
observer.resolve_recovered(cfg_for(MAIN), conn)
ok("fault resolved after a later 'ok' for the same step+subject",
   state.get_fault(conn, fid_b)["status"] == "resolved")

print("\n--- D. stalled lead -> stall fault -> resolves when it advances ")
ins_lead(MAIN, "stall@x.com", "queued", "2020-01-01T00:00:00+00:00")   # queued forever
flagged = observer.flag_stalls(cfg_for(MAIN), conn)
fs = open_fault_for(MAIN, "stall@x.com")
ok("a long-queued lead is flagged as a stall", flagged >= 1 and fs is not None)
ok("stall fault carries kind='stall' on the queued step", fs["kind"] == "stall" and fs["step"] == "queued")
fid_s = fs["id"]
state.set_status(conn, MAIN, "stall@x.com", "sent")   # the pipeline finally moved it
observer.resolve_recovered(cfg_for(MAIN), conn)
ok("stall fault resolves once the lead leaves 'queued'",
   state.get_fault(conn, fid_s)["status"] == "resolved")

print("\n--- E. open fault is handed to the healer ---------------------")
ins_event(MAIN, "send", "error", "heal@x.com", "connection reset by peer")
observer.flag_error_faults(cfg_for(MAIN), conn)
fh = open_fault_for(MAIN, "heal@x.com")
healed = observer.heal_open_faults(cfg_for(MAIN), conn)
fh2 = state.get_fault(conn, fh["id"])
ok("the healer worked at least one fault", healed >= 1)
ok("a transient fault was retried (status healing, action retry)",
   fh2["status"] == "healing" and fh2["action"] == "retry")

print("\n--- F. run_once: full pass; heal-step errors never self-fault --")
ez = ins_event(MAIN, "heal", "error", "z@x.com", "healer blew up")
summary = observer.run_once(cfg_for(MAIN), conn)
ok("run_once returns a summary dict",
   isinstance(summary, dict) and {"opened", "stalled", "recovered", "healed"} <= set(summary))
heal_faults = conn.execute(
    "SELECT COUNT(*) AS c FROM faults WHERE client=? AND step='heal'", (MAIN,)).fetchone()["c"]
ok("the observer does not open faults for its own healer's errors", heal_faults == 0)
ok("watermark still advances past a non-fault error (won't rescan)",
   state.get_observer_watermark(conn, MAIN) >= ez)

conn.close()
os.unlink(tmpdb)
print(f"\n=========== {P} passed, {F} failed ===========")
sys.exit(1 if F else 0)
