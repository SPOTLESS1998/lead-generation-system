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
  mx_checks   — cached per-domain mail-route verdicts (see core/verify.py)

WAL mode is enabled so the agent and the Flask approval server can both touch
the same DB safely. connect() runs a tiny idempotent migration so DBs created
before Tier 2 gain the new column/tables without losing data.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

from .leads import domain_key

# The lead lifecycle, in order:
#   sourced           — discovered, qualified and BANKED. Never contacted. This is the
#                       durable lead list: a lead reaches this state the moment
#                       discovery qualifies it, whether or not it is ever drafted.
#   queued            — CLAIMED by the drafter for this run (a short-lived working
#                       state). A lead sitting here means the machine has it in hand
#                       and owes us a draft, so it going stale is a real fault.
#   awaiting_approval — a draft exists and is sitting in the operator's queue. This is
#                       a HUMAN's turn, not a stalled machine: it may sit here for
#                       days and that is correct behaviour.
#   approved          — the operator approved the draft.
#   sent              — actually dispatched.
#
# Why `queued` and `awaiting_approval` are separate states: they used to both be
# `queued`, and that single overloaded value broke self-healing. The stall detector
# flags any lead sitting in `queued` too long (observability.stall_minutes), which
# correctly catches a dead drafter — but it ALSO flagged every draft correctly waiting
# for a human, and the healer's remedy for a stall is to re-queue, which set the status
# it already had. The fault could therefore never resolve, so it re-diagnosed with an
# LLM call on every pass until it escalated and emailed the operator. At daily volume
# that is a token-burning escalation storm. One status now means one thing.
SOURCED = "sourced"
AWAITING_APPROVAL = "awaiting_approval"

# A lead is considered "already handled" (skip on re-runs) in any of these states.
# `sourced` is deliberately NOT here: a banked lead has never been contacted, so it
# must stay eligible for drafting on a later run.
CONTACTED_STATES = ("queued", AWAITING_APPROVAL, "approved", "sent")

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
    status_changed_at TEXT,            -- when `status` last moved; stall detection
                                       -- measures from here, NOT from created_at
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

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    client      TEXT NOT NULL,
    run_id      TEXT,
    status      TEXT NOT NULL,        -- ok | failed
    sourced     INTEGER NOT NULL DEFAULT 0,
    drafted     INTEGER NOT NULL DEFAULT 0,
    failed_steps TEXT,                -- JSON list of step names that failed
    detail      TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL
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

-- Every judge verdict, kept. The rewrite loop used a critique once and threw it
-- away, so the drafter re-learned the same lesson on every run and run 11 was no
-- better than run 1. Keeping them is what makes improvement measurable (the
-- per-run scorecard) and learnable (the proposed lessons below).
CREATE TABLE IF NOT EXISTS draft_critiques (
    id          INTEGER PRIMARY KEY,
    client      TEXT NOT NULL,
    run_id      TEXT,
    lead_email  TEXT,
    attempt     TEXT,                 -- draft | revision-1 | revision-2 ...
    score       INTEGER,              -- 1-10, NULL if the judge was unavailable
    issues      TEXT,                 -- JSON list of short phrases
    fix_hint    TEXT,
    outcome     TEXT,                 -- shipped | refused_floor | refused_citation | judge_down
    created_at  TEXT NOT NULL
);

-- Lessons the reflection pass PROPOSES from recurring critiques.
-- It may propose; it may never promote. Nothing here reaches a prompt until a
-- human sets status='approved' — the same gate the Nova learning loop uses, and
-- for the same reason: a drafter that silently rewrites its own instructions can
-- reintroduce exactly the fabrication the citation checker exists to stop.
CREATE TABLE IF NOT EXISTS copy_lessons (
    id          INTEGER PRIMARY KEY,
    client      TEXT NOT NULL,
    lesson      TEXT NOT NULL,        -- one short STYLE rule, never a fact
    evidence    TEXT,                 -- JSON: counts + sample critique phrases
    status      TEXT NOT NULL DEFAULT 'proposed',   -- proposed | approved | rejected
    proposed_at TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  TEXT
);

-- Per-run quality scorecard: the trend that shows whether run 10 beats run 5.
CREATE TABLE IF NOT EXISTS run_quality (
    id                INTEGER PRIMARY KEY,
    client            TEXT NOT NULL,
    run_id            TEXT,
    attempted         INTEGER NOT NULL DEFAULT 0,
    shipped           INTEGER NOT NULL DEFAULT 0,
    refused_floor     INTEGER NOT NULL DEFAULT 0,
    refused_citation  INTEGER NOT NULL DEFAULT 0,
    provider_failures INTEGER NOT NULL DEFAULT 0,
    mean_score        REAL,
    median_score      REAL,
    first_pass_score  REAL,           -- mean score BEFORE any revision
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mx_checks (
    domain     TEXT PRIMARY KEY,   -- mail route is a property of the DOMAIN, not the address
    status     TEXT NOT NULL,      -- ok | invalid  (UNKNOWN is deliberately never stored)
    reason     TEXT,
    checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sends_client_mailbox_day ON sends(client, mailbox, sent_at);
CREATE INDEX IF NOT EXISTS idx_events_client_step ON pipeline_events(client, step, created_at);
CREATE INDEX IF NOT EXISTS idx_faults_client_status ON faults(client, status);
CREATE INDEX IF NOT EXISTS idx_critiques_client ON draft_critiques(client, created_at);
CREATE INDEX IF NOT EXISTS idx_lessons_client_status ON copy_lessons(client, status);
CREATE INDEX IF NOT EXISTS idx_runq_client ON run_quality(client, created_at);
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

    # status_changed_at (added with the stall-semantics fix): how long a lead has been
    # in its CURRENT state. Stall detection used created_at, which was wrong for
    # `queued` — a lead banked last week and claimed by the drafter this morning would
    # have looked instantly stalled. Backfilled to created_at, which is the best
    # available estimate for a row whose status has never moved.
    if "status_changed_at" not in lead_cols:
        conn.execute("ALTER TABLE leads ADD COLUMN status_changed_at TEXT")
        conn.execute("UPDATE leads SET status_changed_at=created_at WHERE status_changed_at IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(client, status)")

    # Split the overloaded 'queued' state. Rows written before AWAITING_APPROVAL
    # existed used 'queued' for BOTH "the drafter is working on this" and "a draft is
    # waiting for the operator", because that was the only value there was. Anything
    # already parked awaiting a human decision has to move to the new state —
    # otherwise the stall detector keeps flagging it forever and the healer burns an
    # LLM call per pass trying to "fix" a lead that is behaving correctly. Rows left in
    # 'queued' really are mid-draft claims.
    #
    # Two signals, because neither alone is complete: magnets exist only for drafts
    # whose audit page generated successfully (that step degrades gracefully), while
    # pending_leads.json is the arena the draft actually waits in but is a local file
    # that may be absent on a fresh machine. A lead matches on either.
    _reclassify_queued_as_awaiting(conn)

    conn.commit()


def _reclassify_queued_as_awaiting(conn):
    """Move pre-existing 'queued' rows that are really awaiting approval (see _migrate)."""
    emails = set()
    try:
        for r in conn.execute("SELECT DISTINCT lead_email FROM magnets WHERE lead_email IS NOT NULL"):
            emails.add(r["lead_email"])
    except sqlite3.OperationalError:
        pass
    # Imported lazily: core/review.py imports core/config.py and core/sender.py, so
    # keeping this import inside the function avoids a cycle at module load time.
    try:
        from . import review
        for entry in (review.load_pending() or {}).values():
            if entry.get("status") == "pending" and entry.get("target_email"):
                emails.add(entry["target_email"])
    except Exception:
        pass   # a missing/corrupt queue must never block a DB connection

    if not emails:
        return
    conn.executemany(
        "UPDATE leads SET status=? WHERE status='queued' AND email=?",
        [(AWAITING_APPROVAL, e) for e in emails],
    )


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
                company_description, company_facts, website_domain, niche, status,
                status_changed_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(client, email) DO NOTHING""",
        (client, lead.get("email"), lead.get("first_name"), lead.get("last_name"),
         lead.get("title"), lead.get("company_name"), lead.get("website_url"),
         lead.get("company_description"), lead.get("company_facts"),
         domain_key(lead.get("website_url")), niche, status, _now(), _now()),
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
    """Move a lead to a new status, stamping when it happened.

    status_changed_at is what stall detection measures from, so it must be updated on
    every move — otherwise a lead claimed today but created last week reads as stalled.
    """
    conn.execute(
        "UPDATE leads SET status=?, status_changed_at=? WHERE client=? AND email=?",
        (status, _now(), client, email),
    )
    conn.commit()


def already_contacted(conn, client, email):
    return lead_status(conn, client, email) in CONTACTED_STATES


# --- sends -----------------------------------------------------------------

def record_send(conn, client, lead_email, mailbox, subject, redirected_to=None, message_id=None):
    """Record a dispatch and mark the lead 'sent'. Ensures a leads row exists."""
    if lead_status(conn, client, lead_email) is None:
        conn.execute(
            """INSERT INTO leads (client, email, status, status_changed_at, created_at)
               VALUES (?, ?, 'sent', ?, ?) ON CONFLICT(client, email) DO NOTHING""",
            (client, lead_email, _now(), _now()),
        )
    conn.execute(
        "INSERT INTO sends (client, lead_email, mailbox, subject, redirected_to, message_id, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (client, lead_email, mailbox, subject, redirected_to, message_id, _now()),
    )
    conn.execute(
        "UPDATE leads SET status='sent', status_changed_at=? WHERE client=? AND email=?",
        (_now(), client, lead_email),
    )
    conn.commit()


def first_send_at(conn, client, mailbox=None, live_only=False):
    """ISO timestamp of the earliest send, overall or for ONE mailbox — or None.

    Per-mailbox on purpose: it is what the warmup ramp counts days from, and a
    mailbox added in month three must warm from ITS OWN first send rather than
    inherit the pool's age. Sending a brand-new mailbox 40/day on day one because
    a sibling has been running for weeks is exactly how a domain gets filtered.

    `live_only` excludes controlled-mode sends (those have a `redirected_to`, i.e.
    they were delivered to our own safe inbox, not to the prospect). The warmup
    ramp MUST pass it. Otherwise two weeks of controlled testing would age the
    domain on paper — flip to live and the ramp reports "day 15" and opens at
    near-full volume on a domain that has never sent a single real email. The
    clock has to start at the first message that actually reached a stranger.
    """
    where = "client=?"
    args = [client]
    if mailbox is not None:
        where += " AND mailbox=?"
        args.append(mailbox)
    if live_only:
        where += " AND redirected_to IS NULL"
    row = conn.execute(
        f"SELECT MIN(sent_at) AS t FROM sends WHERE {where}", tuple(args)
    ).fetchone()
    return row["t"] if row and row["t"] else None


def get_mx_check(conn, domain, ttl_days=30, invalid_ttl_days=7):
    """A cached mail-route verdict for `domain`, or None if absent/stale.

    Two TTLs on purpose. An 'ok' domain is stable, so it is trusted for
    ttl_days. An 'invalid' one is re-checked far sooner: a business that has
    just moved its DNS, or whose MX was mid-migration when we looked, must not
    be written off for a month on the strength of one bad afternoon. Cheap to
    re-ask, expensive to be wrong.
    """
    if not domain:
        return None
    row = conn.execute(
        "SELECT domain, status, reason, checked_at FROM mx_checks WHERE domain=?",
        (domain.lower(),),
    ).fetchone()
    if not row:
        return None
    ttl = invalid_ttl_days if row["status"] == "invalid" else ttl_days
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["checked_at"])
    except Exception:
        return None                      # unparseable timestamp: treat as stale
    if age > timedelta(days=ttl):
        return None
    return {"domain": row["domain"], "status": row["status"],
            "reason": row["reason"], "checked_at": row["checked_at"]}


def record_mx_check(conn, domain, status, reason=""):
    """Cache a mail-route verdict. Callers must never pass 'unknown' — it is a
    statement about a moment's network, not about the domain, and storing it
    would freeze a brief outage into a multi-week verdict."""
    if not domain or status not in ("ok", "invalid"):
        return
    conn.execute(
        "INSERT INTO mx_checks (domain, status, reason, checked_at) VALUES (?,?,?,?) "
        "ON CONFLICT(domain) DO UPDATE SET status=excluded.status, "
        "reason=excluded.reason, checked_at=excluded.checked_at",
        (domain.lower(), status, reason or "", _now()),
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
            "UPDATE leads SET status='replied', status_changed_at=? WHERE client=? AND email=?",
            (_now(), client, lead_email),
        )
    conn.commit()


def record_booking(conn, client, lead_email, event_id, starts_at):
    conn.execute(
        "INSERT INTO bookings (client, lead_email, event_id, starts_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (client, lead_email, event_id, starts_at, _now()),
    )
    conn.execute(
        "UPDATE leads SET status='meeting_booked', status_changed_at=? WHERE client=? AND email=?",
        (_now(), client, lead_email),
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
    """Return the stored content dict for a magnet page, or None if unknown.

    A row that exists but holds unparseable JSON also returns None, because the
    caller can only render a page or not. It is LOGGED though: to the prospect
    both cases look like "This resource has expired or the link is invalid", but
    they are opposite problems — an unknown token is someone guessing a URL,
    while a corrupt row means a prospect who clicked the personalized audit link
    we promised them in a cold email got a dead end, and nothing anywhere said so.
    """
    row = conn.execute(
        "SELECT content FROM magnets WHERE client=? AND token=?", (client, token)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["content"])
    except (ValueError, TypeError) as e:
        print(f"⚠️  magnet {token} for client {client} exists but its content is "
              f"unreadable ({e}); the prospect will see an 'expired link' page.")
        return None


# --- pipeline events (observability ledger, see core/observability.py) ------

# --- runs (heartbeat / missed-run detection) -------------------------------

def record_run(conn, client, status, sourced=0, drafted=0, failed_steps=None,
               detail=None, run_id=None, started_at=None):
    """Record that a scheduled run finished, and how it went.

    This table is what makes a MISSED run detectable. Every other signal in this
    system is emitted BY a run — the ledger, faults, drafts — so if a run never
    happens, nothing is written anywhere and silence is indistinguishable from health.
    An explicit "I ran, at this time, with this outcome" row turns the absence of a
    run into something a watcher can actually notice (see scripts/heartbeat.py).
    """
    now = _now()
    conn.execute(
        """INSERT INTO runs (client, run_id, status, sourced, drafted, failed_steps,
                             detail, started_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (client, run_id, status, int(sourced), int(drafted),
         json.dumps(list(failed_steps or [])), detail, started_at or now, now),
    )
    conn.commit()


# --- learning: critiques, lessons, and the per-run scorecard ----------------
# The drafter used to forget everything between runs. These three tables are what
# turn "it produced 10 emails" into "it is producing BETTER emails than last week".

def record_critique(conn, client, lead_email, attempt, critique, outcome,
                    run_id=None):
    """Persist one judge verdict. Never raises — losing a critique must not lose a draft."""
    try:
        score = None if critique is None else critique.get("score")
        issues = [] if critique is None else (critique.get("issues") or [])
        hint = "" if critique is None else (critique.get("fix_hint") or "")
        conn.execute(
            """INSERT INTO draft_critiques
               (client, run_id, lead_email, attempt, score, issues, fix_hint, outcome, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (client, run_id, lead_email, attempt,
             int(score) if score is not None else None,
             json.dumps([str(i) for i in issues][:6]), hint, outcome, _now()),
        )
        conn.commit()
    except Exception:
        pass


def recent_critiques(conn, client, limit=200, max_score=None):
    sql = "SELECT * FROM draft_critiques WHERE client=?"
    args = [client]
    if max_score is not None:
        sql += " AND score IS NOT NULL AND score <= ?"
        args.append(int(max_score))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return conn.execute(sql, args).fetchall()


def record_run_quality(conn, client, run_id=None, **kw):
    """Write one row of the per-run scorecard. Keys mirror the run_quality columns."""
    scores = kw.pop("scores", None) or []
    first_pass = kw.pop("first_pass_scores", None) or []
    mean = round(sum(scores) / len(scores), 2) if scores else None
    med = None
    if scores:
        s = sorted(scores)
        mid = len(s) // 2
        med = float(s[mid]) if len(s) % 2 else round((s[mid - 1] + s[mid]) / 2, 2)
    fp = round(sum(first_pass) / len(first_pass), 2) if first_pass else None
    conn.execute(
        """INSERT INTO run_quality
           (client, run_id, attempted, shipped, refused_floor, refused_citation,
            provider_failures, mean_score, median_score, first_pass_score, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (client, run_id, int(kw.get("attempted", 0)), int(kw.get("shipped", 0)),
         int(kw.get("refused_floor", 0)), int(kw.get("refused_citation", 0)),
         int(kw.get("provider_failures", 0)), mean, med, fp, _now()),
    )
    conn.commit()


def run_quality_history(conn, client, limit=20):
    """Newest-first scorecard rows — the trend behind 'is it getting better?'."""
    return conn.execute(
        "SELECT * FROM run_quality WHERE client=? ORDER BY id DESC LIMIT ?",
        (client, int(limit)),
    ).fetchall()


def propose_lesson(conn, client, lesson, evidence=None):
    """Record a PROPOSED lesson. Deliberately never sets status='approved'.

    Returns the row id, or None if an identical lesson is already on file (so a
    nightly reflection pass cannot spam the same suggestion every run).
    """
    lesson = (lesson or "").strip()
    if not lesson:
        return None
    dup = conn.execute(
        "SELECT id FROM copy_lessons WHERE client=? AND lesson=? AND status IN ('proposed','approved')",
        (client, lesson),
    ).fetchone()
    if dup:
        return None
    cur = conn.execute(
        """INSERT INTO copy_lessons (client, lesson, evidence, status, proposed_at)
           VALUES (?, ?, ?, 'proposed', ?)""",
        (client, lesson, json.dumps(evidence or {}), _now()),
    )
    conn.commit()
    return cur.lastrowid


def approved_lessons(conn, client, limit=6):
    """The ONLY lessons allowed anywhere near a prompt."""
    rows = conn.execute(
        "SELECT lesson FROM copy_lessons WHERE client=? AND status='approved' "
        "ORDER BY id DESC LIMIT ?", (client, int(limit)),
    ).fetchall()
    return [r["lesson"] for r in rows]


def list_lessons(conn, client, status=None, limit=50):
    sql = "SELECT * FROM copy_lessons WHERE client=?"
    args = [client]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return conn.execute(sql, args).fetchall()


def decide_lesson(conn, client, lesson_id, status, decided_by="operator"):
    """Approve or reject a proposed lesson. This is the human gate."""
    if status not in ("approved", "rejected"):
        raise ValueError("status must be 'approved' or 'rejected'")
    conn.execute(
        "UPDATE copy_lessons SET status=?, decided_at=?, decided_by=? WHERE id=? AND client=?",
        (status, _now(), decided_by, int(lesson_id), client),
    )
    conn.commit()


def summarize_run_quality(conn, client, run_id, provider_failures=0):
    """Derive one scorecard row from the critiques this run recorded, and store it.

    Derived rather than threaded through the pipeline on purpose: the counters would
    otherwise have to be passed down through three call layers and kept in sync at
    every early-return, which is exactly the kind of bookkeeping that silently drifts.
    The critique rows are already the truth; this just reads them.

    Returns the stored summary dict (or None when the run drafted nothing).
    """
    rows = conn.execute(
        "SELECT attempt, score, outcome FROM draft_critiques WHERE client=? AND run_id=?",
        (client, run_id),
    ).fetchall()
    if not rows:
        return None

    finals = [r for r in rows if r["attempt"] == "final"]
    scores = [r["score"] for r in rows if r["score"] is not None]
    first_pass = [r["score"] for r in rows
                  if r["attempt"] == "draft" and r["score"] is not None]
    counts = {}
    for r in finals:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1

    summary = {
        "attempted": len(finals),
        "shipped": counts.get("shipped", 0) + counts.get("shipped_unscored", 0),
        "refused_floor": counts.get("refused_floor", 0),
        "refused_citation": counts.get("refused_citation", 0),
        "provider_failures": int(provider_failures),
        "scores": scores,
        "first_pass_scores": first_pass,
    }
    record_run_quality(conn, client, run_id=run_id, **summary)
    return summary


def quality_trend(conn, client, window=5):
    """Compare the last `window` runs against the `window` before them.

    This is the answer to "is run 10 better than run 5?" — deliberately a comparison
    of two windows rather than a single number, because one run of a non-deterministic
    provider chain says nothing on its own.
    """
    rows = run_quality_history(conn, client, limit=window * 2)
    if len(rows) < 2:
        return None

    def agg(rs):
        ms = [r["mean_score"] for r in rs if r["mean_score"] is not None]
        att = sum(r["attempted"] or 0 for r in rs)
        shp = sum(r["shipped"] or 0 for r in rs)
        return {
            "runs": len(rs),
            "mean_score": round(sum(ms) / len(ms), 2) if ms else None,
            "attempted": att,
            "shipped": shp,
            "ship_rate": round(shp / att, 3) if att else None,
        }

    recent, prior = agg(rows[:window]), agg(rows[window:])
    delta_score = None
    if recent["mean_score"] is not None and prior["mean_score"] is not None:
        delta_score = round(recent["mean_score"] - prior["mean_score"], 2)
    delta_ship = None
    if recent["ship_rate"] is not None and prior["ship_rate"] is not None:
        delta_ship = round(recent["ship_rate"] - prior["ship_rate"], 3)
    return {"recent": recent, "prior": prior,
            "delta_mean_score": delta_score, "delta_ship_rate": delta_ship}


def last_run(conn, client, status=None):
    """The most recent run row (optionally filtered to a status), or None."""
    sql = "SELECT * FROM runs WHERE client=?"
    params = [client]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY finished_at DESC, id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def recent_runs(conn, client, limit=14):
    """The last N runs, newest first — for the dashboard and the heartbeat report."""
    return conn.execute(
        "SELECT * FROM runs WHERE client=? ORDER BY finished_at DESC, id DESC LIMIT ?",
        (client, int(limit)),
    ).fetchall()


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
    """Leads sitting in `status` since before `cutoff_iso` — a stall the observer flags.

    Age is measured from status_changed_at (when the lead ENTERED this state), not from
    created_at. That distinction matters for the primary case, a lead stuck in
    `queued` — meaning the drafter claimed it and never finished. Measuring from
    created_at would flag a lead banked last week and claimed this morning as stalled
    the moment it was picked up.

    Note `awaiting_approval` is deliberately NOT a state this should be called with: a
    draft waiting for a human is working exactly as intended, however long it sits.
    """
    return conn.execute(
        "SELECT * FROM leads WHERE client=? AND status=? "
        "AND COALESCE(status_changed_at, created_at) <= ? "
        "ORDER BY COALESCE(status_changed_at, created_at) ASC LIMIT ?",
        (client, status, cutoff_iso, int(limit)),
    ).fetchall()
