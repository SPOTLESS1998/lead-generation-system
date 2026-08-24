import warnings
warnings.filterwarnings('ignore')

import os
import sys
import time
import html
import json
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Make the project root importable so `core` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state, reminders, sender
from core.ai import generate

# Safety valve: most reminders that can fire in one pass. Far above any realistic
# number of meetings starting within the same hour — a runaway backstop.
MAX_PER_PASS = 50


def print_step(step):
    print(f"\n[{time.strftime('%H:%M:%S')}] {step}")


def _now(tz):
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:
        return datetime.now(ZoneInfo("UTC"))


def _friendly_when(dt, tz):
    """Aware datetime → 'Monday, 24 Aug 2026 at 10:00 (Africa/Lagos)'."""
    if dt is None:
        return "(time TBD)"
    return dt.strftime("%A, %d %b %Y at %H:%M") + f" ({tz})"


def _display_name(row):
    name = f'{row["first_name"] or ""} {row["last_name"] or ""}'.strip()
    return name or row["lead_email"]


def _service(cfg, row):
    return (row["service"]
            or cfg.get("booking", {}).get("default_service")
            or "intro consultation")


# --------------------------------------------------------------------------
# Building the reminder + delivering it (email + desktop, each independent).
# --------------------------------------------------------------------------

def _context(cfg, row, start, now, fire):
    """Everything the message + channels need for one reminder."""
    return {
        "name": _display_name(row),
        "company": row["company_name"] or "",
        "title": row["title"] or "",
        "service": _service(cfg, row),
        "email": row["lead_email"],
        "event_id": row["event_id"] or "",
        "minutes": fire,                                  # the interval that fired (60/30/15)
        "mins_left": reminders.minutes_until(start, now),  # real remaining minutes
        "when": _friendly_when(start, cfg.get("timezone", "UTC")),
        "booking_id": row["id"],
    }


def _compose_line(cfg, ctx):
    """A short, human reminder sentence — AI-composed, with a template fallback.

    This is the 'AI agent' character: it writes the ping in natural language via
    the free gateway (FreeLLMAPI → Gemini → NVIDIA, never Anthropic). If every
    provider is down we fall back to a fixed template — the reminder still goes.
    """
    who = ctx["name"] + (f" at {ctx['company']}" if ctx["company"] else "")
    prompt = f"""You are the appointment-reminder assistant for '{cfg['client_name']}'.
Write ONE short, friendly reminder sentence (max 20 words) to the operator — the
person who will be on the call — about an upcoming meeting. No greeting, no
sign-off, no emoji: just the one sentence.

Details:
- Prospect: {who}
- Service being discussed: {ctx['service']}
- Starts in about {ctx['minutes']} minutes ({ctx['when']})
"""
    try:
        text, _ = generate(cfg, prompt)
        line = (text or "").strip().split("\n")[0].strip().strip('"').strip()
        if line:
            return line
    except Exception as e:
        print(f"      ⚠️  reminder AI compose failed ({e}); using template.")
    who2 = ctx["name"] + (f" ({ctx['company']})" if ctx["company"] else "")
    return (f"Heads-up — your {ctx['service']} call with {who2} "
            f"starts in {ctx['minutes']} minutes.")


def _send_email(cfg, ctx, line):
    """Email the operator's own inbox — same pattern as review.notify_operator
    (smtp_user → smtp_user), so it bypasses the capped prospect sending pool."""
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    dashboard = (cfg.get("unsubscribe_base_url") or "http://localhost:5001").rstrip("/")

    name = html.escape(ctx["name"])
    company = html.escape(ctx["company"])
    title = html.escape(ctx["title"])
    service = html.escape(ctx["service"])
    email = html.escape(ctx["email"])
    event_id = html.escape(ctx["event_id"])
    when = html.escape(ctx["when"])
    line_html = html.escape(line)
    who = name + (f" · {title}" if title else "")

    subject = f"⏰ {ctx['minutes']} min: {ctx['name']} — {ctx['service']}"
    body_html = (
        '<html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">'
        f'<h2 style="color:#c0392b;margin-bottom:4px;">⏰ Appointment in {ctx["minutes"]} minutes</h2>'
        f'<p style="font-size:16px;margin-top:0;">{line_html}</p>'
        '<div style="background:#f9f9f9;padding:15px;border-left:4px solid #0056b3;margin:18px 0;">'
        f'<strong>Client:</strong> {who}<br>'
        + (f'<strong>Company:</strong> {company}<br>' if company else '')
        + f'<strong>Service:</strong> {service}<br>'
        f'<strong>When:</strong> {when}<br>'
        f'<strong>Contact:</strong> {email}<br>'
        + (f'<strong>Calendar event:</strong> {event_id}<br>' if event_id else '')
        + '</div>'
        f'<p style="font-size:13px;"><a href="{dashboard}/appointments" '
        'style="color:#0056b3;">View all appointments →</a></p>'
        '</body></html>'
    )
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{cfg['client_name']} Reminders <{smtp_user}>"
    msg["To"] = smtp_user
    msg["Subject"] = subject
    msg.attach(MIMEText(line, "plain"))
    msg.attach(MIMEText(body_html, "html"))
    sender.smtp_deliver("smtp.gmail.com", 587, smtp_user, smtp_pass,
                        smtp_user, smtp_user, msg.as_string())


def _desktop_notify(title, message):
    """Best-effort macOS banner. Never raises: on a non-Mac host, or if osascript
    fails, we just return False and the email still carried the reminder."""
    try:
        # json.dumps → a safely-quoted AppleScript string literal (escapes " and \).
        # ensure_ascii=False is REQUIRED: the title/message carry non-ASCII (⏰, —,
        # accented prospect names), and AppleScript cannot parse \uXXXX escapes — the
        # default ascii-escaping makes osascript fail with a syntax error and no banner.
        script = (f'display notification {json.dumps(message, ensure_ascii=False)} '
                  f'with title {json.dumps(title, ensure_ascii=False)} sound name "Glass"')
        result = subprocess.run(["osascript", "-e", script], check=False, timeout=5,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False


def _deliver(cfg, ctx):
    """Fire every enabled channel. Returns True if at least one succeeded (so the
    reminder is recorded and won't repeat); False means retry on the next pass."""
    rcfg = cfg.get("reminders", {})
    email_on = rcfg.get("email", True)
    desktop_on = rcfg.get("desktop", True)

    line = _compose_line(cfg, ctx)
    print(f"   ⏰ {ctx['minutes']} min → {ctx['name']} ({ctx['service']}) at {ctx['when']}")
    print(f"      {line}")

    if not email_on and not desktop_on:
        print("      ⚠️  both channels are disabled in config — recording without notifying.")
        return True  # nothing to do; record so we don't spin on it every pass

    email_ok = desktop_ok = False
    if email_on:
        try:
            _send_email(cfg, ctx, line)
            email_ok = True
            print("      ✉️  emailed your inbox.")
        except Exception as e:
            print(f"      ❌ email failed: {e}")
    if desktop_on:
        desktop_ok = _desktop_notify(f"⏰ {ctx['minutes']} min — {ctx['name']}", line)
        print("      🖥️  desktop banner shown." if desktop_ok
              else "      🖥️  desktop banner unavailable (non-Mac or blocked).")
    return email_ok or desktop_ok


# --------------------------------------------------------------------------
# Per-appointment decision + the pass over the whole list.
# --------------------------------------------------------------------------

def process_appointment(cfg, conn, row, now, tz):
    """Fire at most one reminder for this booking. Returns the interval fired, or None.

    Fires the most-urgent unsent active interval once. On success, marks EVERY
    currently-active interval as sent — so an agent that was offline across
    thresholds sends a single ping (not a burst), and never re-fires a larger,
    now-stale interval later. On failure, records nothing (retried next pass).
    """
    start = reminders.parse_start(row["starts_at"], tz)
    if start is None or now >= start:
        return None  # unparseable, or already started/passed
    lead_times = tuple(cfg.get("reminders", {}).get("lead_times_min", [60, 30, 15]))
    active = reminders.due_intervals(start, now, lead_times)
    if not active:
        return None
    client = cfg["client"]
    unsent = [m for m in active if not state.reminder_sent(conn, client, row["id"], m)]
    if not unsent:
        return None
    fire = min(unsent)  # most urgent
    ctx = _context(cfg, row, start, now, fire)
    if _deliver(cfg, ctx):
        for m in active:  # supersede larger active intervals (INSERT OR IGNORE)
            state.record_reminder(conn, client, row["id"], m)
        return fire
    return None


def print_upcoming(cfg, conn, now, tz):
    """Show the curated list each pass — the visible 'oversee the appointments'
    behavior: soonest first, with a countdown and which reminders have fired."""
    lead_times = cfg.get("reminders", {}).get("lead_times_min", [60, 30, 15])
    upcoming = []
    for row in state.list_appointments(conn, cfg["client"]):
        start = reminders.parse_start(row["starts_at"], tz)
        if start and start > now:
            upcoming.append((start, row))
    upcoming.sort(key=lambda x: x[0])
    if not upcoming:
        print("   (no upcoming appointments)")
        return
    print(f"   {len(upcoming)} upcoming appointment(s):")
    for start, row in upcoming:
        mins = reminders.minutes_until(start, now)
        left = f"in {mins // 60}h{mins % 60:02d}m" if mins >= 120 else f"in {mins}m"
        sent = sorted((m for m in lead_times
                       if state.reminder_sent(conn, cfg["client"], row["id"], m)), reverse=True)
        sent_str = ("sent " + "/".join(str(m) for m in sent)) if sent else "none sent yet"
        print(f"     • {_friendly_when(start, tz)} — {_display_name(row)} · "
              f"{_service(cfg, row)} [{left}] (reminders: {sent_str})")


def run_once(cfg, conn):
    tz = cfg.get("timezone", "UTC")
    now = _now(tz)
    print_step("📋 Reviewing the appointments list...")
    print_upcoming(cfg, conn, now, tz)
    fired = 0
    for row in state.list_appointments(conn, cfg["client"]):
        if fired >= MAX_PER_PASS:
            print_step(f"⏹️  Reached MAX_PER_PASS ({MAX_PER_PASS}); stopping this pass.")
            break
        try:
            if process_appointment(cfg, conn, row, now, tz):
                fired += 1
        except Exception as e:
            print(f"   ⚠️  error on booking {row['id']}: {e}")
    return fired


def main():
    once = "--once" in sys.argv
    cfg = config.load_client()
    rcfg = cfg.get("reminders", {})
    poll = int(rcfg.get("poll_seconds", 60))
    channels = "+".join(c for c, on in (("email", rcfg.get("email", True)),
                                        ("desktop", rcfg.get("desktop", True))) if on) or "none"
    print("=========================================================")
    print(f"⏰ {cfg['client_name']} - Appointment Reminder Agent")
    print(f"   client={cfg['client']}  tz={cfg.get('timezone','UTC')}  "
          f"lead_times={rcfg.get('lead_times_min', [60, 30, 15])}  channels={channels}")
    print(f"   mode={'single pass (--once)' if once else f'polling every {poll}s (Ctrl-C to stop)'}")
    print("=========================================================")

    if not rcfg.get("enabled", True):
        print("Reminders are disabled for this client (reminders.enabled=false). Nothing to do.")
        return

    conn = state.connect(cfg["paths"]["db"])
    try:
        if once:
            run_once(cfg, conn)
        else:
            while True:
                run_once(cfg, conn)
                time.sleep(poll)
    except KeyboardInterrupt:
        print("\n👋 Reminder agent stopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
