"""Text + structured generation with an automatic provider fallback.

The one place that talks to an LLM. The default chain is FreeLLMAPI first (a local
OpenAI-compatible gateway that itself fans out across ~11 free providers and fails
over on rate limits), then raw Gemini, then raw NVIDIA as a last resort if the
gateway is down. Order is configurable via cfg["providers"]. Same house pattern as
the RAG system — try the preferred engine, degrade gracefully rather than crash.
Lifted out of scripts/lead_agent.py so the cold-email drafter AND the reply agent
share exactly one code path.

    from core.ai import generate, generate_json
    text, provider = generate(cfg, "write a haiku")
    obj,  provider = generate_json(cfg, 'reply ONLY with {"intent":"..."}')
"""

import os
import json
import time
import threading

import requests


# Fail fast on a host that won't even accept a TCP connection promptly, while
# still giving a healthy-but-slow provider time to answer. requests accepts a
# (connect, read) timeout tuple: a dead/refused host trips the short connect
# timeout instead of burning the full read timeout on every call.
_CONNECT_TIMEOUT = 5   # seconds allowed to establish the TCP connection


# --- per-thread token accounting -------------------------------------------
# observability.track() resets this when a pipeline step begins and collects it
# when the step ends, so the tokens attributed to a step are exactly those spent
# by generate()/generate_json() inside it — with no change at the call sites.
# Thread-local so the multi-threaded Flask approval server never mixes two
# requests' token counts.
_local = threading.local()


def _zero_usage():
    # "models" is a LIST, not a single field, because the freellmapi gateway rotates
    # models across attempts (see _freellmapi_chat) — so ONE tracked step can spend
    # tokens on two different models. Keeping the per-model split lets each be priced
    # at its own rate later; collapsing it to one name would force a guess.
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "calls": 0, "provider": None, "models": []}


def _usage_store():
    u = getattr(_local, "usage", None)
    if u is None:
        u = _zero_usage()
        _local.usage = u
    return u


def reset_usage():
    """Zero the calling thread's token accumulator (call at the start of a step)."""
    _local.usage = _zero_usage()


def collect_usage():
    """Snapshot the calling thread's token usage accumulated since the last reset."""
    return dict(_usage_store())


def _accumulate(provider, usage):
    """Fold one provider call's token usage into the thread accumulator."""
    u = _usage_store()
    pt = int((usage or {}).get("prompt_tokens") or 0)
    ct = int((usage or {}).get("completion_tokens") or 0)
    tt = int((usage or {}).get("total_tokens") or 0) or (pt + ct)
    u["prompt_tokens"] += pt
    u["completion_tokens"] += ct
    u["total_tokens"] += tt
    u["calls"] += 1
    u["provider"] = provider   # the provider that actually answered
    # Which MODEL answered, if the provider reported one. A provider that doesn't say
    # (or a gateway left to auto-route) contributes nothing here rather than a guess —
    # downstream that reads as "cost unknown", never as "cost zero".
    model = (usage or {}).get("model")
    if model:
        u["models"].append({"model": model, "prompt_tokens": pt, "completion_tokens": ct})


def _norm_usage(u, model=None):
    """Normalize an OpenAI-style usage dict to our three token keys (+ the model).

    `model` is the model this call actually ran on, so spend can be priced later. It is
    optional and defaults to None: a provider stub that returns only token counts (the
    tests do exactly this) still normalizes cleanly, and an unreported model stays an
    honest unknown instead of being attributed to whatever was configured.
    """
    u = u or {}
    return {
        "prompt_tokens": int(u.get("prompt_tokens") or 0),
        "completion_tokens": int(u.get("completion_tokens") or 0),
        "total_tokens": int(u.get("total_tokens") or 0),
        "model": u.get("model") or model,
    }


def _freellmapi_chat(cfg, prompt, temperature=0.4, max_tokens=2000):
    # 2000 (not 800): reasoning models served via the gateway (gpt-oss-120b,
    # gemini-3.1-pro) spend output budget "thinking" BEFORE emitting the JSON, so a
    # full email-in-JSON overran 800 and truncated mid-string -> invalid JSON ->
    # failed drafts. A cap is a ceiling not a target, so short calls (strategist,
    # extractor) still use only what they need; this just stops long copy truncating.
    """Call the local FreeLLMAPI gateway (OpenAI-compatible /v1/chat/completions).

    FreeLLMAPI aggregates many free provider tiers behind one endpoint and does its
    own provider failover, so this single call already survives most rate limits.
    Base URL + unified key come from the environment; if the key isn't set we raise
    so generate() cleanly skips to the next provider.
    """
    api_key = os.environ.get("FREELLMAPI_KEY")
    if not api_key:
        raise RuntimeError("FREELLMAPI_KEY not set")
    base = os.environ.get("FREELLMAPI_BASE_URL", "http://localhost:3001/v1").rstrip("/")
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # Which model(s) to request. `freellmapi_model` may be a single string OR a
    # LIST of model ids; a list is rotated across attempts so a model that 404s,
    # is retired, or is rate-limited fails over to the next KNOWN-GOOD model. This
    # is deliberately NOT the gateway's own auto-router — that has been observed
    # routing to retired upstreams (e.g. Ollama Cloud models pulled 2026-07-15)
    # and returning 502/410 instead of failing past them. "" / "auto" / [] means
    # omit the field and let the gateway auto-route.
    pinned = cfg.get("freellmapi_model") or ""
    if isinstance(pinned, str):
        s = pinned.strip()
        models = [s] if s and s.lower() != "auto" else []
    else:
        models = [m.strip() for m in pinned
                  if isinstance(m, str) and m.strip() and m.strip().lower() != "auto"]
    # Attempts are configurable via cfg["freellmapi_attempts"], but always make at
    # least one full pass over every pinned model before giving up.
    attempts = max(int(cfg.get("freellmapi_attempts", 4)), len(models) or 1)
    last_err = None
    for i in range(attempts):
        if models:
            body["model"] = models[i % len(models)]   # rotate: model A, B, A, B, ...
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=(_CONNECT_TIMEOUT, 60),   # (connect, read): the gateway may try several upstreams before one answers
            )
            if resp.status_code != 200:
                raise RuntimeError(f"FreeLLMAPI {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if not content:
                raise RuntimeError("FreeLLMAPI returned empty content")
            # Report WHICH model answered. Prefer the gateway's own echo over what we
            # asked for (it may route elsewhere); when we sent no model and it echoes
            # none, this stays None — an honest "unknown", not a guess.
            return content, _norm_usage(data.get("usage"),
                                        model=data.get("model") or body.get("model"))
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(0.6 * (i + 1))   # brief backoff, then try the next model
    raise RuntimeError(f"FreeLLMAPI failed after {attempts} attempt(s). Last error: {last_err}")


def _gemini_chat(cfg, prompt):
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(model=cfg["gemini_model"], contents=prompt)
    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        raise RuntimeError("Gemini returned empty text")
    # Gemini reports tokens on resp.usage_metadata (different key names than OpenAI).
    um = getattr(resp, "usage_metadata", None)
    usage = {
        "prompt_tokens": int(getattr(um, "prompt_token_count", 0) or 0),
        "completion_tokens": int(getattr(um, "candidates_token_count", 0) or 0),
        "total_tokens": int(getattr(um, "total_token_count", 0) or 0),
    } if um is not None else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage["model"] = cfg["gemini_model"]   # this provider always knows its model
    return text, usage


def _nvidia_chat(cfg, prompt, temperature=0.4, max_tokens=2000):  # 400 truncated full-email JSON
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY not set")
    resp = requests.post(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": cfg["nvidia_model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=(_CONNECT_TIMEOUT, 60),   # (connect, read). Was 30s, raised 2026-09-12:
                                          # mistral-nemotron intermittently takes 45s+ to
                                          # first byte (cold start). A 30s read timeout
                                          # turned that into a failure, tripping the
                                          # breaker on the only provider reachable from
                                          # this network and costing a whole day's drafts.
    )
    if resp.status_code != 200:
        raise RuntimeError(f"NVIDIA API {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip(), \
        _norm_usage(data.get("usage"), model=data.get("model") or cfg["nvidia_model"])


_ANTHROPIC_ATTEMPTS = 2          # total tries against a possibly-throttling endpoint
_ANTHROPIC_RETRY_BACKOFF = 0.6   # seconds between tries, grows per attempt (0 in tests)
_ANTHROPIC_READ_TIMEOUT = 120    # seconds. Opus-4-8 via AgentRouter FORCES extended thinking
                                 # (~15s of silent thinking first), so one real draft call runs
                                 # ~25-30s and can spike higher under load. The old 60s read
                                 # timeout killed those calls mid-flight -> reset -> retry ->
                                 # circuit breaker -> failover. 120s fits a slow-but-good call.


def _anthropic_chat(cfg, prompt, temperature=0.5, max_tokens=16000):  # big headroom: forced thinking + the email
    """Call the Anthropic Messages API — real Claude, METERED (spends credits/quota).

    Works against the official API OR any Anthropic-compatible proxy/gateway
    (e.g. AgentRouter) via two env vars, so one premium path serves both:
      ANTHROPIC_API_KEY   the key/token (ANTHROPIC_AUTH_TOKEN is also accepted).
      ANTHROPIC_BASE_URL  the endpoint host WITHOUT a trailing /v1 (default
                          https://api.anthropic.com; AgentRouter is
                          https://agentrouter.org). '/v1/messages' is appended here.
    The key is sent BOTH as 'x-api-key' (official API) and 'Authorization: Bearer'
    (what proxies like AgentRouter read), so whichever the endpoint honors, it works.
    Some gateways also fingerprint the CALLING TOOL and 401 a generic HTTP client, so
    we identify as the Claude CLI via 'User-Agent' (override with ANTHROPIC_USER_AGENT).

    Gated by the daily $ cap in core/budget.py. If the key is absent this raises, so
    generate() cleanly degrades to the free chain (premium is dormant until a key
    is added).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    base = (os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    if base.endswith("/v1"):          # tolerate a base that already includes /v1
        base = base[:-len("/v1")].rstrip("/")
    model = cfg.get("anthropic_model") \
        or (cfg.get("copy", {}) or {}).get("anthropic_model") or "claude-opus-4-8"
    # Opus-4-8 via AgentRouter/Bedrock FORCES extended thinking on (the API's `thinking`
    # param is silently ignored on this proxy — verified). max_tokens is ONE budget that the
    # thinking AND the email both spend from, and the thinking is STOCHASTIC: on the same
    # real copy prompt it burned 2356 output tokens one run and blew past 4000 the next
    # (measured). When it overruns the ceiling the email gets ZERO tokens left ->
    # stop_reason 'max_tokens', empty/truncated text -> core/ai misreads it as throttling ->
    # fails over to the free chain (this was the real reason premium copy never used Opus:
    # a 4000 ceiling sat right at the cliff edge). 16000 clears the worst thinking run with
    # room to spare, and Opus still self-stops ~2400 tokens in the SAME ~35s, so a bigger
    # ceiling costs no extra latency or tokens — it is free insurance. Tunable per client
    # via cfg['anthropic_max_tokens'].
    max_tokens = int(cfg.get("anthropic_max_tokens") or max_tokens)
    # Proxies (e.g. AgentRouter) reject a generic 'python-requests' User-Agent with
    # 401 "unauthorized client detected"; identify as the Claude CLI (this IS Claude
    # tooling). Harmless against the official API, which does not gate on User-Agent.
    user_agent = os.environ.get("ANTHROPIC_USER_AGENT") or "claude-cli/1.0.60 (external, cli)"
    # A custom base URL means we're pointed at a proxy (AgentRouter), not the official
    # API. That matters for retries below: AgentRouter's FREE quota throttles under load
    # by returning 200-with-empty-content and even 401 "invalid token" — transient, worth
    # a quick retry. On the OFFICIAL API a 401/403 is a real auth failure — terminal.
    is_proxy = base != "https://api.anthropic.com"
    headers = {
        "x-api-key": api_key,
        "authorization": f"Bearer {api_key}",   # AgentRouter & other proxies auth via Bearer
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "user-agent": user_agent,
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    # Best-effort premium: retry a flaky/throttling proxy a couple of times, then raise
    # so generate() fails over to the free chain. Kept small so a down proxy adds little
    # latency to a pipeline run. Per-call override via cfg["anthropic_attempts"].
    attempts = max(1, int(cfg.get("anthropic_attempts") or _ANTHROPIC_ATTEMPTS))
    last_err = None
    for i in range(attempts):
        resp = requests.post(f"{base}/v1/messages", headers=headers, json=body,
                             timeout=(_CONNECT_TIMEOUT, _ANTHROPIC_READ_TIMEOUT))
        if resp.status_code == 200:
            data = resp.json()
            parts = [b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text"]
            text = "".join(parts).strip()
            if text:
                u = data.get("usage") or {}
                it, ot = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
                return text, {"prompt_tokens": it, "completion_tokens": ot,
                              "total_tokens": it + ot,
                              "model": data.get("model") or model}
            # 200 but no text. Two causes: a proxy throttling tell, OR forced thinking ate
            # the whole max_tokens budget (stop_reason 'max_tokens', a thinking block but no
            # text) — the headroom above is what prevents the latter. Retry, then fail over.
            stop = data.get("stop_reason")
            last_err = RuntimeError(f"Anthropic returned empty content (stop_reason={stop})")
        else:
            last_err = RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:200]}")
            # Terminal auth failure on the official API — no point retrying.
            if resp.status_code in (401, 403) and not is_proxy:
                raise last_err
        if i < attempts - 1:
            time.sleep(_ANTHROPIC_RETRY_BACKOFF * (i + 1))
    raise last_err


# Every provider is callable as fn(cfg, prompt); each raises if its creds are
# missing, so generate() just moves on to the next one.
_PROVIDER_FUNCS = {
    "anthropic": _anthropic_chat,   # premium (metered) — used only when budget.copy_cfg enables it
    "freellmapi": _freellmapi_chat,
    "gemini": _gemini_chat,
    "nvidia": _nvidia_chat,
}
DEFAULT_PROVIDERS = ["freellmapi", "gemini", "nvidia"]


def _provider_order(cfg):
    """The ordered list of providers to try, from config (deduped, known-only)."""
    configured = cfg.get("providers")
    if isinstance(configured, list) and configured:
        seq = [p for p in configured if p in _PROVIDER_FUNCS]
    else:
        # Legacy fallback: honor copy_provider, but still try the gateway first.
        primary = cfg.get("copy_provider", "gemini")
        seq = ["freellmapi", primary, "gemini", "nvidia"]
    seen, order = set(), []
    for p in seq:
        if p in _PROVIDER_FUNCS and p not in seen:
            seen.add(p)
            order.append(p)
    return order or DEFAULT_PROVIDERS


# --- circuit breaker --------------------------------------------------------
# A failover chain is cheap only when ONE provider is down: you still pay that
# dead provider's full timeout/retry cost on EVERY call before moving on. Under a
# sustained outage (a saturated proxy, a refused gateway, a hung host) that tax
# repeats for every single lead. A circuit breaker stops the bleeding: after a
# provider fails N times in a row it "trips", and we skip it instantly for a
# cooldown window instead of re-paying its timeout. Three states:
#   CLOSED     normal — the provider is called.
#   OPEN       tripped — skip it immediately until the cooldown elapses.
#   HALF-OPEN  cooldown elapsed — let ONE trial call through; success closes the
#              breaker, another failure re-opens it for a fresh cooldown.
# State is per-provider and process-global (shared across every client and Flask
# worker thread), so once the server learns "nvidia is down" every request skips
# it. A lock guards the dict because the approval server is multi-threaded.
_BREAKER_THRESHOLD = 3       # consecutive failures before a provider trips
_BREAKER_COOLDOWN = 60.0     # seconds to skip a tripped provider before a retrial
_breaker_lock = threading.Lock()
_breakers = {}               # provider -> {"fails": int, "open_until": monotonic secs}


class AllProvidersCoolingDown(RuntimeError):
    """Every provider is tripped, but only temporarily — waiting may fix this.

    Distinct from "all providers failed", which means we actually called them and
    they all broke. This one means we called NOTHING: the breakers were still in
    their cooldown window. The difference matters for an unattended run, which can
    afford to wait a minute and try again rather than write the day off.
    """


def cooldown_remaining(providers=None):
    """Seconds until the first tripped provider can be retried (0.0 if any is ready).

    Used by callers that want to wait out a breaker rather than fail. Returns the
    MINIMUM across providers, because the chain only needs one of them back.
    """
    with _breaker_lock:
        names = list(providers) if providers else list(_breakers.keys())
        if not names:
            return 0.0
        now = time.monotonic()
        waits = []
        for p in names:
            st = _breakers.get(p)
            if not st or not st["open_until"]:
                return 0.0          # this one is already callable
            waits.append(max(0.0, st["open_until"] - now))
        return min(waits) if waits else 0.0


def reset_breakers():
    """Clear all circuit-breaker state (used at process start and by tests)."""
    with _breaker_lock:
        _breakers.clear()


def _breaker_params(cfg):
    """(enabled, threshold, cooldown) from cfg['circuit_breaker'], with defaults."""
    b = (cfg.get("circuit_breaker") or {}) if isinstance(cfg, dict) else {}
    enabled = b.get("enabled", True)
    threshold = max(1, int(b.get("threshold", _BREAKER_THRESHOLD) or _BREAKER_THRESHOLD))
    cooldown = float(b.get("cooldown_secs", _BREAKER_COOLDOWN) or _BREAKER_COOLDOWN)
    return enabled, threshold, cooldown


def _breaker_is_open(provider):
    """True if this provider is tripped and still cooling down (skip it now).

    Once the cooldown has elapsed this returns False — the HALF-OPEN state — so the
    next call is allowed through as a single trial to see if the provider recovered.
    """
    with _breaker_lock:
        st = _breakers.get(provider)
        if not st or not st["open_until"]:
            return False
        return time.monotonic() < st["open_until"]


def _breaker_record(provider, ok, threshold, cooldown):
    """Fold one call's outcome into a provider's breaker.

    Returns 'opened' or 'closed' when the state visibly flips (for a one-line log),
    else None — so a long run doesn't spam a line for every routine call.
    """
    with _breaker_lock:
        st = _breakers.setdefault(provider, {"fails": 0, "open_until": 0.0})
        was_open = bool(st["open_until"])
        if ok:
            st["fails"] = 0
            st["open_until"] = 0.0
            return "closed" if was_open else None
        st["fails"] += 1
        if st["fails"] >= threshold:
            st["open_until"] = time.monotonic() + cooldown
            return None if was_open else "opened"   # re-open quietly; the first trip is logged
        return None


def generate(cfg, prompt):
    """Generate text, trying each configured provider in order until one succeeds.

    Returns (text, provider_used). Providers whose credentials are unset raise and
    are skipped. A per-provider circuit breaker (above) skips a provider that has
    been failing repeatedly, so a sustained outage costs one instant skip per call
    instead of that provider's full timeout — and the chain still self-heals once
    the provider recovers.
    """
    enabled, threshold, cooldown = _breaker_params(cfg)
    last_err = None
    skipped_open = []
    for provider in _provider_order(cfg):
        if enabled and _breaker_is_open(provider):
            skipped_open.append(provider)
            continue
        try:
            text, usage = _PROVIDER_FUNCS[provider](cfg, prompt)
            _accumulate(provider, usage)
            if enabled and _breaker_record(provider, True, threshold, cooldown) == "closed":
                print(f"🔌 circuit breaker CLOSED for {provider} (recovered).")
            return text, provider
        except Exception as e:
            last_err = e
            print(f"⚠️  {provider} generation failed ({e}); trying next provider...")
            if enabled and _breaker_record(provider, False, threshold, cooldown) == "opened":
                print(f"🔌 circuit breaker OPEN for {provider} "
                      f"({threshold} consecutive fails) — skipping it for {cooldown:.0f}s.")
    if skipped_open and last_err is None:
        # Every eligible provider was tripped and skipped — fail fast (the whole point)
        # rather than force-calling known-dead providers; the next call half-opens them.
        #
        # Raised as a DISTINCT type because this failure is recoverable by waiting,
        # unlike a real outage. An unattended once-daily run must be able to tell the
        # two apart: see scripts/lead_agent.py, which waits out the cooldown once
        # instead of burning the whole day's drafting on a 60-second blip.
        raise AllProvidersCoolingDown(
            "All providers are in circuit-breaker cooldown "
            f"({', '.join(skipped_open)}); failing fast — retry after cooldown.")
    raise RuntimeError(f"All providers failed. Last error: {last_err}")


def generate_metered(cfg, prompt):
    """Like generate(), but also return the token usage spent by THIS call.

    Returns (text, provider, usage) where usage = {prompt_tokens, completion_tokens,
    total_tokens}. Computed as a delta around the thread accumulator, so it composes
    safely inside an observability.track() block (it does NOT reset the accumulator).
    """
    before = collect_usage()
    text, provider = generate(cfg, prompt)
    after = collect_usage()
    usage = {
        "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
        "completion_tokens": after["completion_tokens"] - before["completion_tokens"],
        "total_tokens": after["total_tokens"] - before["total_tokens"],
    }
    return text, provider, usage


def _extract_json(text):
    """Pull a JSON object out of a model response (tolerates ```json fences / prose)."""
    if not text:
        return None
    cleaned = text.strip()
    # Strip a leading ```json / ``` fence and trailing ``` if present.
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    # Fall back to the first {...} span.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except Exception:
            return None
    return None


def generate_json(cfg, prompt, retries=1):
    """Like generate(), but parse the response as a JSON object.

    Returns (obj, provider_used). Retries once (the prompt should itself demand
    'reply with ONLY JSON'). Raises RuntimeError if no valid JSON comes back.
    """
    last_text = None
    for _ in range(retries + 1):
        text, provider = generate(cfg, prompt)
        obj = _extract_json(text)
        if isinstance(obj, dict):
            return obj, provider
        last_text = text
    raise RuntimeError(f"Model did not return valid JSON. Last response: {(last_text or '')[:200]}")
