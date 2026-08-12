"""CAN-SPAM compliance: a signed unsubscribe token + the required email footer.

The token is self-verifying (HMAC-SHA256) — no server-side lookup table needed.
It encodes the client + email so /unsubscribe can suppress the right person and
reject tampered links.
"""

import hmac
import json
import base64
import hashlib


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def unsub_token(client, email, secret):
    """Create a tamper-proof unsubscribe token for this client+email."""
    payload = json.dumps({"c": client, "e": email}, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify_token(token, secret):
    """Return (client, email) if the token is valid and untampered, else None."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        expected = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            return None
        data = json.loads(payload)
        return data.get("c"), data.get("e")
    except Exception:
        return None


def unsub_url(client_cfg, token):
    base = (client_cfg.get("unsubscribe_base_url") or "").rstrip("/")
    return f"{base}/unsubscribe/{token}"


def footer(client_cfg, unsubscribe_url, channel="html"):
    """The CAN-SPAM footer: who it's from, a physical address, and an opt-out."""
    company = client_cfg.get("client_name", "") or client_cfg.get("from_name", "")
    address = client_cfg.get("physical_address", "")
    if channel == "html":
        return (
            '<hr style="border:none;border-top:1px solid #eee;margin:24px 0 12px;">'
            '<p style="font-size:12px;color:#999;line-height:1.5;font-family:Arial,sans-serif;">'
            f"You received this email from {company}.<br>{address}<br>"
            f'<a href="{unsubscribe_url}" style="color:#999;">Unsubscribe</a> '
            "to stop receiving these emails.</p>"
        )
    return (
        "\n\n—\n"
        f"You received this email from {company}.\n"
        f"{address}\n"
        f"Unsubscribe: {unsubscribe_url}\n"
    )
