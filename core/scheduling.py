"""Pick a safe, strictly-future meeting slot.

Shared by two callers:
  * the reply agent — lets the AI propose a time, then validates it here;
  * the one-click 'Interested' button — has no proposal, so it just wants the
    next sensible business-day slot.

Pure/stdlib: depends only on datetime + zoneinfo, no config or DB. Lifted out of
scripts/reply_agent.py so both paths share exactly one implementation.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def safe_future_slot(proposed, tz, min_days=2, hour=11):
    """Return a valid 'YYYY-MM-DD HH:MM' that is strictly in the future.

    A caller-proposed time (from the AI) is kept as-is *only* if it parses AND is
    in the future; otherwise — unparseable, in the past, or None (the button
    case) — fall back to `min_days` days out at `hour`:00 local, nudged past
    Sat/Sun to the next weekday.
    """
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.now()
    if proposed:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
            try:
                dt = datetime.strptime(proposed.strip(), fmt).replace(tzinfo=now.tzinfo)
                if dt > now:
                    return dt.strftime("%Y-%m-%d %H:%M")
                break
            except ValueError:
                continue
    dt = (now + timedelta(days=min_days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    while dt.weekday() >= 5:  # nudge Sat/Sun → Monday
        dt += timedelta(days=1)
    return dt.strftime("%Y-%m-%d %H:%M")
