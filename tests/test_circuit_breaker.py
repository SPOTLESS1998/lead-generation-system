"""Offline tests for the per-provider circuit breaker in core/ai.py.

A plain failover chain pays a dead provider's full timeout on EVERY call. The
breaker trips a provider after N consecutive failures and skips it for a cooldown,
then HALF-OPENs to let it recover. Proven here with fake providers + a fake clock,
so NO network and NO real sleeping happen.

Run:  venv/bin/python tests/test_circuit_breaker.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import ai   # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


# ---- deterministic clock so cooldowns don't need real sleeps ----
_CLOCK = {"t": 10_000.0}
_REAL_MONOTONIC = ai.time.monotonic
ai.time.monotonic = lambda: _CLOCK["t"]

# ---- controllable fake providers wired in at the _PROVIDER_FUNCS seam ----
HEALTHY = {"nvidia": False, "gemini": True}
CALLS = {"nvidia": 0, "gemini": 0}
_REAL_NV = ai._PROVIDER_FUNCS["nvidia"]
_REAL_GEM = ai._PROVIDER_FUNCS["gemini"]


def _prov(name):
    def _fn(cfg, prompt):
        CALLS[name] += 1
        if HEALTHY[name]:
            return f"{name}-ok", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        raise RuntimeError(f"{name} is down")
    return _fn


ai._PROVIDER_FUNCS["nvidia"] = _prov("nvidia")
ai._PROVIDER_FUNCS["gemini"] = _prov("gemini")


def _reset(nv_healthy, gem_healthy):
    ai.reset_breakers()
    ai.reset_usage()
    HEALTHY["nvidia"] = nv_healthy
    HEALTHY["gemini"] = gem_healthy
    CALLS["nvidia"] = 0
    CALLS["gemini"] = 0


CFG = {"providers": ["nvidia", "gemini"],
       "circuit_breaker": {"enabled": True, "threshold": 3, "cooldown_secs": 60}}


print("\n--- A. CLOSED -> OPEN: trips after `threshold` consecutive fails -------")
_reset(nv_healthy=False, gem_healthy=True)
prov = None
for _ in range(3):
    text, prov = ai.generate(CFG, "x")
check("healthy backup answers while nvidia fails", prov == "gemini")
check("dead provider is tried exactly `threshold` (3) times before tripping", CALLS["nvidia"] == 3)

print("\n--- B. OPEN: a tripped provider is SKIPPED (the cost we save) ----------")
before = CALLS["nvidia"]
ai.generate(CFG, "x")
ai.generate(CFG, "x")
check("once OPEN, the dead provider is NOT called again", CALLS["nvidia"] == before)   # still 3
check("the backup keeps answering every call", CALLS["gemini"] == 5)

print("\n--- C. HALF-OPEN: after the cooldown, a recovered provider closes it ---")
HEALTHY["nvidia"] = True          # nvidia comes back to life
_CLOCK["t"] += 61                 # advance past the 60s cooldown
text, prov = ai.generate(CFG, "x")
check("after cooldown the provider is retried (half-open) and now answers", prov == "nvidia")
check("the retrial actually called nvidia once", CALLS["nvidia"] == 4)
g_before = CALLS["gemini"]
text, prov = ai.generate(CFG, "x")
check("breaker CLOSED again: primary answers, backup not needed",
      CALLS["gemini"] == g_before and prov == "nvidia")

print("\n--- D. ALL OPEN: whole chain fails fast, calls nobody -----------------")
cfg2 = {"providers": ["nvidia", "gemini"],
        "circuit_breaker": {"enabled": True, "threshold": 2, "cooldown_secs": 60}}
_reset(nv_healthy=False, gem_healthy=False)
for _ in range(2):
    try:
        ai.generate(cfg2, "x")
    except RuntimeError:
        pass
tripped_calls = (CALLS["nvidia"], CALLS["gemini"])   # (2, 2): each tried twice, then both tripped
failed_fast = False
try:
    ai.generate(cfg2, "x")
except RuntimeError as e:
    failed_fast = "cooldown" in str(e).lower()
check("both providers tried `threshold` (2) times before tripping", tripped_calls == (2, 2))
check("with every provider OPEN, generate() fails fast with a cooldown error", failed_fast)
check("fail-fast does NOT call the known-dead providers", CALLS["nvidia"] == 2 and CALLS["gemini"] == 2)

print("\n--- E. disabled: enabled:false never trips ----------------------------")
cfgd = {"providers": ["nvidia", "gemini"], "circuit_breaker": {"enabled": False}}
_reset(nv_healthy=False, gem_healthy=True)
for _ in range(5):
    ai.generate(cfgd, "x")
check("with the breaker disabled, the dead provider is tried on EVERY call", CALLS["nvidia"] == 5)

print("\n--- F. config drives threshold/cooldown -------------------------------")
en, th, cd = ai._breaker_params({"circuit_breaker": {"threshold": 7, "cooldown_secs": 12}})
check("threshold read from cfg", th == 7)
check("cooldown read from cfg", cd == 12.0)
en2, th2, cd2 = ai._breaker_params({})
check("sensible defaults when cfg omits the block", en2 is True and th2 == 3 and cd2 == 60.0)


# ---- restore everything we patched ----
ai.time.monotonic = _REAL_MONOTONIC
ai._PROVIDER_FUNCS["nvidia"] = _REAL_NV
ai._PROVIDER_FUNCS["gemini"] = _REAL_GEM
ai.reset_breakers()

print(f"\n{'='*54}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*54}")
sys.exit(1 if FAIL else 0)
