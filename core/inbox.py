"""Inbound reply reader — IMAP (stdlib), over the same mailbox we send from.

Reuses the mailbox's existing user/password (the Gmail app password already in
.env as SMTP_PASS) against imap.gmail.com:993 — no new credential, no OAuth, so a
client instance stays self-contained. fetch_replies() returns lightweight dicts;
the caller (scripts/reply_agent.py) owns dedup via state.reply_seen and the job of
matching each reply back to the send that triggered it.

Deliberately read-only: we open the mailbox with readonly=True and fetch with
BODY.PEEK[] so reading a reply never changes its \\Seen flag. Dedup is therefore
independent of IMAP flags (robust even if the operator opens the mail in Gmail).
"""

import re
import email
import imaplib
import html as _html
from email.header import decode_header, make_header
from email.utils import parseaddr

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993


# --- header / body helpers --------------------------------------------------

def _decode(value):
    """Decode a possibly RFC2047-encoded header (e.g. '=?UTF-8?B?..?=') to str."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _split_ids(value):
    """Extract the <...> message-ids from an In-Reply-To / References header."""
    if not value:
        return []
    return re.findall(r"<[^>]+>", value)


def _decode_payload(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def _html_to_text(html_str):
    """Dependency-free HTML → text: drop script/style, tags become breaks/spaces."""
    text = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", html_str)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _body_text(msg):
    """Best-effort readable body. Prefer text/plain; fall back to stripped HTML."""
    plain, html_part = None, None
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if "attachment" in str(part.get("Content-Disposition") or "").lower():
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = _decode_payload(part)
            elif ctype == "text/html" and html_part is None:
                html_part = _decode_payload(part)
    elif msg.get_content_type() == "text/html":
        html_part = _decode_payload(msg)
    else:
        plain = _decode_payload(msg)
    if plain and plain.strip():
        return plain
    if html_part:
        return _html_to_text(html_part)
    return ""


def _is_quote_start(line):
    """True if this line begins the quoted history of an earlier message."""
    s = line.strip()
    if s.startswith(">"):
        return True
    # Gmail/Apple: "On <date>, <name> wrote:" (single line, or wrapped tail).
    if s.endswith("wrote:") and (s.lower().startswith("on ") or len(s) < 200):
        return True
    # Outlook: "-----Original Message-----"
    if re.match(r"^-{2,}\s*original message\s*-{2,}$", s, re.I):
        return True
    return False


def strip_quoted(text):
    """Drop quoted history so the classifier sees only the prospect's new text.

    Best-effort: cut at the first quote marker. If that would leave nothing
    (unusual layout), keep the whole body rather than lose the reply.
    """
    kept = []
    for line in (text or "").splitlines():
        if _is_quote_start(line):
            break
        kept.append(line)
    out = "\n".join(kept).strip()
    return out or (text or "").strip()


# --- fetch ------------------------------------------------------------------

def fetch_replies(client_cfg, mailbox, limit=50):
    """Return a list of unseen inbound messages as dicts, newest first.

    Each dict: {message_id, in_reply_to, references[], from_email, from_name,
    subject, body_text, date}. IMAP host/port come from the mailbox config
    (defaults imap.gmail.com:993); creds are the same user/password used to send.
    """
    host = mailbox.get("imap_host") or DEFAULT_IMAP_HOST
    port = int(mailbox.get("imap_port") or DEFAULT_IMAP_PORT)
    out = []
    conn = imaplib.IMAP4_SSL(host, port)
    try:
        conn.login(mailbox["user"], mailbox["password"])
        conn.select("INBOX", readonly=True)   # read-only: never touch \Seen
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return out
        nums = data[0].split()
        for num in reversed(nums[-limit:]):    # newest first, bounded
            typ, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            from_name, from_email = parseaddr(_decode(msg.get("From")))
            in_reply_to = _split_ids(msg.get("In-Reply-To"))
            out.append({
                "message_id": (msg.get("Message-ID") or "").strip() or None,
                "in_reply_to": in_reply_to[0] if in_reply_to else None,
                "references": _split_ids(msg.get("References")),
                "from_email": (from_email or "").strip().lower(),
                "from_name": from_name or "",
                "subject": _decode(msg.get("Subject")),
                "body_text": strip_quoted(_body_text(msg)),
                "date": msg.get("Date"),
            })
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out
