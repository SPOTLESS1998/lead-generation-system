"""Draft the leads already on the list that have never been drafted.

Every discovered lead is banked as status='sourced' (see state.bank_leads), and a
draft that fails releases its claim by returning the lead to 'sourced' rather than
deleting it — so a transient outage leaves leads saved but undrafted. This re-drafts
them WITHOUT re-running discovery, so an outage doesn't cost a fresh Google Maps +
Firecrawl scrape of the whole prospect list.

Scope: leads with status='sourced' that don't already have a pending draft.
The grounding facts (company_facts / company_description) are persisted at
discovery time, so a re-draft reads the SAME real facts the first pass had; we
still fall back to the company name for rows saved before facts were stored.

Drafting itself is NOT reimplemented here — it calls lead_agent.draft_one_lead,
the one shared recipe, so a re-draft is identical to a first-pass draft (fitted
offering, personalized magnet page, quality gate and all).

Run:  venv/bin/python -u scripts/draft_queued.py
"""

import warnings
warnings.filterwarnings('ignore')

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # repo root -> `core`
sys.path.insert(0, HERE)                     # scripts/  -> `lead_agent`

from core import config, state, review, suppression
from core import observability as obs
# The shared drafting recipe lives in lead_agent so BOTH entry points draft
# identically (fit → strategy → magnet → copy → quality gate). Never re-spell
# those steps here; that drift is what shipped dead-link, unscored pitches.
from lead_agent import draft_one_lead, pending_entry

# Same safety valve as lead_agent: never prepare more than a day's send capacity.
MAX_PER_RUN = 50


def _draft_lead_view(row):
    """Rebuild the lead dict shape the drafting recipe expects from a stored leads
    row. Coerce NULLs; prefer the persisted grounding facts, falling back to the
    company name for old rows / blank name on role mailboxes."""
    first = (row["first_name"] or "").strip()
    company = row["company_name"] or ""
    # Prefer the real scraped facts saved at discovery time; fall back to the
    # one-line description, then to the (often descriptive) company name so rows
    # saved before facts existed — and role mailboxes — still draft with signal.
    facts = (row["company_facts"] or "").strip()
    desc = (row["company_description"] or "").strip()
    return {
        "email": row["email"],
        # Pass the REAL name, blank included. We used to substitute the literal
        # "there" here to force a "Hi there," greeting, but that reaches the model
        # as `Name: there` and confuses it — one draft spent its whole body arguing
        # with itself about it. quality.copy_instructions already says to open with
        # exactly "Hi there," when no name is given, and finalize_body rewrites a
        # leaked placeholder greeting, so a blank is handled properly downstream.
        "first_name": first,
        "last_name": (row["last_name"] or "").strip(),
        "title": (row["title"] or "").strip(),
        "company_name": company,
        "company_description": desc or company,
        "company_facts": facts or desc or company,
        "ejentic_service": row["niche"] or "",
    }


def main():
    cfg = config.load_client()
    print("=========================================================")
    print(f"✍️  {cfg['client_name']} - Draft Queued Leads (no re-discovery)")
    print(f"   client={cfg['client']}  copy path -> core.ai provider chain")
    print("=========================================================")

    conn = state.connect(cfg["paths"]["db"])
    client = cfg["client"]
    run_id = obs.new_run_id()   # groups every event this run emits in the ledger

    rows = conn.execute(
        "SELECT email, first_name, last_name, title, company_name, website_url, niche, "
        "company_description, company_facts "
        "FROM leads WHERE client=? AND status=? ORDER BY company_name",
        (client, state.SOURCED),
    ).fetchall()

    # Skip any queued lead that already has a pending draft awaiting approval,
    # so re-running this is idempotent (never double-drafts / double-emails).
    pending_emails = {
        e.get("target_email") for e in review.load_pending().values()
        if e.get("client") == client
    }
    todo = [r for r in rows if r["email"] not in pending_emails]

    print(f"\n[{time.strftime('%H:%M:%S')}] {len(rows)} queued lead(s); "
          f"{len(rows) - len(todo)} already drafted; drafting {len(todo)}.")

    if not todo:
        print("\n✅ Nothing to draft — every queued lead already has a pending draft.")
        return

    drafted = failed = 0
    for row in todo:
        if drafted >= MAX_PER_RUN:
            print(f"\n⏹️  Reached MAX_PER_RUN ({MAX_PER_RUN}); stopping.")
            break

        # Gate ONLY on the real never-contact list (opt-out / bounce / manual).
        # We deliberately do NOT call suppression.should_skip here: it also treats
        # already-drafted states as "already contacted" for idempotency — but we have
        # already selected for 'sourced', which is by definition never contacted, and
        # the queue check above is what stops a double-draft.
        if suppression.is_suppressed(conn, client, row["email"]):
            print(f"   ⏭️  Skipping {row['email']} (on suppression list).")
            continue

        lead = _draft_lead_view(row)
        try:
            # The ONE shared drafting recipe (scripts/lead_agent.draft_one_lead):
            # fit the offering → strategy → build the personalized magnet page →
            # draft → quality gate. Re-spelling these steps here is exactly how this
            # script drifted into shipping dead "[Link to Free Gift]" pitches that
            # were never scored — so it calls the shared function, same as the main
            # pipeline. Metering into the ledger happens inside it.
            subject, body, provider, magnet_url, magnet_token = draft_one_lead(
                conn, cfg, lead, run_id)
        except Exception as e:
            failed += 1
            print(f"   ❌ Generation failed for {lead['company_name']}: {e}. "
                  f"Leaving lead '{state.SOURCED}' for retry.")
            continue

        print("\n📝 --- DRAFTED PITCH READY ---")
        print(f"Target: {row['company_name']} <{row['email']}>")
        print(f"Subject: {subject}   (drafted by: {provider})")
        print("------------------------")

        entry = pending_entry(cfg, lead, subject, body, magnet_url, magnet_token)
        lead_id = review.save_pending(entry)
        review.notify_operator(cfg, lead_id, entry)
        drafted += 1

    print(f"\n🎉 Done. Drafted {drafted} pitch(es), {failed} failed (left queued) "
          f"— awaiting your approval at {cfg.get('unsubscribe_base_url')}.")


if __name__ == "__main__":
    main()
