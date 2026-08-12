import warnings
warnings.filterwarnings('ignore')

import os
import sys
import json
import logging

from flask import Flask

# Make the project root importable so `core` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state, sender, suppression, compliance

PENDING_LEADS_FILE = os.path.join(config.ROOT, "pending_leads.json")

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Cache resolved client configs (creds pulled from env once per client).
_CFG_CACHE = {}


def get_cfg(client_name):
    if client_name not in _CFG_CACHE:
        _CFG_CACHE[client_name] = config.load_client(client_name)
    return _CFG_CACHE[client_name]


def open_conn(cfg):
    # Fresh connection per request → thread-safe under Flask's threaded dev server.
    return state.connect(cfg["paths"]["db"])


def page(title, body, emoji="✅"):
    return f"""<html><body style="font-family: Arial, sans-serif; text-align:center; margin-top:60px; color:#333;">
    <h1>{emoji} {title}</h1><p style="font-size:16px; color:#555;">{body}</p>
    <p style="color:#999;">You may now close this tab.</p></body></html>"""


# --- pending-draft queue (transient hand-off from the agent) ---------------

def get_pending_leads():
    if not os.path.exists(PENDING_LEADS_FILE):
        return {}
    with open(PENDING_LEADS_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def update_lead_status(lead_id, status):
    leads = get_pending_leads()
    if lead_id in leads:
        leads[lead_id]["status"] = status
        with open(PENDING_LEADS_FILE, "w") as f:
            json.dump(leads, f, indent=4)


# --- routes ----------------------------------------------------------------

@app.route('/approve/<lead_id>')
def approve(lead_id):
    leads = get_pending_leads()
    if lead_id not in leads or leads[lead_id].get("status") != "pending":
        return page("Link Expired or Invalid",
                    "This lead has already been processed or does not exist.", "⚠️"), 400

    data = leads[lead_id]
    client = data.get("client", config.active_client())
    to_email = data["target_email"]
    cfg = get_cfg(client)
    conn = open_conn(cfg)

    print(f"\n[!] WEB APPROVAL for {lead_id} ({data.get('company_name')}) — client={client}")
    try:
        pool = sender.SendingPool(cfg)
        token = compliance.unsub_token(client, to_email, config.unsub_secret())
        unsub = compliance.unsub_url(cfg, token)
        result = pool.send(
            conn, to_email, data["drafted_subject"], data["drafted_body"],
            footer_html=compliance.footer(cfg, unsub, "html"),
            footer_text=compliance.footer(cfg, unsub, "text"),
            throttle=False,  # a human clicking already paces sends; throttle is for batch/cron
        )
    except sender.SendCapExceeded as e:
        return page("Daily Limit Reached",
                    f"Not sent: {e}. This protects the sending domain's reputation. "
                    f"Try again tomorrow or raise the cap in the client config.", "⛔"), 429
    except sender.SendError as e:
        return page("Failed to Send", f"SMTP error: {e}. Check the terminal.", "❌"), 500
    finally:
        conn.close()

    update_lead_status(lead_id, "approved_and_sent")
    dest = "your controlled inbox" if result["redirected"] else to_email
    print(f"✅ Sent via {result['mailbox']} → {dest} (intended for {result['intended_for']}).")
    return page("Pitch Approved & Sent!",
                f"Delivered to <b>{dest}</b> (intended for {result['intended_for']}), "
                f"sent via {result['mailbox']}. A CAN-SPAM footer with an unsubscribe link was included."), 200


@app.route('/decline/<lead_id>')
def decline(lead_id):
    leads = get_pending_leads()
    if lead_id in leads and leads[lead_id].get("status") == "pending":
        update_lead_status(lead_id, "declined")
        print(f"\n[!] WEB DECLINE for {lead_id}. Draft discarded.")
    return page("Pitch Declined", "The draft has been discarded and will not be sent.", "❌"), 200


@app.route('/unsubscribe/<token>')
def unsubscribe(token):
    parsed = compliance.verify_token(token, config.unsub_secret())
    if not parsed:
        return page("Invalid Link",
                    "This unsubscribe link is invalid or has been tampered with.", "⚠️"), 400
    client, email = parsed
    cfg = get_cfg(client)
    conn = open_conn(cfg)
    try:
        suppression.add(conn, client, email, reason="unsubscribed")
    finally:
        conn.close()
    print(f"\n[!] UNSUBSCRIBE: {email} (client={client}) added to suppression list.")
    return page("Unsubscribed",
                f"<b>{email}</b> has been removed and will not be contacted again."), 200


@app.route('/magnet/blueprint')
def blueprint():
    return """
    <html>
      <head><title>Architectural Blueprint</title></head>
      <body style="font-family: 'Inter', sans-serif; background-color: #f4f7f6; color: #333; padding: 40px; max-width: 800px; margin: auto;">
        <div style="background: white; padding: 40px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1);">
          <h1 style="color: #2c3e50;">🏗️ Step-by-Step AI Architecture Blueprint</h1>
          <p style="color: #7f8c8d; font-size: 18px;">Prepared exclusively for <strong>Adebayo & Co Tax Partners</strong></p>
          <hr style="border: 1px solid #eee; margin: 20px 0;">
          <h3 style="color: #2980b9;">Phase 1: The Problem (Manual Ledger Entry)</h3>
          <p>Your team is spending 40+ hours a month manually cross-referencing bank statements against vendor receipts. This limits the number of enterprise clients you can onboard.</p>
          <h3 style="color: #2980b9;">Phase 2: The AI Solution Pipeline</h3>
          <p><strong>Step 1: Ingestion</strong> - Deploy an OCR pipeline that automatically scans and digitizes physical receipts via WhatsApp or email.<br>
          <strong>Step 2: Processing</strong> - Llama 3.1 analyzes the scanned text, identifying vendor names, amounts, and tax categories.<br>
          <strong>Step 3: Reconciliation</strong> - The AI API automatically cross-references these amounts with the bank ledger and highlights only the discrepancies for human review.</p>
          <div style="margin-top: 40px; padding: 20px; background: #e8f4f8; border-radius: 8px;">
            <strong>Ready to build this?</strong> Reply to the email to schedule a free technical deployment consultation with Ejentic AI.
          </div>
        </div>
      </body>
    </html>
    """


@app.route('/magnet/chatbot')
def chatbot():
    return """
    <html>
      <head><title>Capital Homes - AI Prototype</title></head>
      <body style="font-family: 'Inter', sans-serif; background-color: #1e1e1e; color: #fff; padding: 40px; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0;">
        <div style="background: #2d2d2d; width: 400px; height: 600px; border-radius: 20px; border: 4px solid #4CAF50; overflow: hidden; display: flex; flex-direction: column; position: relative;">
          <div style="position: absolute; top: 10px; right: 10px; background: #e74c3c; padding: 5px 10px; border-radius: 10px; font-weight: bold; font-size: 10px; transform: rotate(10deg); box-shadow: 0 4px 8px rgba(0,0,0,0.3);">
            7-DAY TEMPORARY ACCESS
          </div>
          <div style="background: #4CAF50; padding: 20px; text-align: center; font-weight: bold; font-size: 20px;">
            🏠 Capital Homes Assistant
          </div>
          <div style="padding: 20px; flex-grow: 1; overflow-y: auto;">
            <div style="background: #444; padding: 15px; border-radius: 10px; margin-bottom: 15px; max-width: 80%;">
              Hi Amina! I am your new custom AI. I'm currently trained on all properties in Maitama and Asokoro. How can I help your clients today?
            </div>
            <div style="background: #4CAF50; padding: 15px; border-radius: 10px; margin-bottom: 15px; max-width: 80%; align-self: flex-end; margin-left: auto;">
              What is the price of the 4-bedroom duplex in Maitama?
            </div>
            <div style="background: #444; padding: 15px; border-radius: 10px; margin-bottom: 15px; max-width: 80%;">
              The 4-bedroom duplex on Gana Street, Maitama is currently listed at ₦450,000,000. Would you like me to schedule a viewing for you this week?
            </div>
          </div>
          <div style="padding: 20px; background: #222; text-align: center; color: #888; font-size: 14px;">
            <em>Your temporary access expires in 7 days. Reply to Ejentic AI's email to unlock full deployment.</em>
          </div>
        </div>
      </body>
    </html>
    """


@app.route('/magnet/strategic-audit')
def strategic_audit():
    return """
    <html>
      <head><title>Strategic Audit</title></head>
      <body style="font-family: 'Inter', sans-serif; background-color: #fdfbf7; color: #333; padding: 40px; max-width: 800px; margin: auto;">
        <div style="background: white; padding: 40px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); border-top: 5px solid #8e44ad;">
          <h1 style="color: #8e44ad;">📊 Strategic Workflow Audit</h1>
          <p style="color: #7f8c8d; font-size: 18px;">Custom mapped for <strong>Lagos Style Hub</strong></p>
          <hr style="border: 1px solid #eee; margin: 20px 0;">
          <h3 style="color: #333;">The Bottleneck: WhatsApp Cart Abandonment</h3>
          <p>We audited common fashion e-commerce workflows. When a customer DMs you at 11:00 PM asking for available sizes, your human team cannot respond until 9:00 AM. By then, the customer has lost impulse buying interest. You are losing a large percentage of potential late-night sales to slow response times.</p>
          <h3 style="color: #333;">The Step-by-Step AI Solution</h3>
          <p><strong>Step 1: Data Integration</strong> - We extract your entire inventory data (sizes, colors, stock).<br>
          <strong>Step 2: AI Brain Training</strong> - We train an AI model to understand your exact brand voice and inventory.<br>
          <strong>Step 3: WhatsApp Automation</strong> - The AI takes over your WhatsApp API. When a customer messages at 2 AM, the AI instantly checks stock, replies with a payment link, and closes the sale.</p>
          <div style="margin-top: 30px; text-align: center; color: #888;">
            Reply to our email to integrate the AI agent into your workflow.
          </div>
        </div>
      </body>
    </html>
    """


if __name__ == '__main__':
    print("=========================================================")
    print("🚀 Ejentic AI - Web Approval Server")
    print("Listening for button clicks on http://localhost:5001 ...")
    print("=========================================================")
    app.run(port=5001, debug=False)
