"""Missed-run detection — the alarm for the failure that is otherwise invisible.

    CLIENT=ejentic venv/bin/python scripts/heartbeat.py
    CLIENT=ejentic venv/bin/python scripts/heartbeat.py --max-age-hours 26
    CLIENT=ejentic venv/bin/python scripts/heartbeat.py --check      # report, never email

Exit codes:  0 = a recent run exists   1 = stale or never ran   2 = misconfigured

THE GAP THIS CLOSES
Every other signal in this system is emitted BY a run: the event ledger, faults,
drafts, the digest. So if the run never happens — the laptop slept, launchd was
unloaded, Composio's auth expired before the first step, the venv broke — nothing is
written anywhere, and an absence of alerts is indistinguishable from a healthy quiet
morning. The observer cannot help: it reacts to rows, and there are none.

This reads the `runs` table (written by scripts/daily_run.py) and alerts when the most
recent successful run is older than max_age_hours.

WHERE TO RUN IT — this matters more than the code
Run it on the ALWAYS-ON BOX, by cron, against the synced database. A missed-run
detector living on the same machine that missed the run cannot fire: if the laptop is
asleep, so is its watchdog. The box is always up, and sync_to_box.sh only runs at the
end of a successful daily run — so a stale DB on the box IS the signal that the
laptop never ran. Running it on the laptop still catches "ran but failed", which is
worth having, but it cannot catch "never ran at all".
"""

import warnings
warnings.filterwarnings('ignore')

import os
import sys
import argparse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import config, state


def _parse_iso(value):
    """Parse a stored timestamp, tolerating both naive and tz-aware forms."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def check(conn, client, max_age_hours):
    """Return (ok, status, detail, age_hours).

    `status` is one of: fresh | stale | failed_last | never_ran.
    """
    last_ok = state.last_run(conn, client, status="ok")
    last_any = state.last_run(conn, client)

    if last_any is None:
        return False, "never_ran", ("No run has ever been recorded for this client. "
                                    "Either the schedule has never fired, or it has "
                                    "never got far enough to record a run."), None

    when = _parse_iso(last_ok["finished_at"]) if last_ok else None
    age = ((datetime.now(timezone.utc) - when).total_seconds() / 3600.0) if when else None

    if when is None:
        return False, "failed_last", ("Runs have been attempted but none has ever "
                                      "SUCCEEDED. Check the most recent run's failures."), None

    if age > max_age_hours:
        detail = (f"The last successful run finished {age:.1f}h ago, which is past the "
                  f"{max_age_hours}h threshold. The scheduled job is not running, or is "
                  f"failing before it can record a run.")
        return False, "stale", detail, age

    # Fresh, but flag if the MOST RECENT attempt failed even though an older one passed.
    if last_any["status"] != "ok":
        detail = (f"The last successful run was {age:.1f}h ago, but the most recent "
                  f"attempt FAILED: {last_any['detail'] or '(no detail)'}")
        return False, "failed_last", detail, age

    return True, "fresh", f"Last successful run was {age:.1f}h ago.", age


def alert(cfg, status, detail, recent):
    """Email the operator that the pipeline has gone quiet. Returns True if sent."""
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    if not smtp_user or not smtp_pass:
        print("   ⚠️  SMTP_USER/SMTP_PASS not set — cannot send the heartbeat alert.")
        return False

    import html
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from core import sender

    titles = {
        "stale": "has stopped running",
        "never_ran": "has never run",
        "failed_last": "is failing",
    }
    subject = f"[ALERT] {cfg['client_name']} lead-gen {titles.get(status, 'needs attention')}"

    rows = "".join(
        f'<tr><td style="padding:4px 10px;border-bottom:1px solid #eee;">'
        f'{html.escape(str(r["finished_at"]))}</td>'
        f'<td style="padding:4px 10px;border-bottom:1px solid #eee;">{html.escape(str(r["status"]))}</td>'
        f'<td style="padding:4px 10px;border-bottom:1px solid #eee;">'
        f'{r["sourced"]} sourced, {r["drafted"]} drafted</td></tr>'
        for r in recent
    ) or '<tr><td colspan="3" style="padding:4px 10px;">no runs recorded</td></tr>'

    html_body = (
        f'<html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">'
        f'<h2>⚠️ {html.escape(cfg["client_name"])} — lead-gen heartbeat</h2>'
        f'<div style="background:#ffebee;padding:15px;border-left:4px solid #e53935;">'
        f'{html.escape(detail)}</div>'
        f'<p><strong>Nothing has been sent to any prospect</strong> — this alert is about the '
        f'pipeline not running, not about outbound mail.</p>'
        f'<h3>Recent runs</h3>'
        f'<table style="border-collapse:collapse;font-size:14px;">{rows}</table>'
        f'<h3>What to check</h3><ul>'
        f'<li>Is the schedule loaded? <code>launchctl list | grep leadgen</code></li>'
        f'<li>Did it error? <code>tail -50 logs/$(date +%%Y-%%m-%%d).log</code></li>'
        f'<li>Run it by hand: <code>CLIENT={html.escape(cfg["client"])} '
        f'venv/bin/python scripts/daily_run.py</code></li></ul>'
        f'</body></html>'
    )
    text = (f"{cfg['client_name']} lead-gen heartbeat\n\n{detail}\n\n"
            f"Nothing has been sent to any prospect.\n\n"
            f"Check: launchctl list | grep leadgen; tail -50 logs/<today>.log\n")
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['client_name']} System <{smtp_user}>"
        msg["To"] = smtp_user
        msg["Subject"] = subject
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        sender.smtp_deliver("smtp.gmail.com", 587, smtp_user, smtp_pass,
                            smtp_user, smtp_user, msg.as_string())
        print(f"   📣 Alert sent to {smtp_user}")
        return True
    except Exception as e:
        print(f"   ❌ Could not send the alert: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="Alert if the daily run has stopped happening.")
    ap.add_argument("--max-age-hours", type=float,
                    help="override heartbeat.max_age_hours from the client config")
    ap.add_argument("--check", action="store_true",
                    help="report only; never send an alert email")
    args = ap.parse_args()

    cfg = config.load_client()
    hb = cfg.get("heartbeat") or {}
    if not hb.get("enabled", True):
        print("Heartbeat is disabled for this client (heartbeat.enabled=false).")
        return 0

    max_age = args.max_age_hours or float(hb.get("max_age_hours", 26))
    conn = state.connect(cfg["paths"]["db"])
    try:
        ok, status, detail, age = check(conn, cfg["client"], max_age)
        recent = state.recent_runs(conn, cfg["client"], limit=7)
    finally:
        conn.close()

    print(f"💓 {cfg['client_name']} heartbeat: {status}")
    print(f"   {detail}")

    if ok:
        return 0
    if args.check or not hb.get("alert_email", True):
        print("   (alert suppressed)")
        return 1
    alert(cfg, status, detail, recent)
    return 1


if __name__ == "__main__":
    sys.exit(main())
