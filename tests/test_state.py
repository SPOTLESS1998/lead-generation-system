"""Offline tests for core/state.py lead persistence — NO network, throwaway DBs.

Focus: the grounding-facts columns (company_description, company_facts) that the
cold-email copy is anchored on — they must persist through upsert_lead, degrade
safely for minimal-dict callers, and be added by _migrate to a pre-existing DB
that predates them (so live DBs upgrade without losing data).

Run:  venv/bin/python tests/test_state.py
"""

import os
import sys
import sqlite3
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import state                         # noqa: E402
from core.leads import FIELDS                  # noqa: E402

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


def _lead_cols(conn):
    return {r["name"] for r in conn.execute("PRAGMA table_info(leads)").fetchall()}


# --------------------------------------------------------------------------
def test_fields():
    print("\n[leads.FIELDS carries the grounding facts]")
    check("company_description in FIELDS", "company_description" in FIELDS)
    check("company_facts in FIELDS", "company_facts" in FIELDS)


def test_schema_has_columns():
    print("\n[fresh DB: _SCHEMA creates both fact columns]")
    conn = state.connect(_tmpdb())
    cols = _lead_cols(conn)
    check("fresh leads table has company_description", "company_description" in cols)
    check("fresh leads table has company_facts", "company_facts" in cols)
    conn.close()


def test_roundtrip():
    print("\n[upsert_lead persists both fact columns]")
    conn = state.connect(_tmpdb())
    lead = {
        "email": "hi@acme.ng", "first_name": "Ada", "last_name": "N",
        "title": "Founder", "company_name": "Acme", "website_url": "https://acme.ng",
        "company_description": "Acme sells solar kits to Lagos SMEs.",
        "company_facts": "Services: solar install, maintenance. Detail: 5-year battery warranty. Well-reviewed — 4.8 stars across 210 reviews.",
    }
    state.upsert_lead(conn, "demo", lead, niche="AI Lead Generation", status="queued")
    row = conn.execute(
        "SELECT company_description, company_facts, niche, status FROM leads "
        "WHERE client=? AND email=?", ("demo", "hi@acme.ng")
    ).fetchone()
    check("company_description persisted", row["company_description"] == lead["company_description"])
    check("company_facts persisted", row["company_facts"] == lead["company_facts"])
    check("niche + status still written", row["niche"] == "AI Lead Generation" and row["status"] == "queued")
    conn.close()


def test_minimal_dict_degrades_to_null():
    print("\n[minimal-dict caller: missing fact keys -> NULL, no crash]")
    conn = state.connect(_tmpdb())
    # A caller (e.g. a test or an old CSV row) that omits the fact keys entirely.
    state.upsert_lead(conn, "demo", {"email": "bare@x.ng", "company_name": "Bare"})
    row = conn.execute(
        "SELECT company_description, company_facts FROM leads WHERE client=? AND email=?",
        ("demo", "bare@x.ng")
    ).fetchone()
    check("row was inserted", row is not None)
    check("company_description is NULL when omitted", row["company_description"] is None)
    check("company_facts is NULL when omitted", row["company_facts"] is None)
    conn.close()


def test_migration_of_preexisting_db():
    print("\n[migration: a pre-facts DB gains the columns without losing data]")
    path = _tmpdb()
    # Build a leads table exactly as it existed BEFORE the facts columns, seed a row.
    raw = sqlite3.connect(path)
    raw.execute("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY, client TEXT NOT NULL, email TEXT NOT NULL,
            first_name TEXT, last_name TEXT, title TEXT, company_name TEXT,
            website_url TEXT, niche TEXT, status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL, UNIQUE(client, email)
        )""")
    raw.execute("INSERT INTO leads (client, email, company_name, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)", ("demo", "old@x.ng", "OldCo", "sent", "2020-01-01T00:00"))
    raw.commit()
    raw.close()

    cols_before = None
    pre = sqlite3.connect(path)
    pre.row_factory = sqlite3.Row
    cols_before = {r["name"] for r in pre.execute("PRAGMA table_info(leads)").fetchall()}
    pre.close()
    check("pre-existing DB lacks the fact columns", "company_facts" not in cols_before)

    # connect() runs _migrate -> should ALTER the columns in.
    conn = state.connect(path)
    cols = _lead_cols(conn)
    check("migrate added company_description", "company_description" in cols)
    check("migrate added company_facts", "company_facts" in cols)

    # Old row survives intact; new columns read back NULL for it.
    old = conn.execute("SELECT company_name, status, company_facts FROM leads "
                       "WHERE client=? AND email=?", ("demo", "old@x.ng")).fetchone()
    check("old row preserved", old is not None and old["company_name"] == "OldCo" and old["status"] == "sent")
    check("old row's new column is NULL", old["company_facts"] is None)

    # And a fresh upsert on the migrated DB can now write facts.
    state.upsert_lead(conn, "demo", {"email": "new@x.ng", "company_name": "NewCo",
                                     "company_facts": "Services: X. Detail: Y."})
    new = conn.execute("SELECT company_facts FROM leads WHERE client=? AND email=?",
                       ("demo", "new@x.ng")).fetchone()
    check("post-migration upsert writes facts", new["company_facts"] == "Services: X. Detail: Y.")

    # Idempotent: a second connect()/_migrate on the same DB must not error.
    conn.close()
    conn2 = state.connect(path)
    check("second connect()/_migrate is idempotent", "company_facts" in _lead_cols(conn2))
    conn2.close()


def main():
    test_fields()
    test_schema_has_columns()
    test_roundtrip()
    test_minimal_dict_degrades_to_null()
    test_migration_of_preexisting_db()
    print(f"\n{'='*50}\nRESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
