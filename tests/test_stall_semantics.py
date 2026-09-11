"""Offline tests for the stall-semantics fix — the bug that would have made unattended
runs email the operator in a loop.

THE BUG BEING PINNED
`queued` used to mean two different things at once: "the drafter is working on this"
AND "a draft is waiting for the operator to approve it". The stall detector flags any
lead sitting in `queued` past observability.stall_minutes, which correctly catches a
dead drafter — but it also flagged every draft correctly waiting for a human. The
healer's remedy for a stall is `requeue`, which set status to `queued`: the value the
lead was already stuck in. resolve_recovered only closes a stall when the lead LEAVES
the status, which it never did, and open_faults() includes 'healing'. So the fault
re-opened every pass, re-diagnosing with an LLM call each time, until max_heal_attempts
escalated it to the operator. At daily volume: an escalation-email storm.

The fix is that one status means one thing. These tests pin the loop closed.

Run:  venv/bin/python tests/test_stall_semantics.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import state, healer                                    # noqa: E402
import scripts.observer_agent as obs_agent                        # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _tmpdb():
    return tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name


def _lead(email="a@a.com", company="Acme"):
    return {"first_name": "A", "last_name": "B", "title": "CEO", "email": email,
            "company_name": company, "company_description": "d", "company_facts": "f",
            "website_url": f"https://{email.split('@')[1]}", "ejentic_service": "Svc"}


def _age(conn, email, minutes):
    """Backdate a lead's status_changed_at to simulate time passing."""
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    conn.execute("UPDATE leads SET status_changed_at=? WHERE email=?", (when, email))
    conn.commit()


def _cfg(**over):
    cfg = {"client": "t", "client_name": "T",
           "observability": {"enabled": True, "stall_minutes": {"queued": 120},
                             "escalate_email": False, "max_heal_attempts": 3}}
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------
def test_awaiting_approval_is_never_stalled():
    print("\n[a draft waiting for a human is NOT a stall — however long it waits]")
    conn = state.connect(_tmpdb())
    state.bank_leads(conn, "t", [_lead()])
    state.set_status(conn, "t", "a@a.com", state.AWAITING_APPROVAL)
    _age(conn, "a@a.com", 60 * 24 * 30)          # parked for a month

    # The default config watches only 'queued', so this is the realistic path.
    flagged = obs_agent.flag_stalls(_cfg(), conn)
    check("nothing flagged for a month-old approval wait", flagged == 0)
    check("no fault was opened", state.fault_counts(conn, "t") == {})

    # And even if a config WRONGLY lists it, the observer refuses.
    cfg_bad = _cfg(observability={"enabled": True, "escalate_email": True,
                                  "max_heal_attempts": 3,
                                  "stall_minutes": {"awaiting_approval": 120}})
    flagged_bad = obs_agent.flag_stalls(cfg_bad, conn)
    check("a misconfigured stall_minutes on 'awaiting_approval' is ignored", flagged_bad == 0)
    check("...and opened no fault", state.fault_counts(conn, "t") == {})

    # 'sourced' is the other waiting state (for a future drafting slot) — same rule.
    state.bank_leads(conn, "t", [_lead("b@b.com", "Beta")])
    _age(conn, "b@b.com", 60 * 24 * 30)
    cfg_src = _cfg(observability={"enabled": True, "escalate_email": True,
                                  "max_heal_attempts": 3,
                                  "stall_minutes": {"sourced": 120}})
    check("a misconfigured stall_minutes on 'sourced' is ignored too",
          obs_agent.flag_stalls(cfg_src, conn) == 0)


def test_genuinely_stuck_queued_is_still_caught():
    print("\n[a lead the drafter claimed and never finished IS still a stall]")
    conn = state.connect(_tmpdb())
    state.bank_leads(conn, "t", [_lead()])
    state.set_status(conn, "t", "a@a.com", "queued")
    _age(conn, "a@a.com", 300)                   # 5h, past the 120m threshold

    flagged = obs_agent.flag_stalls(_cfg(), conn)
    check("the genuinely stuck lead is flagged", flagged == 1)
    counts = state.fault_counts(conn, "t")
    check("exactly one open fault", counts.get("open") == 1)

    # A lead claimed a moment ago must NOT be flagged (this is the created_at trap:
    # banked long ago, claimed just now).
    conn2 = state.connect(_tmpdb())
    state.bank_leads(conn2, "t", [_lead("c@c.com", "Cee")])
    conn2.execute("UPDATE leads SET created_at=? WHERE email='c@c.com'",
                  ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),))
    conn2.commit()
    state.set_status(conn2, "t", "c@c.com", "queued")     # claimed NOW, created 30d ago
    check("a lead claimed just now is not flagged (measures status age, not row age)",
          obs_agent.flag_stalls(_cfg(), conn2) == 0)


def test_requeue_actually_moves_the_lead():
    print("\n[the healer's requeue genuinely re-queues — it is not a no-op]")
    conn = state.connect(_tmpdb())
    state.bank_leads(conn, "t", [_lead()])
    state.set_status(conn, "t", "a@a.com", "queued")
    fid = state.record_fault(conn, "t", "stall", "queued", "a@a.com", "stuck", event_id=None)
    fault = [dict(f) for f in state.open_faults(conn, "t") if f["id"] == fid][0]

    cfg = _cfg()
    action = healer._execute(cfg, conn, fault, "stall", "requeue", "test", 1)
    check("the lead left 'queued'", state.lead_status(conn, "t", "a@a.com") != "queued")
    check("it is back in the drafting pool as 'sourced'",
          state.lead_status(conn, "t", "a@a.com") == state.SOURCED)

    # And the fault can now RESOLVE — the half that was impossible before, because the
    # lead never left the state the fault was about.
    state.resolve_fault(conn, fid, "test")
    check("a requeued lead's fault can be resolved",
          state.lead_status(conn, "t", "a@a.com") == state.SOURCED)


def test_resolve_loop_is_closed():
    print("\n[the escalation loop is actually closed end to end]")
    conn = state.connect(_tmpdb())
    state.bank_leads(conn, "t", [_lead()])
    state.set_status(conn, "t", "a@a.com", "queued")
    _age(conn, "a@a.com", 300)

    cfg = _cfg(observability={"enabled": True, "escalate_email": True,
                              "max_heal_attempts": 3, "stall_minutes": {"queued": 120}})
    obs_agent.flag_stalls(cfg, conn)
    check("fault opened", state.fault_counts(conn, "t").get("open") == 1)

    # Heal it, then resolve exactly as the observer's own pass does.
    for f in state.open_faults(conn, "t"):
        healer.attempt_heal(cfg, conn, dict(f))
    check("the lead moved out of 'queued' during healing",
          state.lead_status(conn, "t", "a@a.com") == state.SOURCED)

    obs_agent.resolve_recovered(cfg, conn)
    counts = state.fault_counts(conn, "t")
    check("the fault is resolved, not stuck healing",
          counts.get("open", 0) == 0 and counts.get("healing", 0) == 0)
    check("nothing escalated to a human", counts.get("escalated", 0) == 0)

    # The proof of the fix: a second pass finds the fault already closed and does no
    # further work. Before the fix this looped forever.
    before = state.fault_counts(conn, "t")
    heals = obs_agent.heal_open_faults(cfg, conn)
    check("a second pass has no open faults left to re-heal", heals == 0)
    check("no new faults appeared", state.fault_counts(conn, "t") == before)


def test_approving_moves_the_lead_on():
    print("\n[the approval path can move an awaiting_approval lead forward]")
    conn = state.connect(_tmpdb())
    state.bank_leads(conn, "t", [_lead()])
    state.set_status(conn, "t", "a@a.com", state.AWAITING_APPROVAL)
    check("awaiting_approval counts as contacted (never re-drafted)",
          state.already_contacted(conn, "t", "a@a.com") is True)
    state.set_status(conn, "t", "a@a.com", "approved")
    check("approval moves it to 'approved'",
          state.lead_status(conn, "t", "a@a.com") == "approved")
    check("status_changed_at was stamped on the move",
          conn.execute("SELECT status_changed_at FROM leads WHERE email='a@a.com'")
              .fetchone()["status_changed_at"] is not None)


def test_migration_splits_the_overloaded_state():
    print("\n[a pre-existing DB has its overloaded 'queued' rows split]")
    path = _tmpdb()
    conn = state.connect(path)
    state.bank_leads(conn, "t", [_lead("old@old.com", "Old"), _lead("mid@mid.com", "Mid")])
    # Simulate the OLD world: both are 'queued', which used to cover both meanings.
    conn.execute("UPDATE leads SET status='queued'")
    conn.commit()
    # One of them has an audit page, which only exists for a COMPLETED draft.
    state.save_magnet(conn, "t", "tok1", "old@old.com", {"title": "x"})
    conn.commit()
    conn.close()

    conn = state.connect(path)                   # runs _migrate
    check("the drafted lead became awaiting_approval",
          state.lead_status(conn, "t", "old@old.com") == state.AWAITING_APPROVAL)
    check("the mid-draft lead stayed queued",
          state.lead_status(conn, "t", "mid@mid.com") == "queued")
    check("status_changed_at was backfilled for every row",
          all(r["status_changed_at"] for r in conn.execute("SELECT status_changed_at FROM leads")))
    check("re-connecting is a safe no-op",
          state.connect(path) is not None)


test_awaiting_approval_is_never_stalled()
test_genuinely_stuck_queued_is_still_caught()
test_requeue_actually_moves_the_lead()
test_resolve_loop_is_closed()
test_approving_moves_the_lead_on()
test_migration_splits_the_overloaded_state()

print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
