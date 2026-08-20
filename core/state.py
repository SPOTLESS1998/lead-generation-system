"""Idempotent state — SQLite (stdlib) at data/<client>/state.sqlite.

Tables:
  leads       — who we know about + their status (dedup key: client+email)
  sends       — one row per dispatch; powers idempotency + daily-cap accounting;
                stores each send's Message-ID so inbound replies can be matched back
  suppression — never-contact list (unsubscribes, bounces, manual)
  replies     — one row per inbound reply we processed (dedup on Message-ID)
  bookings    — meetings booked off an interested reply

WAL mode is enabled so the agent and the Flask approval server can both touch
the same DB safely. connect() runs a tiny idempotent migration so DBs created
before Tier 2 gain the new column/tables without losing data.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# A lead is considered "already handled" (skip on re-runs) in any of these states.
CONTACTED_STATES = ("queued", "approved", "sent")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY,
    client       TEXT NOT NULL,
    email        TEXT NOT NULL,
    first_name   TEXT,
    last_name    TEXT,
    title        TEXT,
    company_name TEXT,
    website_url  TEXT,
    niche        TEXT,
    status       TEXT NOT NULL DEFAULT 'new',
    created_at   TEXT NOT NULL,
    UNIQUE(client, email)
);

CREATE TABLE IF NOT EXISTS sends (
    id            INTEGER PRIMARY KEY,
    client        TEXT NOT NULL,
    lead_email    TEXT NOT NULL,   -- the logical recipient (the prospect)
    mailbox       TEXT NOT NULL,   -- the sending identity used
    subject       TEXT,
    redirected_to TEXT,            -- set when delivered to a controlled inbox instead of the prospect
    message_id    TEXT,            -- RFC Message-ID header we stamped, so replies can be matched back
    sent_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
    id         INTEGER PRIMARY KEY,
    client     TEXT NOT NULL,
    email      TEXT NOT NULL,
    reason     TEXT NOT NULL,      -- unsubscribed | bounced | complained | manual | not_interested
    created_at TEXT NOT NULL,
    UNIQUE(client, email)
);

CREATE TABLE IF NOT EXISTS replies (
    id          INTEGER PRIMARY KEY,
    client      TEXT NOT NULL,
    lead_email  TEXT NOT NULL,     -- the prospect who replied (logical, not the redirect inbox)
    message_id  TEXT NOT NULL,     -- the inbound reply's Message-ID (dedup key)
    in_reply_to TEXT,              -- the send Message-ID it responded to
    subject     TEXT,
    body        TEXT,
    intent      TEXT,              -- interested | question | objection | not_interested | unsubscribe | auto_reply
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    UNIQUE(client, message_id)
);

CREATE TABLE IF NOT EXISTS bookings (
    id         INTEGER PRIMARY KEY,
    client     TEXT NOT NULL,
    lead_email TEXT NOT NULL,
    event_id   TEXT,
    starts_at  TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS magnets (
    id         INTEGER PRIMARY KEY,
    client     TEXT NOT NULL,
    token      TEXT NOT NULL,       -- unguessable id in the magnet URL
    lead_email TEXT,                -- who this page was built for
    content    TEXT NOT NULL,       -- JSON: the page's structured content (see core/magnet.py)
    created_at TEXT NOT NULL,
    UNIQUE(client, token)
);

CREATE INDEX IF NOT EXISTS idx_sends_client_mailbox_day ON sends(client, mailbox, sent_at);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def connect(db_path):
    """Open (creating dirs + schema as needed) the per-client state DB."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Idempotent, additive migrations for DBs created before Tier 2.

    CREATE TABLE IF NOT EXISTS covers new tables; only pre-existing tables need an
    ALTER. Safe to run on every connect — each change is guarded by a column check.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sends)").fetchall()}
    if "message_id" not in cols:
        conn.execute("ALTER TABLE sends ADD COLUMN message_id TEXT")
    # Index built here (not in _SCHEMA) so it never runs before the column exists.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sends_message_id ON sends(client, message_id)")
    conn.commit()


# --- leads -----------------------------------------------------------------

def upsert_lead(conn, client, lead, niche=None, status="queued"):
    """Insert a lead if new; leave an existing lead's status untouched."""
    conn.execute(
        """INSERT INTO leads
               (client, email, first_name, last_name, title, company_name, website_url, niche, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(client, email) DO NOTHING""",
        (client, lead.get("email"), lead.get("first_name"), lead.get("last_name"),
         lead.get("title"), lead.get("company_name"), lead.get("website_url"),
         niche, status, _now()),
    )
    conn.commit()


def lead_status(conn, client, email):
    row = conn.execute(
        "SELECT status FROM leads WHERE client=? AND email=?", (client, email)
    ).fetchone()
    return row["status"] if row else None


def set_status(conn, client, email, status):
    conn.execute(
        "UPDATE leads SET status=? WHERE client=? AND email=?", (status, client, email)
    )
    conn.commit()


def already_contacted(conn, client, email):
    return lead_status(conn, client, email) in CONTACTED_STATES


# --- sends -----------------------------------------------------------------

def record_send(conn, client, lead_email, mailbox, subject, redirected_to=None, message_id=None):
    """Record a dispatch and mark the lead 'sent'. Ensures a leads row exists."""
    if lead_status(conn, client, lead_email) is None:
        conn.execute(
            """INSERT INTO leads (client, email, status, created_at)
               VALUES (?, ?, 'queued', ?) ON CONFLICT(client, email) DO NOTHING""",
            (client, lead_email, _now()),
        )
    conn.execute(
        "INSERT INTO sends (client, lead_email, mailbox, subject, redirected_to, message_id, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (client, lead_email, mailbox, subject, redirected_to, message_id, _now()),
    )
    conn.execute(
        "UPDATE leads SET status='sent' WHERE client=? AND email=?", (client, lead_email)
    )
    conn.commit()


def sends_today(conn, client, mailbox=None):
    """Count sends so far today (UTC), overall or for one mailbox."""
    if mailbox is not None:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM sends WHERE client=? AND mailbox=? AND substr(sent_at, 1, 10)=?",
            (client, mailbox, _today()),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM sends WHERE client=? AND substr(sent_at, 1, 10)=?",
            (client, _today()),
        ).fetchone()
    return row["c"]


# --- replies & bookings (Tier 2) -------------------------------------------

def lead_for_message_id(conn, client, message_ids):
    """Given the In-Reply-To/References ids from an inbound reply, return the
    lead_email of the send it responds to (most recent match), or None."""
    ids = [m for m in (message_ids or []) if m]
    if not ids:
        return None
    placeholders = ",".join("?" for _ in ids)
    row = conn.execute(
        f"SELECT lead_email FROM sends WHERE client=? AND message_id IN ({placeholders}) "
        f"ORDER BY sent_at DESC LIMIT 1",
        (client, *ids),
    ).fetchone()
    return row["lead_email"] if row else None


def reply_seen(conn, client, message_id):
    """True if we've already recorded this inbound reply (dedup on Message-ID)."""
    if not message_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM replies WHERE client=? AND message_id=?", (client, message_id)
    ).fetchone()
    return row is not None


def record_reply(conn, client, lead_email, message_id, in_reply_to, subject, body, intent):
    """Record an inbound reply (idempotent on Message-ID). Bumps the lead status
    to a reply state unless it's already been moved further along."""
    conn.execute(
        """INSERT INTO replies
               (client, lead_email, message_id, in_reply_to, subject, body, intent, created_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new')
           ON CONFLICT(client, message_id) DO NOTHING""",
        (client, lead_email, message_id, in_reply_to, subject, body, intent, _now()),
    )
    if lead_email and lead_status(conn, client, lead_email) in CONTACTED_STATES:
        conn.execute(
            "UPDATE leads SET status='replied' WHERE client=? AND email=?", (client, lead_email)
        )
    conn.commit()


def record_booking(conn, client, lead_email, event_id, starts_at):
    conn.execute(
        "INSERT INTO bookings (client, lead_email, event_id, starts_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (client, lead_email, event_id, starts_at, _now()),
    )
    conn.execute(
        "UPDATE leads SET status='meeting_booked' WHERE client=? AND email=?", (client, lead_email)
    )
    conn.commit()


def latest_booking(conn, client, email):
    """The most recent booking row for a lead (event_id, starts_at, created_at), or None.

    Used by the one-click buttons to stay idempotent: a second 'Interested' click,
    or a stray 'Not interested' click after booking, shows the existing meeting
    instead of double-booking or cancelling it.
    """
    return conn.execute(
        "SELECT event_id, starts_at, created_at FROM bookings "
        "WHERE client=? AND lead_email=? ORDER BY created_at DESC LIMIT 1",
        (client, email),
    ).fetchone()


# --- magnets (personalized lead-magnet pages) ------------------------------

def save_magnet(conn, client, token, lead_email, content):
    """Store one prospect's magnet page content (a dict, saved as JSON).

    Keyed by (client, token); re-saving the same token refreshes its content.
    """
    conn.execute(
        """INSERT INTO magnets (client, token, lead_email, content, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(client, token) DO UPDATE SET content=excluded.content""",
        (client, token, lead_email, json.dumps(content), _now()),
    )
    conn.commit()


def get_magnet(conn, client, token):
    """Return the stored content dict for a magnet page, or None if unknown."""
    row = conn.execute(
        "SELECT content FROM magnets WHERE client=? AND token=?", (client, token)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["content"])
    except (ValueError, TypeError):
        return None
