import warnings
warnings.filterwarnings('ignore')

import os
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask
from dotenv import load_dotenv
import logging

PENDING_LEADS_FILE = "pending_leads.json"
app = Flask(__name__)

# Suppress Flask default terminal logging to keep it clean
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

load_dotenv()
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

def send_final_email(to_email, subject, body):
    try:
        msg = MIMEMultipart()
        msg['From'] = f"Ejentic AI Outreach <{SMTP_USER}>"
        msg['To'] = to_email
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        text = msg.as_string()
        server.sendmail(SMTP_USER, to_email, text)
        server.quit()
        return True
    except Exception as e:
        print(f"❌ Error sending final email: {e}")
        return False

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

@app.route('/approve/<lead_id>')
def approve(lead_id):
    leads = get_pending_leads()
    if lead_id in leads and leads[lead_id]["status"] == "pending":
        lead_data = leads[lead_id]
        
        # Test Mode: Send to ourselves
        target_email = SMTP_USER
        
        print(f"\n[!] Received WEB APPROVAL for Lead ID: {lead_id} ({lead_data['company_name']})")
        print(f"📧 Sending FINAL APPROVED email to: {target_email}...")
        
        success = send_final_email(target_email, lead_data["drafted_subject"], lead_data["drafted_body"])
        if success:
            update_lead_status(lead_id, "approved_and_sent")
            print(f"✅ Email sent successfully! Lead {lead_id} is complete.")
            return f"<html><body style='font-family: Arial, sans-serif; text-align: center; margin-top: 50px;'><h1>✅ Pitch Approved and Sent!</h1><p>The highly personalized email has been dispatched to {lead_data['company_name']}.</p><p>You may now close this tab.</p></body></html>", 200
        else:
            return "<html><body style='font-family: Arial, sans-serif; text-align: center; margin-top: 50px;'><h1>❌ Failed to send email</h1><p>Check the terminal for errors.</p></body></html>", 500
            
    return "<html><body style='font-family: Arial, sans-serif; text-align: center; margin-top: 50px;'><h1>⚠️ Link Expired or Invalid</h1><p>This lead has already been processed or does not exist.</p></body></html>", 400

@app.route('/decline/<lead_id>')
def decline(lead_id):
    leads = get_pending_leads()
    if lead_id in leads and leads[lead_id]["status"] == "pending":
        update_lead_status(lead_id, "declined")
        print(f"\n[!] Received WEB DECLINE for Lead ID: {lead_id}. Discarding draft.")
        return "<html><body style='font-family: Arial, sans-serif; text-align: center; margin-top: 50px;'><h1>❌ Pitch Declined</h1><p>The draft has been discarded and will not be sent.</p><p>You may now close this tab.</p></body></html>", 200
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
    print("🚀 Ejentic AI - Web Approval Server (Phase 2)")
    print("Listening for button clicks on http://localhost:5001...")
    print("=========================================================")
    app.run(port=5001, debug=False)
