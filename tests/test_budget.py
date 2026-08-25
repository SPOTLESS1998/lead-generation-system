"""Offline tests for the premium-copy daily budget guard (core/budget.py).

Proves the hard $ cap that protects the shared Anthropic credit pool:
  - spend is reconstructed from the observability ledger at real Claude rates,
  - only TODAY's `anthropic*` rows count (free-provider rows never do),
  - copy_cfg puts 'anthropic' first while under cap, and strips it once hit,
  - everything degrades to the free chain when premium is off / capped.

NO network, NO API credits — seeds the ledger directly via state.record_event.

Run:  venv/bin/python tests/test_budget.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import budget, state          # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


CLIENT = "acme"


def make_cfg(premium=True, cap=5.0, model="claude-opus-4-8", providers=None, gate=None):
    copy = {"premium": premium, "anthropic_model": model}
    if cap is not None:
        copy["premium_daily_usd_cap"] = cap
    if gate is not None:
        copy["quality_gate"] = gate
    cfg = {"client": CLIENT, "client_name": "Ejentic AI", "copy": copy}
    if providers is not None:
        cfg["providers"] = providers
    return cfg


def fresh():
    db = os.path.join(tempfile.mkdtemp(), "state.sqlite")
    return state.connect(db)


def seed(conn, provider, pt=0, ct=0):
    """One anthropic (or other) ledger row dated NOW."""
    state.record_event(conn, CLIENT, "draft", "ok",
                        provider=provider, prompt_tokens=pt, completion_tokens=ct)


# --------------------------------------------------------------------------
# config readers
# --------------------------------------------------------------------------
check("daily_cap_usd reads the configured cap", budget.daily_cap_usd(make_cfg(cap=5.0)) == 5.0)
check("daily_cap_usd None when unset", budget.daily_cap_usd(make_cfg(cap=None)) is None)
check("daily_cap_usd None when 0 (treated as no cap-able value)",
      budget.daily_cap_usd(make_cfg(cap=0)) is None)
check("daily_cap_usd None when non-numeric",
      budget.daily_cap_usd(make_cfg(cap="abc")) is None)
check("daily_cap_usd None when no copy block", budget.daily_cap_usd({}) is None)

check("premium_enabled True", budget.premium_enabled(make_cfg(premium=True)) is True)
check("premium_enabled False", budget.premium_enabled(make_cfg(premium=False)) is False)
check("premium_enabled False when no copy block", budget.premium_enabled({}) is False)

check("premium_model reads config", budget.premium_model(make_cfg(model="claude-x")) == "claude-x")
check("premium_model defaults to opus", budget.premium_model({}) == "claude-opus-4-8")


# --------------------------------------------------------------------------
# _rates — conservative pricing (unknown models priced as Opus)
# --------------------------------------------------------------------------
check("opus rates", budget._rates("claude-opus-4-8") == (15.0, 75.0))
check("sonnet rates", budget._rates("claude-sonnet-4-5") == (3.0, 15.0))
check("haiku rates", budget._rates("claude-haiku-4-5") == (0.80, 4.0))
check("unknown model priced as opus (conservative)", budget._rates("mystery-model") == (15.0, 75.0))


# --------------------------------------------------------------------------
# spent_today_usd — reconstruct from the ledger at Claude rates
# --------------------------------------------------------------------------
cfg = make_cfg(cap=1000.0)   # high cap so nothing is blocked while we measure
conn = fresh()
check("fresh ledger spends $0", budget.spent_today_usd(conn, cfg) == 0.0)

seed(conn, "anthropic", pt=1_000_000, ct=0)        # 1M input @ $15/M = $15
check("1M input priced at $15", budget.spent_today_usd(conn, cfg) == 15.0)

seed(conn, "anthropic", pt=0, ct=1_000_000)        # +1M output @ $75/M = $75
check("adds output at $75/M (=> $90)", budget.spent_today_usd(conn, cfg) == 90.0)

seed(conn, "freellmapi", pt=5_000_000, ct=5_000_000)   # free provider — must NOT count
check("free-provider tokens never count", budget.spent_today_usd(conn, cfg) == 90.0)

seed(conn, None, pt=9_000_000, ct=9_000_000)       # no provider — must NOT count
check("null-provider tokens never count", budget.spent_today_usd(conn, cfg) == 90.0)

seed(conn, "anthropic/claude-opus-4-8", pt=1_000_000, ct=0)   # startswith 'anthropic' — counts
check("provider prefixed 'anthropic...' counts (=> $105)", budget.spent_today_usd(conn, cfg) == 105.0)

# A row dated on a PRIOR day must not count against today's cap.
conn.execute(
    "INSERT INTO pipeline_events (client, step, status, provider, prompt_tokens, "
    "completion_tokens, created_at) VALUES (?,?,?,?,?,?,?)",
    (CLIENT, "draft", "ok", "anthropic", 1_000_000, 1_000_000, "2020-01-01T00:00:00+00:00"))
conn.commit()
check("yesterday's anthropic spend excluded (still $105)", budget.spent_today_usd(conn, cfg) == 105.0)
conn.close()


# --------------------------------------------------------------------------
# remaining_usd
# --------------------------------------------------------------------------
conn = fresh()
cfg5 = make_cfg(cap=5.0)
check("remaining == cap on empty ledger", budget.remaining_usd(conn, cfg5) == 5.0)
seed(conn, "anthropic", pt=100_000)                 # 0.1M @ $15 = $1.50
check("remaining subtracts spend", budget.remaining_usd(conn, cfg5) == 3.5)
seed(conn, "anthropic", ct=1_000_000)               # +$75 — blows past the cap
check("remaining floors at 0 (never negative)", budget.remaining_usd(conn, cfg5) == 0.0)
check("remaining None when uncapped", budget.remaining_usd(conn, make_cfg(cap=None)) is None)
conn.close()


# --------------------------------------------------------------------------
# premium_allowed — the gate
# --------------------------------------------------------------------------
conn = fresh()
check("not allowed when premium off", budget.premium_allowed(conn, make_cfg(premium=False)) is False)
check("allowed when on + uncapped", budget.premium_allowed(conn, make_cfg(cap=None)) is True)
check("allowed when on + under cap", budget.premium_allowed(conn, make_cfg(cap=5.0)) is True)
seed(conn, "anthropic", ct=1_000_000)               # $75 spent, cap $5
check("NOT allowed once spend >= cap", budget.premium_allowed(conn, make_cfg(cap=5.0)) is False)
conn.close()


# --------------------------------------------------------------------------
# copy_cfg — the provider-chain rewrite the whole system relies on
# --------------------------------------------------------------------------
conn = fresh()   # $0 spent
base = ["freellmapi", "gemini", "nvidia"]
cfg = make_cfg(cap=5.0, model="claude-opus-4-8", providers=base)

out = budget.copy_cfg(conn, cfg)
check("under cap: anthropic is tried FIRST", out["providers"][0] == "anthropic")
check("under cap: free chain kept after it",
      out["providers"] == ["anthropic", "freellmapi", "gemini", "nvidia"])
check("under cap: anthropic_model is set", out["anthropic_model"] == "claude-opus-4-8")
check("copy_cfg does not mutate the caller's cfg", cfg["providers"] == base)

out_off = budget.copy_cfg(conn, make_cfg(premium=False, providers=base))
check("premium off: no anthropic in chain", "anthropic" not in out_off["providers"])
check("premium off: chain is the plain base", out_off["providers"] == base)

# Now blow the cap and confirm the chain drops anthropic.
seed(conn, "anthropic", ct=1_000_000)               # $75 >> $5 cap
out_hit = budget.copy_cfg(conn, cfg)
check("cap hit: anthropic stripped from chain", "anthropic" not in out_hit["providers"])
check("cap hit: falls back to the free chain", out_hit["providers"] == base)

# No explicit providers → default free chain, anthropic prepended when allowed.
conn2 = fresh()
out_def = budget.copy_cfg(conn2, make_cfg(cap=5.0))   # no 'providers' key
check("default chain used when providers unset",
      out_def["providers"] == ["anthropic"] + budget.DEFAULT_PROVIDERS)
conn2.close()
conn.close()


# --------------------------------------------------------------------------
# status — the dashboard/CLI summary
# --------------------------------------------------------------------------
conn = fresh()
seed(conn, "anthropic", pt=100_000)                 # $1.50 of a $5 cap
st = budget.status(conn, make_cfg(cap=5.0))
check("status: premium flag", st["premium"] is True)
check("status: model", st["model"] == "claude-opus-4-8")
check("status: cap", st["cap_usd"] == 5.0)
check("status: spent", st["spent_today_usd"] == 1.5)
check("status: remaining", st["remaining_usd"] == 3.5)
check("status: allowed now", st["allowed_now"] is True)
conn.close()


print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
