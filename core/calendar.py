"""Meeting-booking seam — a real Google Calendar event via Composio.

The one place we book a meeting, mirroring core/leads.py (the Apollo seam) and
core/sender.py (the email seam). Composio is used ONLY here, because Google
Calendar has no app-password path the way Gmail SMTP/IMAP does.

    from core import calendar as gcal
    booked = gcal.create_event(cfg, summary="Intro call",
                               start_local="2026-08-14 15:00", duration_min=30,
                               attendee_email="prospect@acme.com")
    # -> {"event_id": "...", "html_link": "https://...", "start_iso": "2026-08-14T15:00:00"}

Graceful degradation (the RAG-reranker-404 pattern): every failure path raises
CalendarError so the caller can send the reply anyway and just flag that the
event wasn't booked. Booking must never block replying to a prospect.

Needs a one-time `composio link googlecalendar` (browser OAuth). Until then
create_event() raises CalendarError and the reply still goes out.
"""

import json
import subprocess
from datetime import datetime

try:
    from zoneinfo import ZoneInfo   # stdlib 3.9+
except Exception:                    # pragma: no cover
    ZoneInfo = None

DEFAULT_SLUG = "GOOGLECALENDAR_CREATE_EVENT"


class CalendarError(Exception):
    """Any booking failure — invalid time, unlinked account, CLI/API error."""


def _normalize_start(start_local):
    """'YYYY-MM-DD HH:MM[:SS]' or ISO 'T' form -> naive 'YYYY-MM-DDTHH:MM:SS'."""
    if not start_local:
        raise CalendarError("no proposed start time")
    s = str(start_local).strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise CalendarError(f"unparseable start time {start_local!r}: {e}")
    # Composio wants a naive ISO string; the timezone travels in its own field.
    return dt.replace(tzinfo=None, microsecond=0).isoformat()


def _split_duration(duration_min):
    """(hours, minutes) with minutes in 0-59, as Composio requires."""
    m = max(1, int(duration_min or 30))
    return m // 60, m % 60


def _loads_tolerant(text):
    """composio may print log lines around the JSON; parse the outermost object."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def _extract_success(payload):
    """Pull (event_id, html_link) out of Composio's response, tolerantly."""
    candidates = []
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            candidates.append(data)
            for k in ("response_data", "event", "result"):
                if isinstance(data.get(k), dict):
                    candidates.append(data[k])
    for c in candidates:
        eid = c.get("id") or c.get("event_id")
        link = c.get("htmlLink") or c.get("html_link")
        if eid or link:
            return eid, link
    return None, None


def create_event(client_cfg, summary, start_local, duration_min=30,
                 attendee_email=None, description=""):
    """Create a Google Calendar event via `composio execute <slug>`.

    Returns {event_id, html_link, start_iso} on success; raises CalendarError on
    any failure so the caller can degrade (send the reply, skip the booking).
    """
    booking = client_cfg.get("booking") or {}
    if not booking.get("enabled", False):
        raise CalendarError("booking disabled in config")
    slug = booking.get("action_slug") or DEFAULT_SLUG
    tz = client_cfg.get("timezone") or "UTC"
    if ZoneInfo is not None:
        try:
            ZoneInfo(tz)   # validate; a bad IANA name should degrade, not crash
        except Exception:
            raise CalendarError(f"invalid IANA timezone {tz!r}")

    start_iso = _normalize_start(start_local)
    hours, minutes = _split_duration(duration_min)
    payload = {
        "summary": summary or "Intro call",
        "start_datetime": start_iso,
        "timezone": tz,
        "event_duration_hour": hours,
        "event_duration_minutes": minutes,
        "calendar_id": "primary",
    }
    if attendee_email:
        payload["attendees"] = [attendee_email]
    if description:
        payload["description"] = description

    cmd = ["composio", "execute", slug, "-d", json.dumps(payload)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except FileNotFoundError:
        raise CalendarError("composio CLI not found on PATH")
    except subprocess.TimeoutExpired:
        raise CalendarError("composio execute timed out")
    if proc.returncode != 0:
        raise CalendarError(f"composio execute rc={proc.returncode}: "
                            f"{(proc.stderr or proc.stdout or '').strip()[:300]}")

    obj = _loads_tolerant((proc.stdout or "").strip())
    if not isinstance(obj, dict):
        raise CalendarError(f"unparseable composio output: {(proc.stdout or '')[:200]}")
    ok = obj.get("successful", obj.get("successfull", True))
    if ok is False or obj.get("error"):
        raise CalendarError(f"composio reported failure: {str(obj.get('error'))[:200]}")

    event_id, html_link = _extract_success(obj)
    return {"event_id": event_id, "html_link": html_link, "start_iso": start_iso}
