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

import requests


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
    # A specific model can be pinned via config; "" / "auto" means let the gateway
    # auto-route (this build rejects a literal "auto", so we simply omit the field).
    model = (cfg.get("freellmapi_model") or "").strip()
    if model and model.lower() != "auto":
        body["model"] = model
    # Each call re-rolls the gateway's internal routing, so a transient 502 (an
    # upstream that's momentarily unreachable — e.g. a blocked Gemini endpoint — or
    # a tier that needs billing) is usually cleared by simply trying again: the
    # retry lands on a healthy provider (Mistral, Groq, Cerebras, ...) instead of
    # letting generate() cascade to a dead direct-Gemini / slow direct-NVIDIA.
    # Attempts are configurable via cfg["freellmapi_attempts"].
    attempts = max(1, int(cfg.get("freellmapi_attempts", 4)))
    last_err = None
    for i in range(attempts):
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=60,   # the gateway may try several upstreams before one answers
            )
            if resp.status_code != 200:
                raise RuntimeError(f"FreeLLMAPI {resp.status_code}: {resp.text[:200]}")
            content = (resp.json()["choices"][0]["message"].get("content") or "").strip()
            if not content:
                raise RuntimeError("FreeLLMAPI returned empty content")
            return content
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(0.6 * (i + 1))   # brief backoff, then re-roll the routing
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
    return text


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
    return resp.json()["choices"][0]["message"]["content"].strip()


# Every provider is callable as fn(cfg, prompt); each raises if its creds are
# missing, so generate() just moves on to the next one.
_PROVIDER_FUNCS = {
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
            text = _PROVIDER_FUNCS[provider](cfg, prompt)
            return text, provider
        except Exception as e:
            last_err = e
            print(f"⚠️  {provider} generation failed ({e}); trying next provider...")
    raise RuntimeError(f"All providers failed. Last error: {last_err}")


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
