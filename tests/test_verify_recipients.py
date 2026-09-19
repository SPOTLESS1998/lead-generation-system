"""Offline tests for core/verify.py + the send-path gate — NO network by default.

The governing rule of the module under test is **unknown must never mean
invalid**, and most of this file exists to hold that line. Getting it backwards
would be worse than having no verification at all: a laptop on a flaky network
would mark an entire prospect list undeliverable, and the failure would look
exactly like diligence.

DNS is stubbed at the `_dig` seam so every case — NXDOMAIN, SERVFAIL, null MX,
timeout — is reproducible without a resolver. A handful of REAL lookups run at
the end, and they are opt-in (VERIFY_LIVE_DNS=1) so the suite stays green on a
machine with no network.

Run:  venv/bin/python tests/test_verify_recipients.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import state, verify                          # noqa: E402
from core import sender as sender_mod                   # noqa: E402
from core.sender import SendingPool, UnverifiedRecipient  # noqa: E402

PASS = FAIL = 0
CLIENT = "testco"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _conn():
    return state.connect(tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name)


class _FakeDig:
    """Stand in for core.verify._dig. Maps (domain, qtype) -> (rcode, answers)."""

    def __init__(self, table, default=("NOERROR", [])):
        self.table = table
        self.default = default
        self.calls = []

    def __call__(self, domain, qtype, timeout):
        self.calls.append((domain, qtype))
        return self.table.get((domain, qtype), self.default)


def _with_dig(fake, fn):
    real = verify._dig
    verify._dig = fake
    try:
        return fn()
    finally:
        verify._dig = real


def _mx(*hosts):
    return [f"d.test.\t300\tIN\tMX\t10 {h}." for h in hosts]


# --------------------------------------------------------------------------
def test_syntax_and_placeholders():
    print("\n[syntax / placeholder: decidable offline, never UNKNOWN]")
    for bad in ["contact@example.com", "info@mail.example.org", "x@test.com",
                "a@localhost", "a@invalid", "no-at-sign", "@nolocal.com",
                "a@nodot", "a b@spaces.com", "two@@at.com"]:
        s, _ = verify.check_syntax(bad)
        check(f"refuses {bad!r}", s == verify.INVALID)

    for good in ["info@realbiz.com", "first.last+tag@sub.company.co.uk"]:
        s, _ = verify.check_syntax(good)
        check(f"accepts {good!r}", s == verify.OK)

    check("check_syntax NEVER returns unknown",
          all(verify.check_syntax(x)[0] != verify.UNKNOWN
              for x in ["", None, "a@b.com", "junk", "a@example.com"]))

    # Absent is not fake — that distinction is load-bearing upstream.
    check("blank is not a PLACEHOLDER (require_email decides)",
          verify.is_placeholder("") is False and verify.is_placeholder(None) is False)
    check("...but blank IS invalid as a send target",
          verify.check_syntax("")[0] == verify.INVALID)


def test_clean_and_domain():
    print("\n[clean / domain_of: scrape artifacts]")
    check("strips mailto:", verify.clean("mailto:a@b.com") == "a@b.com")
    check("strips angle brackets", verify.clean("<a@b.com>") == "a@b.com")
    check("strips trailing period", verify.clean("a@b.com.") == "a@b.com")
    check("strips trailing comma", verify.clean("a@b.com,") == "a@b.com")
    check("domain_of lowercases", verify.domain_of("A@Example.COM") == "example.com")
    check("domain_of on garbage is ''", verify.domain_of("nope") == "")


def test_mail_route_positive():
    print("\n[resolve_mail_route: routes that exist]")
    f = _FakeDig({("d.test", "MX"): ("NOERROR", _mx("mx1", "mx2"))})
    s, r = _with_dig(f, lambda: verify.resolve_mail_route("d.test"))
    check("MX records => ok", s == verify.OK)
    check("reason counts them", "2 MX" in r)

    # No MX, but an A record: RFC 5321 implicit MX.
    f = _FakeDig({("d.test", "MX"): ("NOERROR", []),
                  ("d.test", "A"): ("NOERROR", ["d.test. 300 IN A 1.2.3.4"])})
    s, r = _with_dig(f, lambda: verify.resolve_mail_route("d.test"))
    check("no MX but an A record => ok (implicit MX)", s == verify.OK)
    check("reason says implicit MX", "implicit MX" in r)


def test_mail_route_negative():
    print("\n[resolve_mail_route: proven undeliverable]")
    f = _FakeDig({("d.test", "MX"): ("NXDOMAIN", [])})
    s, r = _with_dig(f, lambda: verify.resolve_mail_route("d.test"))
    check("NXDOMAIN => invalid", s == verify.INVALID)
    check("reason names NXDOMAIN", "NXDOMAIN" in r)

    # RFC 7505 null MX — a REAL record that means "no mail here". A naive
    # "any MX => deliverable" check passes this; example.com publishes it.
    f = _FakeDig({("d.test", "MX"): ("NOERROR", ["d.test. 300 IN MX 0 ."])})
    s, r = _with_dig(f, lambda: verify.resolve_mail_route("d.test"))
    check("null MX (RFC 7505) => invalid", s == verify.INVALID)
    check("reason names the null MX", "null MX" in r)

    f = _FakeDig({("d.test", "MX"): ("NOERROR", []),
                  ("d.test", "A"): ("NOERROR", []),
                  ("d.test", "AAAA"): ("NOERROR", [])})
    s, r = _with_dig(f, lambda: verify.resolve_mail_route("d.test"))
    check("no MX, no A, no AAAA => invalid", s == verify.INVALID)

    check("empty domain => invalid", verify.resolve_mail_route("")[0] == verify.INVALID)


def test_unknown_is_never_invalid():
    print("\n[THE RULE: a failed lookup is UNKNOWN, not INVALID]  <-- mutation target")
    # rcode None is how _dig reports "we could not ask" — no dig binary, a hung
    # resolver, a network that eats port 53.
    f = _FakeDig({("d.test", "MX"): (None, [])})
    s, r = _with_dig(f, lambda: verify.resolve_mail_route("d.test"))
    check("resolver unreachable => unknown", s == verify.UNKNOWN)
    check("...and NOT invalid", s != verify.INVALID)

    for rcode in ("SERVFAIL", "REFUSED", "NOTIMP"):
        f = _FakeDig({("d.test", "MX"): (rcode, [])})
        s, _ = _with_dig(f, lambda: verify.resolve_mail_route("d.test"))
        check(f"{rcode} => unknown (not invalid)", s == verify.UNKNOWN)

    # The subtle one: MX answered cleanly with no records, then the A lookup died.
    # We know the domain exists but NOT whether it has an address record, so the
    # honest verdict is unknown — calling it invalid would condemn a live domain
    # on a transient failure.
    f = _FakeDig({("d.test", "MX"): ("NOERROR", []), ("d.test", "A"): (None, [])})
    s, r = _with_dig(f, lambda: verify.resolve_mail_route("d.test"))
    check("no MX + A lookup unavailable => unknown", s == verify.UNKNOWN)

    f = _FakeDig({("d.test", "MX"): ("NOERROR", []),
                  ("d.test", "A"): ("SERVFAIL", [])})
    s, _ = _with_dig(f, lambda: verify.resolve_mail_route("d.test"))
    check("no MX + A SERVFAIL => unknown", s == verify.UNKNOWN)


def test_should_block():
    print("\n[should_block: what actually stops a send]")
    inv = {"status": verify.INVALID}
    unk = {"status": verify.UNKNOWN}
    ok = {"status": verify.OK}
    check("invalid blocks", verify.should_block(inv) is True)
    check("ok never blocks", verify.should_block(ok) is False)
    check("unknown does NOT block by default", verify.should_block(unk) is False)
    check("unknown blocks only when opted in",
          verify.should_block(unk, block_on_unknown=True) is True)
    check("ok still passes even when opted in",
          verify.should_block(ok, block_on_unknown=True) is False)
    check("invalid blocks regardless",
          verify.should_block(inv, block_on_unknown=True) is True)


def test_cache():
    print("\n[cache: ok is trusted, invalid is re-checked sooner, unknown is never stored]")
    conn = _conn()
    state.record_mx_check(conn, "good.test", "ok", "2 MX record(s)")
    hit = state.get_mx_check(conn, "good.test")
    check("ok roundtrips", hit and hit["status"] == "ok")
    check("case-insensitive", state.get_mx_check(conn, "GOOD.TEST") is not None)
    check("unknown domain misses", state.get_mx_check(conn, "nope.test") is None)

    state.record_mx_check(conn, "unk.test", "unknown", "timeout")
    check("'unknown' is REFUSED by the cache", state.get_mx_check(conn, "unk.test") is None)

    # Age a row past each TTL by rewriting checked_at.
    def _age(domain, days):
        conn.execute("UPDATE mx_checks SET checked_at=? WHERE domain=?",
                     ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(), domain))
        conn.commit()

    state.record_mx_check(conn, "bad.test", "invalid", "NXDOMAIN")
    _age("bad.test", 8)
    check("invalid expires after invalid_ttl_days (7)",
          state.get_mx_check(conn, "bad.test", ttl_days=30, invalid_ttl_days=7) is None)
    state.record_mx_check(conn, "bad.test", "invalid", "NXDOMAIN")
    _age("bad.test", 3)
    check("invalid still trusted at 3 days",
          state.get_mx_check(conn, "bad.test", invalid_ttl_days=7) is not None)

    _age("good.test", 8)
    check("ok is STILL trusted at 8 days (30d TTL) — unlike invalid",
          state.get_mx_check(conn, "good.test", ttl_days=30, invalid_ttl_days=7) is not None)
    _age("good.test", 31)
    check("ok expires after ttl_days (30)", state.get_mx_check(conn, "good.test") is None)
    conn.close()


def test_verify_email_uses_cache():
    print("\n[verify_email: cache hit skips DNS entirely]")
    conn = _conn()
    f = _FakeDig({("real.test", "MX"): ("NOERROR", _mx("mx1"))})
    v1 = _with_dig(f, lambda: verify.verify_email("a@real.test", conn))
    check("first call resolves", v1["status"] == verify.OK and v1["cached"] is False)
    check("DNS was queried once", len(f.calls) == 1)

    v2 = _with_dig(f, lambda: verify.verify_email("b@real.test", conn))
    check("second call is served from cache", v2["cached"] is True)
    check("no further DNS query", len(f.calls) == 1)

    # A syntax failure must short-circuit before any DNS at all.
    f2 = _FakeDig({})
    v3 = _with_dig(f2, lambda: verify.verify_email("x@example.com", conn))
    check("placeholder => invalid", v3["status"] == verify.INVALID)
    check("...without touching DNS", f2.calls == [])

    # An UNKNOWN result must not be written to the cache.
    f3 = _FakeDig({("flaky.test", "MX"): (None, [])})
    v4 = _with_dig(f3, lambda: verify.verify_email("a@flaky.test", conn))
    check("unknown verdict returned", v4["status"] == verify.UNKNOWN)
    check("unknown was NOT cached", state.get_mx_check(conn, "flaky.test") is None)
    conn.close()


# --- the send gate ---------------------------------------------------------

def _pool(mode="live", verify_cfg=None):
    return SendingPool({
        "client": CLIENT,
        "from_name": "Test",
        "sending": {
            "mode": mode,
            "controlled_inbox": "safe@mine.test",
            "daily_global_cap": 100,
            "mailboxes": [{"address": "a@x.test", "daily_cap": 40,
                           "smtp_host": "h", "smtp_port": 587,
                           "user": "u", "password": "p"}],
            "warmup": {"enabled": False},
            "verify_recipients": verify_cfg if verify_cfg is not None else {"enabled": True},
        },
    })


def _attempt_send(pool, conn, to_email, dig_table):
    """Try one send with DNS stubbed and SMTP replaced. Returns (error, wire_calls)."""
    calls = []
    real_smtp = sender_mod.smtp_deliver
    sender_mod.smtp_deliver = lambda *a, **k: calls.append(a)
    f = _FakeDig(dig_table)
    err = None
    try:
        def go():
            try:
                pool.send(conn, to_email, "subj", "body", throttle=False)
            except Exception as e:
                return e
            return None
        err = _with_dig(f, go)
    finally:
        sender_mod.smtp_deliver = real_smtp
    return err, calls


def test_send_blocks_invalid():
    print("\n[send(): a PROVEN-undeliverable recipient never reaches the wire]")
    conn = _conn()
    err, calls = _attempt_send(_pool(), conn, "a@dead.test",
                               {("dead.test", "MX"): ("NXDOMAIN", [])})
    check("raises UnverifiedRecipient", isinstance(err, UnverifiedRecipient))
    check("smtp_deliver was never called", calls == [])
    check("nothing was recorded as sent", state.sends_today(conn, CLIENT) == 0)
    check("the error says why", err and "NXDOMAIN" in str(err))
    conn.close()


def test_send_proceeds_on_unknown():
    print("\n[send(): an UNKNOWN verdict does NOT block]  <-- fail-open, on purpose")
    conn = _conn()
    err, calls = _attempt_send(_pool(), conn, "a@flaky.test",
                               {("flaky.test", "MX"): (None, [])})
    check("no exception raised", err is None)
    check("the message WAS sent", len(calls) == 1)
    check("recorded as sent", state.sends_today(conn, CLIENT) == 1)
    conn.close()

    # ...unless the operator explicitly opts in.
    conn = _conn()
    pool = _pool(verify_cfg={"enabled": True, "block_on_unknown": True})
    err, calls = _attempt_send(pool, conn, "a@flaky.test",
                               {("flaky.test", "MX"): (None, [])})
    check("block_on_unknown=True does block", isinstance(err, UnverifiedRecipient))
    check("...and nothing reached the wire", calls == [])
    conn.close()


def test_send_verifies_the_prospect_not_the_safe_inbox():
    print("\n[controlled mode: the LOGICAL recipient is what gets verified]")
    conn = _conn()
    # safe@mine.test resolves fine; the prospect's domain does not exist. If the
    # gate checked actual_to it would rubber-stamp this and do nothing at all.
    err, calls = _attempt_send(
        _pool(mode="controlled"), conn, "a@dead.test",
        {("dead.test", "MX"): ("NXDOMAIN", []),
         ("mine.test", "MX"): ("NOERROR", _mx("mx1"))})
    check("still blocked in controlled mode", isinstance(err, UnverifiedRecipient))
    check("the prospect's domain is the one named", err and "dead.test" in str(err))
    check("nothing reached the wire", calls == [])
    conn.close()


def test_verification_can_be_disabled():
    print("\n[enabled=false: the gate is inert]")
    conn = _conn()
    pool = _pool(verify_cfg={"enabled": False})
    err, calls = _attempt_send(pool, conn, "a@dead.test",
                               {("dead.test", "MX"): ("NXDOMAIN", [])})
    check("a dead domain sends when verification is off", err is None)
    check("the message went out", len(calls) == 1)
    check("verify_recipient returns None when off",
          pool.verify_recipient(conn, "a@dead.test") is None)
    conn.close()


# --- real DNS (opt-in) -----------------------------------------------------

def test_live_dns():
    print("\n[REAL DNS]  (set VERIFY_LIVE_DNS=1 to run)")
    if os.environ.get("VERIFY_LIVE_DNS") != "1":
        print("  ⏭️  skipped — offline by default so the suite needs no network")
        return
    s, r = verify.resolve_mail_route("gmail.com")
    check(f"gmail.com => ok ({r})", s == verify.OK)
    s, r = verify.resolve_mail_route("example.com")
    check(f"example.com => invalid via null MX ({r})", s == verify.INVALID)
    s, r = verify.resolve_mail_route("nonexistent-xyzzy-12345.invalid-tld-here")
    check(f"bogus domain => invalid or unknown, never ok ({r})", s != verify.OK)


if __name__ == "__main__":
    print("=" * 62)
    print("RECIPIENT VERIFICATION — DNS stubbed, no SMTP")
    print("=" * 62)
    test_syntax_and_placeholders()
    test_clean_and_domain()
    test_mail_route_positive()
    test_mail_route_negative()
    test_unknown_is_never_invalid()
    test_should_block()
    test_cache()
    test_verify_email_uses_cache()
    test_send_blocks_invalid()
    test_send_proceeds_on_unknown()
    test_send_verifies_the_prospect_not_the_safe_inbox()
    test_verification_can_be_disabled()
    test_live_dns()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
