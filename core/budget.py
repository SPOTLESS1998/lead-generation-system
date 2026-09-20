"""Premium-copy budget guard — the hard $ cap that protects the shared Anthropic pool.

'Premium everywhere' routes cold-email copy to real Claude (the Anthropic Messages
API), which spends the same dev credits that power Claude Code. To make that safe,
every draft PRE-CHECKS today's real-Claude spend against a per-client daily cap:

    gen_cfg = budget.copy_cfg(conn, cfg)          # premium-first if under cap, else free chain
    subject, body, _ = generate_copy(gen_cfg, lead, brief, magnet_url)

Spend is reconstructed from the observability ledger (`pipeline_events`): tokens
attributed to provider 'anthropic', priced at official Anthropic list rates. This is a
SEPARATE number from observability's `cost_usd` (that one is notional, for pricing the
service). Unknown models are priced as Opus (the expensive tier) so the guard errs
toward stopping early. The cap is a soft daily ceiling: a single in-flight draft can
overshoot it by at most one draft's cost (the ledger row is written when the draft
finishes), which is bounded and acceptable.

⚠️ WHAT THIS NUMBER IS NOT. It is only a real dollar bill when ANTHROPIC_BASE_URL is
the official API. Point it at a proxy (AgentRouter is the live setting — see
core/ai.py:_anthropic_chat) and the resale rates are not Anthropic's, so `spent_today_usd`
and /health report a figure NOBODY invoices. Treat it there as what it actually is: a
deterministic TOKEN-VOLUME ceiling denominated in official-price dollars. That is still
a genuine guard — it stops the run at a predictable token count — but do not quote it as
cost, and do not claim a saving measured against it. `_rates()` also cannot know a model
it has no entry for: `claude-opus-5` reaches the Opus row by fallthrough, not by knowing
its price. Never substitute a guessed rate for a missing one; add a measured entry or
leave the conservative fallthrough alone.

No secrets and no network here — this module only reads the local ledger and the
config. It must NOT be imported by core/ai.py (observability already imports ai +
state; keep the arrow pointing one way). Callers do the budget check.
"""

from core import state

# Official Anthropic list prices, $ per 1M tokens (input, output). Used ONLY to meter the
# daily cap + surface spend on /health — never for the notional observability cost. See the
# module docstring: against a proxy base URL these are NOT the rates you are billed, so the
# cap behaves as a token-volume ceiling rather than a dollar one.
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


class BudgetCapInvalid(Exception):
    """Raised when premium_daily_usd_cap is set but is not a usable number."""


def daily_cap_usd(cfg):
    """The per-client daily cap on real-Claude spend. None => unlimited (no guard).

    A cap that is ABSENT or explicitly null means "uncapped", which is a
    deliberate choice an operator can make. A cap that is PRESENT but unusable
    ("$5", "5 USD", "", -1) is a mistake, and it used to be treated as uncapped
    too: float() raised, we returned None, and None is the sentinel for
    unlimited. So a typo in the one config value whose job is to bound real
    money silently removed the bound, and /health rendered "uncapped ·
    Remaining today: ∞" — indistinguishable from having meant it.

    Now a malformed cap fails CLOSED: it raises, and premium_allowed treats that
    as "not allowed". Refusing to spend on a broken config is recoverable in one
    edit; spending without a ceiling is not. Note this is the only value in the
    system denominated in real dollars, which is why it gets the strict reading
    while the rest of config degrades gracefully.
    """
    block = _copy_block(cfg)
    if "premium_daily_usd_cap" not in block:
        return None                       # absent => uncapped, on purpose
    cap = block.get("premium_daily_usd_cap")
    if cap is None:
        return None                       # explicit null => uncapped, on purpose
    if isinstance(cap, bool):
        # True/False are ints in Python; a boolean here is always a mistake and
        # True would otherwise become a $1.00 cap.
        raise BudgetCapInvalid(
            f"premium_daily_usd_cap is {cap!r} (a boolean). Set a number, or null "
            f"for no cap.")
    try:
        cap = float(cap)
    except (TypeError, ValueError):
        raise BudgetCapInvalid(
            f"premium_daily_usd_cap is {cap!r}, which is not a number. Write it as a "
            f"bare number (5 or 5.0), not a string with a currency symbol. Premium "
            f"copy is DISABLED until this is fixed — a malformed cap must never be "
            f"read as 'no cap'.")
    if cap != cap or cap in (float("inf"), float("-inf")):   # NaN / inf
        raise BudgetCapInvalid(f"premium_daily_usd_cap is {cap!r}, which is not finite.")
    if cap < 0:
        raise BudgetCapInvalid(
            f"premium_daily_usd_cap is {cap!r}. A negative cap is meaningless; use 0 "
            f"to disable premium spend, or null for no cap.")
    return cap if cap > 0 else 0.0        # 0 => spend nothing (NOT unlimited)


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
    """$ left under today's cap, or None if there is no cap (unlimited).

    A malformed cap returns 0.0, not None: premium is refused, so there is
    nothing remaining. Returning None here would render as "∞" on /health, which
    is the exact lie this is meant to stop.
    """
    try:
        cap = daily_cap_usd(cfg)
    except BudgetCapInvalid:
        return 0.0
    if cap is None:
        return None
    return round(max(0.0, cap - spent_today_usd(conn, cfg)), 6)


def premium_allowed(conn, cfg):
    """True if premium copy is ON and today's real-Claude spend is still under the cap."""
    if not premium_enabled(cfg):
        return False
    try:
        cap = daily_cap_usd(cfg)
    except BudgetCapInvalid as e:
        # Fail CLOSED. The guard is unreadable, so we do not spend real money
        # behind it. Loud, because the operator has to fix a config value and
        # nothing else in the run will tell them.
        print(f"⚠️  premium copy DISABLED: {e}")
        return False
    if cap is None:
        return True   # premium on, no cap set
    if cap <= 0:
        return False  # cap of 0 => spend nothing
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
    """A small dict for /health: is premium on, the cap, spent today, remaining.

    `cap_error` carries a malformed-cap message so the page can say so outright
    instead of rendering the misconfiguration as "uncapped · ∞".
    """
    cap = None
    cap_error = None
    try:
        cap = daily_cap_usd(cfg)
    except BudgetCapInvalid as e:
        cap_error = str(e)
    return {
        "premium": premium_enabled(cfg),
        "model": premium_model(cfg),
        "cap_usd": cap,
        "cap_error": cap_error,
        "spent_today_usd": spent_today_usd(conn, cfg),
        "remaining_usd": remaining_usd(conn, cfg),
        "allowed_now": premium_allowed(conn, cfg),
    }
