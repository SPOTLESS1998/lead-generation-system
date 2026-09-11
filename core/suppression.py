"""The safety gate: is this person off-limits to contact right now?

Off-limits = on the suppression list (opted out / bounced / manual) OR already
contacted (idempotency — never email the same person twice on a re-run).
"""

from datetime import datetime, timezone

from . import state


def add(conn, client, email, reason="manual"):
    """Add (or refresh the reason for) an email on the never-contact list."""
    conn.execute(
        """INSERT INTO suppression (client, email, reason, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(client, email) DO UPDATE SET reason=excluded.reason""",
        (client, email, reason, datetime.now(timezone.utc).isoformat()),
    )
    # Reflect it on the lead record too, if we have one. status_changed_at is stamped
    # for the same reason set_status does it: stall detection measures from it.
    conn.execute(
        "UPDATE leads SET status='suppressed', status_changed_at=? WHERE client=? AND email=?",
        (datetime.now(timezone.utc).isoformat(), client, email),
    )
    conn.commit()


def is_suppressed(conn, client, email):
    row = conn.execute(
        "SELECT 1 FROM suppression WHERE client=? AND email=?", (client, email)
    ).fetchone()
    return row is not None


def should_skip(conn, client, email):
    """Return (skip: bool, reason: str|None). The single check the agent calls."""
    if is_suppressed(conn, client, email):
        return True, "suppressed"
    if state.already_contacted(conn, client, email):
        return True, "already_contacted"
    return False, None
