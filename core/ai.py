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


# --- per-thread token accounting -------------------------------------------
# observability.track() resets this when a pipeline step begins and collects it
# when the step ends, so the tokens attributed to a step are exactly those spent
# by generate()/generate_json() inside it — with no change at the call sites.
# Thread-local so the multi-threaded Flask approval server never mixes two
# requests' token counts.
_local = threading.local()


def _zero_usage():
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "calls": 0, "provider": None}


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


def _norm_usage(u):
    """Normalize an OpenAI-style usage dict to our three token keys."""
    u = u or {}
    return {
        "prompt_tokens": int(u.get("prompt_tokens") or 0),
        "completion_tokens": int(u.get("completion_tokens") or 0),
        "total_tokens": int(u.get("total_tokens") or 0),
    }


def _freellmapi_chat(cfg, prompt, temperature=0.4, max_tokens=800):
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
                timeout=60,   # the gateway may try several upstreams before one answers
            )
            if resp.status_code != 200:
                raise RuntimeError(f"FreeLLMAPI {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if not content:
                raise RuntimeError("FreeLLMAPI returned empty content")
            return content, _norm_usage(data.get("usage"))
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
    return text, usage


def _nvidia_chat(cfg, prompt, temperature=0.4, max_tokens=400):
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
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"NVIDIA API {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip(), _norm_usage(data.get("usage"))


def _anthropic_chat(cfg, prompt, temperature=0.5, max_tokens=1024):
    """Call the Anthropic Messages API — real Claude, METERED (spends dev credits).

    This is the 'premium' copy path. It is gated by a hard daily $ cap in
    core/budget.py: a caller only puts 'anthropic' in cfg['providers'] when today's
    spend is under the cap, so a bulk run can never drain the shared credit pool that
    also powers Claude Code. If the key is absent this raises, so generate() cleanly
    degrades to the free chain (premium is simply dormant until a key is added).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    model = cfg.get("anthropic_model") \
        or (cfg.get("copy", {}) or {}).get("anthropic_model") or "claude-opus-4-8"
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    parts = [b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("Anthropic returned empty content")
    u = data.get("usage") or {}
    it, ot = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    return text, {"prompt_tokens": it, "completion_tokens": ot, "total_tokens": it + ot}


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


def generate(cfg, prompt):
    """Generate text, trying each configured provider in order until one succeeds.

    Returns (text, provider_used). Providers whose credentials are unset raise and
    are skipped, so the chain degrades gracefully instead of crashing.
    """
    last_err = None
    for provider in _provider_order(cfg):
        try:
            text, usage = _PROVIDER_FUNCS[provider](cfg, prompt)
            _accumulate(provider, usage)
            return text, provider
        except Exception as e:
            last_err = e
            print(f"⚠️  {provider} generation failed ({e}); trying next provider...")
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
