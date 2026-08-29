import warnings
warnings.filterwarnings('ignore')

import os
import sys
import time

# Make the project root importable so `core` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state, suppression, review, magnet, budget, quality, leads as leads_source
from core import observability as obs
from core.ai import generate, generate_json

# Safety valve: how many drafts to prepare in a single run (no point drafting more
# than a day's sending capacity). The sending cap is enforced separately at send time.
MAX_PER_RUN = 50


def print_step(step):
    print(f"\n[{time.strftime('%H:%M:%S')}] {step}")


# --------------------------------------------------------------------------
# AI copy generation — provider fallback lives in core/ai.py (shared with the
# reply agent). These two functions just build the prompts.
# --------------------------------------------------------------------------

def generate_strategy(cfg, lead):
    print_step(f"🧠 [Strategist] Analyzing {lead['company_name']}...")

    # Auto-discovery tags each prospect with the ONE Ejentic offering their profile
    # matched (their ICP segment). When present, steer the whole brief toward that
    # service so the bottleneck + Free Gift pitch the exact thing they need.
    service = (lead.get("ejentic_service") or "").strip()
    service_line = (
        f"\n    This prospect was matched to Ejentic's \"{service}\" offering — anchor the "
        f"bottleneck and the Free Gift around that specific service.\n"
        if service else ""
    )

    # The real, scraped facts about this prospect (services they list + one verifiable
    # detail + a review signal, assembled at discovery time). Fall back to the older
    # one-line description, then to nothing — so CSV leads and old rows still work.
    facts = (lead.get("company_facts") or lead.get("company_description") or "").strip()

    # House-STANDARD outcome for this service, so every pitch promises the SAME defensible
    # figure instead of a per-call LLM guess (one run said "15-25 leads/mo", another "100").
    # Config-driven: clients/<c>/config.json -> service_outcomes, keyed by the matched service.
    # No standard for this service => fall back to a free "plausible number" (graceful degrade).
    standard_outcome = ((cfg.get("service_outcomes") or {}).get(service, "") or "").strip() if service else ""
    outcome_line = (
        f"state this EXACT expected result, keeping the figures exactly as written — never inflate them: {standard_outcome}"
        if standard_outcome else
        "one believable result of that service, with a plausible number or timeframe — no hype."
    )

    prompt = f"""You are the lead strategist for {cfg['client_name']}, an AI automation agency
    whose offerings include Localized Multilingual Support Agents, AI Lead Generation Systems,
    Air-Gapped Internal Knowledge Bases (RAG), and Enterprise Workflow Automation.{service_line}
    Prospect:
    - Name: {lead.get('first_name','')} {lead.get('last_name','')}
    - Title: {lead.get('title','')}
    - Company: {lead.get('company_name','')}
    - What we actually know about them: {facts}

    Using ONLY the facts above (never invent details), write a tight brief the copywriter will
    turn into a cold email. Fill in each line with something specific to THIS prospect:

    OBSERVATION: one specific, verifiable, FLATTERING thing about this business — quote a concrete detail from what we know that makes them look good (a strength, specialty, market, or achievement). NEVER open on a negative or operational detail (a complaints line, a support number, a disclaimer, a problem).
    PAIN: the single most costly operational bottleneck that detail implies.
    SERVICE: the one {cfg['client_name']} offering that best relieves it{f" (default to '{service}')" if service else ''}.
    OUTCOME: {outcome_line}
    AUDIT: what the free personalized audit page for them should focus on.

    Output ONLY those five labeled lines, nothing else.
    """
    brief, provider = generate(cfg, prompt)
    return brief, provider


def generate_copy(cfg, lead, strategy_brief, magnet_url=None):
    print_step(f"✍️  [Copywriter] Drafting the email for {lead['company_name']}...")
    # The name the email signs off as. Without this the model invents a
    # "[Your Name]" placeholder — fine to fix here so both the prompt and the
    # post-generation safety net below use the same real sender identity.
    sender_name = (cfg.get("from_name") or cfg.get("client_name") or "our team").strip()
    # Blank first name (common for role mailboxes like info@) -> a safe 'there' so the
    # greeting reads "Hi there," not a literal "Hi Name," — mirrors draft_queued.
    greet_first = (lead.get("first_name") or "").strip() or "there"

    # Give the copywriter the real facts directly (not just the digested brief) so
    # sentence 1 can cite something true and specific about THIS prospect.
    facts = (lead.get("company_facts") or lead.get("company_description") or "").strip()
    facts_block = (
        f"WHAT WE ACTUALLY KNOW ABOUT THEM (ground sentence 1 in this; do not invent other facts):\n    {facts}\n"
        if facts else ""
    )

    prompt = f"""You are a world-class B2B cold-email copywriter writing ONE email for {cfg['client_name']}.

    PROSPECT:
    Name: {greet_first} {lead.get('last_name','')}
    Company: {lead.get('company_name','')}

    {facts_block}
    STRATEGY BRIEF — ground every line in this; do not invent facts beyond it:
    {strategy_brief}

    {quality.copy_instructions(sender_name, magnet_url)}

    Reply with ONLY this JSON, no prose and no markdown fences:
    {{"subject": "<subject, WITHOUT a 'Subject:' prefix>", "body": "<full body, greeting through sign-off, using real newlines>"}}
    """
    # JSON (not free text) so a model that emits chain-of-thought around the answer
    # can't leak its reasoning into the email — generate_json pulls out the {…} block
    # regardless of any preamble/trailer. An empty body raises → lead_agent rolls the
    # lead back and retries rather than shipping a blank pitch.
    obj, provider = generate_json(cfg, prompt)
    subject = (obj.get("subject") or "").strip() or f"A quick idea for {lead['company_name']}"
    body = quality.finalize_body(obj.get("body") or "", sender_name, magnet_url)
    if not body:
        raise RuntimeError("copywriter returned an empty body")
    return subject, body, provider


def get_demo_pitch(company_name, base_url=None):
    """Canned pitches for demo_mode (video recording only) — no API calls.

    Links are built from the client's base URL so they always point at the running
    approval server (never a stale hardcoded port)."""
    base = (base_url or "http://localhost:5001").rstrip("/")
    if "Adebayo" in company_name:
        return ("Automating Tax Audits for Adebayo & Co", "Hi Oluwatobi,\n\nI noticed Adebayo & Co handles a massive volume of tax audits and payroll processing for mid-sized enterprises. Manually verifying those ledgers takes your team hours every week.\n\nI mapped out a step-by-step architectural blueprint showing exactly how your firm can automate ledger ingestion and payroll reconciliation using OCR and secure AI models. I've attached the blueprint below for your review.\n\n" + f"{base}/magnet/blueprint" + "\n\nBest,\nEjentic AI Team")
    elif "Capital Homes" in company_name:
        return ("Automating Client Inquiries for Capital Homes", "Hi Amina,\n\nI love the luxury properties you are brokering at Capital Homes Abuja. Since you manage thousands of client inquiries monthly, your team likely spends hours answering repetitive questions about property viewings, especially late at night.\n\nI built a custom AI chatbot prototype specifically trained on your Maitama listings. I've generated a 7-day temporary access pass for you to test the software live.\n\nHere is the secure link to test the prototype:\n\n" + f"{base}/magnet/chatbot" + "\n\nBest,\nEjentic AI Team")
    else:
        return ("Strategic Audit for Lagos Style Hub", "Hi Chinedu,\n\nI've been following Lagos Style Hub's growth. Managing thousands of daily fashion orders across Nigeria must create a massive bottleneck for your customer support team, leading to missed sales in your DMs.\n\nI did a brief strategic audit of your current workflow and mapped out the exact step-by-step process of how you can build an Autonomous AI Lead Generation & Support System to instantly capture lost WhatsApp sales. \n\nI've linked the strategic breakdown below.\n\n" + f"{base}/magnet/strategic-audit" + "\n\nBest,\nEjentic AI Team")


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def main(preview=False, limit=None):
    cfg = config.load_client()

    # PREVIEW: run the EXACT real pipeline (scrape → strategist → magnet → copywriter →
    # quality gate → metrics), but against a THROWAWAY database, and replace the operator
    # hand-off with a full print. Nothing is queued, emailed, or sent — so what you see is
    # precisely what would ship, with no risk and no misleading shortcuts.
    preview_db = None
    if preview:
        import tempfile
        limit = int(limit or 3)
        f = tempfile.NamedTemporaryFile(prefix="leadgen_preview_", suffix=".sqlite", delete=False)
        f.close()
        preview_db = f.name
        # A preview shouldn't fan out across every segment/query just to show a few
        # drafts — narrow the source to ONE segment + ONE query and a handful of sites,
        # so it's fast and cheap (no firing a dozen Maps searches to draft two leads).
        for src in ("discovery", "yellowpages"):
            if isinstance(cfg.get(src), dict):
                d = dict(cfg[src])
                d["max_leads"] = limit
                d["max_results"] = max(limit + 2, 3)   # a few spares in case some fail to enrich
                segs = d.get("segments") or []
                if segs:
                    seg = dict(segs[0])
                    seg["queries"] = (seg.get("queries") or [])[:1]
                    d["segments"] = [seg]
                if d.get("queries"):
                    d["queries"] = d["queries"][:1]
                cfg = {**cfg, src: d}

    print("=========================================================")
    print(f"🚀 {cfg['client_name']} - Lead Scout & Drafter" + ("   [PREVIEW]" if preview else ""))
    _cap = budget.daily_cap_usd(cfg)
    _prem = ("on" + (f" (≤${_cap:.2f}/day)" if _cap else " (uncapped)")) if budget.premium_enabled(cfg) else "off"
    _gate = "on" if ((cfg.get("copy", {}) or {}).get("quality_gate", {}) or {}).get("enabled") else "off"
    print(f"   client={cfg['client']}  send_mode={cfg['sending']['mode']}  "
          f"demo_mode={cfg['demo_mode']}  premium_copy={_prem}  quality_gate={_gate}")
    if preview:
        print(f"   🔎 PREVIEW — real scrape+facts+agents+metrics on a THROWAWAY db; "
              f"nothing queued/emailed/sent (sample: {limit}).")
    print("=========================================================")

    conn = state.connect(preview_db or cfg["paths"]["db"])
    run_id = obs.new_run_id()   # groups every event this run emits in the ledger
    max_drafts = limit if preview else MAX_PER_RUN

    # Lead source is config-selectable: a curated CSV, or live auto-discovery
    # (Google Maps or Yellow Pages → Firecrawl → AI extraction). All return
    # (leads, skipped) with the same lead shape, so the rest is identical.
    if cfg.get("lead_source") == "maps_firecrawl":
        from core import discovery
        print_step("🛰️  Auto-sourcing leads (Google Maps → Firecrawl → AI extraction)...")
        all_leads, skipped = discovery.load_leads(cfg, conn=conn)
        print_step(f"🔍 Sourced {len(all_leads)} lead(s) "
                   f"({skipped} business(es) skipped: no site/email or scrape failed).")
    elif cfg.get("lead_source") == "yellowpages":
        from core import yellowpages
        print_step("📖 Auto-sourcing leads (Yellow Pages → Firecrawl → AI extraction)...")
        all_leads, skipped = yellowpages.load_leads(cfg)
        print_step(f"🔍 Sourced {len(all_leads)} lead(s) "
                   f"({skipped} listing(s) skipped: no site/email or scrape failed).")
    else:
        all_leads, skipped = leads_source.load_leads(cfg)
        print_step(f"🔍 Loaded {len(all_leads)} curated lead(s) "
                   f"({skipped} template/placeholder row(s) ignored).")

    if not all_leads:
        if cfg.get("lead_source") == "maps_firecrawl":
            print("\n⚠️  Discovery found no usable leads. Check discovery.queries in "
                  "clients/{}/config.json, and that Google Maps + Firecrawl are "
                  "connected in Composio.".format(cfg["client"]))
        elif cfg.get("lead_source") == "yellowpages":
            print("\n⚠️  Yellow Pages found no usable leads. Check yellowpages.queries "
                  "(and location) in clients/{}/config.json, and that Firecrawl is "
                  "connected in Composio.".format(cfg["client"]))
        else:
            print("\n⚠️  No usable leads in clients/{}/leads.csv. "
                  "Add real contacts (see clients/_template/leads.csv) and re-run.".format(cfg["client"]))
        return

    drafted = 0
    for lead in all_leads:
        if drafted >= max_drafts:
            label = "sample limit" if preview else "MAX_PER_RUN"
            print_step(f"⏹️  Reached {label} ({max_drafts}); stopping this run.")
            break

        skip, reason = suppression.should_skip(conn, cfg["client"], lead["email"])
        if skip:
            print(f"   ⏭️  Skipping {lead['email']} ({reason}).")
            continue

        # Record intent BEFORE drafting so a re-run won't re-contact this lead.
        # The ICP tag (which Ejentic offering they matched) is stored as the niche.
        state.upsert_lead(conn, cfg["client"], lead,
                          niche=lead.get("ejentic_service") or None, status="queued")

        magnet_url = None
        magnet_token = None
        if cfg["demo_mode"]:
            print_step(f"🧠 [demo] Strategist analyzing {lead['company_name']}...")
            time.sleep(1.0)
            subject, body = get_demo_pitch(lead["company_name"], cfg.get("unsubscribe_base_url"))
            provider = "demo"
        else:
            # Premium-first copy when today's real-Claude spend is under the daily cap,
            # else the free chain (decided in core/budget.copy_cfg). The strategist, the
            # copywriter, and the quality gate all use gen_cfg so their tokens count
            # toward the cap; the magnet page stays on the free chain to control spend.
            gen_cfg = budget.copy_cfg(conn, cfg)
            try:
                # Track the whole draft as one ledger step: the strategist, copywriter,
                # and quality-gate LLM calls attribute their tokens/cost here, and a
                # generation failure is recorded as an 'error' event (then re-raised
                # into the rollback below — the observer will open a fault for it).
                with obs.track(conn, cfg, "draft", subject=lead["email"], run_id=run_id):
                    brief, _ = generate_strategy(gen_cfg, lead)
                    # Build the prospect's real "free gift": a personalized audit page,
                    # stored now so its link resolves the moment the email is approved.
                    try:
                        content = magnet.build_content(cfg, lead, brief)
                        magnet_token = magnet.new_token()
                        state.save_magnet(conn, cfg["client"], magnet_token, lead["email"], content)
                        magnet_url = magnet.url(cfg, cfg["client"], magnet_token)
                        print_step(f"🎁 [Magnet] Built personalized audit page → {magnet_url}")
                    except Exception as e:
                        print(f"   ⚠️  Magnet generation failed ({e}); drafting without a link.")
                        magnet_url = None
                    # Draft → score → rewrite weak copy (LLM-as-judge, see core/quality).
                    subject, body, provider = quality.draft_and_polish(
                        gen_cfg, lead, brief, magnet_url, generate_copy)
            except Exception as e:
                print(f"   ❌ Generation failed for {lead['company_name']}: {e}. "
                      f"Rolling back so this lead retries on the next run.")
                # We 'claimed' this lead as queued BEFORE drafting so a re-run
                # wouldn't double-draft it. Since no draft was produced, undo that
                # claim by removing the row — otherwise a stuck 'queued' status
                # counts as already_contacted forever and the lead is never retried.
                conn.execute("DELETE FROM leads WHERE client=? AND email=?",
                             (cfg["client"], lead["email"]))
                conn.commit()
                continue

        print("\n📝 --- DRAFTED PITCH READY ---")
        print(f"Target: {lead['first_name']} {lead['last_name']} - {lead['company_name']}")
        print(f"Subject: {subject}   (drafted by: {provider})")
        print("------------------------")

        if preview:
            # Real draft, real embedded magnet — just shown to you instead of queued.
            print("\n📧 --- FULL EMAIL (exactly what would go for your approval) ---")
            print(f"To: {lead['first_name']} {lead['last_name']} <{lead['email']}>  |  {lead['company_name']}")
            print(f"Subject: {subject}")
            print("-" * 60)
            print(body)
            print("-" * 60)
            print(f"🎁 Embedded lead magnet (personalized audit page): {magnet_url or '(none built)'}")
            drafted += 1
            continue

        entry = {
            "kind": "cold",
            "client": cfg["client"],
            "company_name": lead["company_name"],
            "target_email": lead["email"],
            "first_name": lead["first_name"],
            "last_name": lead["last_name"],
            "title": lead["title"],
            "drafted_subject": subject,
            "drafted_body": body,
            "magnet_url": magnet_url,
            "magnet_token": magnet_token,
        }
        lead_id = review.save_pending(entry)
        review.notify_operator(cfg, lead_id, entry)
        drafted += 1

    print(f"\n🎉 Done. Drafted {drafted} pitch(es) awaiting your web approval "
          f"at {cfg.get('unsubscribe_base_url')}.")

    # Always-live performance readout: what THIS run actually spent (tokens/cost/time),
    # straight from the ledger. Wrapped so a metrics hiccup can never fail the run.
    if (cfg.get("observability", {}) or {}).get("enabled", True):
        try:
            summary = obs.run_metrics(conn, cfg, run_id)
            if summary["totals"]["events"]:
                print("\n" + obs.format_run_summary(summary))
        except Exception as e:
            print(f"⚠️  couldn't print run metrics ({e}); the ledger still has the raw events.")

    if preview and preview_db:
        conn.close()
        try:
            os.unlink(preview_db)
        except OSError:
            pass
        print("\n✅ PREVIEW complete — throwaway db removed. Nothing was queued, emailed, or sent.")


if __name__ == "__main__":
    _args = sys.argv[1:]
    _preview = any(a in ("--preview", "--dry-run") for a in _args)
    _limit = None
    for _a in _args:
        if _a.startswith("--limit="):
            _limit = int(_a.split("=", 1)[1])
        elif _a.isdigit():
            _limit = int(_a)
    main(preview=_preview, limit=_limit)
