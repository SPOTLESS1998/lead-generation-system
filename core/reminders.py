"""When does a booked appointment need a reminder?

Pure/stdlib clock math for the reminder agent — depends only on datetime +
zoneinfo, no config, no DB, no I/O. Kept in its own module (like
core/scheduling.py) so the agent stays thin and this logic is unit-testable
without a database or a network.

The bookings table stores `starts_at` as a NAIVE local ISO string (e.g.
"2026-08-24T10:00") in the client's configured timezone. So every function here
takes that timezone and works in timezone-AWARE datetimes — you must never
compare a naive booking time against a real "now".
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def parse_start(starts_at, tz):
    """Interpret a booking's naive-local `starts_at` as an aware datetime in `tz`.

    `starts_at` is whatever record_booking stored — normally "YYYY-MM-DDTHH:MM"
    or "YYYY-MM-DD HH:MM" (a calendar API may hand back seconds, or a trailing
    offset/'Z'). Returns an aware datetime in `tz`, or None if it's empty or
    unparseable — the agent then skips that row rather than crash.
    """
    if not starts_at:
        return None
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("UTC")
    s = str(starts_at).strip()
    # Naive local forms first (what record_booking stores): tolerate a space or
    # 'T' separator and optional seconds. strptime (not fromisoformat) keeps
    # parsing identical across Python versions.
    naive = s.replace(" ", "T", 1)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(naive, fmt).replace(tzinfo=zone)
        except ValueError:
            continue
    # Offset-aware ISO (e.g. a calendar that returns "...+01:00" or "...Z").
    try:
        iso = naive[:-1] + "+00:00" if naive.endswith("Z") else naive
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=zone)
    except Exception:
        return None


def due_intervals(start_aware, now_aware, lead_times=(60, 30, 15)):
    """Which lead-time reminders are 'active' right now, most-urgent-first order.

    A lead-time of m minutes is active when `now` is inside [start - m min, start):
    within m minutes of the start, but the meeting has not begun. Returns the
    active minutes sorted DESCENDING (e.g. [60, 30, 15] when all three are live),
    or [] if the meeting is still far off, or has already started/passed.
    """
    if start_aware is None or now_aware is None:
        return []
    if now_aware >= start_aware:
        return []  # started or in the past — nothing to remind about
    active = [m for m in lead_times
              if now_aware >= start_aware - timedelta(minutes=m)]
    return sorted(active, reverse=True)


def minutes_until(start_aware, now_aware):
    """Whole minutes from now until the meeting starts (floored; may be negative)."""
    if start_aware is None or now_aware is None:
        return 0
    return int((start_aware - now_aware).total_seconds() // 60)
