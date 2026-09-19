"""The system learns across runs — safely.

Two capabilities are tested here, and the second is the dangerous one.

1. THE SCORECARD. Every judge verdict is persisted and rolled up per run, so
   "is run 10 better than run 5?" is answerable from data instead of vibes.

2. LEARNED LESSONS. Recurring critiques become PROPOSED style rules. The safety
   properties are the point of this suite:
     - a proposal NEVER reaches a prompt on its own (human gate), and
     - a "lesson" carrying a fact (number, proper noun) is refused outright,
       because a fact in the prompt is asserted about every prospect and enters
       the copy ABOVE the citation checker, where nothing downstream can catch it.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

os.environ.setdefault("CLIENT", "demo")
os.environ.setdefault("ANNOUNCE_TENANT", "0")
os.environ.setdefault("UNSUB_SECRET", "test-secret-123")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("NVIDIA_API_KEY", "x")

from core import state, quality        # noqa: E402
import reflect_copy                    # noqa: E402

_passed = _failed = 0


def ok(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {label}")
    else:
        _failed += 1
        print(f"  ❌ {label}")


def fresh():
    return state.connect(os.path.join(tempfile.mkdtemp(), "t.sqlite"))


C = "demo"

print("\n--- 1. every judge verdict is persisted --------------------------")
conn = fresh()
state.record_critique(conn, C, "a@x.com", "draft",
                      {"score": 5, "issues": ["generic opener"], "fix_hint": "name the pain"},
                      None, run_id="r1")
state.record_critique(conn, C, "a@x.com", "final",
                      {"score": 5, "issues": ["generic opener"], "fix_hint": "name the pain"},
                      "refused_floor", run_id="r1")
state.record_critique(conn, C, "b@x.com", "final",
                      {"score": 8, "issues": [], "fix_hint": ""}, "shipped", run_id="r1")
ok("critiques are stored", len(state.recent_critiques(conn, C)) == 3)
ok("weak-only filter works", len(state.recent_critiques(conn, C, max_score=6)) == 2)

# A telemetry write must never be able to break a draft.
state.record_critique(conn, C, "c@x.com", "draft", None, None, run_id="r1")
ok("a None critique (judge down) is stored, not crashed on",
   len(state.recent_critiques(conn, C)) == 4)

print("\n--- 2. the per-run scorecard ------------------------------------")
summary = state.summarize_run_quality(conn, C, "r1")
ok("attempted counts finals only", summary["attempted"] == 2)
ok("shipped counted", summary["shipped"] == 1)
ok("floor refusals counted", summary["refused_floor"] == 1)
row = state.run_quality_history(conn, C)[0]
ok("scorecard row persisted", row["attempted"] == 2 and row["shipped"] == 1)
ok("mean score computed", row["mean_score"] == 6.0)
ok("a run with no critiques summarises to None",
   state.summarize_run_quality(conn, C, "run-that-never-drafted") is None)

print("\n--- 3. the trend answers 'is it getting better?' -----------------")
conn2 = fresh()
for i, sc in enumerate([4, 4, 5, 5, 5]):        # older, weaker runs
    state.record_critique(conn2, C, f"o{i}@x.com", "final",
                          {"score": sc, "issues": [], "fix_hint": ""}, "shipped",
                          run_id=f"old{i}")
    state.summarize_run_quality(conn2, C, f"old{i}")
for i, sc in enumerate([8, 8, 9, 8, 9]):        # newer, stronger runs
    state.record_critique(conn2, C, f"n{i}@x.com", "final",
                          {"score": sc, "issues": [], "fix_hint": ""}, "shipped",
                          run_id=f"new{i}")
    state.summarize_run_quality(conn2, C, f"new{i}")
t = state.quality_trend(conn2, C, window=5)
ok("trend is computed over two windows", t is not None and t["recent"]["runs"] == 5)
ok("improvement shows as a positive delta", t["delta_mean_score"] > 0)
ok("the delta is the real difference", round(t["delta_mean_score"], 1) == 3.8)
ok("a single run is not enough to claim a trend",
   state.quality_trend(fresh(), C) is None)

print("\n--- 4. THE HUMAN GATE: proposals never reach a prompt ------------")
conn3 = fresh()
lid = state.propose_lesson(conn3, C, "Open on the implied bottleneck, not a compliment.")
ok("a proposal is stored", lid is not None)
ok("a PROPOSED lesson is NOT active", state.approved_lessons(conn3, C) == [])
instr = quality.copy_instructions("Ada", None,
                                  lessons=state.approved_lessons(conn3, C))
ok("proposed text appears nowhere in the drafting instructions",
   "implied bottleneck" not in instr)
state.decide_lesson(conn3, C, lid, "approved")
ok("approval activates it", len(state.approved_lessons(conn3, C)) == 1)
instr2 = quality.copy_instructions("Ada", None,
                                   lessons=state.approved_lessons(conn3, C))
ok("an APPROVED lesson does reach the instructions", "implied bottleneck" in instr2)
ok("lessons are appended, never replacing the fixed framework",
   "must appear in the facts you were given" in instr2)
rid = state.propose_lesson(conn3, C, "Ask one concrete question at the end.")
state.decide_lesson(conn3, C, rid, "rejected")
ok("a REJECTED lesson never becomes active",
   all("concrete question at the end" not in l for l in state.approved_lessons(conn3, C)))
ok("re-proposing an existing lesson is a no-op (no nightly spam)",
   state.propose_lesson(conn3, C, "Open on the implied bottleneck, not a compliment.") is None)

print("\n--- 5. a 'lesson' may never carry a FACT ------------------------")
# This is the fabrication-laundering guard. A fact here would be asserted about
# EVERY prospect, and it enters above the citation checker where it cannot be caught.
unsafe = [
    ("Mention their 15 years of experience.", "number"),
    ("Reference their Lagos office.", "proper noun"),
    ("Cite the 40% improvement figure.", "number"),
    ("Say they serve $2M clients.", "currency"),
    ("Always say they are the market leader.", "assertion"),
    ("Mention Acme Corp as a comparable.", "proper noun"),
]
for text, why in unsafe:
    ok(f"refuses a lesson carrying a {why}: {text[:34]!r}",
       bool(reflect_copy.lesson_problems(text)))

safe = [
    "Open on the implied bottleneck rather than a compliment.",
    "End with one concrete next step, never a vague invitation.",
    "Keep the subject under six words and free of hype.",
]
for text in safe:
    ok(f"allows a genuine style rule: {text[:38]!r}",
       reflect_copy.lesson_problems(text) == [])

ok("refuses an empty lesson", bool(reflect_copy.lesson_problems("")))
ok("refuses a one-word lesson", bool(reflect_copy.lesson_problems("Better")))
ok("refuses an over-long lesson", bool(reflect_copy.lesson_problems("x " * 120)))

print("\n--- 6. the sink can never break a draft -------------------------")
# quality.draft_and_polish calls on_critique inside a try/except. If a telemetry
# bug could fail a draft, the learning layer would be a liability, not an asset.
# The gate is ENABLED and the judge stubbed, so the sink is genuinely invoked —
# with the gate off, draft_and_polish returns before any critique is emitted and
# this would pass without testing anything.
calls = []


def _explode(attempt, critique, outcome=None):
    calls.append(attempt)
    raise RuntimeError("storage is down")


cfg = {"client_name": "Demo", "from_name": "Ada",
       "copy": {"quality_gate": {"enabled": True, "min_score": 8, "floor_score": 6,
                                 "max_revisions": 0, "best_of": 1}}}
lead = {"company_name": "Acme", "company_facts": "Services: audit.", "email": "a@x.com"}
_real_score = quality.score
quality.score = lambda *a, **k: {"score": 9, "issues": [], "fix_hint": ""}
try:
    s, b, p = quality.draft_and_polish(
        cfg, lead, "brief", None,
        lambda c, l, br, m: ("A clear subject", "Hi Ada,\n\nShort body.\n\nBest,\nAda", "stub"),
        on_critique=_explode)
    survived = True
except Exception:
    survived = False
finally:
    quality.score = _real_score

ok("the sink was actually called (otherwise this proves nothing)", len(calls) > 0)
ok("a failing critique sink does not fail the draft", survived)

print(f"\n=========== {_passed} passed, {_failed} failed ===========")
sys.exit(1 if _failed else 0)
