"""The pending-draft queue + operator notification — shared by both agents.

A draft (a cold pitch, or a reply to a prospect) is written here as JSON, and the
operator gets an email with 1-click APPROVE / DECLINE links that the Flask server
handles. This one module replaces the copies that used to live in lead_agent.py and
approval_server.py, and it renders both kinds of draft:

    kind="cold"   — a first-touch pitch (Tier 1)
    kind="reply"  — a threaded reply to a prospect's response (Tier 2), optionally
                    carrying a proposed meeting to book on approval.

The queue file (pending_leads.json) is a transient hand-off; it is git-ignored.
"""

import os
import html
import json
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from . import config, spam, sender, state

PENDING_FILE = os.path.join(config.ROOT, "pending_leads.json")


# --- queue ------------------------------------------------------------------

def load_pending():
    if not os.path.exists(PENDING_FILE):
        return {}
    with open(PENDING_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _write(leads):
    with open(PENDING_FILE, "w") as f:
        json.dump(leads, f, indent=4)


def save_pending(entry):
    """Store a draft (adds status='pending'); returns its short id.

    Every draft is run through the offline spam linter here (the single chokepoint
    both agents pass through), so the score + flagged words are stashed on the entry
    and shown to the operator in the approval email.

    Also moves the lead to `awaiting_approval` in the lead table. That lives here, at
    the one chokepoint both drafting entry points pass through, rather than at each
    call site — the same reason the spam check is here. A second call site that forgot
    it would leave the lead in `queued`, which the stall detector correctly reads as
    "the drafter never finished", reopening the escalation storm this state split
    exists to close.
    """
    entry.setdefault("kind", "cold")
    entry["status"] = "pending"
    if "spam" not in entry:
        entry["spam"] = spam.check(entry.get("drafted_subject", ""),
                                   entry.get("drafted_body", ""))
    leads = load_pending()
    lead_id = str(uuid.uuid4())[:8]
    leads[lead_id] = entry
    _write(leads)

    client = entry.get("client")
    target = entry.get("target_email")
    if client and target:
        try:
            conn = state.connect(config.client_db_path(client))
            state.set_status(conn, client, target, state.AWAITING_APPROVAL)
            conn.close()
        except Exception as e:
            # Never fail the draft over this: the queue entry is what the operator
            # acts on, and a lead left in `queued` is at worst a noisy stall alert.
            print(f"⚠️  could not mark {target} awaiting_approval: {e}")
    return lead_id


def set_pending_status(lead_id, status):
    leads = load_pending()
    if lead_id in leads:
        leads[lead_id]["status"] = status
        _write(leads)


# --- operator notification --------------------------------------------------

def _html_shell(inner):
    return (f'<html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">'
            f'{inner}</body></html>')


def _buttons(dashboard, lead_id):
    return (
        f'<p style="margin-top:20px;">'
        f'<a href="{dashboard}/approve/{lead_id}" style="background:#4CAF50;color:white;'
        f'padding:14px 25px;text-decoration:none;border-radius:4px;font-weight:bold;'
        f'margin-right:15px;">✅ APPROVE &amp; SEND</a>'
        f'<a href="{dashboard}/decline/{lead_id}" style="background:#f44336;color:white;'
        f'padding:14px 25px;text-decoration:none;border-radius:4px;font-weight:bold;">❌ DECLINE</a>'
        f'</p>'
        f'<p style="margin-top:30px;font-size:12px;color:#777;">Click these on the machine '
        f'running the approval server ({dashboard}).</p>'
    )


def _spam_html(entry):
    """Render the spam-linter readout block (empty string if no check was run)."""
    result = entry.get("spam")
    if not result:
        return ""
    palette = {
        "ok":   ("#e8f5e9", "#4CAF50", "✅ Spam check: looks clean"),
        "warn": ("#fff8e1", "#ffb300", "⚠️ Spam check: minor issues"),
        "high": ("#ffebee", "#e53935", "🚫 Spam check: high risk — consider editing"),
    }
    bg, border, title = palette.get(result.get("level", "ok"), palette["ok"])
    flags = result.get("flags") or []
    if flags:
        items = "".join(f'<li>{html.escape(f.get("detail", ""))}</li>' for f in flags)
        inner = f'<ul style="margin:8px 0 0;padding-left:20px;">{items}</ul>'
    else:
        inner = '<p style="margin:8px 0 0;">No trigger words or risky patterns found.</p>'
    return (
        f'<div style="background:{bg};padding:15px;border-left:4px solid {border};margin-bottom:20px;">'
        f'<strong>{title} (score {result.get("score", 0)})</strong>{inner}</div>'
    )


def _cold_body(cfg, lead_id, entry, dashboard):
    body_html = (entry.get("drafted_body") or "").replace(chr(10), "<br>")
    name = f'{entry.get("first_name","")} {entry.get("last_name","")}'.strip()
    return _html_shell(
        f'<h2>{cfg["client_name"]} — Approval Request</h2>'
        f'<p>Your AI system scouted a lead and drafted a personalized pitch.</p>'
        f'<div style="background:#f9f9f9;padding:15px;border-left:4px solid #0056b3;margin-bottom:20px;">'
        f'<strong>LEAD:</strong><br>'
        f'Company: {entry.get("company_name","")}<br>'
        f'Decision Maker: {name} ({entry.get("title","")})<br>'
        f'Prospect Email: {entry.get("target_email","")}</div>'
        f'<div style="background:#f1f8e9;padding:15px;border-left:4px solid #4CAF50;margin-bottom:20px;">'
        f'<strong>PROPOSED PITCH:</strong><br>'
        f'<strong>Subject:</strong> {entry.get("drafted_subject","")}<br><br>{body_html}</div>'
        f'{_spam_html(entry)}'
        f'<p>Approve to send (goes to your controlled inbox in demo mode):</p>'
        f'{_buttons(dashboard, lead_id)}'
    )


def _reply_body(cfg, lead_id, entry, dashboard):
    reply_html = (entry.get("drafted_body") or "").replace(chr(10), "<br>")
    incoming = (entry.get("incoming_snippet") or "").replace(chr(10), "<br>")
    meeting = entry.get("meeting")
    meeting_html = ""
    if meeting:
        meeting_html = (
            f'<div style="background:#fff3e0;padding:15px;border-left:4px solid #ff9800;margin-bottom:20px;">'
            f'<strong>📅 MEETING TO BOOK ON APPROVAL:</strong><br>'
            f'{meeting.get("title","Intro call")} — {meeting.get("start_local","(time TBD)")} '
            f'({meeting.get("duration_min",30)} min, {cfg.get("timezone","UTC")})</div>'
        )
    return _html_shell(
        f'<h2>{cfg["client_name"]} — Reply Approval</h2>'
        f'<p>A prospect replied. Intent detected: <strong>{entry.get("intent","?")}</strong>.</p>'
        f'<div style="background:#f9f9f9;padding:15px;border-left:4px solid #0056b3;margin-bottom:20px;">'
        f'<strong>FROM:</strong> {entry.get("target_email","")}<br>'
        f'<strong>THEY WROTE:</strong><br>{incoming}</div>'
        f'{meeting_html}'
        f'<div style="background:#f1f8e9;padding:15px;border-left:4px solid #4CAF50;margin-bottom:20px;">'
        f'<strong>DRAFTED REPLY:</strong><br>'
        f'<strong>Subject:</strong> {entry.get("drafted_subject","")}<br><br>{reply_html}</div>'
        f'{_spam_html(entry)}'
        f'<p>Approve to send this reply in-thread'
        f'{" and create the calendar event" if meeting else ""} '
        f'(goes to your controlled inbox in demo mode):</p>'
        f'{_buttons(dashboard, lead_id)}'
    )


def notify_operator(cfg, lead_id, entry):
    """Email the operator (their own inbox) the draft + approve/decline links.

    Cold drafts are suppressed when notify.mode is "digest" (the default): a scheduled
    run sends ONE summary via core/digest.py instead of an email per draft, because at
    ~10 drafts/day a per-draft message buries the thing you actually need to see. Reply
    approvals are always sent individually — a prospect has written back and that is
    time-sensitive enough to deserve its own message.

    The decision lives here rather than at each call site so a new caller cannot
    accidentally spam the operator by forgetting to check the mode.
    """
    kind = entry.get("kind", "cold")
    if kind == "cold":
        try:
            from . import digest
            if digest.notify_mode(cfg) == "digest":
                return None
        except Exception:
            pass   # a broken notify.mode must never block the draft from being queued

    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    dashboard = (cfg.get("unsubscribe_base_url") or "http://localhost:5001").rstrip("/")
    who = entry.get("company_name") or entry.get("target_email") or "prospect"
    subject = (f"[ACTION REQUIRED] Review reply to {who}" if kind == "reply"
               else f"[ACTION REQUIRED] Review pitch for {who}")
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['client_name']} System <{smtp_user}>"
        msg["To"] = smtp_user
        msg["Subject"] = subject
        html_content = (_reply_body(cfg, lead_id, entry, dashboard) if kind == "reply"
                        else _cold_body(cfg, lead_id, entry, dashboard))
        msg.attach(MIMEText("Please view this email in an HTML compatible client.", "plain"))
        msg.attach(MIMEText(html_content, "html"))

        sender.smtp_deliver("smtp.gmail.com", 587, smtp_user, smtp_pass,
                            smtp_user, smtp_user, msg.as_string())
        print(f"✅ Approval request sent to your inbox ({who}).")
        return True
    except Exception as e:
        print(f"❌ Error sending approval request: {e}")
        return False
