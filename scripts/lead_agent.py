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
# This is the hard ceiling; a client can choose a LOWER everyday number via
# `drafting.daily_cap` in its config (see draft_cap below).
MAX_PER_RUN = 50


def draft_cap(cfg, preview_limit=None):
    """How many drafts this run may produce.

    Sourcing and drafting are deliberately decoupled: discovery banks every qualified
    lead it finds (cheap, and the list is the asset), while drafting is metered to
    what a human can actually review in a day. The surplus waits in the list as
    `sourced` and is drafted oldest-first on later runs.
    """
    if preview_limit:
        return int(preview_limit)
    configured = (cfg.get("drafting") or {}).get("daily_cap")
    if configured is None:
        return MAX_PER_RUN
    return max(0, min(int(configured), MAX_PER_RUN))


def _lead_from_row(row):
    """Rehydrate a banked DB row into the lead dict the drafter expects.

    Drafting always reads from the lead LIST, never straight from the scrape, so a
    lead found today and one banked last week travel the identical code path (and
    `niche` — how the row stores the matched offering — becomes `ejentic_service`
    again, which is what the strategist keys off).
    """
    row = dict(row)
    return {
        "first_name": row.get("first_name") or "",
        "last_name": row.get("last_name") or "",
        "title": row.get("title") or "",
        "email": row.get("email") or "",
        "company_name": row.get("company_name") or "",
        "company_description": row.get("company_description") or "",
        "company_facts": row.get("company_facts") or "",
        "website_url": row.get("website_url") or "",
        "ejentic_service": row.get("niche") or "",
    }


def print_step(step):
    print(f"\n[{time.strftime('%H:%M:%S')}] {step}")


# --------------------------------------------------------------------------
# AI copy generation — provider fallback lives in core/ai.py (shared with the
# reply agent). These two functions just build the prompts.
# --------------------------------------------------------------------------

def _client_offerings(cfg):
    """The CLOSED menu of what THIS client sells — the only services the strategist
    may pitch. Read entirely from the client's own config, in precedence order:

        1. `offerings`          — the explicit menu (recommended; say it plainly)
        2. discovery/yellowpages segment `service` values
        3. `service_outcomes` keys

    There is deliberately NO house fallback. A default menu here would put OUR
    services into ANOTHER company's cold emails: this function used to fall back to
    Ejentic's four offerings, so a client onboarded from the template would have
    pitched Ejentic's catalogue to their own prospects. A client with no offerings is
    a configuration error (core.config._validate raises), never a silent borrow.
    See MULTITENANCY.md.
    """
    seen, out = set(), []

    def _add(s):
        s = (s or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    for svc in (cfg.get("offerings") or []):
        _add(svc if isinstance(svc, str) else (svc or {}).get("name"))
    for src in ("discovery", "yellowpages"):
        for seg in ((cfg.get(src) or {}).get("segments") or []):
            _add(seg.get("service"))
    for svc in (cfg.get("service_outcomes") or {}):
        _add(svc)
    return out


def _canon_offering(value, offerings):
    """Map a model's answer back to the EXACT menu item (case/whitespace-insensitive),
    or None if it named something not on the menu. This is the guard that keeps the
    closed-menu invariant: an off-menu answer is rejected, never pitched."""
    v = (value or "").strip().lower()
    for o in offerings:
        if o.strip().lower() == v:
            return o
    return None


def select_offering(cfg, lead):
    """Pick the ONE offering (from the client's closed menu) that best fits THIS
    prospect, judged from their real facts. The discovery segment tag is only a hint,
    not a cage: a business can surface under one segment's search yet clearly fit a
    different offering (a marketing agency found via an 'e-commerce' query really needs
    Lead Gen, not Support). Returns a menu item verbatim, and degrades to the original
    tag on ANY failure or off-menu answer — so it can never pitch a service we don't
    sell, and never does worse than today."""
    tag = (lead.get("ejentic_service") or "").strip()
    offerings = _client_offerings(cfg)
    if len(offerings) <= 1:                      # nothing to choose between
        return tag or (offerings[0] if offerings else "")
    facts = (lead.get("company_facts") or lead.get("company_description") or "").strip()
    if not facts:                                # no evidence to re-judge -> trust the tag
        return tag or offerings[0]

    menu = "\n".join(f"- {o}" for o in offerings)
    prompt = f"""You match a business to the ONE service (from a fixed menu) that would help it most.

PROSPECT: {lead.get('company_name','')}
What we actually know about them: {facts}
They surfaced under a search targeting: "{tag or '(none)'}" — treat this only as a hint; pick it only if it genuinely fits best.

MENU — you MUST choose exactly one, copied VERBATIM (never invent a service that isn't listed):
{menu}

Choose the single service whose value would be most obvious to THIS business given the facts above.
Reply with ONLY this JSON, no prose: {{"service": "<one menu item, copied verbatim>", "reason": "<max 12 words>"}}"""
    try:
        obj, _ = generate_json(cfg, prompt)
    except Exception:
        return tag or offerings[0]
    chosen = _canon_offering(obj.get("service"), offerings)
    if not chosen:                               # off-menu / unparseable -> safe fallback
        return tag or offerings[0]
    if tag and chosen != tag:
        reason = str(obj.get("reason") or "").strip()
        print(f"   🎯 [Fit] {lead.get('company_name','')}: re-matched to '{chosen}' "
              f"(surfaced under '{tag}')" + (f" — {reason}" if reason else "") + ".")
    return chosen


def generate_strategy(cfg, lead):
    print_step(f"🧠 [Strategist] Analyzing {lead['company_name']}...")

    # Auto-discovery tags each prospect with the ONE Ejentic offering their profile
    # matched (their ICP segment). When present, steer the whole brief toward that
    # service so the bottleneck + Free Gift pitch the exact thing they need.
    service = (lead.get("ejentic_service") or "").strip()
    service_line = (
        f"\n    This prospect was matched to your \"{service}\" offering — anchor the "
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

    # The CLOSED menu of what this client actually sells. The strategist may pitch ONLY
    # these — never a service we don't provide (this is what stops a marketing-agency
    # prospect getting an off-catalog "ad-spend optimization" pitch). When discovery has
    # already matched this prospect to one offering, that match is LOCKED, not a default.
    offerings = _client_offerings(cfg)
    offerings_list = "; ".join(offerings)
    if service:
        service_rule = (
            f'SERVICE: MUST be exactly "{service}" — this prospect was pre-matched to that '
            f"offering; do NOT substitute or invent a different one, even if their own industry "
            f"suggests it (never pitch ad-spend, SEO, web design, or branding — we don't sell those)."
        )
    else:
        service_rule = (
            "SERVICE: MUST be exactly ONE of our offerings above, copied verbatim — never invent "
            "a service we don't provide (no ad-spend, SEO, web design, or branding)."
        )

    prompt = f"""You are the lead strategist for {cfg['client_name']}, an AI automation agency.
    Our ONLY offerings — the complete menu you may pitch — are: {offerings_list}.{service_line}
    Prospect:
    - Name: {lead.get('first_name','')} {lead.get('last_name','')}
    - Title: {lead.get('title','')}
    - Company: {lead.get('company_name','')}
    - What we actually know about them: {facts}

    Using ONLY the facts above (never invent details), write a tight brief the copywriter will
    turn into a cold email. Fill in each line with something specific to THIS prospect:

    OBSERVATION: one specific, verifiable, FLATTERING thing about this business — quote a concrete detail from what we know that makes them look good (a strength, specialty, market, or achievement). NEVER open on a negative or operational detail (a complaints line, a support number, a disclaimer, a problem).
    PAIN: the single most costly operational bottleneck that detail implies — one the SERVICE below actually relieves.
    {service_rule}
    OUTCOME: the direct result of that exact SERVICE — {outcome_line}
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
    # Blank first name is common for role mailboxes (info@, hello@). Say that plainly
    # instead of passing the literal string "there" as their name: a model handed
    # `Name: there` argues with itself about it — one draft burned its entire body on
    # exactly that ("prospect name is 'there'? That seems odd") and queued the argument.
    # copy_instructions already carries the rule (open with exactly "Hi there," when the
    # name is unknown), so state the truth and let that rule fire.
    first = (lead.get("first_name") or "").strip()
    last = (lead.get("last_name") or "").strip()
    who = f"{first} {last}".strip() if first else '(unknown — a role mailbox; greet with "Hi there,")'

    # Give the copywriter the real facts directly (not just the digested brief) so
    # sentence 1 can cite something true and specific about THIS prospect.
    facts = (lead.get("company_facts") or lead.get("company_description") or "").strip()
    facts_block = (
        f"WHAT WE ACTUALLY KNOW ABOUT THEM (ground sentence 1 in this; do not invent other facts):\n    {facts}\n"
        if facts else ""
    )

    prompt = f"""You are a world-class B2B cold-email copywriter writing ONE email for {cfg['client_name']}.

    PROSPECT:
    Name: {who}
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


# --------------------------------------------------------------------------
# The ONE drafting recipe — shared by every entry point
# --------------------------------------------------------------------------
# Written once on purpose. There are two ways a cold email gets drafted: the main
# pipeline (main() below, which discovers leads first) and scripts/draft_queued.py
# (which re-drafts leads already in the DB). They used to each spell the recipe out
# themselves, and they drifted: draft_queued never built a magnet and never ran the
# quality gate, so every draft it produced shipped a dead "[Link to Free Gift]" and
# was never scored — one even queued the model's raw internal monologue. Any new
# drafting entry point MUST call these two functions rather than re-spell the steps.

def draft_one_lead(conn, cfg, lead, run_id):
    """Draft one prospect end to end: fit the offering, plan the angle, build their
    personalized magnet page, then write + quality-gate the copy.

    Returns (subject, body, provider, magnet_url, magnet_token).

    RAISES on failure, deliberately: a caller that claimed the lead up front wants to
    roll that claim back, while a caller re-drafting a stored lead wants to leave it
    queued for retry. Both are correct; neither belongs in here. The whole draft is
    metered as one ledger step, so tokens/cost/faults are attributed the same way no
    matter which entry point ran it.
    """
    # Premium-first copy when today's real-Claude spend is under the daily cap, else
    # the free chain (decided in core/budget.copy_cfg). The strategist, the copywriter
    # and the quality gate all use gen_cfg so their tokens count toward the cap; the
    # magnet page stays on the free chain (plain cfg) to control spend.
    gen_cfg = budget.copy_cfg(conn, cfg)
    with obs.track(conn, cfg, "draft", subject=lead["email"], run_id=run_id):
        # Pick the best-fitting offering from our CLOSED menu before drafting. The
        # segment tag is only how they were sourced; re-judging from their real facts
        # means we pitch the RIGHT service, not just an on-catalog one. Overwrite the
        # tag so the strategist AND its standard-outcome figure both key off the
        # fitted choice. Degrades to the tag on failure.
        fitted = select_offering(gen_cfg, lead)
        if fitted:
            lead["ejentic_service"] = fitted
        brief, _ = generate_strategy(gen_cfg, lead)

        # Build the prospect's real "free gift": a personalized audit page, stored now
        # so its link resolves the moment the email is approved.
        magnet_url = magnet_token = None
        try:
            content = magnet.build_content(cfg, lead, brief)
            magnet_token = magnet.new_token()
            state.save_magnet(conn, cfg["client"], magnet_token, lead["email"], content)
            magnet_url = magnet.url(cfg, cfg["client"], magnet_token)
            print_step(f"🎁 [Magnet] Built personalized audit page → {magnet_url}")
        except Exception as e:
            print(f"   ⚠️  Magnet generation failed ({e}); drafting without a link.")
            magnet_url = magnet_token = None

        # Draft → score → rewrite weak copy (LLM-as-judge, see core/quality).
        subject, body, provider = quality.draft_and_polish(
            gen_cfg, lead, brief, magnet_url, generate_copy)
    return subject, body, provider, magnet_url, magnet_token


def pending_entry(cfg, lead, subject, body, magnet_url=None, magnet_token=None):
    """The operator-approval queue entry for one drafted cold email.

    Shared so both drafting entry points produce the IDENTICAL shape — the magnet keys
    going missing here is what hid the dead-link bug from the dashboard.
    """
    return {
        "kind": "cold",
        "client": cfg["client"],
        "company_name": lead.get("company_name") or "",
        "target_email": lead["email"],
        "first_name": lead.get("first_name") or "",
        "last_name": lead.get("last_name") or "",
        "title": lead.get("title") or "",
        "drafted_subject": subject,
        "drafted_body": body,
        "magnet_url": magnet_url,
        "magnet_token": magnet_token,
    }


def get_demo_pitch(company_name, base_url=None, sign_off=None):
    """Canned pitches for demo_mode (video recording only) — no API calls.

    Links are built from the client's base URL so they always point at the running
    approval server (never a stale hardcoded port). The SIGN-OFF comes from the
    caller's config: a brand string baked in here would sign another tenant's demo
    mail with our name (MULTITENANCY.md). The bodies are fixtures matched to
    clients/demo/leads.csv — swap them per client if you record a client's own demo.
    """
    base = (base_url or "http://localhost:5001").rstrip("/")
    tail = f"\n\nBest,\n{(sign_off or '').strip()}".rstrip()
    if "Adebayo" in company_name:
        return ("Automating Tax Audits for Adebayo & Co", "Hi Oluwatobi,\n\nI noticed Adebayo & Co handles a massive volume of tax audits and payroll processing for mid-sized enterprises. Manually verifying those ledgers takes your team hours every week.\n\nI mapped out a step-by-step architectural blueprint showing exactly how your firm can automate ledger ingestion and payroll reconciliation using OCR and secure AI models. I've attached the blueprint below for your review.\n\n" + f"{base}/magnet/blueprint" + tail)
    elif "Capital Homes" in company_name:
        return ("Automating Client Inquiries for Capital Homes", "Hi Amina,\n\nI love the luxury properties you are brokering at Capital Homes Abuja. Since you manage thousands of client inquiries monthly, your team likely spends hours answering repetitive questions about property viewings, especially late at night.\n\nI built a custom AI chatbot prototype specifically trained on your Maitama listings. I've generated a 7-day temporary access pass for you to test the software live.\n\nHere is the secure link to test the prototype:\n\n" + f"{base}/magnet/chatbot" + tail)
    else:
        return ("Strategic Audit for Lagos Style Hub", "Hi Chinedu,\n\nI've been following Lagos Style Hub's growth. Managing thousands of daily fashion orders across Nigeria must create a massive bottleneck for your customer support team, leading to missed sales in your DMs.\n\nI did a brief strategic audit of your current workflow and mapped out the exact step-by-step process of how you can build an autonomous AI lead-generation and support system to instantly capture lost WhatsApp sales. \n\nI've linked the strategic breakdown below.\n\n" + f"{base}/magnet/strategic-audit" + tail)


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
    print(f"   draft_cap={draft_cap(cfg, preview_limit=limit if preview else None)}/run  "
          f"(sourcing is uncapped by this — every qualified lead is banked)")
    if preview:
        print(f"   🔎 PREVIEW — real scrape+facts+agents+metrics on a THROWAWAY db; "
              f"nothing queued/emailed/sent (sample: {limit}).")
    print("=========================================================")

    conn = state.connect(preview_db or cfg["paths"]["db"])
    run_id = obs.new_run_id()   # groups every event this run emits in the ledger
    max_drafts = draft_cap(cfg, preview_limit=limit if preview else None)

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

    # --- Bank the list FIRST ------------------------------------------------
    # Every qualified lead is persisted as `sourced` before a single draft is
    # attempted, and independently of the draft cap. This is the whole point of the
    # lead list: what we scraped is an asset we keep, not a by-product of drafting.
    # A lead we never draft today is still ours tomorrow.
    if all_leads:
        newly_banked = state.bank_leads(conn, cfg["client"], all_leads)
        print_step(f"🏦 [List] Banked {newly_banked} new lead(s) "
                   f"({len(all_leads) - newly_banked} already known).")

    # --- Draft from the LIST, oldest first ---------------------------------
    # Reading the pool from the DB (not from today's scrape) means the backlog gets
    # used before anything new, and one code path serves both.
    pool = [_lead_from_row(r) for r in
            state.leads_by_status(conn, cfg["client"], state.SOURCED, limit=max_drafts * 4)]
    totals = state.count_leads_by_status(conn, cfg["client"])
    print_step(f"📇 [List] {sum(totals.values())} lead(s) on the list "
               f"{totals or '{}'}; drafting up to {max_drafts} this run.")
    if not pool:
        print("\n   Nothing new to draft — every banked lead has already been drafted "
              "or contacted. The list is still growing; add queries or wait for the "
              "next rotation window.")

    drafted = 0
    for lead in pool:
        if drafted >= max_drafts:
            label = "sample limit" if preview else "daily draft cap"
            print_step(f"⏹️  Reached the {label} ({max_drafts}); stopping this run.")
            break

        skip, reason = suppression.should_skip(conn, cfg["client"], lead["email"])
        if skip:
            print(f"   ⏭️  Skipping {lead['email']} ({reason}).")
            continue

        # Claim this lead for this run BEFORE drafting, so a concurrent or repeated
        # run won't draft it twice. The lead is already banked, so this only moves
        # its status forward — it can never lose the contact details.
        state.set_status(conn, cfg["client"], lead["email"], "queued")

        magnet_url = None
        magnet_token = None
        if cfg["demo_mode"]:
            print_step(f"🧠 [demo] Strategist analyzing {lead['company_name']}...")
            time.sleep(1.0)
            subject, body = get_demo_pitch(
                lead["company_name"], cfg.get("unsubscribe_base_url"),
                sign_off=cfg.get("from_name") or cfg.get("client_name"))
            provider = "demo"
        else:
            try:
                # One shared recipe (see draft_one_lead): fit → strategy → magnet →
                # copy → quality gate, metered as a single 'draft' ledger step.
                subject, body, provider, magnet_url, magnet_token = draft_one_lead(
                    conn, cfg, lead, run_id)
            except Exception as e:
                print(f"   ❌ Generation failed for {lead['company_name']}: {e}. "
                      f"Releasing the claim so this lead retries on the next run.")
                # We 'claimed' this lead as queued BEFORE drafting so a re-run wouldn't
                # double-draft it. No draft was produced, so release the claim by
                # returning it to the list as `sourced`.
                #
                # This used to DELETE the row, which threw away a perfectly good
                # scraped contact every time a provider hiccuped — the lead had to be
                # re-discovered and re-scraped from scratch. Rolling the STATUS back
                # keeps the contact and still frees it for retry.
                state.set_status(conn, cfg["client"], lead["email"], state.SOURCED)
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

        entry = pending_entry(cfg, lead, subject, body, magnet_url, magnet_token)
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
