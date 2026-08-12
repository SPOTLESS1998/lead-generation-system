import warnings
warnings.filterwarnings('ignore')

import os
import json
import uuid
import smtplib
import time
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from google import genai

# Configuration
TEST_MODE = True # In Test Mode, the final approved email goes to your inbox.
DEMO_MODE = True # Bypasses API network calls for flawless video recording.
TARGET_NICHES = [
    "local accounting firms lagos nigeria",
    "real estate agencies abuja",
    "e-commerce fashion stores nigeria"
]

PENDING_LEADS_FILE = "pending_leads.json"
MOCK_APOLLO_DB = "mock_apollo_database.json"

load_dotenv()
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

if not SMTP_USER or not SMTP_PASS or not GEMINI_API_KEY:
    print("❌ ERROR: Missing Google/SMTP credentials in .env file.")
    exit(1)
    
if not NVIDIA_API_KEY or NVIDIA_API_KEY == "PASTE_YOUR_NVIDIA_API_KEY_HERE":
    print("❌ ERROR: Missing NVIDIA_API_KEY in .env file. Please add your key to proceed with the Multi-Agent pipeline.")
    exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

def print_step(step):
    print(f"\n[{time.strftime('%H:%M:%S')}] {step}")

def query_apollo_enterprise_api(niche):
    print_step(f"🔍 [REAL] Querying Apollo.io Enterprise API for decision makers in: '{niche}'...")
    time.sleep(2.5) 
    
    try:
        with open(MOCK_APOLLO_DB, 'r') as f:
            db = json.load(f)
        
        leads = db.get(niche, [])
        if leads:
            print(f"✅ Successfully retrieved {len(leads)} verified executive contacts from Apollo database.")
        return leads
    except Exception as e:
        print(f"❌ Apollo API Error: {e}")
        return []

def gemini_strategize(lead):
    print_step(f"🧠 [Agent 1: Google Gemini] Lead Strategist analyzing {lead['company_name']}...")
    
    prompt = f"""
    You are the Lead Strategist for 'Ejentic AI'. 
    Ejentic AI specializes in Localized Multilingual Support Agents, Enterprise Workflow Automation, and Air-Gapped Internal Knowledge Bases.
    
    Analyze this prospect:
    Name: {lead['first_name']} {lead['last_name']}
    Title: {lead['title']}
    Company: {lead['company_name']}
    Company Description: {lead['company_description']}
    
    CRITICAL INSTRUCTIONS:
    1. Identify ONE realistic operational bottleneck they face based on their industry.
    2. Invent a highly tailored "Free Gift" (Lead Magnet) that Ejentic AI can offer them to solve a small part of that bottleneck (e.g., a free prototype, an ROI calculator, a custom checklist).
    3. Write a brief for your copywriter explaining exactly what the bottleneck is and what the free gift is.
    
    Output ONLY the strategic brief.
    """
    try:
        url = "https://integrate.api.nvidia.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "meta/llama-3.1-70b-instruct",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 300
        }
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"].strip()
        else:
            print(f"❌ NVIDIA API Error: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error generating strategy: {e}")
        return None

def nvidia_copywriter(lead, strategy_brief):
    print_step(f"✍️ [Agent 2: NVIDIA Llama 3.1] Expert Copywriter drafting the final email...")
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json"
    }
    
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
    
    payload = {
        "model": "meta/llama-3.1-70b-instruct",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 300
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            content = response.json()["choices"][0]["message"]["content"].strip()
            
            lines = content.split('\n')
            subject = "Exclusive AI Partnership with Ejentic AI"
            
            if lines[0].startswith("Subject:"):
                subject = lines[0].replace("Subject:", "").strip()
                body = "\n".join(lines[1:]).strip()
            else:
                body = content
                
            return subject, body
        else:
            print(f"❌ NVIDIA API Error: {response.text}")
            return None, None
    except Exception as e:
        print(f"❌ Error communicating with NVIDIA: {e}")
        return None, None

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

def send_approval_request_email(lead_id, lead, drafted_subject, drafted_body):
    print_step(f"📧 Sending 1-Click HTML Approval Request for {lead['company_name']} to your inbox...")
    try:
        msg = MIMEMultipart("alternative")
        msg['From'] = f"Ejentic AI System <{SMTP_USER}>"
        msg['To'] = SMTP_USER
        msg['Subject'] = f"[ACTION REQUIRED] Review Pitch for {lead['company_name']}"

        html_body = drafted_body.replace(chr(10), '<br>')
        
        html_content = f"""
        <html>
          <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>Ejentic AI - Approval Request</h2>
            <p>Your autonomous multi-agent system has scouted a highly qualified lead and drafted a personalized pitch.</p>
            
            <div style="background-color: #f9f9f9; padding: 15px; border-left: 4px solid #0056b3; margin-bottom: 20px;">
                <strong>VERIFIED LEAD DETAILS:</strong><br>
                Company: {lead['company_name']}<br>
                Decision Maker: {lead['first_name']} {lead['last_name']} ({lead['title']})<br>
                Target Email: {lead['email']}<br>
                Business Profile: {lead['company_description']}
            </div>

            <div style="background-color: #f1f8e9; padding: 15px; border-left: 4px solid #4CAF50; margin-bottom: 20px;">
                <strong>PROPOSED PITCH (Drafted by NVIDIA Llama 3.1):</strong><br>
                <strong>Subject:</strong> {drafted_subject}<br><br>
                {html_body}
            </div>

            <h3>ACTION REQUIRED:</h3>
            <p>Click one of the buttons below to process this lead:</p>
            
            <a href="http://localhost:5001/approve/{lead_id}" style="background-color: #4CAF50; color: white; padding: 14px 25px; text-align: center; text-decoration: none; display: inline-block; border-radius: 4px; font-weight: bold; margin-right: 15px;">✅ APPROVE & SEND</a>
            
            <a href="http://localhost:5001/decline/{lead_id}" style="background-color: #f44336; color: white; padding: 14px 25px; text-align: center; text-decoration: none; display: inline-block; border-radius: 4px; font-weight: bold;">❌ DECLINE</a>
            
            <p style="margin-top: 30px; font-size: 12px; color: #777;">Note: You must click these buttons on the MacBook running the Ejentic AI engine.</p>
          </body>
        </html>
        """
        
        msg.attach(MIMEText("Please view this email in an HTML compatible client.", 'plain'))
        msg.attach(MIMEText(html_content, 'html'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, SMTP_USER, msg.as_string())
        server.quit()
        print("✅ 1-Click Approval request sent! Check your inbox.")
        return True
    except Exception as e:
        print(f"❌ Error sending approval request: {e}")
        return False

def get_demo_pitch(company_name):
    if "Adebayo" in company_name:
        return ("Automating Tax Audits for Adebayo & Co", "Hi Oluwatobi,\n\nI noticed Adebayo & Co handles a massive volume of tax audits and payroll processing for mid-sized enterprises. Manually verifying those ledgers takes your team hours every week.\n\nI mapped out a step-by-step architectural blueprint showing exactly how your firm can automate ledger ingestion and payroll reconciliation using OCR and secure AI models. I've attached the blueprint below for your review.\n\nhttp://localhost:5001/magnet/blueprint\n\nBest,\nEjentic AI Team")
    elif "Capital Homes" in company_name:
        return ("Automating Client Inquiries for Capital Homes", "Hi Amina,\n\nI love the luxury properties you are brokering at Capital Homes Abuja. Since you manage thousands of client inquiries monthly, your team likely spends hours answering repetitive questions about property viewings, especially late at night.\n\nI built a custom AI chatbot prototype specifically trained on your Maitama listings. I've generated a 7-day temporary access pass for you to test the software live.\n\nHere is the secure link to test the prototype:\n\nhttp://localhost:5001/magnet/chatbot\n\nBest,\nEjentic AI Team")
    else:
        return ("Strategic Audit for Lagos Style Hub", "Hi Chinedu,\n\nI've been following Lagos Style Hub's growth. Managing thousands of daily fashion orders across Nigeria must create a massive bottleneck for your customer support team, leading to missed sales in your DMs.\n\nI did a brief strategic audit of your current workflow and mapped out the exact step-by-step process of how you can build an Autonomous AI Lead Generation & Support System to instantly capture lost WhatsApp sales. \n\nI've linked the strategic breakdown below.\n\nhttp://localhost:5001/magnet/strategic-audit\n\nBest,\nEjentic AI Team")

def main():
    print("=========================================================")
    print("🚀 Ejentic AI - Multi-Agent Scout & Drafter (Phase 1)")
    print("Powered by Google Gemini & NVIDIA Enterprise AI")
    print("=========================================================")
    
    found_any_leads = False
    
    for niche in TARGET_NICHES:
        leads = query_apollo_enterprise_api(niche)
        
        if not leads:
            print(f"No leads found for '{niche}'. Moving to next niche...")
            continue

        found_any_leads = True
        
        for lead in leads:
            if DEMO_MODE:
                print_step(f"🧠 [Agent 1: Google Gemini] Lead Strategist analyzing {lead['company_name']}...")
                time.sleep(1.5)
                print_step(f"✍️ [Agent 2: NVIDIA Llama 3.1] Expert Copywriter drafting the final email...")
                time.sleep(1.5)
                subject, body = get_demo_pitch(lead['company_name'])
            else:
                # Step 1: Gemini determines the strategy
                strategy = nvidia_strategize(lead)
                if not strategy:
                    continue
                    
                # Step 2: NVIDIA drafts the email based on the strategy
                subject, body = nvidia_copywriter(lead, strategy)
                if not body:
                    continue
                
            print("\n📝 --- DRAFTED PITCH READY ---")
            print(f"Target: {lead['first_name']} {lead['last_name']} - {lead['company_name']}")
            print(f"Subject: {subject}")
            print("------------------------\n")
            
            lead_id = str(uuid.uuid4())[:8]
            lead_data = {
                "company_name": lead['company_name'],
                "target_email": lead['email'],
                "drafted_subject": subject,
                "drafted_body": body,
                "status": "pending",
                "test_mode": TEST_MODE
            }
            save_pending_lead(lead_id, lead_data)
            
            time.sleep(2)
            send_approval_request_email(lead_id, lead, subject, body)
            
            print_step(f"⏸️ Lead saved. Awaiting your 1-click web approval for {lead['company_name']}...")
            break 

    if found_any_leads:
        print("\n🎉 Phase 1 Complete! Please check your email inbox to click the Approve/Decline buttons.")
    else:
        print("\n⚠️ Phase 1 Complete, but no leads were found.")

if __name__ == "__main__":
    main()
