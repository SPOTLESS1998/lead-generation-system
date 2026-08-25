"""Premium-copy budget guard — the hard $ cap that protects the shared Anthropic pool.

'Premium everywhere' routes cold-email copy to real Claude (the Anthropic Messages
API), which spends the same dev credits that power Claude Code. To make that safe,
every draft PRE-CHECKS today's real-Claude spend against a per-client daily cap:

    gen_cfg = budget.copy_cfg(conn, cfg)          # premium-first if under cap, else free chain
    subject, body, _ = generate_copy(gen_cfg, lead, brief, magnet_url)

Real spend is reconstructed from the observability ledger (`pipeline_events`): tokens
attributed to provider 'anthropic', priced at real Anthropic list rates. This is a
SEPARATE number from observability's `cost_usd` (that one is notional, for pricing the
service). Unknown models are priced as Opus (the expensive tier) so the guard errs
toward stopping early. The cap is a soft daily ceiling: a single in-flight draft can
overshoot it by at most one draft's cost (the ledger row is written when the draft
finishes), which is bounded and acceptable.

No secrets and no network here — this module only reads the local ledger and the
config. It must NOT be imported by core/ai.py (observability already imports ai +
state; keep the arrow pointing one way). Callers do the budget check.
"""

from core import state

# Real Anthropic list prices, $ per 1M tokens (input, output). Used ONLY to meter the
# daily cap + surface real spend on /health — never for the notional observability cost.
_ANTHROPIC_PRICES = {
    "opus":   (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku":  (0.80, 4.0),
}
DEFAULT_PROVIDERS = ["freellmapi", "gemini", "nvidia"]


def _copy_block(cfg):
    return cfg.get("copy", {}) or {}


def premium_model(cfg):
    return _copy_block(cfg).get("anthropic_model") or "claude-opus-4-8"


def _rates(model):
    """(input, output) $/1M for a Claude model id. Unknown => Opus (conservative)."""
    m = (model or "").lower()
    if "haiku" in m:
        return _ANTHROPIC_PRICES["haiku"]
    if "sonnet" in m:
        return _ANTHROPIC_PRICES["sonnet"]
    return _ANTHROPIC_PRICES["opus"]   # opus / unknown -> price as the expensive tier


def daily_cap_usd(cfg):
    """The per-client daily cap on real-Claude spend. None => unlimited (no guard)."""
    cap = _copy_block(cfg).get("premium_daily_usd_cap", None)
    if cap is None:
        return None
    try:
        cap = float(cap)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def premium_enabled(cfg):
    """Is premium copy switched on for this client at all?"""
    return bool(_copy_block(cfg).get("premium"))


def spent_today_usd(conn, cfg):
    """Real $ spent on Anthropic copy so far TODAY (UTC), reconstructed from the ledger.

    `state.list_events(since=...)` filters on `created_at >= since`; passing the 10-char
    date prefix of the ledger's own UTC clock (`state._now()[:10]`) selects exactly
    today's rows (ISO timestamps sort lexically, so 'YYYY-MM-DDT..' >= 'YYYY-MM-DD').
    """
    today = state._now()[:10]
    rate_in, rate_out = _rates(premium_model(cfg))
    total = 0.0
    for r in state.list_events(conn, cfg["client"], since=today):
        if (r["provider"] or "").startswith("anthropic"):
            total += (r["prompt_tokens"] or 0) / 1e6 * rate_in
            total += (r["completion_tokens"] or 0) / 1e6 * rate_out
    return round(total, 6)


def remaining_usd(conn, cfg):
    """$ left under today's cap, or None if there is no cap (unlimited)."""
    cap = daily_cap_usd(cfg)
    if cap is None:
        return None
    return round(max(0.0, cap - spent_today_usd(conn, cfg)), 6)


def premium_allowed(conn, cfg):
    """True if premium copy is ON and today's real-Claude spend is still under the cap."""
    if not premium_enabled(cfg):
        return False
    cap = daily_cap_usd(cfg)
    if cap is None:
        return True   # premium on, no cap set
    return spent_today_usd(conn, cfg) < cap


def copy_cfg(conn, cfg):
    """A cfg for the copy LLM calls: premium-first when the cap allows, else free chain.

    Returns a SHALLOW COPY with 'providers' (and 'anthropic_model') set so
    core.ai.generate() tries real Claude first only when the daily cap permits it.
    Never mutates the caller's cfg. When premium is off or the cap is spent, 'anthropic'
    is stripped from the chain entirely so no metered call (and no noisy 'key not set'
    fallback) ever happens.
    """
    base = list(cfg.get("providers") or DEFAULT_PROVIDERS)
    out = dict(cfg)
    if premium_allowed(conn, cfg):
        out["providers"] = ["anthropic"] + [p for p in base if p != "anthropic"]
        out["anthropic_model"] = premium_model(cfg)
    else:
        out["providers"] = [p for p in base if p != "anthropic"]
    return out


def status(conn, cfg):
    """A small dict for /health: is premium on, the cap, spent today, remaining."""
    return {
        "premium": premium_enabled(cfg),
        "model": premium_model(cfg),
        "cap_usd": daily_cap_usd(cfg),
        "spent_today_usd": spent_today_usd(conn, cfg),
        "remaining_usd": remaining_usd(conn, cfg),
        "allowed_now": premium_allowed(conn, cfg),
    }
