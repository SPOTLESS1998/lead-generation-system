"""Text + structured generation with an automatic provider fallback.

The one place that talks to an LLM. Gemini is the mandated primary; NVIDIA is the
automatic fallback (same pattern the RAG system uses — try the mandated engine,
degrade gracefully rather than crash). Lifted out of scripts/lead_agent.py so the
cold-email drafter AND the reply agent share exactly one code path.

    from core.ai import generate, generate_json
    text, provider = generate(cfg, "write a haiku")
    obj,  provider = generate_json(cfg, 'reply ONLY with {"intent":"..."}')
"""

import os
import json

import requests


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


def generate(cfg, prompt):
    """Generate text using the configured provider, falling back to the other.

    Returns (text, provider_used). Tries the mandated engine first, then degrades.
    """
    order = ["gemini", "nvidia"] if cfg.get("copy_provider") == "gemini" else ["nvidia", "gemini"]
    last_err = None
    for provider in order:
        try:
            text = _gemini_chat(cfg, prompt) if provider == "gemini" else _nvidia_chat(cfg, prompt)
            return text, provider
        except Exception as e:
            last_err = e
            print(f"⚠️  {provider} generation failed ({e}); trying fallback...")
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
