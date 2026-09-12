"""The morning digest — ONE email per run instead of one per draft.

WHY THIS EXISTS
Every draft used to email its own "[ACTION REQUIRED]" message (core/review.py's
notify_operator). That is right for a hand-run pipeline where you draft two leads and
act on them. At daily volume it is ~10 emails every morning, ~70 a week, all saying
the same thing — and the signal that actually matters (did the run work? is there
anything wrong?) is buried in the pile. So a scheduled run sends one summary and lets
the dashboard be the place you review drafts.

Config: `notify.mode` — "digest" (default) or "per_draft". notify_operator() is
unchanged and still used for one-off manual runs and for reply approvals, which are
time-sensitive and worth their own message.

Sending uses the same SMTP path as every other operator notification (the operator's
own inbox, via sender.smtp_deliver) — no new credentials, no new dependency.
"""

import os
import html
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from . import sender


def _rows(report):
    """The per-step table, as HTML rows."""
    palette = {"ok": "#4CAF50", "failed": "#e53935", "skipped": "#9e9e9e"}
    out = []
    for s in report.steps:
        colour = palette.get(s["status"], "#555")
        detail = html.escape(s.get("detail") or "")
        err = html.escape(s.get("error") or "")
        err_html = f'<div style="color:#b71c1c;font-size:12px;margin-top:4px;">{err}</div>' if err else ""
        out.append(
            f'<tr>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;color:{colour};'
            f'font-weight:bold;white-space:nowrap;">{html.escape(s["status"])}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">'
            f'<strong>{html.escape(s["name"])}</strong>'
            f'{(" — " + detail) if detail else ""}{err_html}</td>'
            f'</tr>'
        )
    return "".join(out)


def build_digest(cfg, report, dashboard=None):
    """Return (subject, html, text) for a run report. Pure — no sending."""
    dashboard = (dashboard or cfg.get("unsubscribe_base_url") or "").rstrip("/")
    counts = report.lead_counts or {}
    failed = report.failed

    if failed:
        subject = (f"[ACTION REQUIRED] {cfg['client_name']} daily run: "
                   f"{len(failed)} failure(s)")
    else:
        subject = (f"✅ {cfg['client_name']} daily run — "
                   f"{report.sourced} sourced, {report.drafted} drafted")

    headline = ("The run finished with problems." if failed
                else "The run completed cleanly.")

    list_rows = "".join(
        f'<li><strong>{html.escape(k)}</strong>: {v}</li>' for k, v in sorted(counts.items())
    ) or "<li>(the list is empty)</li>"
    fault_rows = "".join(
        f'<li><strong>{html.escape(k)}</strong>: {v}</li>' for k, v in sorted((report.faults or {}).items())
    ) or "<li>none</li>"

    html_body = (
        f'<html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">'
        f'<h2>{html.escape(cfg["client_name"])} — Daily Run</h2>'
        f'<p>{headline}</p>'
        f'<div style="background:#f1f8e9;padding:15px;border-left:4px solid #4CAF50;margin-bottom:20px;">'
        f'<strong>This run:</strong> {report.sourced} lead(s) sourced, '
        f'{report.drafted} draft(s) awaiting approval.<br>'
        f'<span style="font-size:13px;color:#555;">Nothing was sent to a prospect — drafts wait '
        f'for your approval.</span></div>'
        f'<h3 style="margin-bottom:6px;">Steps</h3>'
        f'<table style="border-collapse:collapse;width:100%;font-size:14px;">{_rows(report)}</table>'
        f'<h3 style="margin-bottom:6px;">Lead list</h3><ul>{"".join(list_rows)}</ul>'
        f'<h3 style="margin-bottom:6px;">Open faults</h3><ul>{fault_rows}</ul>'
        f'<p style="margin-top:24px;">'
        f'<a href="{html.escape(dashboard)}" style="background:#0056b3;color:white;'
        f'padding:14px 25px;text-decoration:none;border-radius:4px;font-weight:bold;">'
        f'Review drafts →</a></p>'
        f'</body></html>'
    )

    text_lines = [
        f"{cfg['client_name']} — Daily Run", "",
        f"Sourced: {report.sourced}   Drafted: {report.drafted}",
        "(nothing was sent to a prospect — drafts wait for your approval)", "",
        "Steps:",
    ]
    for s in report.steps:
        line = f"  [{s['status']}] {s['name']}"
        if s.get("detail"):
            line += f" — {s['detail']}"
        text_lines.append(line)
        if s.get("error"):
            text_lines.append(f"        {s['error']}")
    text_lines += ["", f"Lead list: {counts or '{}'}", f"Open faults: {report.faults or '{}'}", "",
                   f"Review: {dashboard}"]
    return subject, html_body, "\n".join(text_lines)


def send_run_digest(cfg, report, dashboard=None):
    """Email the operator one summary of this run. Returns True if delivered.

    Never raises: a digest that fails must not fail the run that already did its work
    (the caller records the failure and moves on).
    """
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    if not smtp_user or not smtp_pass:
        print("   ⚠️  SMTP_USER/SMTP_PASS not set — skipping the digest.")
        return False

    subject, html_body, text_body = build_digest(cfg, report, dashboard=dashboard)
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['client_name']} System <{smtp_user}>"
        msg["To"] = smtp_user
        msg["Subject"] = subject
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        sender.smtp_deliver("smtp.gmail.com", 587, smtp_user, smtp_pass,
                            smtp_user, smtp_user, msg.as_string())
        print(f"   ✅ Digest sent to {smtp_user} — '{subject}'")
        return True
    except Exception as e:
        print(f"   ❌ Digest failed to send: {e}")
        return False


def notify_mode(cfg):
    """'digest' (default) or 'per_draft' — see the module docstring."""
    return ((cfg.get("notify") or {}).get("mode") or "digest").strip().lower()
