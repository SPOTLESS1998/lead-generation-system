"""Draft the leads already sitting in the DB as 'queued' but never drafted.

Discovery upserts every sourced lead as status='queued' BEFORE it drafts, and
lead_agent leaves a lead 'queued' (for retry) if generation fails — e.g. the
network drops mid-run. Those leads are saved but have no pending draft. This
re-drafts them WITHOUT re-running discovery, so a transient outage doesn't cost
a fresh Google Maps + Firecrawl scrape of the whole prospect list.

Scope: leads with status='queued' that don't already have a pending draft.
The AI-written company_description isn't persisted (it only lives in memory
during a discovery run), so we fall back to the company name — still plenty of
signal alongside the stored ICP service tag (the lead's niche).

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
# generate_strategy / generate_copy only build prompts; the provider fallback
# they call lives in core.ai — so drafting here shares the exact copy path.
from lead_agent import generate_strategy, generate_copy

# Same safety valve as lead_agent: never prepare more than a day's send capacity.
MAX_PER_RUN = 50


def _draft_lead_view(row):
    """Rebuild the lead dict shape generate_strategy/generate_copy expect from a
    stored leads row. Coerce NULLs; supply gentle fallbacks for the fields that
    aren't persisted (company_description) or are blank on role mailboxes (name)."""
    first = (row["first_name"] or "").strip()
    company = row["company_name"] or ""
    return {
        "email": row["email"],
        "first_name": first or "there",       # generic mailbox -> greeting reads 'Hi there,'
        "last_name": (row["last_name"] or "").strip(),
        "title": (row["title"] or "").strip(),
        "company_name": company,
        # The real AI description isn't stored; the (often very descriptive)
        # company name plus the service tag below carry the industry inference.
        "company_description": company,
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
        "SELECT email, first_name, last_name, title, company_name, website_url, niche "
        "FROM leads WHERE client=? AND status='queued' ORDER BY company_name",
        (client,),
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
        # 'queued' as "already contacted" for idempotency — but 'queued' is exactly
        # the state we're re-drafting, so that check would skip every lead.
        if suppression.is_suppressed(conn, client, row["email"]):
            print(f"   ⏭️  Skipping {row['email']} (on suppression list).")
            continue

        lead = _draft_lead_view(row)
        try:
            # Meter the re-draft in the ledger just like the first-pass draft in
            # lead_agent, so retried leads' tokens/cost + any failure are counted.
            with obs.track(conn, cfg, "draft", subject=row["email"], run_id=run_id):
                brief, _ = generate_strategy(cfg, lead)
                subject, body, provider = generate_copy(cfg, lead, brief)
        except Exception as e:
            failed += 1
            print(f"   ❌ Generation failed for {lead['company_name']}: {e}. "
                  f"Leaving lead 'queued' for retry.")
            continue

        print("\n📝 --- DRAFTED PITCH READY ---")
        print(f"Target: {row['company_name']} <{row['email']}>")
        print(f"Subject: {subject}   (drafted by: {provider})")
        print("------------------------")

        entry = {
            "kind": "cold",
            "client": client,
            "company_name": row["company_name"],
            "target_email": row["email"],
            "first_name": (row["first_name"] or ""),
            "last_name": (row["last_name"] or ""),
            "title": (row["title"] or ""),
            "drafted_subject": subject,
            "drafted_body": body,
        }
        lead_id = review.save_pending(entry)
        review.notify_operator(cfg, lead_id, entry)
        drafted += 1

    print(f"\n🎉 Done. Drafted {drafted} pitch(es), {failed} failed (left queued) "
          f"— awaiting your approval at {cfg.get('unsubscribe_base_url')}.")


if __name__ == "__main__":
    main()
