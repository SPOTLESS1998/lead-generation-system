"""Offline tests for the durable lead list — NO network, throwaway DBs.

The lead list is the asset: every qualified lead discovery finds must be banked and
STAY banked, whatever happens to the draft afterwards. These tests pin the three
things that were broken before it existed:

  1. a lead is persisted the moment it is qualified, not just before drafting;
  2. leads beyond the draft cap are still banked (sourcing != drafting);
  3. a FAILED draft releases its claim without destroying the contact — the old code
     did `DELETE FROM leads`, throwing away a scraped lead on any provider hiccup.

Plus the cross-run de-dupe key (website_domain) that stops us re-buying leads we
already own, including its backfill onto a pre-existing DB.

Run:  venv/bin/python tests/test_lead_list.py
"""

import os
import sys
import sqlite3
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import state                          # noqa: E402
from core.leads import domain_key               # noqa: E402

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


def _lead(email, company="Acme Ltd", url="https://www.acme.com/", service=""):
    return {"first_name": "A", "last_name": "B", "title": "CEO", "email": email,
            "company_name": company, "company_description": "Does things.",
            "company_facts": "Services: things.", "website_url": url,
            "ejentic_service": service}


# --------------------------------------------------------------------------
def test_domain_key():
    print("\n[domain_key normalises hosts for de-duplication]")
    cases = {
        "https://www.Foo.com/contact": "foo.com",
        "http://foo.com": "foo.com",
        "foo.com": "foo.com",
        "https://foo.com:8443/x": "foo.com",
        "https://user@foo.com/": "foo.com",
        "": "",
        None: "",
    }
    for raw, want in cases.items():
        check(f"{raw!r} -> {want!r}", domain_key(raw) == want)
    check("www and non-www collapse to the same key",
          domain_key("https://www.acme.com") == domain_key("http://acme.com/contact"))


# --------------------------------------------------------------------------
def test_banking_persists_every_lead():
    print("\n[every qualified lead is banked, independent of drafting]")
    conn = state.connect(_tmpdb())
    leads = [_lead(f"c{i}@x{i}.com", company=f"Co{i}", url=f"https://x{i}.com") for i in range(25)]
    new = state.bank_leads(conn, "t", leads)
    check("all 25 banked in one call", new == 25)
    check("all 25 are on the list", state.count_leads(conn, "t") == 25)
    check("all 25 are status 'sourced'",
          state.count_leads_by_status(conn, "t") == {"sourced": 25})

    # Re-banking the same batch (a later run rediscovering them) must be a no-op.
    again = state.bank_leads(conn, "t", leads)
    check("re-banking the same leads adds nothing", again == 0)
    check("still exactly 25 rows", state.count_leads(conn, "t") == 25)

    # A lead with no email cannot be contacted, so it is not banked.
    check("a lead with no email is not banked",
          state.bank_leads(conn, "t", [_lead("")]) == 0)


def test_sourced_is_not_contacted():
    print("\n[a banked lead has NOT been contacted]")
    conn = state.connect(_tmpdb())
    state.bank_leads(conn, "t", [_lead("a@a.com")])
    check("status is 'sourced'", state.lead_status(conn, "t", "a@a.com") == "sourced")
    check("'sourced' is NOT in CONTACTED_STATES",
          state.SOURCED not in state.CONTACTED_STATES)
    check("already_contacted() is False for a banked lead",
          state.already_contacted(conn, "t", "a@a.com") is False)
    # This is the invariant that keeps the list usable: if `sourced` counted as
    # contacted, every banked lead would be permanently unreachable.
    from core import suppression
    skip, reason = suppression.should_skip(conn, "t", "a@a.com")
    check("suppression does not skip a banked-but-uncontacted lead", skip is False)


def test_claim_and_release_keeps_the_lead():
    print("\n[a failed draft releases the claim WITHOUT losing the lead]")
    conn = state.connect(_tmpdb())
    state.bank_leads(conn, "t", [_lead("a@a.com")])

    state.set_status(conn, "t", "a@a.com", "queued")     # the drafter claims it
    check("claimed lead counts as contacted (no double-draft)",
          state.already_contacted(conn, "t", "a@a.com") is True)

    state.set_status(conn, "t", "a@a.com", state.SOURCED)  # draft failed -> release
    check("released lead is back to 'sourced'",
          state.lead_status(conn, "t", "a@a.com") == "sourced")
    check("the CONTACT DETAILS survived the failure",
          conn.execute("SELECT company_facts FROM leads WHERE email='a@a.com'")
              .fetchone()["company_facts"] == "Services: things.")
    check("the row still exists (not DELETEd)", state.count_leads(conn, "t") == 1)


def test_known_domains_and_pool():
    print("\n[known_domains + the oldest-first drafting pool]")
    conn = state.connect(_tmpdb())
    state.bank_leads(conn, "t", [
        _lead("a@acme.com", url="https://www.acme.com/"),
        _lead("b@beta.com", url="http://beta.com/contact"),
    ])
    known = state.known_domains(conn, "t")
    check("acme.com is known (www stripped)", "acme.com" in known)
    check("beta.com is known", "beta.com" in known)
    check("an unseen domain is not known", "gamma.com" not in known)

    # Tenant isolation: one client's list must never leak into another's de-dupe.
    check("another client's list is empty", state.known_domains(conn, "other") == set())

    pool = state.leads_by_status(conn, "t", state.SOURCED, limit=10)
    check("pool returns both banked leads", len(pool) == 2)
    check("pool is oldest-first", pool[0]["email"] == "a@acme.com")
    state.set_status(conn, "t", "a@acme.com", "queued")
    pool2 = state.leads_by_status(conn, "t", state.SOURCED, limit=10)
    check("a claimed lead leaves the pool",
          [r["email"] for r in pool2] == ["b@beta.com"])
    check("pool honours its limit",
          len(state.leads_by_status(conn, "t", state.SOURCED, limit=0)) == 0)


def test_migration_backfills_domain():
    print("\n[a pre-existing DB gains website_domain, backfilled]")
    path = _tmpdb()
    # Build a leads table exactly as it looked BEFORE website_domain existed.
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY, client TEXT NOT NULL, email TEXT NOT NULL,
            first_name TEXT, last_name TEXT, title TEXT, company_name TEXT,
            website_url TEXT, niche TEXT, status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL, UNIQUE(client, email));
        INSERT INTO leads (client,email,website_url,status,created_at)
             VALUES ('t','a@a.com','https://www.Legacy.com/x','queued','2026-01-01');
        INSERT INTO leads (client,email,website_url,status,created_at)
             VALUES ('t','b@b.com','','queued','2026-01-01');
    """)
    old.commit()
    old.close()

    conn = state.connect(path)          # runs _migrate
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(leads)").fetchall()}
    check("website_domain column added", "website_domain" in cols)
    check("existing row backfilled from its URL",
          conn.execute("SELECT website_domain FROM leads WHERE email='a@a.com'")
              .fetchone()["website_domain"] == "legacy.com")
    check("a row with no URL stays empty, not crashing the migration",
          not conn.execute("SELECT website_domain FROM leads WHERE email='b@b.com'")
                  .fetchone()["website_domain"])
    check("no data was lost", state.count_leads(conn, "t") == 2)
    check("backfilled domain is immediately usable for de-dupe",
          "legacy.com" in state.known_domains(conn, "t"))
    # Idempotent: connecting again must not fail or duplicate anything.
    conn2 = state.connect(path)
    check("re-migrating is a safe no-op", state.count_leads(conn2, "t") == 2)


test_domain_key()
test_banking_persists_every_lead()
test_sourced_is_not_contacted()
test_claim_and_release_keeps_the_lead()
test_known_domains_and_pool()
test_migration_backfills_domain()

print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
