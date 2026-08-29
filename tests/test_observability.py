"""Offline tests for the observability ledger + token/cost metering.

Covers: the per-thread token accumulator in core.ai, generate()/generate_metered()
capture, compute_cost() math, the track() context manager (ok / skipped / error /
meta), record_event/list_events, and the metrics() rollup incl. unit economics.

The LLM providers are monkeypatched at the _PROVIDER_FUNCS seam, so NO network
happens and the real accumulate → generate → track path is exercised end to end.
Run:  venv/bin/python tests/test_observability.py
"""

import os
import sys
import json
import tempfile

os.environ["UNSUB_SECRET"] = "test-secret-123"
os.environ.setdefault("SMTP_USER", "safe-inbox@example.com")
os.environ.setdefault("SMTP_PASS", "dummy-pass")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("NVIDIA_API_KEY", "x")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config, state, ai                    # noqa: E402
from core import observability as obs                 # noqa: E402

CLIENT = "demo"
cfg = config.load_client(CLIENT)
tmpdb = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
cfg["paths"]["db"] = tmpdb
# A concrete reference rate so cost math is non-zero and checkable.
cfg["observability"]["cost"] = {"rate_per_million_input": 3.0,
                                "rate_per_million_output": 15.0,
                                "margin_multiplier": 2.0}
conn = state.connect(tmpdb)

P = 0; F = 0
def ok(name, cond):
    global P, F
    if cond: P += 1; print(f"  ✅ {name}")
    else:    F += 1; print(f"  ❌ {name}")

def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol

# A fake provider that reports a fixed token spend, wired in at the _PROVIDER_FUNCS
# seam so generate() runs its real accumulate path against it.
def fake_provider(pt, ct):
    def _fn(cfg, prompt):
        return "generated text", {"prompt_tokens": pt, "completion_tokens": ct,
                                  "total_tokens": pt + ct}
    return _fn

def use_provider(pt, ct, name="freellmapi"):
    ai._PROVIDER_FUNCS[name] = fake_provider(pt, ct)
    cfg["providers"] = [name]

print("\n--- A. token accumulator (reset / accumulate / collect) -------")
ai.reset_usage()
u = ai.collect_usage()
ok("reset zeroes the accumulator", u["prompt_tokens"] == 0 and u["completion_tokens"] == 0 and u["calls"] == 0)
ai._accumulate("p1", {"prompt_tokens": 10, "completion_tokens": 5})
ai._accumulate("p2", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
u = ai.collect_usage()
ok("accumulate sums prompt tokens", u["prompt_tokens"] == 11)
ok("accumulate sums completion tokens", u["completion_tokens"] == 7)
ok("total falls back to p+c when absent, else uses given", u["total_tokens"] == 15 + 3)
ok("call count tracked", u["calls"] == 2)
ok("provider = last one that answered", u["provider"] == "p2")

print("\n--- B. generate() feeds the accumulator; generate_metered delta ")
use_provider(120, 30)
ai.reset_usage()
text, prov = ai.generate(cfg, "hi")
u = ai.collect_usage()
ok("generate returns (text, provider)", text == "generated text" and prov == "freellmapi")
ok("generate accumulated the provider's tokens", u["prompt_tokens"] == 120 and u["completion_tokens"] == 30)
t2, p2, usage2 = ai.generate_metered(cfg, "hi again")
ok("generate_metered returns just THIS call's tokens (delta)",
   usage2["prompt_tokens"] == 120 and usage2["completion_tokens"] == 30)
ok("accumulator is cumulative across both calls", ai.collect_usage()["prompt_tokens"] == 240)

print("\n--- C. compute_cost math --------------------------------------")
# 1M in @ $3 + 1M out @ $15 = $18 base; × margin 2 = $36.
ok("priced at reference rate × margin", approx(obs.compute_cost(cfg, 1_000_000, 1_000_000), 36.0))
ok("zero tokens => $0", obs.compute_cost(cfg, 0, 0) == 0.0)
zcfg = {"observability": {"cost": {}}}
ok("no rates configured => $0 (tokens still count elsewhere)", obs.compute_cost(zcfg, 1_000_000, 1_000_000) == 0.0)

print("\n--- D. track(): ok path records tokens + cost + duration ------")
use_provider(100, 40)
run = obs.new_run_id()
with obs.track(conn, cfg, "draft", subject="buyer@acme.com", run_id=run) as ev:
    ai.generate(cfg, "draft a cold email")
    ev.note(subject_line="Quick idea for Acme")
rows = state.list_events(conn, CLIENT, step="draft")
ok("one draft event written", len(rows) == 1)
r = rows[0]
ok("status ok", r["status"] == "ok")
ok("captured prompt/completion tokens", r["prompt_tokens"] == 100 and r["completion_tokens"] == 40)
ok("priced the step (100·3/1e6 + 40·15/1e6)·2 = 0.0018", approx(r["cost_usd"], 0.0018))
ok("recorded the provider", r["provider"] == "freellmapi")
ok("duration_ms present (>=0)", r["duration_ms"] is not None and r["duration_ms"] >= 0)
ok("run_id threaded through", r["run_id"] == run)
ok("meta note stored as JSON", json.loads(r["meta"])["subject_line"] == "Quick idea for Acme")

print("\n--- E. track(): skip path -------------------------------------")
with obs.track(conn, cfg, "reply", subject="nobody@acme.com") as ev:
    ev.skip("no new replies")
r = state.list_events(conn, CLIENT, step="reply")[0]
ok("skip => status skipped", r["status"] == "skipped")
ok("skip reason captured in meta", json.loads(r["meta"])["skip_reason"] == "no new replies")

print("\n--- F. track(): error path records AND re-raises ---------------")
raised = False
try:
    with obs.track(conn, cfg, "send", subject="oops@acme.com"):
        raise ValueError("smtp exploded")
except ValueError:
    raised = True
ok("exception re-raised (not swallowed)", raised)
r = state.list_events(conn, CLIENT, step="send")[0]
ok("error event recorded", r["status"] == "error")
ok("error message captured", "ValueError" in r["error"] and "smtp exploded" in r["error"])

print("\n--- G. metrics(): rollup + unit economics ---------------------")
# Use a clean, separate client so counts are deterministic.
MC = "metrics_client"
mcfg = dict(cfg); mcfg["client"] = MC
state.record_event(conn, MC, "draft", "ok", prompt_tokens=100, completion_tokens=50, cost_usd=0.001, duration_ms=200)
state.record_event(conn, MC, "draft", "ok", prompt_tokens=200, completion_tokens=60, cost_usd=0.002, duration_ms=400)
state.record_event(conn, MC, "send", "ok", duration_ms=50)
state.record_event(conn, MC, "send", "error", error="smtp", duration_ms=10)
state.record_event(conn, MC, "reply", "skipped")
state.upsert_lead(conn, MC, {"email": "l1@mc.com"})
state.upsert_lead(conn, MC, {"email": "l2@mc.com"})
state.record_booking(conn, MC, "l1@mc.com", "evt1", "2026-08-25T10:00")

m = obs.metrics(conn, mcfg)
t = m["totals"]
ok("counts all 5 events", t["events"] == 5)
ok("ok/error/skipped split right", t["ok"] == 3 and t["error"] == 1 and t["skipped"] == 1)
ok("error_rate = 1/5", approx(t["error_rate"], 0.2))
ok("prompt tokens summed", t["prompt_tokens"] == 300)
ok("completion tokens summed", t["completion_tokens"] == 110)
ok("total tokens = 410", t["total_tokens"] == 410)
ok("cost summed = 0.003", approx(t["cost_usd"], 0.003))
ok("per-step draft has 2 events", m["by_step"]["draft"]["events"] == 2)
ok("per-step send counts the error", m["by_step"]["send"]["error"] == 1)
ue = m["unit_economics"]
ok("leads counted", ue["leads"] == 2)
ok("bookings counted", ue["bookings"] == 1)
ok("cost per lead = 0.003/2", approx(ue["cost_per_lead"], 0.0015))
ok("cost per booking = 0.003/1", approx(ue["cost_per_booking"], 0.003))

print("\n--- H. run_metrics(): scoped to ONE run + console summary ------")
RC = "run_client"
rcfg = dict(cfg); rcfg["client"] = RC
state.record_event(conn, RC, "draft", "ok", prompt_tokens=100, completion_tokens=40, cost_usd=0.0, duration_ms=300, run_id="RUN_A")
state.record_event(conn, RC, "draft", "error", error="boom", prompt_tokens=10, completion_tokens=0, cost_usd=0.0, duration_ms=20, run_id="RUN_A")
# A DIFFERENT run's event must be excluded from RUN_A's summary.
state.record_event(conn, RC, "draft", "ok", prompt_tokens=999, completion_tokens=999, cost_usd=0.0, duration_ms=999, run_id="RUN_B")

rm = obs.run_metrics(conn, rcfg, "RUN_A")
rt = rm["totals"]
ok("run_metrics counts only this run's events", rt["events"] == 2)
ok("run_metrics excludes other runs' tokens", rt["prompt_tokens"] == 110)  # 100+10, not the 999
ok("run_metrics total tokens = 150", rt["total_tokens"] == 150)
ok("run_metrics counts this run's ok drafts", rm["unit_economics"]["drafts"] == 1)
ok("list_events run_id filter scopes rows", len(state.list_events(conn, RC, run_id="RUN_B")) == 1)

summary = obs.format_run_summary(rm)
ok("format_run_summary returns a string", isinstance(summary, str) and "RUN METRICS" in summary)
ok("summary shows the run id", "RUN_A" in summary)
ok("summary notes tokens are the real number when cost is 0", "tokens are the real number" in summary)

conn.close()
os.unlink(tmpdb)
print(f"\n=========== {P} passed, {F} failed ===========")
sys.exit(1 if F else 0)
