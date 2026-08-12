import warnings
warnings.filterwarnings('ignore')

import os
import sys
import json
import uuid
import time
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Make the project root importable so `core` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state, suppression, leads as leads_source

# The pending-draft queue the approval server reads (transient hand-off; git-ignored).
PENDING_LEADS_FILE = os.path.join(config.ROOT, "pending_leads.json")

# Safety valve: how many drafts to prepare in a single run (no point drafting more
# than a day's sending capacity). The sending cap is enforced separately at send time.
MAX_PER_RUN = 50


def print_step(step):
    print(f"\n[{time.strftime('%H:%M:%S')}] {step}")


# --------------------------------------------------------------------------
# AI copy generation — Gemini primary (mandated), NVIDIA an automatic fallback.
# --------------------------------------------------------------------------

def _gemini_chat(cfg, prompt):
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(model=cfg["gemini_model"], contents=prompt)
    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        raise RuntimeError("Gemini returned empty text")
    return text


def _nvidia_chat(cfg, prompt, temperature=0.4, max_tokens=400):
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY not set")
    resp = requests.post(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": cfg["nvidia_model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"NVIDIA API {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"].strip()


def generate(cfg, prompt):
    """Generate text using the configured provider, falling back to the other.

    Returns (text, provider_used). Mirrors the fallback-chain pattern used in the
    RAG system: try the mandated engine, degrade gracefully rather than crash.
    """
    order = ["gemini", "nvidia"] if cfg.get("copy_provider") == "gemini" else ["nvidia", "gemini"]
    last_err = None
    for provider in order:
        try:
            text = _gemini_chat(cfg, prompt) if provider == "gemini" else _nvidia_chat(cfg, prompt)
            return text, provider
        except Exception as e:
            last_err = e
            print(f"⚠️  {provider} generation failed ({e}); trying fallback...")
    raise RuntimeError(f"All copy providers failed. Last error: {last_err}")


def generate_strategy(cfg, lead):
    print_step(f"🧠 [Strategist] Analyzing {lead['company_name']}...")
    prompt = f"""
    You are the Lead Strategist for '{cfg['client_name']}'.
    {cfg['client_name']} specializes in Localized Multilingual Support Agents, Enterprise Workflow Automation, and Air-Gapped Internal Knowledge Bases.

    Analyze this prospect:
    Name: {lead['first_name']} {lead['last_name']}
    Title: {lead['title']}
    Company: {lead['company_name']}
    Company Description: {lead['company_description']}

    CRITICAL INSTRUCTIONS:
    1. Identify ONE realistic operational bottleneck they face based on their industry.
    2. Invent a highly tailored "Free Gift" (Lead Magnet) that we can offer them to solve a small part of that bottleneck (e.g., a free prototype, an ROI calculator, a custom checklist).
    3. Write a brief for your copywriter explaining exactly what the bottleneck is and what the free gift is.

    Output ONLY the strategic brief.
    """
    brief, provider = generate(cfg, prompt)
    return brief, provider


def generate_copy(cfg, lead, strategy_brief):
    print_step(f"✍️  [Copywriter] Drafting the email for {lead['company_name']}...")
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
    4. Do NOT ask for permission to send the Free Gift. Provide the Free Gift directly in the email by including a placeholder link (e.g., "I've included a link to the prototype below for you to test.\n\n[Link to Free Gift]").
    5. Start the very first line strictly with 'Subject: ' to provide the email subject.

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
# Draft queue + operator notification
# --------------------------------------------------------------------------

def save_pending_lead(lead_id, data):
    leads = {}
    if os.path.exists(PENDING_LEADS_FILE):
        with open(PENDING_LEADS_FILE, "r") as f:
            try:
                leads = json.load(f)
            except json.JSONDecodeError:
                pass
    leads[lead_id] = data
    with open(PENDING_LEADS_FILE, "w") as f:
        json.dump(leads, f, indent=4)


def send_approval_request_email(cfg, lead_id, lead, drafted_subject, drafted_body):
    """Notify the operator (their own inbox) with 1-click approve/decline links."""
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    dashboard = (cfg.get("unsubscribe_base_url") or "http://localhost:5001").rstrip("/")
    print_step(f"📧 Sending approval request for {lead['company_name']} to your inbox...")
    try:
        msg = MIMEMultipart("alternative")
        msg['From'] = f"{cfg['client_name']} System <{smtp_user}>"
        msg['To'] = smtp_user
        msg['Subject'] = f"[ACTION REQUIRED] Review pitch for {lead['company_name']}"

        html_body = drafted_body.replace(chr(10), '<br>')
        html_content = f"""
        <html>
          <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>{cfg['client_name']} - Approval Request</h2>
            <p>Your AI system scouted a lead and drafted a personalized pitch.</p>
            <div style="background:#f9f9f9; padding:15px; border-left:4px solid #0056b3; margin-bottom:20px;">
                <strong>LEAD:</strong><br>
                Company: {lead['company_name']}<br>
                Decision Maker: {lead['first_name']} {lead['last_name']} ({lead['title']})<br>
                Prospect Email: {lead['email']}
            </div>
            <div style="background:#f1f8e9; padding:15px; border-left:4px solid #4CAF50; margin-bottom:20px;">
                <strong>PROPOSED PITCH:</strong><br>
                <strong>Subject:</strong> {drafted_subject}<br><br>
                {html_body}
            </div>
            <p>Click to process this lead (the approved email goes to your controlled inbox in demo mode):</p>
            <a href="{dashboard}/approve/{lead_id}" style="background:#4CAF50; color:white; padding:14px 25px; text-decoration:none; border-radius:4px; font-weight:bold; margin-right:15px;">✅ APPROVE &amp; SEND</a>
            <a href="{dashboard}/decline/{lead_id}" style="background:#f44336; color:white; padding:14px 25px; text-decoration:none; border-radius:4px; font-weight:bold;">❌ DECLINE</a>
            <p style="margin-top:30px; font-size:12px; color:#777;">Click these on the machine running the approval server ({dashboard}).</p>
          </body>
        </html>
        """
        msg.attach(MIMEText("Please view this email in an HTML compatible client.", 'plain'))
        msg.attach(MIMEText(html_content, 'html'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, smtp_user, msg.as_string())
        server.quit()
        print("✅ Approval request sent! Check your inbox.")
        return True
    except Exception as e:
        print(f"❌ Error sending approval request: {e}")
        return False


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
    all_leads, skipped = leads_source.load_leads(cfg)
    print_step(f"🔍 Loaded {len(all_leads)} curated lead(s) "
               f"({skipped} template/placeholder row(s) ignored).")

    if not all_leads:
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
        state.upsert_lead(conn, cfg["client"], lead, status="queued")

        if cfg["demo_mode"]:
            print_step(f"🧠 [demo] Strategist analyzing {lead['company_name']}...")
            time.sleep(1.0)
            subject, body = get_demo_pitch(lead["company_name"])
            provider = "demo"
        else:
            try:
                brief, _ = generate_strategy(cfg, lead)
                subject, body, provider = generate_copy(cfg, lead, brief)
            except Exception as e:
                print(f"   ❌ Generation failed for {lead['company_name']}: {e}. "
                      f"Leaving lead 'queued' for retry.")
                continue

        print("\n📝 --- DRAFTED PITCH READY ---")
        print(f"Target: {lead['first_name']} {lead['last_name']} - {lead['company_name']}")
        print(f"Subject: {subject}   (drafted by: {provider})")
        print("------------------------")

        lead_id = str(uuid.uuid4())[:8]
        save_pending_lead(lead_id, {
            "client": cfg["client"],
            "company_name": lead["company_name"],
            "target_email": lead["email"],
            "first_name": lead["first_name"],
            "drafted_subject": subject,
            "drafted_body": body,
            "status": "pending",
        })
        send_approval_request_email(cfg, lead_id, lead, subject, body)
        drafted += 1

    print(f"\n🎉 Done. Drafted {drafted} pitch(es) awaiting your web approval "
          f"at {cfg.get('unsubscribe_base_url')}.")


if __name__ == "__main__":
    main()
