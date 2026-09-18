import warnings
warnings.filterwarnings('ignore')

import os
import sys
import html
import logging
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask

# Make the project root importable so `core` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state, sender, suppression, compliance, review, magnet, scheduling, reminders, budget, theme
from core import observability as obs
from core import calendar as gcal   # core/calendar.py (the booking seam), not stdlib calendar

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Cache resolved client configs (creds pulled from env once per client).
_CFG_CACHE = {}


def get_cfg(client_name):
    if client_name not in _CFG_CACHE:
        _CFG_CACHE[client_name] = config.load_client(client_name)
    return _CFG_CACHE[client_name]


def _brand():
    """The ACTIVE tenant's display name, for pages that aren't tied to one lead.

    Never hardcode a brand into a served page — this server runs for whichever
    client is deployed (MULTITENANCY.md). Degrades to a neutral label rather than
    borrowing our own name if the tenant's config can't be read.
    """
    try:
        return (get_cfg(config.active_client()).get("client_name") or "").strip() or "our team"
    except Exception:
        return "our team"


def open_conn(cfg):
    # Fresh connection per request → thread-safe under Flask's threaded dev server.
    return state.connect(cfg["paths"]["db"])


def page(title, body, emoji="✅"):
    """A one-line outcome page (approved / declined / unsubscribed).

    Styling comes from core/theme.shell — see that module for why the palette is
    hardcoded rather than read from the tenant's config.
    """
    inner = (
        '<div class="wrap center">'
        '<div class="panel">'
        f'<h1>{emoji} {title}</h1>'
        f'<p class="muted" style="margin:10px 0 0;">{body}</p>'
        '<p class="meta" style="margin:18px 0 0;">You may now close this tab.</p>'
        '</div></div>'
    )
    return theme.shell(title, inner)


def _friendly_when(when_str, tz):
    """'2026-08-25 10:00' (or ISO) → 'Monday, 25 Aug 2026 at 10:00 (Africa/Lagos)'.

    Falls back to the raw string if it doesn't parse — never blocks a page render.
    """
    if not when_str:
        return "your scheduled time"
    s = str(when_str).strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
        return dt.strftime("%A, %d %b %Y at %H:%M") + f" ({tz})"
    except Exception:
        return f"{when_str} ({tz})"


# --- routes ----------------------------------------------------------------
# The pending-draft queue lives in core.review (shared by both agents).

# Spam-badge palette — mirrors the readout in core/review.py.
_SPAM_BADGE = {
    # (badge class, label) — colours come from the shared tokens in core/theme.py
    # so the three states stay legible in both light and dark.
    "ok":   ("badge-success", "Spam: clean"),
    "warn": ("badge-warning", "Spam: minor"),
    "high": ("badge-danger",  "Spam: high risk"),
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
    badge_cls, badge_label = _SPAM_BADGE.get(spam.get("level", "ok"), _SPAM_BADGE["ok"])
    badge = (f'<span class="badge {badge_cls}">{badge_label} '
             f'({spam.get("score", 0)})</span>')

    kind_label = "↩ Reply" if kind == "reply" else "✉ Cold"
    kind_badge = f'<span class="badge badge-accent">{kind_label}</span>'

    magnet = ""
    if entry.get("magnet_url"):
        magnet = (f'<p style="margin:0 0 16px;"><a href="{html.escape(entry["magnet_url"])}" '
                  f'target="_blank" rel="noopener">🎁 View their personalized page →</a></p>')

    who_html = f'<p class="muted" style="margin:2px 0 0;">{who_line}</p>' if who_line else ""

    return f"""
    <div class="panel">
      <div class="head">
        <div>
          <h2>{company}</h2>
          {who_html}
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">{kind_badge}{badge}</div>
      </div>
      <div class="draft">
        <span class="subject">{subject}</span>
        {body}
      </div>
      {magnet}
      <div class="actions">
        <a class="btn btn-primary" href="/approve/{lead_id}">Approve &amp; Send</a>
        <a class="btn btn-danger" href="/decline/{lead_id}">Decline</a>
      </div>
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
        cards = ('<div class="panel empty">No drafts waiting. Run <code>lead_agent.py</code> '
                 'or <code>reply_agent.py</code> to queue some.</div>')
        count_line = "Nothing pending right now"

    inner = f"""
      <div class="wrap">
        <h1>{client_label}</h1>
        <p class="muted" style="margin:6px 0 4px;">Approval queue · {count_line}</p>
        <p class="meta" style="margin:0 0 28px;">
          <a href="/appointments">📅 Booked appointments</a> &nbsp;·&nbsp;
          <a href="/health">🩺 Pipeline health</a>
        </p>
        {cards}
      </div>"""
    return theme.shell(f"{client_label} — Approvals", inner), 200


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
    run_id = obs.new_run_id()   # one ledger run per approval action (send is metered below)

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
            with obs.track(conn, cfg, "send", subject=to_email, run_id=run_id):
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

        # kind == "cold" (default) — Tier 1 send + one-click prospect buttons.
        # The buttons reuse the same signed token as the unsubscribe link; a click
        # on "interested"/"not-interested" hits the routes below.
        cta_html = compliance.cta_buttons(cfg, token, "html")
        cta_text = compliance.cta_buttons(cfg, token, "text")
        with obs.track(conn, cfg, "send", subject=to_email, run_id=run_id):
            result = pool.send(
                conn, to_email, data["drafted_subject"], data["drafted_body"],
                footer_html=footer_html, footer_text=footer_text,
                throttle=False,  # a human clicking already paces sends; throttle is for batch/cron
                list_unsubscribe=unsub,
                cta_html=cta_html, cta_text=cta_text,
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


@app.route('/interested/<token>')
def interested(token):
    """One-click 'Yes, I'm interested' from a cold email → book a meeting, no typing.

    Books the next sensible business-day slot (10:00 local), creates a real Google
    Calendar event, and confirms on-screen. Idempotent: a second click shows the
    existing booking instead of double-booking. If the calendar step fails, we
    still record their interest and tell them we'll follow up (graceful degrade).
    """
    parsed = compliance.verify_token(token, config.unsub_secret())
    if not parsed:
        return page("Invalid Link",
                    "This link is invalid or has been tampered with.", "⚠️"), 400
    client, email = parsed
    try:
        cfg = get_cfg(client)
    except Exception:
        return page("Link Not Found", "This link is not valid.", "⚠️"), 404

    conn = open_conn(cfg)
    tz = cfg.get("timezone", "UTC")
    company = html.escape(cfg.get("client_name", "us"))
    try:
        # Idempotent: already booked? Show the existing time, don't book again.
        existing = state.latest_booking(conn, client, email)
        if existing:
            when = _friendly_when(existing["starts_at"], tz)
            return page("You're already booked 📅",
                        f"We've got you down for <b>{when}</b>. See you then! "
                        f"Need a different time? Just reply to our email.", "📅"), 200

        # One click = book the next sensible business-day slot (10:00 local).
        start_local = scheduling.safe_future_slot(None, tz, hour=10)
        # Attendee follows the sending mode: never invite a real stranger during a
        # controlled demo — point the invite at the safe inbox instead.
        attendee = (cfg["sending"]["controlled_inbox"]
                    if cfg["sending"]["mode"] == "controlled" else email)
        try:
            booked = gcal.create_event(
                cfg,
                summary=f"Intro call — {cfg['client_name']}",
                start_local=start_local,
                duration_min=int(cfg.get("booking", {}).get("default_duration_min", 30)),
                attendee_email=attendee,
                description=f"Booked via the one-click 'Interested' button by {email}.",
            )
        except gcal.CalendarError as e:
            # Graceful degrade: keep their interest, promise a follow-up.
            print(f"⚠️  One-click booking failed for {email} ({e}); recorded interest instead.")
            state.set_status(conn, client, email, "interested")
            return page("Thanks — you're on the list! 🎉",
                        f"Great to hear you're interested in {company}. We'll email you "
                        f"shortly to lock in a time. Talk soon!", "🎉"), 200

        state.record_booking(conn, client, email,
                             booked.get("event_id"), booked.get("start_iso"))
        when = _friendly_when(booked.get("start_iso") or start_local, tz)
        link = booked.get("html_link")
        extra = (f'<br><br><a href="{html.escape(link)}" '
                 f'style="color:var(--success);font-weight:600;">Add to your calendar →</a>'
                 if link else "")
        print(f"✅ One-click booking for {email} at {start_local} ({tz}).")
        return page("You're booked! 🎉",
                    f"Your intro call with {company} is set for <b>{when}</b>.{extra}"
                    f"<br><br>Need a different time? Just reply to our email and "
                    f"we'll reschedule.", "📅"), 200
    finally:
        conn.close()


@app.route('/not-interested/<token>')
def not_interested(token):
    """One-click 'Not interested' from a cold email → opt them out.

    A stray click after a meeting is already booked does NOT cancel it — we show
    the booking and tell them to reply if they really want to cancel. Suppression
    only stops future OUTBOUND cold sends; it never blocks the inbound reply path,
    so someone who mis-clicks here can still email back and get booked.
    """
    parsed = compliance.verify_token(token, config.unsub_secret())
    if not parsed:
        return page("Invalid Link",
                    "This link is invalid or has been tampered with.", "⚠️"), 400
    client, email = parsed
    try:
        cfg = get_cfg(client)
    except Exception:
        return page("Link Not Found", "This link is not valid.", "⚠️"), 404

    conn = open_conn(cfg)
    tz = cfg.get("timezone", "UTC")
    try:
        # Don't let a mis-click cancel a real meeting.
        existing = state.latest_booking(conn, client, email)
        if existing:
            when = _friendly_when(existing["starts_at"], tz)
            return page("You have a meeting booked 📅",
                        f"You're currently booked for <b>{when}</b>. To cancel or "
                        f"reschedule, just reply to our email and we'll sort it out.", "📅"), 200
        suppression.add(conn, client, email, reason="not_interested")
    finally:
        conn.close()
    print(f"\n[!] NOT-INTERESTED click: {email} (client={client}) suppressed.")
    return page("No problem — thanks for letting us know",
                "We won't email you again. Changed your mind? Just reply to our last "
                "email and we'll pick things up.", "👍"), 200


def _reminder_pills(lead_times, sent):
    """Small pills showing which pre-meeting reminders have fired (green ✓) vs not (grey ○)."""
    out = []
    for m in lead_times:
        done = m in sent
        bg, fg, mark = ("var(--accent-quiet)", "var(--success)", "✓") if done else ("transparent", "var(--ink-faint)", "○")
        out.append(f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;'
                   f'font-size:12px;margin-right:4px;white-space:nowrap;">{m}m {mark}</span>')
    return "".join(out) or '<span class="meta">—</span>'


def _appt_row(item, tz, lead_times):
    """One appointment as a table row (every field escaped)."""
    start, r, sent = item
    name = html.escape(f'{r["first_name"] or ""} {r["last_name"] or ""}'.strip() or r["lead_email"])
    company = html.escape(r["company_name"] or "")
    service = html.escape(r["service"] or "—")
    email = html.escape(r["lead_email"])
    when = html.escape(_friendly_when(r["starts_at"], tz))
    who = name + (f'<br><span class="meta">{company}</span>' if company else "")
    return (
        '<tr>'
        f'<td style="padding:12px 10px;">{who}</td>'
        f'<td style="padding:12px 10px;">{service}</td>'
        f'<td style="padding:12px 10px;white-space:nowrap;">{when}</td>'
        f'<td><a href="mailto:{email}">{email}</a></td>'
        f'<td style="padding:12px 10px;">{_reminder_pills(lead_times, sent)}</td>'
        '</tr>'
    )


def _appt_table(title, items, empty_msg, tz, lead_times):
    if not items:
        return (f'<h2 style="margin-top:34px;">{title}</h2>'
                f'<p class="muted">{empty_msg}</p>')
    rows = "".join(_appt_row(it, tz, lead_times) for it in items)
    return (
        f'<h2 style="margin-top:34px;">{title}</h2>'
        '<div class="panel" style="overflow-x:auto;padding:6px 14px;"><table>'
        'border-radius:10px;box-shadow:0 2px 6px rgba(0,0,0,0.06);overflow:hidden;">'
        '<tr>'
        '<th style="padding:10px;">Client</th><th style="padding:10px;">Service</th>'
        '<th style="padding:10px;">When</th><th style="padding:10px;">Contact</th>'
        '<th style="padding:10px;">Reminders</th></tr>'
        f'{rows}</table></div>'
    )


@app.route('/appointments')
def appointments():
    """The curated booked-appointments list for the active client.

    Every booking joined to its lead — name, the service we're rendering them
    (leads.niche), the time, and their contact — split into Upcoming vs Past,
    each row showing which of the 60/30/15-min reminders have already fired.
    """
    client = config.active_client()
    try:
        cfg = get_cfg(client)
        client_label = html.escape(cfg.get("client_name", client))
    except Exception:
        return page("Config Error", "Could not load the client configuration.", "⚠️"), 500

    tz = cfg.get("timezone", "UTC")
    lead_times = cfg.get("reminders", {}).get("lead_times_min", [60, 30, 15])
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.now(ZoneInfo("UTC"))

    conn = open_conn(cfg)
    try:
        upcoming, past = [], []
        for r in state.list_appointments(conn, client):
            start = reminders.parse_start(r["starts_at"], tz)
            sent = [m for m in lead_times if state.reminder_sent(conn, client, r["id"], m)]
            (upcoming if (start and start > now) else past).append((start, r, sent))
    finally:
        conn.close()

    upcoming.sort(key=lambda x: x[0])
    past.sort(key=lambda x: x[0].isoformat() if x[0] else "", reverse=True)

    total = len(upcoming) + len(past)
    count_line = (f"{len(upcoming)} upcoming, {len(past)} past" if total
                  else "No meetings booked yet")

    body = (
        _appt_table("📅 Upcoming", upcoming,
                    "No upcoming meetings. When a prospect books, it lands here.", tz, lead_times)
        + _appt_table("✅ Past", past, "Nothing here yet.", tz, lead_times)
    )
    inner = f"""
      <div class="wrap" style="max-width:920px;">
        <p style="margin:0 0 10px;"><a href="/">← Approval queue</a></p>
        <h1>📅 Booked Appointments</h1>
        <p class="muted" style="margin:6px 0 28px;">{count_line}. The reminder agent pings you before each one.</p>
        {body}
      </div>"""
    return theme.shell(f"{client_label} — Appointments", inner), 200


# --- pipeline health / accounting dashboard --------------------------------

# Fault-status palette — badge classes from core/theme.py, so the states stay
# legible in both light and dark (mirrors the lifecycle in core/state.py's faults).
_FAULT_BADGE = {
    "open":      "badge-danger",
    "healing":   "badge-warning",
    "escalated": "badge-danger",
    "resolved":  "badge-success",
}


def _kpi(label, value, sub="", accent="var(--ink)"):
    """One big-number card for the KPI rows."""
    sub_html = f'<div class="meta" style="margin-top:3px;">{sub}</div>' if sub else ""
    return (f'<div class="panel" style="flex:1;min-width:150px;padding:18px 20px;margin-bottom:0;">'
            f'<div class="muted" style="font-size:13px;">{label}</div>'
            f'<div style="color:{accent};font-size:26px;font-weight:600;'
            f'letter-spacing:-0.02em;margin-top:4px;">{value}</div>'
            f'{sub_html}</div>')


def _fault_row(f):
    """One open fault as a table row (every field escaped)."""
    badge_cls = _FAULT_BADGE.get(f["status"], "")
    status_badge = (f'<span class="badge {badge_cls}">{html.escape(f["status"])}</span>')
    return (
        '<tr>'
        f'<td style="white-space:nowrap;">#{f["id"]} {status_badge}</td>'
        f'<td>{html.escape(f["kind"] or "")}</td>'
        f'<td>{html.escape(f["step"] or "")}</td>'
        f'<td>{html.escape(f["subject"] or "—")}</td>'
        f'<td class="muted">{html.escape((f["detail"] or "")[:90])}</td>'
        f'<td>{html.escape(f["action"] or "—")}</td>'
        f'<td style="text-align:center;">{f["attempts"]}</td>'
        '</tr>'
    )


@app.route('/health')
def health():
    """Pipeline health + accounting for the active client.

    Rolls up the observability ledger (event/error counts, token spend, notional
    cost, unit economics) and shows the live fault list from the self-healing
    layer. Pure reads — viewing this never mutates state.
    """
    client = config.active_client()
    try:
        cfg = get_cfg(client)
        client_label = html.escape(cfg.get("client_name", client))
    except Exception:
        return page("Config Error", "Could not load the client configuration.", "⚠️"), 500

    conn = open_conn(cfg)
    try:
        m = obs.metrics(conn, cfg)
        counts = state.fault_counts(conn, client)
        faults = state.open_faults(conn, client, limit=50)
        bstat = budget.status(conn, cfg)
    finally:
        conn.close()

    t = m["totals"]
    ue = m["unit_economics"]
    err_color = ("var(--danger)" if t["error_rate"] > 0.1
                 else ("var(--warning)" if t["error_rate"] > 0 else "var(--success)"))

    kpis = (
        _kpi("Events", f'{t["events"]:,}', f'{t["ok"]} ok · {t["skipped"]} skipped')
        + _kpi("Error rate", f'{t["error_rate"]:.0%}', f'{t["error"]} error(s)', err_color)
        + _kpi("Tokens", f'{t["total_tokens"]:,}',
               f'{t["prompt_tokens"]:,} in · {t["completion_tokens"]:,} out')
        + _kpi("Notional cost", f'${t["cost_usd"]:,.4f}', "at your reference rate")
    )
    econ = (
        _kpi("Leads", f'{ue["leads"]:,}')
        + _kpi("Bookings", f'{ue["bookings"]:,}')
        + _kpi("Cost / lead", f'${ue["cost_per_lead"]:,.4f}')
        + _kpi("Cost / booking", f'${ue["cost_per_booking"]:,.4f}')
    )

    if counts:
        pills = " ".join(
            f'<span class="badge {_FAULT_BADGE.get(s, "")}" style="margin-right:6px;">'
            f'{html.escape(s)}: {c}</span>'
            for s, c in sorted(counts.items()))
    else:
        pills = '<span class="muted">No faults recorded — clean run. 🎉</span>'

    if faults:
        frows = "".join(_fault_row(f) for f in faults)
        faults_table = (
            '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;'
            ''
            '<tr>'
            '<th style="padding:10px;">Fault</th><th style="padding:10px;">Kind</th>'
            '<th style="padding:10px;">Step</th><th style="padding:10px;">Subject</th>'
            '<th style="padding:10px;">Detail</th><th style="padding:10px;">Action</th>'
            '<th style="padding:10px;">Tries</th></tr>'
            f'{frows}</table></div>')
    else:
        faults_table = '<p class="muted">No open faults — the healer has nothing to work. ✅</p>'

    steps = m["by_step"]
    if steps:
        srows = ""
        for name_, s in sorted(steps.items()):
            tok = s["prompt_tokens"] + s["completion_tokens"]
            avg = (s["duration_ms"] / s["events"]) if s["events"] else 0
            ecolor = "var(--danger)" if s["error"] else "var(--ink)"
            srows += (
                '<tr>'
                f'<td style="padding:10px;font-weight:bold;">{html.escape(name_)}</td>'
                f'<td style="padding:10px;">{s["events"]}</td>'
                f'<td style="padding:10px;">{s["ok"]}</td>'
                f'<td style="padding:10px;color:{ecolor};">{s["error"]}</td>'
                f'<td style="padding:10px;">{tok:,}</td>'
                f'<td style="padding:10px;">${s["cost_usd"]:.4f}</td>'
                f'<td style="padding:10px;">{avg:.0f} ms</td></tr>')
        steps_table = (
            '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;'
            ''
            '<tr>'
            '<th style="padding:10px;">Step</th><th style="padding:10px;">Events</th>'
            '<th style="padding:10px;">OK</th><th style="padding:10px;">Error</th>'
            '<th style="padding:10px;">Tokens</th><th style="padding:10px;">Cost</th>'
            '<th style="padding:10px;">Avg time</th></tr>'
            f'{srows}</table></div>')
    else:
        steps_table = ('<p class="muted">No steps recorded yet. Run the agents (draft / '
                       'reply / send) and they\'ll show up here.</p>')

    def _section(title):
        return f'<h2 style="margin:34px 0 12px;">{title}</h2>'

    row_style = 'display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;'

    # Premium copy (real Claude) spend + daily-cap status, straight from the ledger.
    if bstat.get("premium"):
        cap = bstat.get("cap_usd")
        allowed = bstat.get("allowed_now")
        rem = bstat.get("remaining_usd")
        prem_kpis = (
            _kpi("Premium model", html.escape(str(bstat.get("model") or "—")))
            + _kpi("Spent today", f'${bstat.get("spent_today_usd", 0) or 0:.4f}',
                   (f'of ${cap:.2f} daily cap' if cap else 'uncapped'))
            + _kpi("Remaining today", (f'${rem:.4f}' if rem is not None else '∞'),
                   ("premium active" if allowed else "cap hit — free fallback"),
                   "var(--success)" if allowed else "var(--warning)")
        )
        premium_section = (
            _section("💎 Premium copy (real Claude)")
            + f'<div style="{row_style}">{prem_kpis}</div>'
            + '<p class="meta" style="margin-top:6px;">Real-Claude copy spend is '
              'reconstructed from the ledger and capped per day; once the cap is hit, drafting '
              'auto-falls back to the free provider chain.</p>')
    else:
        premium_section = (
            _section("💎 Premium copy")
            + '<p class="muted">Off — drafting uses the free provider chain. Set '
              '<code>copy.premium</code> in the client config to route copy to real Claude.</p>')

    inner = f"""
      <div class="wrap" style="max-width:980px;">
        <p style="margin:0 0 10px;">
          <a href="/">← Approval queue</a> &nbsp;·&nbsp;
          <a href="/appointments">📅 Appointments</a></p>
        <h1>🩺 Pipeline Health</h1>
        <p class="muted" style="margin:6px 0 24px;">Every step is metered in the ledger; the observer flags
           missteps and the healer works them automatically, escalating to you only when it can't.</p>
        {_section("📊 Throughput &amp; spend")}
        <div style="{row_style}">{kpis}</div>
        {_section("💰 Unit economics")}
        <div style="{row_style}">{econ}</div>
        <p class="meta" style="margin-top:8px;">Cost is <b>notional</b> — free providers
           are $0; set a reference rate + margin in <code>observability.cost</code> to price clients.</p>
        {premium_section}
        {_section("🚑 Faults")}
        <p style="margin:0 0 14px;">{pills}</p>
        {faults_table}
        {_section("🪜 By step")}
        {steps_table}
      </div>"""
    return theme.shell(f"{client_label} — Pipeline Health", inner), 200


@app.route('/magnet/<client>/<token>')
def magnet_page(client, token):
    """Serve a prospect's real, personalized lead-magnet page.

    Two path segments (/magnet/<client>/<token>). The content was generated +
    stored when the cold email was drafted (see core/magnet.py).
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


def _resolve_port():
    """Which port to bind. Single source of truth so the baked email links and the
    server can never drift again:
      1. env PORT / APPROVAL_PORT wins (for a reverse-proxy / custom setup), else
      2. the port in the active client's unsubscribe_base_url (what the links use), else
      3. 5001 (legacy default).
    """
    for var in ("PORT", "APPROVAL_PORT"):
        v = (os.environ.get(var) or "").strip()
        if v.isdigit():
            return int(v)
    try:
        base = _CFG_CACHE.get(config.active_client()) or config.load_client(config.active_client())
        p = urlparse(base.get("unsubscribe_base_url") or "").port
        if p:
            return int(p)
    except Exception:
        pass
    return 5001


if __name__ == '__main__':
    _port = _resolve_port()
    print("=========================================================")
    print(f"🚀 {_brand()} - Web Approval Server  (tenant: {config.active_client()})")
    print(f"Listening for button clicks on http://localhost:{_port} ...")
    print("=========================================================")
    app.run(port=_port, debug=False)
