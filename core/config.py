"""Per-client configuration — the single seam that makes this multi-client.

Reads clients/<name>/config.json, resolves credential *names* to values from the
environment (secrets never live in config), applies defaults, and anchors every
path off this file's location so nothing depends on the current directory.

    from core import config
    cfg = config.load_client()          # active client (CLIENT env, default "ejentic")
    cfg = config.load_client("acme")    # a specific client
"""

import os
import json
import hashlib
from pathlib import Path

from dotenv import load_dotenv

# --- Paths, anchored to the project root (this file is <root>/core/config.py) ---
ROOT = Path(__file__).resolve().parent.parent
CLIENTS_DIR = ROOT / "clients"
DATA_DIR = ROOT / "data"

# Load .env from the project root once, regardless of the working directory.
load_dotenv(ROOT / ".env")


class ConfigError(Exception):
    """Raised when a client config is missing, malformed, or missing credentials."""


DEFAULTS = {
    "client_name": "",
    "from_name": "",
    "reply_to": None,
    "physical_address": "",
    "unsubscribe_base_url": "http://localhost:5001",
    "copy_provider": "gemini",              # legacy hint; superseded by "providers"
    "providers": ["freellmapi", "gemini", "nvidia"],  # LLM fallback order (see core/ai.py)
    "freellmapi_model": "",                  # "" / "auto" = let the gateway auto-route
    "gemini_model": "gemini-2.5-flash",
    "nvidia_model": "mistralai/mistral-nemotron",
    "copy": {                                # premium-copy controls (see core/budget.py)
        "premium": False,                    # True = try real Claude (Anthropic API) FIRST for copy
        "anthropic_model": "claude-opus-4-8",  # which Claude writes the copy when premium is on
        "premium_daily_usd_cap": 5.0,        # hard $/day cap on real-Claude spend; null = unlimited
        "quality_gate": {                    # LLM-as-judge: score every draft, rewrite weak ones
            "enabled": True,
            "min_score": 8,                  # 1-10; drafts below this get a rewrite pass
            "max_revisions": 1,              # rewrite passes before accepting the best so far
            "best_of": 2,                    # draft N candidates, keep the highest-scoring (more robust on a flaky free chain than a longer serial revise chain)
        },
    },
    "demo_mode": False,                      # False = real AI generation
    "lead_source": "csv",                    # "csv" (curated file) | "maps_firecrawl" | "yellowpages" (auto-discovery)
    "discovery": {                           # used only when lead_source == "maps_firecrawl"
        "queries": [],                       # flat Maps queries (fallback if no segments)
        "segments": [],                      # ICP segments: [{name, service, queries:[...]}, ...]
                                             #   each lead is tagged with the segment's `service`
                                             #   so the strategist pitches the right offering
        "max_results": 10,                   # businesses per query (Maps allows 1-20)
        "max_leads": 25,                     # overall cap on leads returned per run
        "require_email": True,               # skip businesses with no discoverable public email
        "scrape_pages": ["", "contact"],     # paths to try (homepage first; stops once an email is found)
        "max_workers": 3,                    # concurrent Composio calls (higher => more rate-limit hits)
        "maps_search_slug": "GOOGLE_MAPS_TEXT_SEARCH",
        "firecrawl_scrape_slug": "FIRECRAWL_SCRAPE",
    },
    "yellowpages": {                         # used only when lead_source == "yellowpages"
        "search_url_template": "https://www.yellowpages.com/search?search_terms={query}&geo_location_terms={location}",
        "location": "",                      # geographic filter, e.g. "Lagos" (blank = national)
        "queries": [],                       # flat YP searches (fallback if no segments)
        "segments": [],                      # ICP segments: [{name, service, queries:[...]}, ...]
                                             #   each lead is tagged with the segment's `service`
        "max_listings_per_query": 20,        # businesses to take from each results page
        "max_leads": 25,                     # overall cap on leads returned per run
        "require_email": True,               # skip businesses with no discoverable public email
        "scrape_pages": ["", "contact"],     # paths to try on each business site (homepage first)
        "max_workers": 6,                    # concurrent Composio (Firecrawl) calls
        "firecrawl_scrape_slug": "FIRECRAWL_SCRAPE",
    },
    "timezone": "UTC",                        # IANA tz for proposing/booking meetings
    "booking": {                              # Tier 2: book a real meeting on an approved 'interested' reply
        "enabled": True,                      # False = still reply, just don't create a calendar event
        "provider": "composio_googlecalendar",
        "default_duration_min": 30,
        "default_service": "",                # fallback service label when a lead has no niche tag
        "action_slug": "GOOGLECALENDAR_CREATE_EVENT",
    },
    "reminders": {                            # Tier 2.5: pre-meeting reminders to the operator (see scripts/reminder_agent.py)
        "enabled": True,                      # False = the reminder agent does nothing
        "lead_times_min": [60, 30, 15],       # minutes-before to remind; most-urgent unsent one fires per pass
        "poll_seconds": 60,                   # how often the agent re-checks the appointments list
        "email": True,                        # email the operator's own inbox
        "desktop": True,                      # show a macOS desktop banner (osascript; best-effort)
    },
    "observability": {                        # self-healing + accounting ledger (see core/observability.py)
        "enabled": True,                      # False = pipeline still runs, just isn't tracked
        "poll_seconds": 60,                   # how often the observer agent scans the ledger
        "stall_minutes": {                    # a lead stuck in a state longer than this => a stall to heal
            "queued": 120,                    # queued but never sent
            "replied": 240,                   # replied but not yet actioned
        },
        "max_heal_attempts": 3,               # tries before the healer escalates to a human
        "escalate_email": True,               # email the operator when a fault can't be auto-healed
        "cost": {                             # NOTIONAL pricing model (real providers are free)
            "rate_per_million_input": 0.0,    # $ per 1M input tokens at your chosen reference rate
            "rate_per_million_output": 0.0,   # $ per 1M output tokens at your chosen reference rate
            "margin_multiplier": 1.0,         # markup applied to reference cost => client price
        },
    },
    "sending": {
        "mode": "controlled",                # "controlled" (redirect to safe inbox) | "live"
        "controlled_inbox_env": "SMTP_USER", # where sends go in controlled mode
        "daily_global_cap": 50,
        "throttle_seconds": {"min": 20, "max": 60},
        "mailboxes": [],                     # see _resolve_mailbox for the shape
    },
    "circuit_breaker": {                      # skip a provider that keeps failing instead of
        "enabled": True,                      # re-paying its timeout/retries on every call (core/ai.py)
        "threshold": 3,                       # consecutive failures before the provider "trips"
        "cooldown_secs": 60,                  # seconds to skip a tripped provider before one retrial
    },
    "service_outcomes": {},                   # per-service STANDARD outcome promise, keyed by the
                                              # lead's matched service. Pins the pitch's headline
                                              # number so it's identical across every email for a
                                              # service (no more "15-25" one run, "100" the next).
                                              # Empty => strategist falls back to a free "plausible number".
    "target_niches": [],
}


def active_client():
    """The client to operate on: the CLIENT env var, or 'ejentic' by default."""
    return os.environ.get("CLIENT", "ejentic")


def _env(key):
    return os.environ.get(key) if key else None


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_mailbox(m, client):
    """Turn a config mailbox into a ready-to-use one, pulling secrets from env.

    Config shape (creds are ALWAYS env references, never literals):
        {"smtp_host","smtp_port","user_env","pass_env","daily_cap",
         "imap_host","imap_port" (optional; default imap.gmail.com:993 — Tier 2 reply reading),
         "address"|"address_env" (optional; defaults to the resolved user)}
    """
    user = _env(m.get("user_env"))
    password = _env(m.get("pass_env"))
    if not user or not password:
        raise ConfigError(
            f"[{client}] mailbox is missing credentials — set '{m.get('user_env')}' "
            f"and '{m.get('pass_env')}' in your .env"
        )
    address = m.get("address") or _env(m.get("address_env")) or user
    return {
        "address": address,
        "smtp_host": m.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": int(m.get("smtp_port", 587)),
        "imap_host": m.get("imap_host", "imap.gmail.com"),   # reply reading reuses these creds
        "imap_port": int(m.get("imap_port", 993)),
        "user": user,
        "password": password,            # in-memory only; never logged
        "daily_cap": int(m.get("daily_cap", 40)),
    }


def load_client(name=None):
    name = name or active_client()
    cfg_path = CLIENTS_DIR / name / "config.json"
    if not cfg_path.exists():
        raise ConfigError(f"No config for client '{name}': expected {cfg_path}")

    with open(cfg_path, encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"[{name}] config.json is not valid JSON: {e}")

    cfg = _deep_merge(DEFAULTS, raw)
    cfg["client"] = name
    cfg["paths"] = {
        "root": str(ROOT),
        "db": str(DATA_DIR / name / "state.sqlite"),
        "leads_csv": str(CLIENTS_DIR / name / "leads.csv"),
    }
    cfg["sending"]["mailboxes"] = [
        _resolve_mailbox(m, name) for m in cfg["sending"].get("mailboxes", [])
    ]
    cfg["sending"]["controlled_inbox"] = _resolve_controlled_inbox(cfg)
    _validate(cfg)
    return cfg


def _resolve_controlled_inbox(cfg):
    env_key = cfg["sending"].get("controlled_inbox_env")
    resolved = _env(env_key)
    if resolved:
        return resolved
    mailboxes = cfg["sending"].get("mailboxes") or []
    return mailboxes[0]["address"] if mailboxes else None


def _validate(cfg):
    client = cfg["client"]
    if not cfg["sending"]["mailboxes"]:
        raise ConfigError(f"[{client}] no sending mailboxes configured")
    if cfg["copy_provider"] not in ("gemini", "nvidia"):
        raise ConfigError(f"[{client}] copy_provider must be 'gemini' or 'nvidia'")
    if cfg["sending"]["mode"] not in ("controlled", "live"):
        raise ConfigError(f"[{client}] sending.mode must be 'controlled' or 'live'")

    # CAN-SPAM needs a real postal address; warn loudly but don't block the demo.
    addr = (cfg.get("physical_address") or "").strip()
    if not addr or "REPLACE-ME" in addr.upper():
        _warn(f"[{client}] physical_address is not set — required by CAN-SPAM before "
              f"sending to real prospects. Edit clients/{client}/config.json.")
    if cfg["sending"]["mode"] == "live":
        _warn(f"[{client}] sending.mode is 'live' — emails will go to REAL prospects. "
              f"Ensure a warmed sending domain, SPF/DKIM/DMARC, and verification are in place.")


def _warn(msg):
    import sys
    print(f"⚠️  {msg}", file=sys.stderr)


def unsub_secret():
    """Stable secret for signing unsubscribe tokens.

    Uses UNSUB_SECRET if set (recommended for production); otherwise derives a
    stable, non-guessable secret from existing credentials so the demo works
    with zero new configuration.
    """
    explicit = os.environ.get("UNSUB_SECRET")
    if explicit:
        return explicit.encode("utf-8")
    basis = (os.environ.get("SMTP_USER", "") + "|" + os.environ.get("SMTP_PASS", "")).encode("utf-8")
    return hashlib.sha256(basis).digest()
