"""Recipient verification — can this address plausibly be delivered to?

Cold outreach lives or dies on sender reputation. A bounce rate above a few
percent gets a domain throttled or blocklisted, and by the time a bad address
reaches the sender the damage IS the bounce — nothing downstream can undo it.
So an address is checked before it is used, not after.

Two checks, cheapest first:

  1. **Syntax / placeholder** — free, offline, deterministic. Also the canonical
     home of the reserved-domain list (RFC 2606 / 6761) that core/discovery.py
     used to own. One list, one definition: two copies would drift, and a
     divergence here is invisible until it bounces.

  2. **Mail route** — one DNS query for MX, falling back to A/AAAA (RFC 5321
     §5.1 treats an address record as an implicit MX). Cached per domain.

THE RULE THAT GOVERNS THIS WHOLE MODULE: **unknown must never mean invalid.**

A DNS timeout, a missing resolver, a SERVFAIL, a network that blocks port 53 —
none of those are evidence about the recipient. They are evidence about US. The
verdict for any of them is UNKNOWN, and UNKNOWN does not block a send unless the
operator explicitly opts in (`block_on_unknown`). Getting this backwards would
be far worse than no verification at all: a laptop on a flaky network would
silently mark an entire prospect list undeliverable, and the failure would look
exactly like diligence. `dig +short` is deliberately NOT used anywhere here for
this reason — it prints nothing for "no such record" AND nothing for "resolver
died", which is precisely the ambiguity that turns unknown into invalid.

Deliberately NOT done: SMTP probing (connecting to the MX and issuing RCPT TO to
see if the mailbox itself exists). It is the only way to verify an individual
mailbox rather than its domain, but it is intrusive, widely rate-limited, often
answered with a catch-all "yes" that means nothing, and being seen doing it can
itself damage the reputation we are protecting. So the honest claim this module
makes is domain-level: "mail to this domain has somewhere to go", not "this
person exists".
"""

import re
import socket
import subprocess

OK = "ok"              # there is a route for mail to this domain
INVALID = "invalid"     # definitively undeliverable — refuse it
UNKNOWN = "unknown"     # we could not find out; NOT a negative verdict


# Domains that can never receive mail: RFC 2606 / RFC 6761 reserve them so
# examples and docs can't accidentally hit a real inbox. A scraper will happily
# hand one back — on 2026-09-13 "JMJ PROPERTIES" was banked as
# contact@example.com, an address guaranteed to hard-bounce.
PLACEHOLDER_DOMAINS = (
    "example.com", "example.org", "example.net", "example.edu",
    "test.com", "test.org", "test.net",
    "invalid", "localhost", "domain.com", "email.com",
    "yourdomain.com", "yourcompany.com", "sentry.io", "wixpress.com",
)


def clean(email):
    """Normalise a scraped address: strip mailto:, brackets, trailing punctuation.

    The trailing dot is legal in DNS but is a scrape artifact far more often than
    intent ("a@b.com." from end-of-sentence punctuation). Stripping it makes the
    domain compare cleanly against the reserved list — otherwise a placeholder
    slips through as "example.com." and still hard-bounces.
    """
    if not email:
        return ""
    e = str(email).strip()
    if e.lower().startswith("mailto:"):
        e = e[len("mailto:"):]
    e = e.strip().strip("<>").strip().rstrip(".,;:>)")
    return e[:-1] if e.endswith(".") else e


def domain_of(email):
    """The lowercased domain part, or "" if there isn't one."""
    e = clean(email)
    if "@" not in e:
        return ""
    return e.rpartition("@")[2].strip().lower().strip(".")


def is_placeholder(email):
    """True if `email` is a PRESENT address that can never receive mail.

    Absent/blank is deliberately NOT placeholder — that distinction is
    load-bearing. A business with no public email is a legitimate lead when
    require_email=False (it stays on the list for enrichment later), and the
    require_email gate already decides what to do with it. Folding "no email"
    in here would silently drop those businesses.

    Beyond that, deliberately narrow: a handful of reserved/boilerplate domains
    plus the obvious malformed cases. A broader "looks fake" heuristic would
    start rejecting real small-business addresses, and a missed lead is
    invisible while a bounce is not.
    """
    if email is None or not str(email).strip():
        return False                                  # absent, not fake
    e = clean(email)
    if "@" not in e:
        return True
    local, _, domain = e.rpartition("@")
    if not local.strip():
        return True                                   # "@host.com" has no recipient
    domain = domain.strip().lower().strip(".")
    if not domain or "." not in domain:
        return True                                   # no TLD: cannot resolve
    if domain in PLACEHOLDER_DOMAINS:
        return True
    return any(domain.endswith("." + d) for d in PLACEHOLDER_DOMAINS)


def check_syntax(email):
    """(status, reason) from the offline checks alone. Never returns UNKNOWN —
    syntax is decidable without a network."""
    if email is None or not str(email).strip():
        return INVALID, "no address"
    e = clean(email)
    if is_placeholder(e):
        return INVALID, "placeholder or unroutable address"
    # One more shape check beyond is_placeholder: whitespace or a second @ make
    # an address unusable in a header even when the domain looks fine.
    if e.count("@") != 1 or re.search(r"\s", e):
        return INVALID, "malformed address"
    return OK, "syntax ok"


# --- DNS ------------------------------------------------------------------
# `dig` is used rather than a library because it is present on both the laptop
# and the box, and adding dnspython for three queries is a dependency we would
# carry forever (see the no-heavy-frameworks rule in the engineering params).

def _dig(domain, qtype, timeout):
    """Ask DNS one question. Returns (rcode, [answer lines]).

    rcode is None when we could not ask at all — no dig binary, a timeout, a
    blocked resolver. That is the UNKNOWN signal and callers must treat it as
    "no information", never as "no record".
    """
    t = max(1, int(timeout))
    try:
        p = subprocess.run(
            ["dig", f"+time={t}", "+tries=1", "+noall", "+comments", "+answer",
             qtype, domain],
            capture_output=True, text=True, timeout=t + 3,
        )
    except (OSError, subprocess.SubprocessError):
        return None, []                    # no dig, or it hung: we learned nothing
    out = p.stdout or ""
    m = re.search(r"status:\s*([A-Z]+)", out)
    if not m:
        # No header at all => "connection timed out; no servers could be reached".
        return None, []
    answers = [
        ln for ln in out.splitlines()
        if not ln.startswith(";") and re.search(r"\sIN\s+%s\s" % qtype, ln)
    ]
    return m.group(1), answers


def _is_null_mx(answers):
    """RFC 7505: a single `MX 0 .` is an explicit declaration that the domain
    accepts NO mail. It is a real MX record, so a naive "any MX => deliverable"
    check passes it — example.com publishes exactly this."""
    if len(answers) != 1:
        return False
    return re.search(r"\sIN\s+MX\s+\d+\s+\.\s*$", answers[0]) is not None


def resolve_mail_route(domain, timeout=5):
    """(status, reason) for whether mail to `domain` has anywhere to go."""
    if not domain or "." not in domain:
        return INVALID, "no resolvable domain"

    rcode, answers = _dig(domain, "MX", timeout)
    if rcode is None:
        return UNKNOWN, "DNS unavailable (no resolver, timeout, or dig missing)"
    if rcode == "NXDOMAIN":
        return INVALID, "domain does not exist (NXDOMAIN)"
    if rcode != "NOERROR":
        return UNKNOWN, f"DNS returned {rcode}"
    if answers:
        if _is_null_mx(answers):
            return INVALID, "null MX (RFC 7505): domain declares it accepts no mail"
        return OK, f"{len(answers)} MX record(s)"

    # No MX. RFC 5321 §5.1: fall back to the address record as an implicit MX.
    for qt in ("A", "AAAA"):
        arcode, aanswers = _dig(domain, qt, timeout)
        if arcode is None:
            return UNKNOWN, f"no MX; {qt} lookup unavailable"
        if aanswers:
            return OK, f"no MX, but an {qt} record accepts mail (implicit MX)"
        if arcode not in ("NOERROR", "NXDOMAIN"):
            return UNKNOWN, f"no MX; {qt} lookup returned {arcode}"
    return INVALID, "domain has no MX and no A/AAAA record — no route for mail"


def _resolve_via_stdlib(domain):
    """Last-resort A-record check when dig is missing entirely.

    Can only ever CONFIRM a route (an address resolves), never deny one: stdlib
    gives us no way to see MX records or to tell NXDOMAIN from a resolver
    failure, so a miss here is UNKNOWN, not INVALID.
    """
    try:
        socket.getaddrinfo(domain, None)
        return OK, "address record resolves (stdlib fallback; MX not checked)"
    except Exception:
        return UNKNOWN, "stdlib resolution failed (cannot distinguish absent from unreachable)"


# --- the public verdict ----------------------------------------------------

def verify_email(email, conn=None, timeout=5, ttl_days=30, invalid_ttl_days=7,
                 use_cache=True):
    """Verify one address. Returns
        {"email", "domain", "status", "reason", "cached"}

    `conn` is an open state DB; pass it to use the per-domain cache (MX rarely
    changes and a run re-checks the same domains constantly). UNKNOWN is never
    cached: it is a statement about a moment's network, and freezing it would
    turn a five-second outage into a multi-week verdict.
    """
    from . import state                       # local: keeps this module importable bare

    e = clean(email)
    status, reason = check_syntax(e)
    if status == INVALID:
        return {"email": e, "domain": domain_of(e), "status": INVALID,
                "reason": reason, "cached": False}

    domain = domain_of(e)
    if conn is not None and use_cache:
        hit = state.get_mx_check(conn, domain, ttl_days=ttl_days,
                                 invalid_ttl_days=invalid_ttl_days)
        if hit:
            return {"email": e, "domain": domain, "status": hit["status"],
                    "reason": hit["reason"], "cached": True}

    status, reason = resolve_mail_route(domain, timeout=timeout)
    if status == UNKNOWN and "dig missing" in reason:
        status, reason = _resolve_via_stdlib(domain)

    if conn is not None and use_cache and status in (OK, INVALID):
        try:
            state.record_mx_check(conn, domain, status, reason)
        except Exception:
            pass                              # a cache write must never fail a check
    return {"email": e, "domain": domain, "status": status, "reason": reason,
            "cached": False}


def should_block(verdict, block_on_unknown=False):
    """Does this verdict stop a send? INVALID always; UNKNOWN only on request.

    The default is the whole point of the module: we refuse what we have proven
    undeliverable and we get out of the way for everything else.
    """
    if verdict["status"] == INVALID:
        return True
    return bool(block_on_unknown) and verdict["status"] == UNKNOWN
