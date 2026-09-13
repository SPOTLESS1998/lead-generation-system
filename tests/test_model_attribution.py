"""Offline tests for MODEL ATTRIBUTION — which model actually spent each step's tokens.

Why this exists: the ledger's `provider` column says "freellmapi", but that gateway
ROTATES across a pinned list of models per attempt (see core/ai.py _freellmapi_chat).
So the provider alone can never tell you what a step cost — two runs of the same step
can hit models whose prices differ by ~50x. Without the model recorded, real token
counts sit in the ledger with genuinely unknown cost.

These tests pin the three properties that make spend priceable without ever inventing
a number:

  1. BACK-COMPAT — a provider that reports only token counts (every existing test
     monkeypatches exactly that shape) still works, and contributes NO model rather
     than being attributed to whatever happened to be configured.
  2. ATTRIBUTION  — a provider that does report its model gets it persisted into the
     ledger row's existing `meta` JSON, alongside any meta the caller already set.
  3. ROTATION     — when one tracked step spans two models, BOTH are kept with their
     own token counts, so each prices at its own rate.

An unreported model means "cost unknown", which is NOT the same as "cost zero".

Run:  venv/bin/python tests/test_model_attribution.py
"""

import json
import os
import sys
import tempfile

os.environ["UNSUB_SECRET"] = "test-secret-123"
os.environ.setdefault("SMTP_USER", "safe-inbox@example.com")
os.environ.setdefault("SMTP_PASS", "dummy-pass")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("NVIDIA_API_KEY", "x")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config, state, ai              # noqa: E402
from core import observability as obs           # noqa: E402

CLIENT = "demo"
cfg = config.load_client(CLIENT)
tmpdb = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
cfg["paths"]["db"] = tmpdb
conn = state.connect(tmpdb)

P = 0; F = 0
def ok(name, cond):
    global P, F
    if cond: P += 1; print(f"  ✅ {name}")
    else:    F += 1; print(f"  ❌ {name}")


print("\n--- A. back-compat: a provider that reports NO model ------------")
# This is the exact shape every other test file monkeypatches in.
ai._PROVIDER_FUNCS["freellmapi"] = lambda c, p: (
    "t", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
cfg["providers"] = ["freellmapi"]
ai.reset_usage()
ai.generate(cfg, "hi")
u = ai.collect_usage()
ok("provider still recorded", u["provider"] == "freellmapi")
ok("tokens still summed", u["total_tokens"] == 3)
ok("no model reported => empty list, never a guess", u["models"] == [])

print("\n--- B. _norm_usage defaults the model to an honest None ---------")
n = ai._norm_usage({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
ok("three token keys untouched", (n["prompt_tokens"], n["completion_tokens"], n["total_tokens"]) == (1, 2, 3))
ok("model is None when nobody said", n["model"] is None)
ok("model passes through when given",
   ai._norm_usage({}, model="gemini-2.5-flash")["model"] == "gemini-2.5-flash")

print("\n--- C. a reported model reaches the ledger row ------------------")
ai._PROVIDER_FUNCS["freellmapi"] = lambda c, p: ("t", ai._norm_usage(
    {"prompt_tokens": 9236, "completion_tokens": 4069}, model="mistral-large-latest"))
ai.reset_usage()
with obs.track(conn, cfg, "draft", subject="buyer@acme.com", run_id="R1") as ev:
    ai.generate(cfg, "draft a cold email")
    ev.note(subject_line="Quick idea for Acme")
r = state.list_events(conn, CLIENT, step="draft")[0]
meta = json.loads(r["meta"])
ok("model persisted into meta", meta["models"][0]["model"] == "mistral-large-latest")
ok("carrying its own token counts", meta["models"][0]["prompt_tokens"] == 9236)
ok("meta the caller set still there", meta["subject_line"] == "Quick idea for Acme")
ok("row token columns unchanged", r["prompt_tokens"] == 9236 and r["completion_tokens"] == 4069)
ok("provider column unchanged", r["provider"] == "freellmapi")

print("\n--- D. rotation: ONE step, TWO models ---------------------------")
# The real freellmapi behaviour: attempt 1 fails over to a different model.
ai.reset_usage()
with obs.track(conn, cfg, "rotate", run_id="R2"):
    ai._accumulate("freellmapi", ai._norm_usage(
        {"prompt_tokens": 100, "completion_tokens": 10}, model="openai/gpt-oss-120b"))
    ai._accumulate("freellmapi", ai._norm_usage(
        {"prompt_tokens": 200, "completion_tokens": 20}, model="gemini-2.5-flash"))
r = state.list_events(conn, CLIENT, step="rotate")[0]
ms = json.loads(r["meta"])["models"]
ok("both models kept, not collapsed to one", len(ms) == 2)
ok("each keeps its own split", ms[0]["prompt_tokens"] == 100 and ms[1]["prompt_tokens"] == 200)
ok("row total is still the sum of both", r["prompt_tokens"] == 300)

print("\n--- E. a step with no LLM call adds no model noise --------------")
with obs.track(conn, cfg, "send", subject="nobody@acme.com") as ev:
    ev.skip("nothing to send")
r = state.list_events(conn, CLIENT, step="send")[0]
ok("meta holds only what the caller set", json.loads(r["meta"]) == {"skip_reason": "nothing to send"})

conn.close()
os.unlink(tmpdb)
print(f"\n=========== {P} passed, {F} failed ===========")
sys.exit(1 if F else 0)
