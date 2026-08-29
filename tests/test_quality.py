"""Offline tests for the draft-quality gate (core/quality.py).

The gate is what makes the pitch convincing: score every draft 1-10 with an
LLM-as-judge, rewrite anything below the bar, optionally best-of-N. This proves
the mechanics WITHOUT any LLM — quality.generate_json is monkeypatched and the
drafter is a counting fake, so score/revise branch on prompt substrings only.

Run:  venv/bin/python tests/test_quality.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import quality          # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


LEAD = {"first_name": "Ada", "last_name": "Obi",
        "company_name": "Acme", "company_description": "sells widgets"}

# Same prospect, but now carrying the richer scraped facts the copy is grounded in.
LEAD_FACTS = {**LEAD,
              "company_facts": "Services: solar install. Notable: serves Maitama landlords."}


def qcfg(gate):
    return {"client": "acme", "client_name": "Ejentic AI",
            "from_name": "Ejentic AI", "copy": {"quality_gate": gate}}


# A drafter stand-in that records how many times it was called and returns a
# scripted (subject, body, provider) per call.
class DraftFn:
    def __init__(self, results):
        self.results = results
        self.calls = 0

    def __call__(self, cfg, lead, brief, magnet_url):
        r = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return r


# quality.generate_json stand-in: branches on the distinguishing substring in
# each prompt (score vs revise), returns scripted scores / revised copy.
def make_gj(scores=(), revise_result=None, judge_raises=False):
    st = {"i": 0}

    def gj(cfg, prompt):
        if "ruthless cold-email editor" in prompt:          # score()
            if judge_raises:
                raise RuntimeError("judge offline")
            s = scores[st["i"]]
            st["i"] += 1
            return {"score": s, "issues": ["too generic"], "fix_hint": "be specific"}, "judge"
        if "strict editor rejected" in prompt:              # revise()
            rs, rb = revise_result
            return {"subject": rs, "body": rb}, "reviser"
        raise AssertionError("generate_json called with an unexpected prompt")

    return gj


def boom(*a, **k):
    raise AssertionError("generate_json must NOT be called here")


def judge_down(*a, **k):
    raise RuntimeError("judge offline")


# --------------------------------------------------------------------------
# finalize_body — the safety net every body passes through
# --------------------------------------------------------------------------
check("finalize_body strips surrounding whitespace",
      quality.finalize_body("  \n Hi there\n\nBest,\nX \n  ", "X", None) == "Hi there\n\nBest,\nX")
check("finalize_body swaps [Your Name] placeholder for the real sender",
      quality.finalize_body("Best,\n[Your Name]", "Ejentic AI", None) == "Best,\nEjentic AI")

URL = "https://x/magnet/abc"
appended = quality.finalize_body("Hi Ada,\n\nBest,\nEjentic AI", "Ejentic AI", URL)
check("finalize_body appends the audit link when missing", appended.endswith("\n\n" + URL))
check("finalize_body appends the link exactly once", appended.count(URL) == 1)
already = quality.finalize_body("Hi\n\n" + URL, "Ejentic AI", URL)
check("finalize_body does not duplicate an already-present link", already.count(URL) == 1)
check("finalize_body appends nothing when magnet_url is None",
      quality.finalize_body("Hi\n\nBest,\nEjentic AI", "Ejentic AI", None).count("http") == 0)

# Sign-off guarantee: a body that lost its sign-off gets one; a present one is never doubled.
check("finalize_body appends a missing sign-off",
      quality.finalize_body("Hi Ada,\n\nThis is the whole pitch.", "Ejentic AI", None)
      .endswith("Best,\nEjentic AI"))
check("finalize_body does not double an existing sign-off",
      quality.finalize_body("Hi Ada,\n\nPitch.\n\nBest,\nEjentic AI", "Ejentic AI", None)
      .count("Best,") == 1)
check("finalize_body keeps the link above an appended sign-off",
      quality.finalize_body("Hi Ada,\n\nPitch.\n\n" + URL, "Ejentic AI", URL)
      .endswith(URL + "\n\nBest,\nEjentic AI"))

# Greeting safety net: a leaked placeholder greeting becomes a safe "Hi there,";
# a real first name (or an already-safe greeting) is never touched.
check("finalize_body rewrites a leaked 'Hi Name,' greeting",
      quality.finalize_body("Hi Name,\n\nPitch.\n\nBest,\nX", "X", None).startswith("Hi there,"))
check("finalize_body rewrites a bracketed '[First Name]' greeting",
      quality.finalize_body("Hi [First Name],\n\nPitch.\n\nBest,\nX", "X", None).startswith("Hi there,"))
check("finalize_body leaves a real first-name greeting alone",
      quality.finalize_body("Hi Ada,\n\nPitch.\n\nBest,\nX", "X", None).startswith("Hi Ada,"))
check("finalize_body leaves an existing 'Hi there,' greeting alone",
      quality.finalize_body("Hi there,\n\nPitch.\n\nBest,\nX", "X", None).startswith("Hi there,"))


# --------------------------------------------------------------------------
# copy_instructions — the single shared framework (drafter + reviser)
# --------------------------------------------------------------------------
with_link = quality.copy_instructions("Ejentic AI", URL)
check("copy_instructions embeds the exact link when one exists", URL in with_link)
check("copy_instructions gives the on-its-own-line link rule",
      "Put this exact audit link" in with_link)
no_link = quality.copy_instructions("Ejentic AI", None)
check("copy_instructions forbids inventing a link when there is none",
      "Do NOT invent or include any link" in no_link)
check("copy_instructions has no link rule when there is no link",
      "Put this exact audit link" not in no_link)
check("copy_instructions carries the real sign-off name", "Ejentic AI" in with_link)
check("copy_instructions gives a safe no-name greeting rule", "Hi there," in with_link)
check("copy_instructions enforces the 60-90 word budget", "60-90 words" in with_link)
check("copy_instructions forbids inventing a current-state stat",
      "Never state a statistic as a fact about THEIR" in with_link)
check("copy_instructions still allows the one projected outcome number",
      "The ONE allowed projection" in with_link)
check("copy_instructions forbids opening on a negative/operational detail",
      "NEVER open by highlighting a negative or operational detail" in with_link)
check("copy_instructions ties sentence 3 to the brief's exact figure (no inflation)",
      "never inflate it or swap in a bigger number" in with_link)


# --------------------------------------------------------------------------
# score — clamp, non-numeric, judge-down
# --------------------------------------------------------------------------
quality.generate_json = lambda cfg, p: ({"score": 7, "issues": ["x"], "fix_hint": "y"}, "j")
r = quality.score(qcfg({}), LEAD, "S", "a body", "brief")
check("score returns the judge's integer", r["score"] == 7)

quality.generate_json = lambda cfg, p: ({"score": 99}, "j")
check("score clamps above 10", quality.score(qcfg({}), LEAD, "S", "b", "")["score"] == 10)

quality.generate_json = lambda cfg, p: ({"score": -4}, "j")
check("score clamps below 1", quality.score(qcfg({}), LEAD, "S", "b", "")["score"] == 1)

quality.generate_json = lambda cfg, p: ({"score": "not-a-number"}, "j")
check("score returns None on a non-numeric score", quality.score(qcfg({}), LEAD, "S", "b", "") is None)

quality.generate_json = judge_down
check("score returns None when the judge raises", quality.score(qcfg({}), LEAD, "S", "b", "") is None)

# The judge must see the REAL facts, or it scores "specific to THIS prospect" blind.
_cap = {}
def _cap_score(cfg, p):
    _cap["p"] = p
    return {"score": 7, "issues": [], "fix_hint": ""}, "j"
quality.generate_json = _cap_score
quality.score(qcfg({}), LEAD_FACTS, "S", "a body", "brief")
check("score prompt is grounded in company_facts", "Maitama landlords" in _cap["p"])


# --------------------------------------------------------------------------
# revise — rewrites, finalizes, rejects an empty body
# --------------------------------------------------------------------------
quality.generate_json = lambda cfg, p: ({"subject": "New subj", "body": "New body, specific.\n\nBest,\nEjentic AI"}, "r")
ns, nb = quality.revise(qcfg({}), LEAD, "Old", "Old body", {"issues": ["x"], "fix_hint": "y"}, None, "Ejentic AI")
check("revise returns the rewritten subject", ns == "New subj")
check("revise finalizes the rewritten body", nb == "New body, specific.\n\nBest,\nEjentic AI")

quality.generate_json = lambda cfg, p: ({"subject": "S", "body": "   "}, "r")
try:
    quality.revise(qcfg({}), LEAD, "Old", "Old body", {}, None, "Ejentic AI")
    check("revise raises on an empty rewritten body", False)
except RuntimeError:
    check("revise raises on an empty rewritten body", True)

# The reviser must also see the facts, or "keep what works" quietly drops the cited detail.
_cap2 = {}
def _cap_rev(cfg, p):
    _cap2["p"] = p
    return {"subject": "S", "body": "Hi Ada,\n\nSpecific pitch.\n\nBest,\nEjentic AI"}, "r"
quality.generate_json = _cap_rev
quality.revise(qcfg({}), LEAD_FACTS, "Old", "Old body", {"issues": [], "fix_hint": ""}, None, "Ejentic AI")
check("revise prompt is grounded in company_facts", "Maitama landlords" in _cap2["p"])


# --------------------------------------------------------------------------
# draft_and_polish — the orchestration
# --------------------------------------------------------------------------
# A) Gate disabled → first candidate ships untouched; the judge is never called.
quality.generate_json = boom
df = DraftFn([("S1", "Body 1", "provA")])
out = quality.draft_and_polish(qcfg({"enabled": False}), LEAD, "brief", None, df)
check("gate off: returns the first candidate verbatim", out == ("S1", "Body 1", "provA"))
check("gate off: drafts exactly once", df.calls == 1)

# B) Enabled, first score already high → no revision.
quality.generate_json = make_gj(scores=[9])
df = DraftFn([("S1", "Body 1", "provA")])
out = quality.draft_and_polish(qcfg({"enabled": True, "min_score": 8}), LEAD, "brief", None, df)
check("high first score: ships the draft as-is", out == ("S1", "Body 1", "provA"))
check("high first score: drafts once, no revision", df.calls == 1)

# C) Enabled, low then high → one revision, the higher version kept.
quality.generate_json = make_gj(scores=[6, 9], revise_result=("Revised subj", "Revised body, very specific.\n\nBest,\nEjentic AI"))
df = DraftFn([("Draft subj", "Draft body", "free-prov")])
out = quality.draft_and_polish(qcfg({"enabled": True, "min_score": 8, "max_revisions": 1}),
                               LEAD, "brief", None, df)
check("low score: keeps the revised subject", out[0] == "Revised subj")
check("low score: keeps the revised body", out[1] == "Revised body, very specific.\n\nBest,\nEjentic AI")
check("low score: provider carried from the scored candidate", out[2] == "free-prov")

# D) Enabled but judge all-None → ships unscored (first candidate).
quality.generate_json = make_gj(judge_raises=True)
df = DraftFn([("S1", "Body 1", "provA")])
out = quality.draft_and_polish(qcfg({"enabled": True, "min_score": 8}), LEAD, "brief", None, df)
check("judge down: ships the first candidate unscored", out == ("S1", "Body 1", "provA"))

# E) best_of=2 → drafts twice, keeps the higher-scoring candidate.
quality.generate_json = make_gj(scores=[6, 8])
df = DraftFn([("S1", "Body 1", "provA"), ("S2", "Body 2", "provB")])
out = quality.draft_and_polish(qcfg({"enabled": True, "min_score": 8, "best_of": 2}),
                               LEAD, "brief", None, df)
check("best_of=2: drafts two candidates", df.calls == 2)
check("best_of=2: keeps the higher-scoring one", out == ("S2", "Body 2", "provB"))


print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
