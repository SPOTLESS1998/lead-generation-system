"""Idempotent state — SQLite (stdlib) at data/<client>/state.sqlite.

Tables:
  leads       — who we know about + their status (dedup key: client+email)
  sends       — one row per dispatch; powers idempotency + daily-cap accounting;
                stores each send's Message-ID so inbound replies can be matched back
  suppression — never-contact list (unsubscribes, bounces, manual)
  replies     — one row per inbound reply we processed (dedup on Message-ID)
  bookings    — meetings booked off an interested reply
  reminders   — which pre-meeting reminders (60/30/15 min) have already fired
  magnets     — personalized lead-magnet page content (Tier 3)
  pipeline_events — observability ledger: one row per pipeline step (status,
                duration, tokens, cost) that powers self-healing + accounting
  faults      — flagged problems + how they were healed or escalated

WAL mode is enabled so the agent and the Flask approval server can both touch
the same DB safely. connect() runs a tiny idempotent migration so DBs created
before Tier 2 gain the new column/tables without losing data.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from .leads import domain_key

# The lead lifecycle, in order:
#   sourced   — discovered, qualified and BANKED. Never contacted. This is the
#               durable lead list: a lead reaches this state the moment discovery
#               qualifies it, whether or not it is ever drafted.
#   queued    — claimed by the drafter for this run (a short-lived working state).
#   approved  — the operator approved the draft.
#   sent      — actually dispatched.
SOURCED = "sourced"

# A lead is considered "already handled" (skip on re-runs) in any of these states.
# `sourced` is deliberately NOT here: a banked lead has never been contacted, so it
# must stay eligible for drafting on a later run.
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
    company_description TEXT,          -- AI one-liner: what this company does
    company_facts       TEXT,          -- richer grounding facts for the cold-email opener
    website_domain TEXT,               -- normalised host, so a later run can skip a
                                       -- business we have already paid to scrape
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

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY,
    client     TEXT NOT NULL,
    booking_id INTEGER NOT NULL,   -- the bookings.id this reminder is for
    minutes    INTEGER NOT NULL,   -- which lead-time fired: 60 | 30 | 15
    sent_at    TEXT NOT NULL,
    UNIQUE(client, booking_id, minutes)   -- each reminder fires exactly once
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

CREATE TABLE IF NOT EXISTS pipeline_events (
    id                INTEGER PRIMARY KEY,
    client            TEXT NOT NULL,
    run_id            TEXT,               -- groups every event from one agent pass/run
    step              TEXT NOT NULL,      -- discover | draft | send | reply | book | remind | heal | ...
    subject           TEXT,               -- the thing acted on (lead email, booking id, ...)
    status            TEXT NOT NULL,      -- ok | error | skipped
    attempt           INTEGER NOT NULL DEFAULT 1,
    duration_ms       INTEGER,
    provider          TEXT,               -- LLM provider that answered this step, if any
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    error             TEXT,               -- error message when status='error'
    meta              TEXT,               -- optional JSON blob of extra context
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS faults (
    id          INTEGER PRIMARY KEY,
    client      TEXT NOT NULL,
    event_id    INTEGER,            -- the pipeline_events row that triggered this (if any)
    run_id      TEXT,
    kind        TEXT NOT NULL,      -- transient | rate_limit | stall | data | auth | config | unknown
    step        TEXT NOT NULL,      -- which pipeline step faulted
    subject     TEXT,               -- the lead/booking the fault concerns
    detail      TEXT,               -- error text / description
    status      TEXT NOT NULL DEFAULT 'open',   -- open | healing | resolved | escalated
    action      TEXT,               -- remediation chosen (from healer.SAFE_ACTIONS)
    attempts    INTEGER NOT NULL DEFAULT 0,
    resolution  TEXT,               -- how it ended (what the healer or human did)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer_state (
    client        TEXT PRIMARY KEY,  -- one cursor row per client
    last_event_id INTEGER NOT NULL DEFAULT 0,  -- highest pipeline_events.id already scanned
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sends_client_mailbox_day ON sends(client, mailbox, sent_at);
CREATE INDEX IF NOT EXISTS idx_events_client_step ON pipeline_events(client, step, created_at);
CREATE INDEX IF NOT EXISTS idx_faults_client_status ON faults(client, status);
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

    # Grounding facts for the cold-email copy (added after Tier 2): the AI one-line
    # company_description and the richer company_facts (services + a verifiable detail
    # + review signal). Pre-existing leads tables gain them here without losing data.
    lead_cols = {r["name"] for r in conn.execute("PRAGMA table_info(leads)").fetchall()}
    for col in ("company_description", "company_facts"):
        if col not in lead_cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} TEXT")

    # website_domain (added with the durable lead list): the cross-run de-dupe key.
    # Backfilled from website_url for every pre-existing row, so a DB built before
    # this change starts skipping known businesses immediately rather than after it
    # has re-scraped them all once.
    if "website_domain" not in lead_cols:
        conn.execute("ALTER TABLE leads ADD COLUMN website_domain TEXT")
        rows = conn.execute(
            "SELECT id, website_url FROM leads WHERE website_url IS NOT NULL AND website_url != ''"
        ).fetchall()
        for r in rows:
            key = domain_key(r["website_url"])
            if key:
                conn.execute("UPDATE leads SET website_domain=? WHERE id=?", (key, r["id"]))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(client, website_domain)")

    conn.commit()


# --- leads -----------------------------------------------------------------

def upsert_lead(conn, client, lead, niche=None, status="queued"):
    """Insert a lead if new; leave an existing lead's status untouched.

    DO NOTHING on conflict is deliberate: re-discovering a business must never
    rewind a lead that has already progressed (or been suppressed). To move a lead
    forward, call set_status() explicitly rather than re-upserting it.
    """
    conn.execute(
        """INSERT INTO leads
               (client, email, first_name, last_name, title, company_name, website_url,
                company_description, company_facts, website_domain, niche, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(client, email) DO NOTHING""",
        (client, lead.get("email"), lead.get("first_name"), lead.get("last_name"),
         lead.get("title"), lead.get("company_name"), lead.get("website_url"),
         lead.get("company_description"), lead.get("company_facts"),
         domain_key(lead.get("website_url")), niche, status, _now()),
    )
    conn.commit()


def bank_leads(conn, client, leads):
    """Persist every qualified lead as `sourced` — the durable lead list.

    Called the moment discovery qualifies a batch, BEFORE any drafting decision, so
    the list is a real asset rather than a side-effect of drafting. Returns how many
    rows were genuinely new (existing leads are left exactly as they are, whatever
    state they have reached).
    """
    before = count_leads(conn, client)
    for lead in leads:
        if not (lead.get("email") or "").strip():
            continue
        upsert_lead(conn, client, lead,
                    niche=lead.get("ejentic_service") or None, status=SOURCED)
    return count_leads(conn, client) - before


def known_domains(conn, client):
    """Every website domain already in this client's lead list.

    Discovery checks this BEFORE the expensive scrape+extract phase, so a business we
    already own is skipped without paying Firecrawl and an LLM call for it again.
    """
    rows = conn.execute(
        "SELECT DISTINCT website_domain FROM leads "
        "WHERE client=? AND website_domain IS NOT NULL AND website_domain != ''",
        (client,),
    ).fetchall()
    return {r["website_domain"] for r in rows}


def count_leads_by_status(conn, client):
    """{status: count} across this client's lead list (for the digest + dashboard)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM leads WHERE client=? GROUP BY status",
        (client,),
    ).fetchall()
    return {r["status"]: r["c"] for r in rows}


def leads_by_status(conn, client, status, limit=50):
    """Banked leads in `status`, oldest first — the drafting pool.

    Oldest-first so yesterday's surplus is used before anything found today, which is
    what stops a growing list from starving its own backlog.
    """
    return conn.execute(
        "SELECT * FROM leads WHERE client=? AND status=? ORDER BY created_at ASC, id ASC LIMIT ?",
        (client, status, int(limit)),
    ).fetchall()


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


# --- appointments & reminders (Tier 2.5) -----------------------------------

def list_appointments(conn, client):
    """The curated appointments list: every booking joined to its lead.

    Each row carries the booking (id, event_id, starts_at) plus the prospect's
    identity and the service we're about to render them (leads.niche) — name,
    company, title, contact email. LEFT JOIN so a booking still shows even if the
    leads row were somehow missing. Ordered by starts_at ASC (soonest first).

    Filtering to only-upcoming is left to the caller: starts_at is naive-LOCAL
    text, so "is it in the future?" can only be judged by someone holding the
    client's timezone (the agent and the dashboard both do — see core/reminders
    .parse_start). We return every row and let them decide.
    """
    return conn.execute(
        """SELECT b.id, b.event_id, b.starts_at, b.created_at, b.lead_email,
                  l.first_name, l.last_name, l.company_name, l.title,
                  l.niche AS service
             FROM bookings b
             LEFT JOIN leads l
               ON l.client = b.client AND l.email = b.lead_email
            WHERE b.client = ?
            ORDER BY b.starts_at ASC""",
        (client,),
    ).fetchall()


def reminder_sent(conn, client, booking_id, minutes):
    """True if the `minutes`-before reminder for this booking has already fired."""
    row = conn.execute(
        "SELECT 1 FROM reminders WHERE client=? AND booking_id=? AND minutes=?",
        (client, booking_id, minutes),
    ).fetchone()
    return row is not None


def record_reminder(conn, client, booking_id, minutes):
    """Mark a reminder as sent (idempotent via the UNIQUE constraint)."""
    conn.execute(
        "INSERT OR IGNORE INTO reminders (client, booking_id, minutes, sent_at) "
        "VALUES (?, ?, ?, ?)",
        (client, booking_id, minutes, _now()),
    )
    conn.commit()


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


# --- pipeline events (observability ledger, see core/observability.py) ------

def record_event(conn, client, step, status, subject=None, run_id=None, attempt=1,
                 duration_ms=None, provider=None, prompt_tokens=0, completion_tokens=0,
                 cost_usd=0.0, error=None, meta=None):
    """Append one row to the observability ledger — one pipeline step attempt.

    `meta` may be a dict/list (stored as JSON) or a string. This never raises on a
    bad meta value: observability must not be able to break the pipeline it watches.
    """
    if isinstance(meta, (dict, list)):
        try:
            meta = json.dumps(meta)
        except (TypeError, ValueError):
            meta = None
    conn.execute(
        """INSERT INTO pipeline_events
               (client, run_id, step, subject, status, attempt, duration_ms, provider,
                prompt_tokens, completion_tokens, cost_usd, error, meta, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (client, run_id, step, subject, status, int(attempt), duration_ms, provider,
         int(prompt_tokens or 0), int(completion_tokens or 0), float(cost_usd or 0.0),
         error, meta, _now()),
    )
    conn.commit()


def list_events(conn, client, since=None, step=None, status=None, limit=None, run_id=None):
    """Ledger rows for a client, newest first. `since` is an ISO-timestamp lower bound.
    Pass `run_id` to scope to a single agent pass (for a per-run metrics summary)."""
    sql = "SELECT * FROM pipeline_events WHERE client=?"
    params = [client]
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    if run_id:
        sql += " AND run_id=?"
        params.append(run_id)
    if step:
        sql += " AND step=?"
        params.append(step)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def count_leads(conn, client):
    """Total leads on record for a client (denominator for cost-per-lead)."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM leads WHERE client=?", (client,)
    ).fetchone()["c"]


def count_bookings(conn, client):
    """Total booked meetings for a client (denominator for cost-per-meeting)."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE client=?", (client,)
    ).fetchone()["c"]


# --- faults (self-healing ledger, see core/healer.py) ----------------------

def record_fault(conn, client, kind, step, subject, detail, event_id=None, run_id=None):
    """Open a fault, idempotently. If an open/healing fault already exists for this
    (client, step, subject), return its id instead of creating a duplicate — so a
    step that keeps failing produces ONE fault the healer works, not a pile."""
    existing = conn.execute(
        "SELECT id FROM faults WHERE client=? AND step=? "
        "AND COALESCE(subject,'')=COALESCE(?, '') AND status IN ('open','healing') "
        "ORDER BY id DESC LIMIT 1",
        (client, step, subject),
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO faults (client, event_id, run_id, kind, step, subject, detail, "
        "status, attempts, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 0, ?, ?)",
        (client, event_id, run_id, kind, step, subject, detail, _now(), _now()),
    )
    conn.commit()
    return cur.lastrowid


def update_fault(conn, fault_id, status=None, action=None, attempts=None,
                 resolution=None, kind=None):
    """Patch the mutable fields of a fault; always bumps updated_at."""
    sets, params = [], []
    for col, val in (("status", status), ("action", action), ("attempts", attempts),
                     ("resolution", resolution), ("kind", kind)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    sets.append("updated_at=?")
    params.append(_now())
    params.append(fault_id)
    conn.execute(f"UPDATE faults SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()


def resolve_fault(conn, fault_id, resolution="recovered"):
    """Mark a fault resolved (e.g. the observer saw the step succeed afterwards)."""
    update_fault(conn, fault_id, status="resolved", resolution=resolution)


def get_fault(conn, fault_id):
    return conn.execute("SELECT * FROM faults WHERE id=?", (fault_id,)).fetchone()


def open_faults(conn, client, limit=50):
    """Faults still needing attention (open or mid-heal), oldest first."""
    return conn.execute(
        "SELECT * FROM faults WHERE client=? AND status IN ('open','healing') "
        "ORDER BY id ASC LIMIT ?",
        (client, limit),
    ).fetchall()


def fault_counts(conn, client):
    """{status: count} across all faults for a client (for the dashboard)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM faults WHERE client=? GROUP BY status",
        (client,),
    ).fetchall()
    return {r["status"]: r["c"] for r in rows}


# --- observer cursor + scan queries (see scripts/observer_agent.py) ---------

def max_event_id(conn, client):
    """Highest pipeline_events.id for a client (0 if none). Used to seed the
    observer's watermark so a fresh observer ignores the historical backlog and
    only reacts to problems that happen once it's watching."""
    row = conn.execute(
        "SELECT MAX(id) AS m FROM pipeline_events WHERE client=?", (client,)
    ).fetchone()
    return row["m"] or 0


def get_observer_watermark(conn, client):
    """The highest event id the observer has already scanned, or None if it has
    never run for this client (caller seeds it on first run)."""
    row = conn.execute(
        "SELECT last_event_id FROM observer_state WHERE client=?", (client,)
    ).fetchone()
    return row["last_event_id"] if row else None


def set_observer_watermark(conn, client, event_id):
    """Advance (upsert) the observer's scan cursor."""
    conn.execute(
        """INSERT INTO observer_state (client, last_event_id, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(client) DO UPDATE SET last_event_id=excluded.last_event_id,
                                             updated_at=excluded.updated_at""",
        (client, int(event_id), _now()),
    )
    conn.commit()


def unscanned_error_events(conn, client, after_id, limit=200):
    """Error events newer than the watermark, oldest first — the observer's fault
    feed. Bounded by `after_id` so an event is never re-reacted to once scanned."""
    return conn.execute(
        "SELECT * FROM pipeline_events WHERE client=? AND status='error' AND id > ? "
        "ORDER BY id ASC LIMIT ?",
        (client, int(after_id), int(limit)),
    ).fetchall()


def has_ok_event_since(conn, client, step, subject, since_iso):
    """True if a later successful ('ok') event for this step+subject exists — i.e.
    the thing that faulted has since worked, so an open fault can be resolved."""
    if subject is None:
        row = conn.execute(
            "SELECT 1 FROM pipeline_events WHERE client=? AND step=? AND subject IS NULL "
            "AND status='ok' AND created_at > ? LIMIT 1",
            (client, step, since_iso),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM pipeline_events WHERE client=? AND step=? AND subject=? "
            "AND status='ok' AND created_at > ? LIMIT 1",
            (client, step, subject, since_iso),
        ).fetchone()
    return row is not None


def stalled_leads(conn, client, status, cutoff_iso, limit=200):
    """Leads sitting in `status` since before `cutoff_iso` — a stall the observer
    flags. Age is measured from leads.created_at: unambiguous for the primary
    'queued but never sent' case (a queued lead older than the threshold means the
    drafting/sending pipeline never picked it up — e.g. a dead cron)."""
    return conn.execute(
        "SELECT * FROM leads WHERE client=? AND status=? AND created_at <= ? "
        "ORDER BY created_at ASC LIMIT ?",
        (client, status, cutoff_iso, int(limit)),
    ).fetchall()
