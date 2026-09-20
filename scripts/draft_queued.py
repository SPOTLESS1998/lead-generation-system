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
from core import quality
from core import observability as obs
# The shared drafting recipe lives in lead_agent so BOTH entry points draft
# identically (fit → strategy → magnet → copy → quality gate). Never re-spell
# those steps here; that drift is what shipped dead-link, unscored pitches.
from lead_agent import draft_one_lead, pending_entry

# Same safety valve as lead_agent: never prepare more than a day's send capacity.
MAX_PER_RUN = 50


def _draft_lead_view(row):
    """Rebuild the lead dict shape the drafting recipe expects from a stored leads
    row. Coerce NULLs; carry the persisted grounding facts through UNCHANGED.

    Does NOT fall back to the company name for `company_facts`. That fallback was a
    real bug: with NULL facts it handed the model the company NAME positioned as
    researched evidence, and the model — asked for a specific bottleneck and given
    something that looked like a fact — produced plausible invented detail instead of
    declining. Five live drafts cited things our data has never contained ("spanning
    six continents", "top Nigerian brands like MTN and Sterling Bank", "powered over
    1,200 Nigerian merchants"). Those may even be true, which is what makes them
    dangerous: they read as researched, cite nothing checkable, and there is no way to
    verify them before they reach a stranger's inbox.

    The intent behind the old fallback was reasonable — let pre-facts rows still draft —
    but the outcome was fabrication, so the gate now lives in main(): a lead with no
    grounding is skipped with a loud count instead of drafted on a guess.
    """
    first = (row["first_name"] or "").strip()
    company = row["company_name"] or ""
    # Carried verbatim. Empty means "we know nothing" and is reported as such, rather
    # than being quietly replaced by something that reads like knowledge.
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
        # NEITHER field falls back to the company name — not even the description.
        # That looks harmless but is a BACKDOOR: lead_agent.py and core/quality.py all
        # coalesce `company_facts or company_description` (lines 130/176/248 and
        # 176/220 respectively), so a name placed in the description slot would be read
        # straight back out as facts and the fabrication would return by another route.
        # Empty is the honest value for "we scraped nothing".
        "company_description": desc,
        "company_facts": facts or desc,
        # Explicit signal for the gate in main(). Kept as its own key rather than inferred
        # from truthiness at the call site so the rule is stated once, here, next to the
        # data it judges. Delegates to core/quality.is_grounded — the SINGLE definition,
        # which draft_one_lead itself now enforces, so this pre-check is only an early
        # exit that saves claiming the lead, never the sole guard.
        "has_grounding": quality.is_grounded({
            "company_facts": facts or desc, "company_description": desc}),
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
    # Shared with scripts/lead_agent.py via core.review so the two entry points
    # cannot drift — they did, and that is what produced double drafts. Note this
    # counts only LIVE (status="pending") drafts: a quarantined or declined draft
    # must not block an honest second attempt. See review.emails_with_live_draft.
    pending_emails = review.emails_with_live_draft(client)
    todo = [r for r in rows if r["email"] not in pending_emails]

    print(f"\n[{time.strftime('%H:%M:%S')}] {len(rows)} queued lead(s); "
          f"{len(rows) - len(todo)} already drafted; drafting {len(todo)}.")

    if not todo:
        print("\n✅ Nothing to draft — every queued lead already has a pending draft.")
        return

    drafted = failed = ungrounded = 0
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

        # GROUNDING GATE. No facts and no description means we know nothing specific
        # about this business, and a cold email that claims specifics it cannot support
        # is worse than no email: it is unverifiable by us, it damages the sending
        # domain if wrong, and it is the one failure this pipeline cannot detect after
        # the fact. So we do not draft it — we count it and say so.
        #
        # The lead KEEPS its status. It stays 'sourced', which already means "banked,
        # never contacted" and is deliberately excluded from CONTACTED_STATES (see
        # core/state.py), so it is picked up automatically once enrichment fills its
        # facts in. No new status is needed: this is missing data, not a new stage in
        # the lead's life, and inventing one would mean auditing every place that
        # enumerates states for no behavioural gain.
        if not lead["has_grounding"]:
            ungrounded += 1
            print(f"   ⏭️  Skipping {row['email']} — no grounding facts "
                  f"(nothing scraped to cite); staying '{state.SOURCED}' for enrichment.")
            continue

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

    try:
        summary = state.summarize_run_quality(conn, client, run_id)
        if summary:
            sc = summary["scores"]
            mean = round(sum(sc) / len(sc), 1) if sc else "—"
            print(f"\n📈 [Quality] {summary['attempted']} attempted · "
                  f"{summary['shipped']} shipped · {summary['refused_floor']} below floor · "
                  f"{summary['refused_citation']} refused for citations · mean {mean}")
    except Exception as e:
        print(f"⚠️  could not write the quality scorecard: {e}")

    print(f"\n🎉 Done. Drafted {drafted} pitch(es), {failed} failed "
          f"(left '{state.SOURCED}'), {ungrounded} skipped for no grounding facts "
          f"— awaiting your approval at {cfg.get('unsubscribe_base_url')}.")
    if ungrounded:
        # Loud and counted, not a whisper: an ungrounded lead is a DISCOVERY gap that
        # will silently cap how many pitches this pipeline can ever produce, so it
        # belongs in front of the operator rather than buried in the per-lead lines.
        print(f"⚠️  {ungrounded} lead(s) skipped: no grounding facts. They were scraped "
              f"without facts/description and cannot be drafted honestly until enriched. "
              f"Check the discovery extractor for these rows rather than hand-writing facts.")


if __name__ == "__main__":
    main()
