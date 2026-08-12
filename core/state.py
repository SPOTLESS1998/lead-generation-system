"""Idempotent state — SQLite (stdlib) at data/<client>/state.sqlite.

Three tables:
  leads       — who we know about + their status (dedup key: client+email)
  sends       — one row per dispatch; powers idempotency + daily-cap accounting
  suppression — never-contact list (unsubscribes, bounces, manual)

WAL mode is enabled so the agent and the Flask approval server can both touch
the same DB safely.
"""

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
    sent_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
    id         INTEGER PRIMARY KEY,
    client     TEXT NOT NULL,
    email      TEXT NOT NULL,
    reason     TEXT NOT NULL,      -- unsubscribed | bounced | complained | manual
    created_at TEXT NOT NULL,
    UNIQUE(client, email)
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
    conn.commit()
    return conn


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

def record_send(conn, client, lead_email, mailbox, subject, redirected_to=None):
    """Record a dispatch and mark the lead 'sent'. Ensures a leads row exists."""
    if lead_status(conn, client, lead_email) is None:
        conn.execute(
            """INSERT INTO leads (client, email, status, created_at)
               VALUES (?, ?, 'queued', ?) ON CONFLICT(client, email) DO NOTHING""",
            (client, lead_email, _now()),
        )
    conn.execute(
        "INSERT INTO sends (client, lead_email, mailbox, subject, redirected_to, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
        (client, lead_email, mailbox, subject, redirected_to, _now()),
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
