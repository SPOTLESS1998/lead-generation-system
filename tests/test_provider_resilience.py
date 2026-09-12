"""Provider failover + circuit-breaker recovery.

WHY THIS SUITE EXISTS
On 2026-09-12 a real run sourced 2 leads and drafted ZERO. Nothing was broken for
long: the freellmapi gateway was down, Gemini's host was unreachable from that
network, and NVIDIA hiccuped once. Those failures tripped all three breakers during
the 45-minute discovery phase — and drafting runs at the END of that phase, so every
lead in the pool hit an already-open breaker, was released, and the day produced no
drafts at all. The error even said "retry after cooldown"; nothing ever did.

The lesson is specific: for a job that runs ONCE a day, "fail fast" is the wrong
default at the point where waiting 60 seconds would have fixed it. Failing fast is
right for the approval server (a human is waiting); it is wrong for an unattended
morning run (nobody is).

Pinned here:
  1. A cooling-down chain raises AllProvidersCoolingDown, NOT a bare RuntimeError,
     so a caller can tell "wait and retry" apart from "genuinely broken".
  2. cooldown_remaining() reports 0 the moment ANY provider is callable again.
  3. A real outage still raises the ordinary error — we must not wait forever on
     something that will never recover.

Run:  venv/bin/python tests/test_provider_resilience.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import ai                                               # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


CFG = {"providers": ["freellmapi", "gemini", "nvidia"],
       "circuit_breaker": {"enabled": True, "threshold": 2, "cooldown_secs": 30.0}}


def _trip_all(cfg=CFG):
    """Drive every provider past its failure threshold so all breakers open."""
    ai.reset_breakers()
    original = dict(ai._PROVIDER_FUNCS)

    def boom(*a, **k):
        raise RuntimeError("provider down")

    for p in ("freellmapi", "gemini", "nvidia"):
        ai._PROVIDER_FUNCS[p] = boom
    try:
        threshold = cfg["circuit_breaker"]["threshold"]
        for _ in range(threshold):
            try:
                ai.generate(cfg, "hello")
            except Exception:
                pass
    finally:
        ai._PROVIDER_FUNCS.clear()
        ai._PROVIDER_FUNCS.update(original)


def test_cooling_down_is_its_own_error_type():
    print("\n[a cooling-down chain is distinguishable from a real outage]")
    _trip_all()
    # Every breaker is now open. The next call touches nothing and must say so.
    try:
        ai.generate(CFG, "hello")
        check("generate raised", False)
    except ai.AllProvidersCoolingDown as e:
        check("raises AllProvidersCoolingDown, not a bare RuntimeError", True)
        check("the message names the cooldown", "cooldown" in str(e).lower())
    except Exception as e:
        check(f"raises AllProvidersCoolingDown (got {type(e).__name__})", False)

    check("AllProvidersCoolingDown is still a RuntimeError (old handlers keep working)",
          issubclass(ai.AllProvidersCoolingDown, RuntimeError))


def test_cooldown_remaining_reports_the_wait():
    print("\n[cooldown_remaining tells a caller how long to wait]")
    _trip_all()
    remaining = ai.cooldown_remaining()
    check("a tripped chain reports a positive wait", remaining > 0)
    check("the wait is bounded by the configured cooldown", remaining <= 30.0)

    ai.reset_breakers()
    check("a clean chain reports no wait", ai.cooldown_remaining() == 0.0)

    # One provider ready is enough: the chain only needs a single working leg.
    _trip_all()
    with ai._breaker_lock:
        ai._breakers["nvidia"]["open_until"] = 0.0
    check("one recovered provider means zero wait", ai.cooldown_remaining() == 0.0)
    ai.reset_breakers()


def test_a_real_outage_still_fails_normally():
    print("\n[a genuine outage is NOT reported as a cooldown]")
    ai.reset_breakers()
    original = dict(ai._PROVIDER_FUNCS)

    def boom(*a, **k):
        raise RuntimeError("upstream exploded")

    for p in ("freellmapi", "gemini", "nvidia"):
        ai._PROVIDER_FUNCS[p] = boom
    try:
        # FIRST call: providers are actually invoked, so this is a real failure.
        try:
            ai.generate(CFG, "hello")
            check("generate raised", False)
        except ai.AllProvidersCoolingDown:
            check("a first-pass outage must NOT be typed as a cooldown", False)
        except RuntimeError as e:
            check("a real outage raises the ordinary RuntimeError", True)
            check("the underlying error is surfaced", "exploded" in str(e))
    finally:
        ai._PROVIDER_FUNCS.clear()
        ai._PROVIDER_FUNCS.update(original)
        ai.reset_breakers()


def test_recovery_closes_the_breaker():
    print("\n[the chain self-heals once a provider comes back]")
    ai.reset_breakers()
    original = dict(ai._PROVIDER_FUNCS)
    calls = []

    def boom(*a, **k):
        raise RuntimeError("down")

    def good(cfg, prompt, *a, **k):
        calls.append("nvidia")
        return "drafted copy", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    ai._PROVIDER_FUNCS["freellmapi"] = boom
    ai._PROVIDER_FUNCS["gemini"] = boom
    ai._PROVIDER_FUNCS["nvidia"] = good
    try:
        text, provider = ai.generate(CFG, "hello")
        check("failover reaches the one healthy provider", provider == "nvidia")
        check("the healthy provider's text is returned", text == "drafted copy")
        check("the healthy provider was actually called", calls == ["nvidia"])
        check("a working provider is never left tripped",
              ai._breaker_is_open("nvidia") is False)
    finally:
        ai._PROVIDER_FUNCS.clear()
        ai._PROVIDER_FUNCS.update(original)
        ai.reset_breakers()


test_cooling_down_is_its_own_error_type()
test_cooldown_remaining_reports_the_wait()
test_a_real_outage_still_fails_normally()
test_recovery_closes_the_breaker()

print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
