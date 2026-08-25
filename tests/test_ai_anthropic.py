"""Offline tests for the premium (real-Claude) provider in core/ai.py.

Proves the Anthropic Messages path WITHOUT a key and WITHOUT network:
  - ai.requests.post is monkeypatched with a fake that captures the request,
  - the ANTHROPIC_API_KEY env var is toggled and restored around each case,
  - graceful degradation is checked by patching a free provider into the chain.

Run:  venv/bin/python tests/test_ai_anthropic.py
"""

import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import ai          # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


# ---- things we monkeypatch; restored at the end ----
_SAVED_KEY = os.environ.get("ANTHROPIC_API_KEY")
_REAL_POST = ai.requests.post
_REAL_FREE = ai._PROVIDER_FUNCS["freellmapi"]

CAP = {}


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def fake_post(status=200, payload=None):
    """Return a requests.post stand-in that records the outgoing request."""
    default = {"content": [{"type": "text", "text": "Hi there"}],
               "usage": {"input_tokens": 10, "output_tokens": 5}}

    def _post(url, headers=None, json=None, timeout=None):
        CAP["url"] = url
        CAP["headers"] = headers or {}
        CAP["body"] = json or {}
        return FakeResp(status, default if payload is None else payload)

    return _post


def set_key(v):
    if v is None:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        os.environ["ANTHROPIC_API_KEY"] = v


# --------------------------------------------------------------------------
# _anthropic_chat — the metered premium call
# --------------------------------------------------------------------------
# Key absent → raises so generate() can skip to the free chain.
set_key(None)
try:
    ai._anthropic_chat({}, "hello")
    check("no key: _anthropic_chat raises", False)
except RuntimeError as e:
    check("no key: _anthropic_chat raises", "ANTHROPIC_API_KEY not set" in str(e))

# Happy path: token mapping + request shape.
set_key("sk-ant-test")
ai.requests.post = fake_post(200)
text, usage = ai._anthropic_chat({"anthropic_model": "claude-opus-4-8"}, "hello")
check("happy: returns the model text", text == "Hi there")
check("happy: input_tokens -> prompt_tokens", usage["prompt_tokens"] == 10)
check("happy: output_tokens -> completion_tokens", usage["completion_tokens"] == 5)
check("happy: total_tokens summed", usage["total_tokens"] == 15)
check("happy: hits the Messages endpoint", CAP["url"] == "https://api.anthropic.com/v1/messages")
check("happy: sends the x-api-key header", CAP["headers"].get("x-api-key") == "sk-ant-test")
check("happy: sends the anthropic-version header", CAP["headers"].get("anthropic-version") == "2023-06-01")

# Model precedence: top-level anthropic_model > copy.anthropic_model > default.
ai._anthropic_chat({"anthropic_model": "m-top"}, "x")
check("model: top-level anthropic_model wins", CAP["body"].get("model") == "m-top")
ai._anthropic_chat({"copy": {"anthropic_model": "m-copy"}}, "x")
check("model: falls back to copy.anthropic_model", CAP["body"].get("model") == "m-copy")
ai._anthropic_chat({}, "x")
check("model: defaults to claude-opus-4-8", CAP["body"].get("model") == "claude-opus-4-8")

# Non-200 → raises with the status code in the message.
ai.requests.post = fake_post(500, {"error": "boom"})
try:
    ai._anthropic_chat({}, "x")
    check("non-200: raises", False)
except RuntimeError as e:
    check("non-200: raises with the status code", "500" in str(e))

# Empty / no text content → raises.
ai.requests.post = fake_post(200, {"content": [], "usage": {}})
try:
    ai._anthropic_chat({}, "x")
    check("empty content: raises", False)
except RuntimeError as e:
    check("empty content: raises", "empty" in str(e).lower())

# A response with only non-text blocks is also "empty" for our purposes.
ai.requests.post = fake_post(200, {"content": [{"type": "thinking", "text": "ignore me"}], "usage": {}})
try:
    ai._anthropic_chat({}, "x")
    check("non-text-only content: raises", False)
except RuntimeError:
    check("non-text-only content: raises", True)


# --------------------------------------------------------------------------
# registration + provider order
# --------------------------------------------------------------------------
check("anthropic is a registered provider", "anthropic" in ai._PROVIDER_FUNCS)
check("default chain excludes anthropic (premium is opt-in)",
      ai.DEFAULT_PROVIDERS == ["freellmapi", "gemini", "nvidia"])

order = ai._provider_order({"providers": ["anthropic", "freellmapi", "gemini", "bogus"]})
check("_provider_order keeps anthropic first, drops unknown providers",
      order == ["anthropic", "freellmapi", "gemini"])
check("_provider_order dedupes repeats",
      ai._provider_order({"providers": ["anthropic", "anthropic", "freellmapi"]}) == ["anthropic", "freellmapi"])


# --------------------------------------------------------------------------
# generate() degrades gracefully: no key → skip anthropic → free provider answers
# --------------------------------------------------------------------------
set_key(None)   # anthropic will raise "not set"
ai._PROVIDER_FUNCS["freellmapi"] = lambda cfg, p: ("free answer",
                                                   {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
ai.reset_usage()
text, provider = ai.generate({"providers": ["anthropic", "freellmapi"]}, "hi")
check("degrade: skips keyless anthropic, free provider answers", text == "free answer")
check("degrade: reports the provider that actually answered", provider == "freellmapi")
u = ai.collect_usage()
check("degrade: usage attributed to the answering provider", u["provider"] == "freellmapi" and u["total_tokens"] == 3)


# ---- restore everything we touched ----
ai.requests.post = _REAL_POST
ai._PROVIDER_FUNCS["freellmapi"] = _REAL_FREE
set_key(_SAVED_KEY)


print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
