"""Throttled, capped, rotation-ready sender.

The one place that actually puts email on the wire. Safe by construction:
  * daily caps  — per-mailbox and global; refuses to exceed them
  * rotation    — spreads load across N mailboxes (demo runs with 1)
  * throttle    — jittered delay between sends (batch mode; off for 1-click sends)
  * controlled  — in 'controlled' mode, delivers to a safe inbox instead of the
                  real prospect, but still records the logical recipient

Every send is written to the `sends` table (idempotency + cap accounting).
"""

import html
import time
import random
import smtplib
from email.utils import make_msgid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from . import state


class SendCapExceeded(Exception):
    """Raised when a send would exceed a per-mailbox or global daily cap."""


class SendError(Exception):
    """Raised when SMTP delivery fails."""


def _nl2br(text):
    return html.escape(text).replace("\n", "<br>")


def smtp_deliver(host, port, user, password, from_addr, to_addr, msg_string,
                 retries=3, backoff=2.0):
    """Put one message on the wire, retrying transient failures.

    Gmail's SMTP occasionally drops the TLS session mid-handshake
    ("EOF occurred in violation of protocol"); a fresh connection on the next
    attempt almost always succeeds. Each attempt opens — and always closes — its
    own connection. Raises the last exception only if every attempt fails.
    """
    last = None
    for attempt in range(1, retries + 1):
        try:
            server = smtplib.SMTP(host, port, timeout=30)
            try:
                server.starttls()
                server.login(user, password)
                server.sendmail(from_addr, to_addr, msg_string)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            return
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise last


class SendingPool:
    def __init__(self, client_cfg):
        self.cfg = client_cfg
        self.client = client_cfg["client"]
        s = client_cfg["sending"]
        self.mailboxes = s["mailboxes"]
        self.mode = s.get("mode", "controlled")
        self.controlled_inbox = s.get("controlled_inbox")
        self.global_cap = int(s.get("daily_global_cap", 50))
        thr = s.get("throttle_seconds") or {}
        self.throttle_min = float(thr.get("min", 20))
        self.throttle_max = float(thr.get("max", 60))
        self.from_name = client_cfg.get("from_name") or client_cfg.get("client_name", "")
        self.reply_to = client_cfg.get("reply_to")

    # --- capacity ----------------------------------------------------------

    def remaining_today(self, conn):
        """How many more sends are allowed today, min of global and pooled caps."""
        global_left = max(0, self.global_cap - state.sends_today(conn, self.client))
        pool_left = sum(
            max(0, m["daily_cap"] - state.sends_today(conn, self.client, m["address"]))
            for m in self.mailboxes
        )
        return min(global_left, pool_left)

    def pick_mailbox(self, conn):
        """The eligible mailbox with the fewest sends today (spreads load); None if all capped."""
        eligible = [
            m for m in self.mailboxes
            if state.sends_today(conn, self.client, m["address"]) < m["daily_cap"]
        ]
        if not eligible:
            return None
        return min(eligible, key=lambda m: state.sends_today(conn, self.client, m["address"]))

    def _throttle(self):
        delay = random.uniform(self.throttle_min, self.throttle_max)
        time.sleep(delay)
        return delay

    # --- send --------------------------------------------------------------

    def send(self, conn, to_email, subject, body_text,
             footer_html="", footer_text="", throttle=True,
             in_reply_to=None, references=None, list_unsubscribe=None,
             cta_html="", cta_text=""):
        """Send one email through the pool. Returns a result dict.

        Raises SendCapExceeded (before any SMTP) if a cap is hit, or SendError
        if delivery fails. `throttle` should be False for interactive 1-click
        sends (a human clicking already paces them) and True for batch/cron.

        `in_reply_to`/`references` (Message-IDs) thread an approved reply under the
        prospect's message in Gmail. Every send is stamped with its own Message-ID,
        which is stored so a future inbound reply can be matched back to it.

        `list_unsubscribe` (an https URL) adds the List-Unsubscribe + one-click
        headers that Gmail/Yahoo expect from legitimate senders — a positive
        deliverability signal, not a spam one.

        `cta_html`/`cta_text` are prospect-facing one-click buttons (interested /
        not interested) injected between the body and the CAN-SPAM footer. Like
        `footer_html`, `cta_html` is trusted raw HTML built by core/compliance.py.
        """
        if state.sends_today(conn, self.client) >= self.global_cap:
            raise SendCapExceeded(f"daily global cap reached ({self.global_cap})")
        mailbox = self.pick_mailbox(conn)
        if mailbox is None:
            raise SendCapExceeded("all mailboxes are at their daily cap")

        # Controlled mode: deliver to the safe inbox, but remember who it was *for*.
        actual_to = to_email
        redirected_to = None
        if self.mode == "controlled":
            actual_to = self.controlled_inbox
            redirected_to = self.controlled_inbox

        msg = MIMEMultipart("alternative")
        msg["From"] = f'{self.from_name} <{mailbox["address"]}>'
        msg["To"] = actual_to
        msg["Subject"] = subject
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        if redirected_to:
            # Make it obvious in the demo that this was routed to a safe inbox.
            msg["X-Ejentic-Intended-For"] = to_email

        # Stamp a Message-ID (so replies can be matched back) and thread if asked.
        domain = mailbox["address"].split("@")[-1] if "@" in mailbox["address"] else None
        message_id = make_msgid(domain=domain)
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to
        elif references:
            msg["References"] = references

        # List-Unsubscribe (+ one-click) — expected by Gmail/Yahoo from real senders.
        if list_unsubscribe:
            msg["List-Unsubscribe"] = f"<{list_unsubscribe}>"
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        html_body = (
            '<html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">'
            f"{_nl2br(body_text)}{cta_html}{footer_html}"
            "</body></html>"
        )
        msg.attach(MIMEText(body_text + cta_text + footer_text, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            smtp_deliver(mailbox["smtp_host"], mailbox["smtp_port"],
                         mailbox["user"], mailbox["password"],
                         mailbox["address"], actual_to, msg.as_string())
        except Exception as e:
            raise SendError(str(e))

        delay = self._throttle() if throttle else 0.0
        state.record_send(conn, self.client, to_email, mailbox["address"], subject,
                          redirected_to, message_id=message_id)
        return {
            "mailbox": mailbox["address"],
            "delivered_to": actual_to,
            "intended_for": to_email,
            "redirected": redirected_to is not None,
            "message_id": message_id,
            "throttle_s": round(delay, 1),
        }
