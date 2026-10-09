"""Domain warm-up sender — earn sending reputation for a cold outreach domain.

    CLIENT=ejentic venv/bin/python scripts/warmup_send.py            # send today's batch
    CLIENT=ejentic venv/bin/python scripts/warmup_send.py --dry-run  # show the plan, send nothing
    CLIENT=ejentic venv/bin/python scripts/warmup_send.py --status   # day / cap / sent / left
    CLIENT=ejentic venv/bin/python scripts/warmup_send.py --once     # send exactly ONE (smoke test)
    CLIENT=ejentic venv/bin/python scripts/warmup_send.py --max 2     # lower today's cap by hand

WHAT THIS IS — and the boundary it must never cross
----------------------------------------------------
A brand-new sending domain has no reputation, so mailbox providers default it to
spam even when SPF/DKIM/DMARC all PASS. We observed exactly that with
tryejentic.xyz (Oct 2026): perfect auth, 2048-bit DKIM, still filed as spam.
Auth proves identity; it does not prove you are wanted. The only fix is
reputation, and reputation is earned by real inboxes ENGAGING with your mail —
opening it, replying, dragging it out of spam.

So this script sends a SMALL, slowly-growing number of plain, personal notes to a
SEED LIST of inboxes the operator controls or has explicit consent from, so those
humans can engage and teach the providers that the domain is legitimate.

THIS IS NOT "GOING LIVE". It is categorically different from the prospect
pipeline, and it keeps the "never email a stranger" fuse fully intact:
  * It NEVER reads leads, the prospect `sends` table, the suppression list, or
    `sending.mode`. The prospect pipeline stays in whatever mode it is (controlled)
    and is completely untouched by anything in this file.
  * It will ONLY ever send to addresses that appear in the seed file, which only
    the operator populates, with consenting recipients.
  * It rides `core.sender.smtp_deliver` directly — the same raw primitive the
    operator-mail paths (heartbeat, digest) use — NOT `SendingPool.send`. That is
    deliberate: SendingPool would redirect to the controlled inbox (useless for
    warming a diverse set of inboxes) and would log to the prospect `sends` table
    (polluting the live-send accounting that protects the real business identity).

Because this is personal 1:1 mail to people who agreed to receive it, it carries
NO unsubscribe footer and NO marketing CTA. Adding those would make it look like
the bulk mail we are specifically trying NOT to look like.

STATE lives in a plain JSONL log at data/<client>/warmup_log.jsonl (gitignored,
machine-local). We deliberately do NOT reuse core.state's `sends` table: warm-up
is not a prospect send, and the box's state schema drifts, so a self-contained log
cannot be broken by that drift.

WHERE TO RUN IT
---------------
On the always-on box. The Mac's network DPI blocks SMTP on 587; the box sends
fine (same reason the prospect pipeline runs there).
"""

import warnings
warnings.filterwarnings('ignore')

import os
import sys
import re
import json
import time
import html
import random
import argparse
from datetime import datetime, timezone
from email.utils import make_msgid, formatdate
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Make the project root importable so `core` resolves regardless of the CWD.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core import config                         # noqa: E402
from core import sender                         # noqa: E402


# A conservative default ramp for a cold domain, used when the client config has
# no `sending.warmup_seed.ramp`. These are MECHANICS defaults (a safe starting
# shape for any new domain), not a tenant's business numbers — a client tunes the
# real values in its config, exactly like the prospect `sending.warmup.ramp`.
# It is intentionally slower than the prospect ramp (5/10/20/30/40): a novelty
# TLD like .xyz starts in a deeper hole and should open up later, not sooner.
DEFAULT_RAMP = [
    {"through_day": 7,  "cap": 3},
    {"through_day": 14, "cap": 6},
    {"through_day": 21, "cap": 10},
    {"through_day": 30, "cap": 15},
    {"through_day": 45, "cap": 25},
]

# Last-resort backstop: never put more than this many warm-up messages on the
# wire in a single invocation, no matter what the ramp or --max say. Defends
# against a config typo (a ramp step of 5000) turning a warm-up into a blast.
HARD_INVOCATION_CAP = 50

# Default pacing between sends within one run. Warm-up must look like a human at a
# keyboard, not a cannon — Google's own guidance is to avoid sending in bursts.
DEFAULT_SPACING = {"min": 90, "max": 420}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Varied, honest, reply-prompting content. The recipients are consenting helpers,
# so the copy does not need to deceive THEM — it needs to (a) look like ordinary
# personal mail to a spam filter and (b) prompt a reply, because a reply is the
# single strongest reputation signal. Variety matters: the same template sent to
# ten people pattern-matches as bulk, so we draw a random subject + body each time.
SUBJECTS = [
    "quick favour",
    "you around this week?",
    "mind a quick reply?",
    "testing a new address",
    "hello from my new email",
    "quick one when you get a sec",
    "could you reply to this?",
    "two-second favour",
]

BODIES = [
    "Hi {name},\n\nI'm setting up a new work email and making sure it actually "
    "reaches people. Could you hit reply with a word or two when you get a chance? "
    "That's genuinely all I need.\n\nThanks a lot,\n{me}",

    "Hey {name},\n\nQuick one — I'm breaking in this new address. If this landed "
    "in your inbox (and not spam), a one-line reply would really help me out.\n\n"
    "Appreciate it,\n{me}",

    "{name},\n\nTesting my new email today. Mind replying with anything at all so "
    "I know it's getting through? And if it turned up in spam, dragging it to your "
    "inbox would be a big help.\n\nCheers,\n{me}",

    "Hi {name},\n\nHope you're keeping well. I'm sorting out a new email setup and "
    "need a few real replies to get it trusted by the mail providers. A quick note "
    "back whenever suits — no rush at all.\n\nThank you,\n{me}",

    "Hey {name},\n\nFavour to ask: could you reply to this one when you can? I'm "
    "warming up a new address and your reply tells the email system it's legit. "
    "Takes two seconds.\n\nMuch appreciated,\n{me}",

    "Hi {name},\n\nJust checking this new address works properly. If you could fire "
    "back a short reply — and mark it 'not spam' if it hid in there — I'd owe you "
    "one.\n\nThanks,\n{me}",
]

# Local-parts that are clearly roles, not people, so we greet them "there" rather
# than "Hi Info,". Not exhaustive; just the obvious ones.
_ROLE_LOCALPARTS = {
    "info", "hello", "hi", "contact", "team", "admin", "mail", "sales",
    "support", "office", "no-reply", "noreply", "help", "service",
}


# --- paths -----------------------------------------------------------------

def seeds_path(client):
    """Where the operator lists consenting warm-up recipients (gitignored)."""
    return os.path.join(ROOT, "clients", client, "warmup_seeds.txt")


def log_path(client):
    """The warm-up send log — next to the client's state DB (data/<client>/).

    Derived from config.client_db_path so it lands in the same per-client data
    dir on every machine, without depending on the state schema itself.
    """
    return os.path.join(os.path.dirname(config.client_db_path(client)),
                        "warmup_log.jsonl")


# --- seed list + log I/O ---------------------------------------------------

def load_seeds(path, warn=lambda m: None):
    """Parse the seed file: one address per line, '#' comments and blanks ignored.

    Accepts a bare address or a "Name <addr>" form. Invalid lines are skipped
    with a warning (never silently) so a typo cannot send to a malformed address.
    De-duplicates case-insensitively, preserving first-seen order.
    """
    if not os.path.exists(path):
        return []
    seen, out = set(), []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            addr = line
            if "<" in addr and ">" in addr:
                addr = addr[addr.find("<") + 1:addr.find(">")].strip()
            if not EMAIL_RE.match(addr):
                warn(f"skipping invalid seed line: {raw.strip()!r}")
                continue
            key = addr.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(addr)
    return out


def read_log(path):
    """Read the JSONL warm-up log into a list of dicts. Missing file => []."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass          # a corrupt line must not crash the whole run
    return out


def append_log(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# --- pure ramp / scheduling logic (fully unit-testable, no I/O) ------------

def warmup_day(log_entries, today):
    """1-based warm-up day. Day 1 = the first day a warm-up actually went out.

    Counts only successful sends. Independent of the prospect pipeline's own
    'first LIVE send' clock — warming the domain and emailing prospects are
    different activities with different start dates.
    """
    dates = [e["date"] for e in log_entries if e.get("ok") and e.get("date")]
    if not dates:
        return 1
    try:
        first = min(datetime.fromisoformat(d).date() for d in dates)
    except Exception:
        return 1          # an unparseable date must not unlock a higher day
    return max(1, (today - first).days + 1)


def cap_for_day(ramp, day, hard_cap):
    """Today's warm-up cap. Mirrors the prospect ramp's contract: the ramp can
    only ever hold volume DOWN — a step above hard_cap is clamped, so a config
    typo cannot silently widen the tap past the mailbox's own daily_cap."""
    for step in sorted(ramp, key=lambda r: int(r.get("through_day", 0))):
        if day <= int(step.get("through_day", 0)):
            return max(0, min(hard_cap, int(step.get("cap", hard_cap))))
    return hard_cap          # past the last step: fully warmed


def sent_today(log_entries, today):
    """How many warm-up messages successfully went out today."""
    ds = today.isoformat()
    return sum(1 for e in log_entries if e.get("ok") and e.get("date") == ds)


def choose_recipients(seeds, log_entries, n, rng):
    """Pick up to `n` seeds, least-recently-contacted first, ties broken randomly.

    Spreads warm-up across the whole seed list over time instead of hammering
    whoever sits at the top, while the random tiebreak keeps the daily pattern
    from looking mechanical. Recipients can ONLY come from `seeds`.
    """
    if n <= 0 or not seeds:
        return []
    last = {}
    for e in log_entries:
        if e.get("ok") and e.get("to"):
            addr, d = e["to"].lower(), e.get("date", "")
            if addr not in last or d > last[addr]:
                last[addr] = d
    pool = list(seeds)
    rng.shuffle(pool)                                   # randomise ties...
    pool.sort(key=lambda a: last.get(a.lower(), ""))    # ...then oldest-first (stable)
    return pool[:n]


# --- message composition ---------------------------------------------------

def _greeting_name(addr):
    """Best-effort first name from an address local-part, else 'there'."""
    local = addr.split("@")[0]
    if local.lower() in _ROLE_LOCALPARTS:
        return "there"
    m = re.match(r"[A-Za-z]+", local)
    if not m or len(m.group(0)) < 2:
        return "there"
    return m.group(0).capitalize()


def compose(recipient, from_name, rng):
    """A varied (subject, body_text) for one warm-up message."""
    name = _greeting_name(recipient)
    me = (from_name or "").split()[0] if from_name else ""
    subject = rng.choice(SUBJECTS)
    body = rng.choice(BODIES).format(name=name, me=me or from_name or "there")
    return subject, body


def build_message(from_name, from_addr, to_addr, subject, text):
    """Build a plain, personal multipart/alternative message. Returns (str, msgid)."""
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)      # real mail has a Date header
    domain = from_addr.split("@")[-1] if "@" in from_addr else None
    mid = make_msgid(domain=domain)               # Message-ID on our own domain
    msg["Message-ID"] = mid
    msg.attach(MIMEText(text, "plain", "utf-8"))
    html_body = ('<html><body style="font-family:Arial,Helvetica,sans-serif;'
                 'font-size:14px;color:#222;line-height:1.5;">'
                 + html.escape(text).replace("\n", "<br>")
                 + '</body></html>')
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg.as_string(), mid


# --- the run ---------------------------------------------------------------

def plan(cfg, seeds, log_entries, now, rng, limit=None):
    """Compute today's warm-up plan without sending. Returns a dict."""
    sconf = cfg["sending"]
    mb = sconf["mailboxes"][0]
    ws = sconf.get("warmup_seed") or {}
    ramp = ws.get("ramp") or DEFAULT_RAMP
    hard_cap = int(mb.get("daily_cap", 40))

    today = now.date()
    day = warmup_day(log_entries, today)
    cap = cap_for_day(ramp, day, hard_cap)
    used = sent_today(log_entries, today)
    left = max(0, cap - used)
    if limit is not None:
        left = min(left, max(0, int(limit)))
    left = min(left, HARD_INVOCATION_CAP)

    recipients = choose_recipients(seeds, log_entries, left, rng)
    return {
        "enabled": bool(ws.get("enabled", True)),
        "from_addr": mb["address"],
        "day": day,
        "cap_today": cap,
        "used_today": used,
        "left": left,
        "seeds": len(seeds),
        "recipients": recipients,
        "spacing": ws.get("spacing_seconds") or DEFAULT_SPACING,
    }


def run(cfg, seeds, log_entries, *, deliver, now, rng,
        dry_run=False, limit=None, sleep_fn=None, record_fn=None, log=print):
    """Send today's warm-up batch. Pure dependencies are injected so this is
    testable with no network: `deliver` (defaults to the real SMTP primitive),
    `now`, `rng`, `sleep_fn`, and `record_fn` (persist one log entry).

    Returns a summary dict. Raises nothing for a single failed send — it records
    the failure and moves on, so one bad address cannot abort the whole batch.
    """
    sleep_fn = sleep_fn or time.sleep
    record_fn = record_fn or (lambda e: None)
    sconf = cfg["sending"]
    mb = sconf["mailboxes"][0]
    from_name = cfg.get("from_name") or cfg.get("client_name", "")

    p = plan(cfg, seeds, log_entries, now, rng, limit=limit)

    if not p["enabled"]:
        log("Seed warm-up is disabled for this client (sending.warmup_seed.enabled=false).")
        return {"sent": 0, "failed": 0, "skipped": "disabled", **p}

    log(f"Warm-up day {p['day']} · cap {p['cap_today']} · used {p['used_today']} · "
        f"sending {len(p['recipients'])} of {p['seeds']} seeds "
        f"from {p['from_addr']}{'  [DRY RUN]' if dry_run else ''}")

    if not p["recipients"]:
        if p["seeds"] == 0:
            log("  No seeds configured — nothing to send.")
        else:
            log("  Nothing to send: today's cap is already used up.")
        return {"sent": 0, "failed": 0, **p}

    host, port = mb["smtp_host"], mb["smtp_port"]
    user, password = mb["user"], mb["password"]
    from_addr = mb["address"]
    lo = float(p["spacing"].get("min", DEFAULT_SPACING["min"]))
    hi = float(p["spacing"].get("max", DEFAULT_SPACING["max"]))

    sent = failed = 0
    for i, to_addr in enumerate(p["recipients"]):
        subject, text = compose(to_addr, from_name, rng)
        if dry_run:
            log(f"  would send → {to_addr}   subj: {subject!r}")
            continue
        msg_string, mid = build_message(from_name, from_addr, to_addr, subject, text)
        entry = {
            "ts": now.isoformat(),
            "date": now.date().isoformat(),
            "to": to_addr,
            "subject": subject,
            "message_id": mid,
        }
        try:
            deliver(host, port, user, password, from_addr, to_addr, msg_string)
            entry["ok"] = True
            record_fn(entry)
            sent += 1
            log(f"  ✅ sent → {to_addr}")
        except Exception as e:                       # noqa: BLE001
            entry["ok"] = False
            entry["error"] = str(e)
            record_fn(entry)
            failed += 1
            log(f"  ❌ failed → {to_addr}: {e}")
        if i < len(p["recipients"]) - 1:
            sleep_fn(rng.uniform(lo, hi))

    log(f"Done: {sent} sent, {failed} failed.")
    return {"sent": sent, "failed": failed, **p}


# --- CLI -------------------------------------------------------------------

def _print_status(p, log=print):
    log("Seed warm-up status")
    log(f"  from address : {p['from_addr']}")
    log(f"  seeds listed : {p['seeds']}")
    log(f"  warm-up day  : {p['day']}")
    log(f"  cap today    : {p['cap_today']}")
    log(f"  used today   : {p['used_today']}")
    log(f"  left today   : {p['left']}")
    if not p["enabled"]:
        log("  NOTE: warm-up is DISABLED (sending.warmup_seed.enabled=false).")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Send a small, ramped batch of personal warm-up emails to a "
                    "consenting seed list, to build sending reputation. Never "
                    "touches the prospect pipeline.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show today's plan and recipients; send nothing")
    ap.add_argument("--status", action="store_true",
                    help="print day/cap/used/left and exit")
    ap.add_argument("--once", action="store_true",
                    help="send exactly one message (smoke test); implies no wait")
    ap.add_argument("--max", type=int, default=None,
                    help="lower today's cap to at most this many (never raises it)")
    ap.add_argument("--no-wait", action="store_true",
                    help="skip the pacing delay between sends (for testing)")
    args = ap.parse_args(argv)

    try:
        cfg = config.load_client()
    except config.ConfigError as e:
        print(f"✗ {e}")
        print("  (Set OUTREACH_SMTP_USER / OUTREACH_SMTP_PASS in .env — the Zoho app password.)")
        return 2

    client = cfg["client"]
    spath = seeds_path(client)
    lpath = log_path(client)

    seeds = load_seeds(spath, warn=lambda m: print(f"  ⚠️  {m}"))
    log_entries = read_log(lpath)
    now = datetime.now(timezone.utc)
    rng = random.Random()

    if not seeds and not args.status:
        print(f"✗ No warm-up seeds found at {os.path.relpath(spath, ROOT)}")
        print("  Create that file with one consenting email address per line")
        print(f"  (see {os.path.relpath(spath, ROOT)}.template for the format).")
        return 2

    if args.status:
        _print_status(plan(cfg, seeds, log_entries, now, rng))
        return 0

    limit = 1 if args.once else args.max
    sleep_fn = (lambda s: None) if (args.no_wait or args.once or args.dry_run) else time.sleep

    summary = run(
        cfg, seeds, log_entries,
        deliver=sender.smtp_deliver,
        now=now, rng=rng,
        dry_run=args.dry_run,
        limit=limit,
        sleep_fn=sleep_fn,
        record_fn=lambda e: append_log(lpath, e),
    )
    return 0 if summary.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
