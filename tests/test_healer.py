"""Offline tests for the self-healing agent (core/healer.py).

The two side-effecting seams — the AI gateway (ai.generate_json) and the operator
escalation email (sender.smtp_deliver) — are monkeypatched, so NO network happens.
Focus: the safety governor (AI can't escape the allowlist; auth/config always
escalate; retry budget is enforced) and that each action's deterministic executor
does the right thing to the lead + the fault row. Run:
    venv/bin/python tests/test_healer.py
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

from core import config, state, ai, healer         # noqa: E402

CLIENT = "demo"
cfg = config.load_client(CLIENT)
tmpdb = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
cfg["paths"]["db"] = tmpdb
conn = state.connect(tmpdb)

P = 0; F = 0
def ok(name, cond):
    global P, F
    if cond: P += 1; print(f"  ✅ {name}")
    else:    F += 1; print(f"  ❌ {name}")

# ---- monkeypatch the two side-effecting seams -----------------------------
smtp_calls = []
def smtp_ok(host, port, user, password, from_addr, to_addr, msg_string, **kw):
    smtp_calls.append({"from": from_addr, "to": to_addr, "msg": msg_string})
healer.sender.smtp_deliver = smtp_ok

def ai_down(cfg, prompt, **kw):
    raise RuntimeError("all providers failed")
def ai_says(kind, action, reason="because"):
    def _fn(cfg, prompt, **kw):
        return ({"kind": kind, "action": action, "reason": reason}, "fake")
    return _fn

def seed_lead(email, status="queued"):
    state.upsert_lead(conn, CLIENT, {"email": email}, status=status)
    if status != "queued":
        state.set_status(conn, CLIENT, email, status)

def lead_status(email):
    return state.lead_status(conn, CLIENT, email)

print("\n--- A. classify_fault (deterministic, pre-AI) -----------------")
ok("timeout => transient", healer.classify_fault("Request timed out after 60s") == "transient")
ok("429 => rate_limit", healer.classify_fault("HTTP 429 rate limit exceeded") == "rate_limit")
ok("401 invalid key => auth", healer.classify_fault("401 Unauthorized: invalid api key") == "auth")
ok("'not set' => config", healer.classify_fault("GEMINI_API_KEY not set") == "config")
ok("invalid email => data", healer.classify_fault("invalid email address for lead") == "data")
ok("gibberish => unknown", healer.classify_fault("kaboom happened") == "unknown")
ok("kind_hint wins (stall)", healer.classify_fault("whatever", kind_hint="stall") == "stall")

print("\n--- B. decide_action (the safety governor) --------------------")
ok("auth ALWAYS escalates, even if AI says retry", healer.decide_action("auth", "retry", 1, 3) == "escalate")
ok("config ALWAYS escalates", healer.decide_action("config", "requeue", 1, 3) == "escalate")
ok("out of retry budget => escalate", healer.decide_action("transient", "retry", 3, 3) == "escalate")
ok("valid AI suggestion honored", healer.decide_action("transient", "requeue", 1, 3) == "requeue")
ok("invalid AI suggestion => kind default", healer.decide_action("transient", "DROP TABLE", 1, 3) == "retry")
ok("no suggestion => kind default (data=quarantine)", healer.decide_action("data", None, 1, 3) == "quarantine")
ok("unknown + no suggestion => escalate (never guess)", healer.decide_action("unknown", None, 1, 3) == "escalate")

print("\n--- C. diagnose (AI proposes; heuristic fallback) -------------")
ai.generate_json = ai_says("transient", "retry", "temporary blip")
k, a, r = healer.diagnose(cfg, {"detail": "boom", "step": "draft", "kind": None, "attempts": 0})
ok("uses AI kind + action", k == "transient" and a == "retry")
ok("carries AI reason", "temporary blip" in r)
ai.generate_json = ai_down
k, a, r = healer.diagnose(cfg, {"detail": "connection timed out", "step": "draft", "kind": None, "attempts": 0})
ok("falls back to heuristic kind when AI down", k == "transient" and a == "retry")
ok("reason notes the fallback", "unavailable" in r.lower())

print("\n--- D. attempt_heal executors (AI down => deterministic) ------")
ai.generate_json = ai_down

seed_lead("t@x.com", "queued")
fid = state.record_fault(conn, CLIENT, "transient", "draft", "t@x.com", "timed out")
act = healer.attempt_heal(cfg, conn, dict(state.get_fault(conn, fid)))
f = state.get_fault(conn, fid)
ok("transient => retry", act == "retry")
ok("retry leaves fault healing", f["status"] == "healing" and f["attempts"] == 1)
ok("retry does not change the lead", lead_status("t@x.com") == "queued")

seed_lead("s@x.com", "sent")
fid2 = state.record_fault(conn, CLIENT, "stall", "send", "s@x.com", "stuck 2h")
act = healer.attempt_heal(cfg, conn, dict(state.get_fault(conn, fid2)))
ok("stall => requeue", act == "requeue")
ok("requeue puts the lead back to 'queued'", lead_status("s@x.com") == "queued")

seed_lead("bad@x.com", "queued")
fid3 = state.record_fault(conn, CLIENT, "data", "draft", "bad@x.com", "invalid email")
act = healer.attempt_heal(cfg, conn, dict(state.get_fault(conn, fid3)))
ok("data => quarantine", act == "quarantine")
ok("quarantine marks the lead 'quarantined'", lead_status("bad@x.com") == "quarantined")
ok("quarantine resolves the fault", state.get_fault(conn, fid3)["status"] == "resolved")

n = len(smtp_calls)
fid4 = state.record_fault(conn, CLIENT, "auth", "draft", None, "401 invalid api key")
act = healer.attempt_heal(cfg, conn, dict(state.get_fault(conn, fid4)))
ok("auth => escalate", act == "escalate")
ok("escalation emailed the operator once", len(smtp_calls) - n == 1)
ok("escalation email is operator->operator", smtp_calls[-1]["from"] == os.environ["SMTP_USER"] and smtp_calls[-1]["to"] == os.environ["SMTP_USER"])
ok("escalated fault marked 'escalated'", state.get_fault(conn, fid4)["status"] == "escalated")

print("\n--- E. retry budget: repeated fault eventually escalates ------")
ai.generate_json = ai_down
seed_lead("loop@x.com", "queued")
fid5 = state.record_fault(conn, CLIENT, "transient", "draft", "loop@x.com", "timed out")
n = len(smtp_calls)
f = dict(state.get_fault(conn, fid5))
for _ in range(6):
    if f["status"] in ("resolved", "escalated"):
        break
    healer.attempt_heal(cfg, conn, f)
    f = dict(state.get_fault(conn, fid5))
ok("escalates after exhausting max_heal_attempts", f["status"] == "escalated")
ok("attempts reached the cap (3)", f["attempts"] >= 3)
ok("escalated exactly once (one email)", len(smtp_calls) - n == 1)

print("\n--- F. escalate idempotency + fault helpers -------------------")
before = len(smtp_calls)
healer.escalate(cfg, conn, dict(state.get_fault(conn, fid4)))  # already escalated
ok("re-escalating an escalated fault sends no new email", len(smtp_calls) == before)

d1 = state.record_fault(conn, CLIENT, "transient", "draft", "dup@x.com", "boom")
d2 = state.record_fault(conn, CLIENT, "transient", "draft", "dup@x.com", "boom again")
ok("record_fault dedups an open (client,step,subject)", d1 == d2)

counts = state.fault_counts(conn, CLIENT)
ok("fault_counts returns a status->count dict", isinstance(counts, dict) and counts.get("escalated", 0) >= 1)
ok("open_faults excludes resolved/escalated", all(r["status"] in ("open", "healing") for r in state.open_faults(conn, CLIENT)))

conn.close()
os.unlink(tmpdb)
print(f"\n=========== {P} passed, {F} failed ===========")
sys.exit(1 if F else 0)
