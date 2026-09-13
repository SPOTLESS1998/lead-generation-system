"""Placeholder/reserved email domains must never become leads.

WHY THIS SUITE EXISTS
On 2026-09-13 a real discovery run on the VPS banked 8 new leads — good — but one of
them was "JMJ PROPERTIES" at contact@example.com. example.com is reserved by RFC 2606
specifically so it can NEVER receive mail, so that address is a guaranteed hard bounce.

A bounce is not a cosmetic problem for cold outreach. Sender reputation is the asset
that decides whether any of this reaches an inbox at all: a bounce rate past a few
percent gets a domain throttled, then blocklisted, and the damage lands on the SENDING
domain, not on the lead. Nothing downstream can repair it — by the time the sender sees
the bounce it has already happened. So the address has to be refused where it is first
read, which is why the guard sits in core/discovery.py at the extraction point.

The check is deliberately NARROW (a reserved-domain list plus the no-TLD case). A broad
"looks fake" heuristic would start silently rejecting real small-business addresses, and
a missed lead is invisible while a bounce is loud — so the asymmetry says: only reject
what is provably unroutable.

Run:  venv/bin/python tests/test_placeholder_email_guard.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import discovery                                        # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


print("[reserved + boilerplate domains are refused]")
for bad in ("contact@example.com", "info@example.org", "sales@example.net",
            "hello@test.com", "a@invalid", "x@localhost", "me@yourdomain.com",
            "hi@yourcompany.com", "post@domain.com", "user@email.com",
            "mail@sub.example.com", "root@mail.test.org"):
    check(f"refuses {bad}", discovery._is_placeholder_email(bad) is True)

print("\n[real business addresses are NOT touched]")
for good in ("info@mustardnet.com", "contact@oxgital.com", "info@pwanmax.com",
             "contact@airealent.ng", "info@leadpropertymall.com",
             "hello@examplefirm.com",          # contains 'example' but is routable
             "sales@mytestcompany.com",        # contains 'test' but is routable
             "a.b+tag@sub.domain.co.uk",
             "info@xn--mgba3a4f.com"):         # IDN/punycode
    check(f"accepts {good}", discovery._is_placeholder_email(good) is False)

print("\n[malformed PRESENT addresses are refused, not crashed on]")
for broken in ("not-an-email", "@nodomain.com", "no-at-sign.com",
               "spacesin@email.com", "nodot@localhost", "user@nodot"):
    check(f"refuses {broken!r}", discovery._is_placeholder_email(broken) is True)

print("\n[ABSENT email is NOT placeholder — it is a legitimate no-email lead]")
# This distinction is load-bearing and was a REAL bug here: an earlier version of the
# guard treated "" as placeholder, which silently dropped every business with no public
# email even when require_email=False asked for them to be kept. tests/test_discovery.py
# ("NoEmail Corp kept when require_email=False") caught it. Absent != fake.
for absent in ("", None, "   "):
    check(f"{absent!r} is not a placeholder (left to require_email)",
          discovery._is_placeholder_email(absent) is False)

# NOTE: "trailing@dot.com." is NOT refused, deliberately. dot.com is a real routable
# domain and the trailing dot is a scrape artifact, so the right answer is "accept it,
# having cleaned the dot". Asserting otherwise was a wrong expectation in this file.
check("a trailing dot is cleaned, not treated as a fake domain",
      discovery._is_placeholder_email(discovery._clean_email("trailing@dot.com.")) is False)

print("\n[the guard uses the CLEANED address, so artifacts cannot smuggle one through]")
# _clean_email strips mailto:, angles, and trailing punctuation. If the guard ran on the
# RAW value instead, "<contact@example.com>." would slip past a naive comparison.
for raw, expect_placeholder in (("<contact@example.com>.", True),
                                ("mailto:info@example.org", True),
                                ("mailto:info@realbiz.com", False)):
    cleaned = discovery._clean_email(raw)
    actual = discovery._is_placeholder_email(cleaned)
    check(f"{raw!r} -> cleaned {cleaned!r} -> placeholder={actual}", actual is expect_placeholder)

print("\n[the guard is actually WIRED INTO extraction, not just defined]")
# The tests above call _is_placeholder_email directly, so they'd still pass if the guard
# were never invoked in the pipeline — and a correct helper that nothing calls looks
# exactly like a fixed system. (Proven: mutating the call site to `if False:` left this
# suite green, which is how this block came to exist.) Drive the REAL extraction path
# with a stubbed LLM and assert the placeholder lead never comes back.
_real_generate_json = discovery.generate_json


def _stub_returning(email):
    """Stand in for the LLM extractor, returning a fixed address."""
    def _fake(cfg, prompt, *a, **k):
        return ({"email": email, "first_name": "Test", "last_name": "Person",
                 "title": "Owner", "company_description": "A firm.",
                 "services": "audit", "specific_detail": "x"}, "stub")
    return _fake


BIZ = {"company_name": "JMJ PROPERTIES", "website_url": "https://jmj.example",
       "rating": None, "review_count": None}

try:
    discovery.generate_json = _stub_returning("contact@example.com")
    out = discovery._extract_lead({}, BIZ, "some page markdown")
    check("extraction REFUSES a placeholder email (returns None)", out is None)

    discovery.generate_json = _stub_returning("info@pwanmax.com")
    out = discovery._extract_lead({}, BIZ, "some page markdown")
    check("extraction still ACCEPTS a real email", isinstance(out, dict))
    check("...and keeps the real address", (out or {}).get("email") == "info@pwanmax.com")
finally:
    discovery.generate_json = _real_generate_json

print(f"\n{'='*54}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*54}")
sys.exit(1 if FAIL else 0)
