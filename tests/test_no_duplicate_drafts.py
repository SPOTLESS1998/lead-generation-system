"""No prospect ever gets two approvable drafts.

This suite exists because the system shipped exactly that bug: scripts/draft_queued.py
filtered out leads that already held a queue entry, scripts/lead_agent.py — the entry
point cron actually runs — did not, and 20 prospects ended up with two approvable
drafts each. Approving both would have sent the same person two cold emails.

The rule now lives in ONE place (core.review.emails_with_live_draft) and both entry
points use it. These tests pin the rule itself and the fact that both callers apply it.
"""
import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("CLIENT", "demo")
os.environ.setdefault("ANNOUNCE_TENANT", "0")
os.environ.setdefault("UNSUB_SECRET", "test-secret-123")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("NVIDIA_API_KEY", "x")

from core import review  # noqa: E402

_passed = 0
_failed = 0


def ok(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {label}")
    else:
        _failed += 1
        print(f"  ❌ {label}")


# Point the queue at a throwaway file for the whole run.
_tmp = tempfile.mkdtemp()
review.PENDING_FILE = os.path.join(_tmp, "pending_leads.json")


def write_queue(entries):
    with open(review.PENDING_FILE, "w") as f:
        json.dump(entries, f)


print("\n--- 1. a LIVE pending draft blocks a re-draft ------------------")
write_queue({
    "aaa": {"client": "demo", "target_email": "one@acme.com", "status": "pending"},
    "bbb": {"client": "demo", "target_email": "two@acme.com", "status": "pending"},
})
live = review.emails_with_live_draft("demo")
ok("both pending addresses are blocked", live == {"one@acme.com", "two@acme.com"})

print("\n--- 2. a DEAD draft does NOT block a re-draft ------------------")
# The whole point of refusing/quarantining a bad draft is that the lead gets an
# honest second attempt. If these blocked, a quarantined lead would be stranded.
write_queue({
    "aaa": {"client": "demo", "target_email": "quar@acme.com", "status": "quarantined_fabrication"},
    "bbb": {"client": "demo", "target_email": "nog@acme.com",  "status": "quarantined_no_grounding"},
    "ccc": {"client": "demo", "target_email": "dec@acme.com",  "status": "declined"},
    "ddd": {"client": "demo", "target_email": "snt@acme.com",  "status": "approved_and_sent"},
})
live = review.emails_with_live_draft("demo")
ok("quarantined (fabrication) does not block", "quar@acme.com" not in live)
ok("quarantined (no grounding) does not block", "nog@acme.com" not in live)
ok("declined does not block", "dec@acme.com" not in live)
ok("already-sent does not block", "snt@acme.com" not in live)
ok("nothing at all is blocked here", live == set())

print("\n--- 3. tenants are isolated ------------------------------------")
write_queue({
    "aaa": {"client": "demo",  "target_email": "mine@acme.com",   "status": "pending"},
    "bbb": {"client": "other", "target_email": "theirs@acme.com", "status": "pending"},
})
live = review.emails_with_live_draft("demo")
ok("only this tenant's pending draft is returned", live == {"mine@acme.com"})
ok("another tenant's draft never leaks in", "theirs@acme.com" not in live)

print("\n--- 4. malformed rows never crash the guard --------------------")
# A crash here would be worse than the bug: it would block ALL drafting.
write_queue({
    "aaa": {"client": "demo", "status": "pending"},                       # no target_email
    "bbb": {"client": "demo", "target_email": None, "status": "pending"},  # null address
    "ccc": {"client": "demo", "target_email": "good@acme.com", "status": "pending"},
})
try:
    live = review.emails_with_live_draft("demo")
    crashed = False
except Exception:
    live, crashed = set(), True
ok("missing/None addresses do not crash the guard", not crashed)
ok("blank addresses are dropped, real one kept", live == {"good@acme.com"})

print("\n--- 5. an empty queue blocks nobody ----------------------------")
write_queue({})
ok("empty queue returns empty set", review.emails_with_live_draft("demo") == set())

print("\n--- 6. BOTH drafting entry points apply the rule ----------------")
# The bug was an asymmetry between these two files, so assert the symmetry directly.
la = open(os.path.join(ROOT, "scripts", "lead_agent.py")).read()
dq = open(os.path.join(ROOT, "scripts", "draft_queued.py")).read()
ok("lead_agent.py calls emails_with_live_draft", "emails_with_live_draft" in la)
ok("draft_queued.py calls emails_with_live_draft", "emails_with_live_draft" in dq)
ok("lead_agent.py filters its drafting pool with it",
   "already_queued" in la and "l.get(\"email\") not in already_queued" in la)
ok("draft_queued.py no longer builds its own broader set",
   "for e in review.load_pending().values()" not in dq)

print(f"\n=========== {_passed} passed, {_failed} failed ===========")
sys.exit(1 if _failed else 0)
