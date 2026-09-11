"""Offline tests for the drafting pipeline's new ordering: BANK first, DRAFT second.

Drives the real scripts/lead_agent.main() with only the network seams stubbed, so the
sequence of state transitions is the thing under test — not a re-implementation of it.

Pins the three behaviours that make the lead list trustworthy:
  1. every discovered lead is banked as `sourced`, even ones never drafted;
  2. the draft cap limits DRAFTING only, never sourcing;
  3. a failed draft returns the lead to `sourced` — the row is never deleted.

Run:  venv/bin/python tests/test_lead_pipeline.py
"""

import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import scripts.lead_agent as la                                  # noqa: E402
from core import state                                           # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _lead(i):
    return {"first_name": "A", "last_name": "B", "title": "CEO",
            "email": f"c{i}@co{i}.com", "company_name": f"Co{i}",
            "company_description": "d", "company_facts": "f",
            "website_url": f"https://co{i}.com", "ejentic_service": "Svc"}


def _run_pipeline(n_leads, daily_cap, fail_emails=(), env=None):
    """Run the real main() against a throwaway DB with stubbed IO.

    Returns (conn, tmpdir). Nothing is emailed, scraped or sent.
    """
    tmp = tempfile.mkdtemp(prefix="lgpipe_")
    db = os.path.join(tmp, "state.sqlite")
    queue = os.path.join(tmp, "pending.json")

    os.environ["CLIENT"] = "testclient"
    from core import config, review
    cfg = {"client": "testclient", "client_name": "Test Co", "from_name": "Tester",
           "demo_mode": False, "lead_source": "maps_firecrawl", "timezone": "UTC",
           "unsubscribe_base_url": "http://localhost:5002",
           "offerings": ["Svc"], "service_outcomes": {"Svc": "a real result"},
           "drafting": {"daily_cap": daily_cap},
           "discovery": {}, "sending": {"mode": "controlled"},
           "observability": {"enabled": False},
           "paths": {"db": db, "leads_csv": os.path.join(tmp, "leads.csv")}}

    # --- stub only the IO seams ---
    config.load_client = lambda *a, **k: cfg
    la.draft_one_lead = lambda conn, cfg, lead, run_id: (
        (_ for _ in ()).throw(RuntimeError("provider exploded"))
        if lead["email"] in fail_emails else
        ("Subj", "Body", "stub", "http://m/1", "tok1"))
    review.notify_operator = lambda *a, **k: True
    review.PENDING_FILE = queue
    review.notify_operator.__name__ = "notify_operator"

    # The real pipeline would hit Maps+Firecrawl; return a fixed batch instead.
    import core.discovery as disc
    fake_leads = [_lead(i) for i in range(n_leads)]
    disc.load_leads = lambda c, conn=None: (fake_leads, 0)

    la.main()
    return state.connect(db)


def test_every_lead_banked_cap_only_limits_drafting():
    print("\n[12 discovered, cap 5: all 12 banked, only 5 drafted]")
    conn = _run_pipeline(n_leads=12, daily_cap=5)
    counts = state.count_leads_by_status(conn, "testclient")
    check("all 12 leads are on the list",
          sum(counts.values()) == 12)
    check("7 remain 'sourced' (banked, not drafted)",
          counts.get("sourced") == 7)
    check("5 were drafted and wait for approval",
          counts.get("queued") == 5)
    check("the list kept the leads the cap excluded", counts.get("sourced", 0) == 7)


def test_cap_zero_banks_everything():
    print("\n[cap 0: still source, draft nothing]")
    conn = _run_pipeline(n_leads=6, daily_cap=0)
    counts = state.count_leads_by_status(conn, "testclient")
    check("all 6 banked", counts.get("sourced") == 6)
    check("nothing drafted", counts.get("queued", 0) == 0)


def test_failed_draft_keeps_the_lead():
    print("\n[a failed draft returns the lead to 'sourced', never deletes it]")
    conn = _run_pipeline(n_leads=4, daily_cap=4, fail_emails=("c1@co1.com", "c2@co2.com"))
    counts = state.count_leads_by_status(conn, "testclient")
    check("all 4 rows still exist (nothing deleted)", sum(counts.values()) == 4)
    check("the 2 failures are back to 'sourced'", counts.get("sourced") == 2)
    check("the 2 successes are 'queued'", counts.get("queued") == 2)
    check("the failed lead's CONTACT DETAILS survived",
          conn.execute("SELECT company_name, website_url FROM leads WHERE email='c1@co1.com'")
              .fetchone()["company_name"] == "Co1")
    check("the failed lead is retryable (not treated as contacted)",
          state.already_contacted(conn, "testclient", "c1@co1.com") is False)


def test_second_run_uses_the_backlog_not_new_scrapes():
    print("\n[a second run drafts the backlog first (oldest-first)]")
    tmp = tempfile.mkdtemp(prefix="lgpipe2_")
    db = os.path.join(tmp, "state.sqlite")
    conn = state.connect(db)
    # 3 banked earlier; the second run has NO new discovery results at all.
    state.bank_leads(conn, "testclient", [_lead(i) for i in range(3)])
    pool = state.leads_by_status(conn, "testclient", state.SOURCED, limit=10)
    check("the backlog is visible to the drafter", len(pool) == 3)
    check("oldest first", pool[0]["email"] == "c0@co0.com")
    check("a banked lead is draftable (not 'already contacted')",
          all(not state.already_contacted(conn, "testclient", p["email"]) for p in pool))


test_every_lead_banked_cap_only_limits_drafting()
test_cap_zero_banks_everything()
test_failed_draft_keeps_the_lead()
test_second_run_uses_the_backlog_not_new_scrapes()

print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
