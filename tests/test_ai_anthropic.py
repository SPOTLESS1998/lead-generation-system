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
_SAVED_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN")
_SAVED_BASE = os.environ.get("ANTHROPIC_BASE_URL")
_SAVED_UA = os.environ.get("ANTHROPIC_USER_AGENT")
_REAL_POST = ai.requests.post
_REAL_FREE = ai._PROVIDER_FUNCS["freellmapi"]

CAP = {}


def clear_env():
    """Deterministic starting point: no key, no token, default (official) base URL.

    The host may have any of these set in its real environment; clear them so the
    URL/header assertions below don't depend on the machine running the tests.
    """
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "ANTHROPIC_USER_AGENT"):
        os.environ.pop(v, None)


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
clear_env()   # neither ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN set
try:
    ai._anthropic_chat({}, "hello")
    check("no key: _anthropic_chat raises", False)
except RuntimeError as e:
    check("no key: _anthropic_chat raises", "ANTHROPIC_API_KEY not set" in str(e))

# Happy path: token mapping + request shape.
clear_env()
set_key("sk-ant-test")
ai.requests.post = fake_post(200)
text, usage = ai._anthropic_chat({"anthropic_model": "claude-opus-4-8"}, "hello")
check("happy: returns the model text", text == "Hi there")
check("happy: input_tokens -> prompt_tokens", usage["prompt_tokens"] == 10)
check("happy: output_tokens -> completion_tokens", usage["completion_tokens"] == 5)
check("happy: total_tokens summed", usage["total_tokens"] == 15)
check("happy: hits the official Messages endpoint by default",
      CAP["url"] == "https://api.anthropic.com/v1/messages")
check("happy: sends the x-api-key header", CAP["headers"].get("x-api-key") == "sk-ant-test")
check("happy: also sends Authorization: Bearer (proxy compatibility)",
      CAP["headers"].get("authorization") == "Bearer sk-ant-test")
check("happy: sends the anthropic-version header", CAP["headers"].get("anthropic-version") == "2023-06-01")
check("happy: sends a Claude-CLI User-Agent (proxies gate on it)",
      "claude-cli" in (CAP["headers"].get("user-agent") or ""))

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
# AgentRouter (and any Anthropic-compatible proxy): base-URL override + Bearer
# --------------------------------------------------------------------------
# Same premium path, pointed at a proxy: ANTHROPIC_BASE_URL sets the host,
# '/v1/messages' is appended, and the key rides as Authorization: Bearer.
ai.requests.post = fake_post(200)   # restore a normal 200 body after the error cases
os.environ["ANTHROPIC_BASE_URL"] = "https://agentrouter.org"
ai._anthropic_chat({}, "x")
check("proxy: posts to <base>/v1/messages", CAP["url"] == "https://agentrouter.org/v1/messages")
check("proxy: sends Authorization: Bearer <key>", CAP["headers"].get("authorization") == "Bearer sk-ant-test")

# The User-Agent is overridable (in case a proxy expects a different signature).
os.environ["ANTHROPIC_USER_AGENT"] = "custom-agent/9.9"
ai._anthropic_chat({}, "x")
check("proxy: User-Agent is overridable via ANTHROPIC_USER_AGENT",
      CAP["headers"].get("user-agent") == "custom-agent/9.9")
os.environ.pop("ANTHROPIC_USER_AGENT", None)

# A base that already ends in /v1 must not become /v1/v1/messages.
os.environ["ANTHROPIC_BASE_URL"] = "https://agentrouter.org/v1"
ai._anthropic_chat({}, "x")
check("proxy: tolerates a base URL that already ends in /v1",
      CAP["url"] == "https://agentrouter.org/v1/messages")

# A trailing slash on the base is fine too.
os.environ["ANTHROPIC_BASE_URL"] = "https://agentrouter.org/"
ai._anthropic_chat({}, "x")
check("proxy: tolerates a trailing slash on the base URL",
      CAP["url"] == "https://agentrouter.org/v1/messages")
os.environ.pop("ANTHROPIC_BASE_URL", None)

# The key may live in ANTHROPIC_AUTH_TOKEN (AgentRouter's own env-var name) instead.
set_key(None)   # drop ANTHROPIC_API_KEY
os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-router-token"
ai._anthropic_chat({}, "x")
check("token: ANTHROPIC_AUTH_TOKEN is used when API key is absent",
      CAP["headers"].get("authorization") == "Bearer sk-router-token")
check("token: it is also sent as x-api-key", CAP["headers"].get("x-api-key") == "sk-router-token")
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


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
clear_env()   # no anthropic creds → the anthropic provider raises "not set"
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
for _name, _val in (("ANTHROPIC_API_KEY", _SAVED_KEY),
                    ("ANTHROPIC_AUTH_TOKEN", _SAVED_TOKEN),
                    ("ANTHROPIC_BASE_URL", _SAVED_BASE),
                    ("ANTHROPIC_USER_AGENT", _SAVED_UA)):
    if _val is None:
        os.environ.pop(_name, None)
    else:
        os.environ[_name] = _val


print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
