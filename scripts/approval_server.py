import warnings
warnings.filterwarnings('ignore')

import os
import sys
import html
import logging

from flask import Flask

# Make the project root importable so `core` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state, sender, suppression, compliance, review, magnet
from core import calendar as gcal   # core/calendar.py (the booking seam), not stdlib calendar

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


# --- routes ----------------------------------------------------------------
# The pending-draft queue lives in core.review (shared by both agents).

# Spam-badge palette — mirrors the readout in core/review.py.
_SPAM_BADGE = {
    "ok":   ("#e8f5e9", "#2e7d32", "✅ Spam: clean"),
    "warn": ("#fff8e1", "#ef6c00", "⚠️ Spam: minor"),
    "high": ("#ffebee", "#c62828", "🚫 Spam: high risk"),
}


def _card(lead_id, entry):
    """One pending draft rendered as an approve/decline card (all text escaped)."""
    kind = entry.get("kind", "cold")
    company = html.escape(entry.get("company_name") or entry.get("target_email") or "prospect")
    name = html.escape(f'{entry.get("first_name","")} {entry.get("last_name","")}'.strip())
    title = html.escape(entry.get("title", ""))
    subject = html.escape(entry.get("drafted_subject", ""))
    body = html.escape(entry.get("drafted_body", "")).replace("\n", "<br>")
    who_line = " · ".join(x for x in [name, title] if x)

    spam = entry.get("spam") or {}
    bg, fg, label = _SPAM_BADGE.get(spam.get("level", "ok"), _SPAM_BADGE["ok"])
    badge = (f'<span style="background:{bg};color:{fg};padding:4px 10px;border-radius:12px;'
             f'font-size:12px;font-weight:bold;">{label} ({spam.get("score", 0)})</span>')

    kind_bg, kind_label = (("#ede7f6", "↩ REPLY") if kind == "reply" else ("#e3f2fd", "✉ COLD"))
    kind_badge = (f'<span style="background:{kind_bg};color:#333;padding:4px 10px;'
                  f'border-radius:12px;font-size:12px;font-weight:bold;">{kind_label}</span>')

    magnet = ""
    if entry.get("magnet_url"):
        magnet = (f'<a href="{html.escape(entry["magnet_url"])}" target="_blank" '
                  f'style="color:#0056b3;font-size:14px;">🎁 View their personalized page →</a>')

    return f"""
    <div style="background:white;border:1px solid #e0e0e0;border-radius:10px;padding:22px;
                margin-bottom:20px;box-shadow:0 2px 6px rgba(0,0,0,0.06);text-align:left;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <h2 style="margin:0;color:#2c3e50;font-size:20px;">{company}</h2>
        <div>{kind_badge} &nbsp; {badge}</div>
      </div>
      <p style="margin:0 0 14px;color:#666;font-size:14px;">{who_line}</p>
      <div style="background:#f1f8e9;border-left:4px solid #4CAF50;padding:14px;border-radius:4px;margin-bottom:14px;">
        <strong>Subject:</strong> {subject}<br><br>{body}
      </div>
      <p style="margin:0 0 18px;">{magnet}</p>
      <a href="/approve/{lead_id}" style="background:#4CAF50;color:white;padding:12px 26px;
         text-decoration:none;border-radius:6px;font-weight:bold;margin-right:12px;">✅ Approve &amp; Send</a>
      <a href="/decline/{lead_id}" style="background:#f44336;color:white;padding:12px 26px;
         text-decoration:none;border-radius:6px;font-weight:bold;">❌ Decline</a>
    </div>"""


@app.route('/')
def dashboard():
    """Home screen: every pending draft for the active client, ready to approve."""
    client = config.active_client()
    try:
        cfg = get_cfg(client)
        client_label = html.escape(cfg.get("client_name", client))
    except Exception:
        client_label = html.escape(client)

    leads = review.load_pending()
    pending = [(lid, e) for lid, e in leads.items()
               if e.get("client") == client and e.get("status") == "pending"]

    if pending:
        cards = "".join(_card(lid, e) for lid, e in pending)
        count_line = f"{len(pending)} draft{'s' if len(pending) != 1 else ''} waiting for your approval"
    else:
        cards = ('<div style="background:white;border-radius:10px;padding:50px;color:#999;">'
                 'No drafts waiting. Run <code>lead_agent.py</code> or <code>reply_agent.py</code> '
                 'to queue some.</div>')
        count_line = "Nothing pending right now"

    return f"""<html><head><title>{client_label} — Approvals</title>
    <meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="font-family:Arial,sans-serif;background:#f4f7f6;margin:0;padding:0;color:#333;">
      <div style="max-width:760px;margin:0 auto;padding:40px 20px;">
        <h1 style="color:#2c3e50;margin-bottom:4px;">📋 {client_label} — Approval Queue</h1>
        <p style="color:#777;margin-top:0;margin-bottom:30px;">{count_line}.</p>
        {cards}
      </div>
    </body></html>""", 200


@app.route('/approve/<lead_id>')
def approve(lead_id):
    leads = review.load_pending()
    if lead_id not in leads or leads[lead_id].get("status") != "pending":
        return page("Link Expired or Invalid",
                    "This lead has already been processed or does not exist.", "⚠️"), 400

    data = leads[lead_id]
    kind = data.get("kind", "cold")
    client = data.get("client", config.active_client())
    to_email = data["target_email"]
    cfg = get_cfg(client)
    conn = open_conn(cfg)

    who = data.get("company_name") or to_email
    print(f"\n[!] WEB APPROVAL for {lead_id} ({who}) — client={client}, kind={kind}")
    try:
        pool = sender.SendingPool(cfg)
        token = compliance.unsub_token(client, to_email, config.unsub_secret())
        unsub = compliance.unsub_url(cfg, token)
        footer_html = compliance.footer(cfg, unsub, "html")
        footer_text = compliance.footer(cfg, unsub, "text")

        if kind == "reply":
            # 1) Optionally book a real meeting FIRST. Graceful (RAG-reranker-404
            #    pattern): a booking failure must never stop the reply from going out.
            meeting = data.get("meeting") or {}
            booked = None
            if meeting.get("start_local") and cfg.get("booking", {}).get("enabled"):
                # Attendee follows the sending mode: never invite a real stranger
                # during a controlled demo — point the invite at the safe inbox.
                attendee = (cfg["sending"]["controlled_inbox"]
                            if cfg["sending"]["mode"] == "controlled" else to_email)
                try:
                    booked = gcal.create_event(
                        cfg,
                        summary=meeting.get("title") or f"Intro call — {cfg['client_name']}",
                        start_local=meeting["start_local"],
                        duration_min=int(meeting.get("duration_min") or 30),
                        attendee_email=attendee,
                        description=f"Auto-created from an interested reply by {to_email}.",
                    )
                except gcal.CalendarError as e:
                    print(f"⚠️  Calendar booking failed ({e}); sending the reply without a booked event.")

            # 2) When we actually booked, weave a confirmation line into the reply.
            body = data["drafted_body"]
            if booked:
                tz = cfg.get("timezone", "UTC")
                body += f"\n\nI've put a hold on the calendar for {meeting['start_local']} ({tz})."
                if booked.get("html_link"):
                    body += f"\nCalendar invite: {booked['html_link']}"

            # 3) Send the threaded reply (In-Reply-To / References keep it in-thread).
            result = pool.send(
                conn, to_email, data["drafted_subject"], body,
                footer_html=footer_html, footer_text=footer_text, throttle=False,
                in_reply_to=data.get("in_reply_to"), references=data.get("references"),
                list_unsubscribe=unsub,
            )
            if booked:
                state.record_booking(conn, client, to_email,
                                     booked.get("event_id"), booked.get("start_iso"))
                state.set_status(conn, client, to_email, "meeting_booked")
            review.set_pending_status(lead_id, "approved_and_sent")

            dest = "your controlled inbox" if result["redirected"] else to_email
            if meeting.get("start_local"):
                note = (f" A Google Calendar event was created for <b>{meeting['start_local']}</b>."
                        if booked else
                        " ⚠️ Calendar booking did not complete — the reply was still sent; book "
                        "the meeting manually (or run <code>composio link googlecalendar</code>).")
            else:
                note = ""
            print(f"✅ Reply sent via {result['mailbox']} → {dest} "
                  f"({'booked' if booked else 'no booking'}).")
            return page("Reply Approved &amp; Sent!",
                        f"Threaded reply delivered to <b>{dest}</b> (intended for "
                        f"{result['intended_for']}), via {result['mailbox']}.{note}"), 200

        # kind == "cold" (default) — unchanged Tier 1 behavior.
        result = pool.send(
            conn, to_email, data["drafted_subject"], data["drafted_body"],
            footer_html=footer_html, footer_text=footer_text,
            throttle=False,  # a human clicking already paces sends; throttle is for batch/cron
            list_unsubscribe=unsub,
        )
    except sender.SendCapExceeded as e:
        return page("Daily Limit Reached",
                    f"Not sent: {e}. This protects the sending domain's reputation. "
                    f"Try again tomorrow or raise the cap in the client config.", "⛔"), 429
    except sender.SendError as e:
        return page("Failed to Send", f"SMTP error: {e}. Check the terminal.", "❌"), 500
    finally:
        conn.close()

    review.set_pending_status(lead_id, "approved_and_sent")
    dest = "your controlled inbox" if result["redirected"] else to_email
    print(f"✅ Sent via {result['mailbox']} → {dest} (intended for {result['intended_for']}).")
    return page("Pitch Approved &amp; Sent!",
                f"Delivered to <b>{dest}</b> (intended for {result['intended_for']}), "
                f"sent via {result['mailbox']}. A CAN-SPAM footer with an unsubscribe link was included."), 200


@app.route('/decline/<lead_id>')
def decline(lead_id):
    leads = review.load_pending()
    if lead_id in leads and leads[lead_id].get("status") == "pending":
        review.set_pending_status(lead_id, "declined")
        print(f"\n[!] WEB DECLINE for {lead_id}. Draft discarded.")
    return page("Draft Declined", "The draft has been discarded and will not be sent.", "❌"), 200


@app.route('/unsubscribe/<token>', methods=['GET', 'POST'])
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


@app.route('/magnet/<client>/<token>')
def magnet_page(client, token):
    """Serve a prospect's real, personalized lead-magnet page.

    Two path segments (/magnet/<client>/<token>) so it never collides with the
    single-segment demo pages below (/magnet/blueprint etc.). The content was
    generated + stored when the cold email was drafted (see core/magnet.py).
    """
    try:
        cfg = get_cfg(client)
    except Exception:
        return page("Link Not Found", "This resource link is not valid.", "⚠️"), 404
    conn = open_conn(cfg)
    try:
        content = state.get_magnet(conn, client, token)
    finally:
        conn.close()
    if not content:
        return page("Link Not Found",
                    "This resource has expired or the link is invalid.", "⚠️"), 404
    return magnet.render_html(cfg, content), 200


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
