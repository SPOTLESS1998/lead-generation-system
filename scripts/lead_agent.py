import warnings
warnings.filterwarnings('ignore')

import os
import sys
import time

# Make the project root importable so `core` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state, suppression, review, magnet, leads as leads_source
from core.ai import generate

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

    prompt = f"""
    You are the Lead Strategist for '{cfg['client_name']}'.
    {cfg['client_name']} specializes in Localized Multilingual Support Agents, Enterprise Workflow Automation, and Air-Gapped Internal Knowledge Bases.
{service_line}
    Analyze this prospect:
    Name: {lead['first_name']} {lead['last_name']}
    Title: {lead['title']}
    Company: {lead['company_name']}
    Company Description: {lead['company_description']}

    CRITICAL INSTRUCTIONS:
    1. Identify ONE realistic operational bottleneck they face based on their industry.
    2. The "Free Gift" (Lead Magnet) we always offer is a personalized mini-audit / AI blueprint page tailored to this prospect. Describe what that audit should focus on for them.
    3. Write a brief for your copywriter explaining exactly what the bottleneck is and what the personalized audit will cover.

    Output ONLY the strategic brief.
    """
    brief, provider = generate(cfg, prompt)
    return brief, provider


def generate_copy(cfg, lead, strategy_brief, magnet_url=None):
    print_step(f"✍️  [Copywriter] Drafting the email for {lead['company_name']}...")

    # The "free gift" is a real, personalized audit page. We hand the copywriter the
    # actual URL and require it verbatim, so the email never ships a dead placeholder.
    if magnet_url:
        gift_instruction = (
            f'4. Offer them their free personalized AI audit and include this EXACT link on its own '
            f'line so they can open it right away: {magnet_url}\n'
            f'   Write the URL in full and unchanged. Never use placeholder text like "[Link to Free Gift]".'
        )
    else:
        gift_instruction = (
            '4. Offer to send them a free personalized AI audit and invite them to reply if they want it. '
            'Do NOT invent or include any link.'
        )

    prompt = f"""
    You are an expert, high-converting B2B Copywriter.
    Your Lead Strategist has provided you with the following strategy to pitch a prospect:

    PROSPECT DETAILS:
    Name: {lead['first_name']} {lead['last_name']}
    Company: {lead['company_name']}

    STRATEGY BRIEF:
    {strategy_brief}

    INSTRUCTIONS:
    Write a cold outreach email based exactly on the Strategy Brief.
    1. Start with a highly personalized greeting using their first name ('Hi {lead['first_name']},').
    2. Be extremely concise (under 100 words), human-sounding, and conversational.
    3. Do not sound like an AI. Do not use corporate buzzwords.
    {gift_instruction}
    5. Deliverability: write in plain, natural language. Avoid spam-trigger words (free money, guarantee, act now, limited time, click here, 100%, cash, urgent, risk-free), do not use ALL-CAPS words, do not use more than one exclamation mark, and only ever use https links.
    6. Start the very first line strictly with 'Subject: ' to provide the email subject.

    Output ONLY the email subject and body. No other text.
    """
    content, provider = generate(cfg, prompt)
    lines = content.split('\n')
    subject = f"A quick idea for {lead['company_name']}"
    if lines and lines[0].startswith("Subject:"):
        subject = lines[0].replace("Subject:", "").strip()
        body = "\n".join(lines[1:]).strip()
    else:
        body = content
    return subject, body, provider


def get_demo_pitch(company_name):
    """Canned pitches for demo_mode (video recording only) — no API calls."""
    if "Adebayo" in company_name:
        return ("Automating Tax Audits for Adebayo & Co", "Hi Oluwatobi,\n\nI noticed Adebayo & Co handles a massive volume of tax audits and payroll processing for mid-sized enterprises. Manually verifying those ledgers takes your team hours every week.\n\nI mapped out a step-by-step architectural blueprint showing exactly how your firm can automate ledger ingestion and payroll reconciliation using OCR and secure AI models. I've attached the blueprint below for your review.\n\nhttp://localhost:5001/magnet/blueprint\n\nBest,\nEjentic AI Team")
    elif "Capital Homes" in company_name:
        return ("Automating Client Inquiries for Capital Homes", "Hi Amina,\n\nI love the luxury properties you are brokering at Capital Homes Abuja. Since you manage thousands of client inquiries monthly, your team likely spends hours answering repetitive questions about property viewings, especially late at night.\n\nI built a custom AI chatbot prototype specifically trained on your Maitama listings. I've generated a 7-day temporary access pass for you to test the software live.\n\nHere is the secure link to test the prototype:\n\nhttp://localhost:5001/magnet/chatbot\n\nBest,\nEjentic AI Team")
    else:
        return ("Strategic Audit for Lagos Style Hub", "Hi Chinedu,\n\nI've been following Lagos Style Hub's growth. Managing thousands of daily fashion orders across Nigeria must create a massive bottleneck for your customer support team, leading to missed sales in your DMs.\n\nI did a brief strategic audit of your current workflow and mapped out the exact step-by-step process of how you can build an Autonomous AI Lead Generation & Support System to instantly capture lost WhatsApp sales. \n\nI've linked the strategic breakdown below.\n\nhttp://localhost:5001/magnet/strategic-audit\n\nBest,\nEjentic AI Team")


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def main():
    cfg = config.load_client()
    print("=========================================================")
    print(f"🚀 {cfg['client_name']} - Lead Scout & Drafter")
    print(f"   client={cfg['client']}  provider={cfg['copy_provider']}  "
          f"send_mode={cfg['sending']['mode']}  demo_mode={cfg['demo_mode']}")
    print("=========================================================")

    conn = state.connect(cfg["paths"]["db"])

    # Lead source is config-selectable: a curated CSV, or live auto-discovery
    # (Google Maps or Yellow Pages → Firecrawl → AI extraction). All return
    # (leads, skipped) with the same lead shape, so the rest is identical.
    if cfg.get("lead_source") == "maps_firecrawl":
        from core import discovery
        print_step("🛰️  Auto-sourcing leads (Google Maps → Firecrawl → AI extraction)...")
        all_leads, skipped = discovery.load_leads(cfg)
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
        if drafted >= MAX_PER_RUN:
            print_step(f"⏹️  Reached MAX_PER_RUN ({MAX_PER_RUN}); stopping this run.")
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
            subject, body = get_demo_pitch(lead["company_name"])
            provider = "demo"
        else:
            try:
                brief, _ = generate_strategy(cfg, lead)
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
                subject, body, provider = generate_copy(cfg, lead, brief, magnet_url)
            except Exception as e:
                print(f"   ❌ Generation failed for {lead['company_name']}: {e}. "
                      f"Leaving lead 'queued' for retry.")
                continue

        print("\n📝 --- DRAFTED PITCH READY ---")
        print(f"Target: {lead['first_name']} {lead['last_name']} - {lead['company_name']}")
        print(f"Subject: {subject}   (drafted by: {provider})")
        print("------------------------")

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


if __name__ == "__main__":
    main()
