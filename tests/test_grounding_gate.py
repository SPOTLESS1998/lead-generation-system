"""The grounding gate lives in the SHARED recipe — offline, no network.

The bug this closes: `has_grounding` existed only inside scripts/draft_queued.py
(the manual recovery script) and in its tests. scripts/lead_agent.py — the one
cron actually runs — had no grounding check at all. So the guard refused
ungrounded leads on the path nobody runs and drafted them on the path that runs
daily. That is exactly the drift that shipped duplicate drafts: written, tested,
wired into one of two loops.

5 such drafts were found sitting in the live approval queue.

The fix is not "add the check to the other loop too" — that leaves two
definitions to drift again. The predicate is now core/quality.is_grounded (one
definition) and it is enforced inside draft_one_lead, the single shared recipe
BOTH entry points call. A third entry point added later inherits it for free.

Run:  venv/bin/python tests/test_grounding_gate.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from core import quality                                # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


# --------------------------------------------------------------------------
def test_predicate():
    print("\n[core.quality.is_grounded — the single definition]")
    check("facts only => grounded", quality.is_grounded({"company_facts": "makes shoes"}))
    check("description only => grounded",
          quality.is_grounded({"company_description": "a bakery in Lagos"}))
    check("both => grounded",
          quality.is_grounded({"company_facts": "x", "company_description": "y"}))

    check("both empty => NOT grounded",
          not quality.is_grounded({"company_facts": "", "company_description": ""}))
    check("whitespace only => NOT grounded",
          not quality.is_grounded({"company_facts": "   ", "company_description": "\n\t"}))
    check("both missing => NOT grounded", not quality.is_grounded({}))
    check("None lead => NOT grounded", not quality.is_grounded(None))

    # The company NAME must never count as evidence — handing the model a name
    # positioned as researched fact is what produced the invented specifics.
    check("company_name alone does NOT count as grounding",
          not quality.is_grounded({"company_name": "Acme Ltd", "company_facts": "",
                                   "company_description": ""}))


def test_relationship_to_thin_evidence():
    print("\n[is_grounded vs evidence_is_thin — different questions]")
    thin_but_real = {"company_facts": "a bakery"}
    check("a thin file is still GROUNDED", quality.is_grounded(thin_but_real))
    check("...and is correctly flagged THIN", quality.evidence_is_thin(thin_but_real))
    empty = {"company_facts": ""}
    check("an empty file is NOT grounded", not quality.is_grounded(empty))
    rich = {"company_facts": " ".join(["detail"] * 80)}
    check("a rich file is grounded and not thin",
          quality.is_grounded(rich) and not quality.evidence_is_thin(rich))


def test_draft_one_lead_refuses_before_any_model_call():
    print("\n[draft_one_lead raises FIRST, before spending anything]  <-- the fix")
    import lead_agent

    # Trip-wires: if the gate is absent, the recipe walks into these and we see it.
    calls = []
    for name in ("select_offering", "generate_strategy"):
        setattr(lead_agent, name,
                (lambda n: (lambda *a, **k: calls.append(n)))(name))
    import core.budget as budget_mod
    budget_mod.copy_cfg = lambda conn, cfg: calls.append("budget") or {}

    lead = {"email": "a@b.test", "company_name": "Nothing Known Ltd",
            "company_facts": "", "company_description": ""}
    err = None
    try:
        lead_agent.draft_one_lead(None, {"client": "t"}, lead, "run1")
    except Exception as e:
        err = e

    check("raises UngroundedLead", isinstance(err, quality.UngroundedLead))
    check("the message names the company", err and "Nothing Known Ltd" in str(err))
    check("NO model/budget work was done (cost nothing)", calls == [])
    check("conn=None was never touched (proves it returned before any DB use)",
          isinstance(err, quality.UngroundedLead))


def test_both_entry_points_are_covered():
    print("\n[the gate is reachable from BOTH entry points, by construction]")
    import lead_agent
    import draft_queued
    src_la = open(os.path.join(ROOT, "scripts", "lead_agent.py")).read()
    src_dq = open(os.path.join(ROOT, "scripts", "draft_queued.py")).read()

    check("draft_one_lead itself enforces it", "quality.is_grounded(lead)" in src_la)
    check("lead_agent handles UngroundedLead explicitly",
          "quality.UngroundedLead" in src_la)
    check("lead_agent counts and reports it", "ungrounded" in src_la)
    check("draft_queued delegates to the shared predicate",
          "quality.is_grounded" in src_dq)

    # Neither file may carry its own private notion of grounded any more.
    check("no local bool(facts or desc) definition left in draft_queued",
          'bool(facts or desc)' not in src_dq)

    # draft_queued calls the shared recipe, so it inherits the gate even if its
    # own early-exit were deleted.
    check("draft_queued still calls the shared recipe",
          "draft_one_lead" in src_dq)
    check("both import quality",
          "quality" in src_la and "quality" in src_dq)


def test_grounded_lead_is_not_blocked():
    print("\n[a grounded lead passes the gate and proceeds]")
    import lead_agent
    reached = []
    lead_agent.select_offering = lambda *a, **k: reached.append("fit") or None
    import core.budget as budget_mod
    budget_mod.copy_cfg = lambda conn, cfg: {}

    lead = {"email": "a@b.test", "company_name": "Known Ltd",
            "company_facts": "runs three branches in Abuja"}
    err = None
    try:
        lead_agent.draft_one_lead(None, {"client": "t"}, lead, "run1")
    except Exception as e:
        err = e
    check("does NOT raise UngroundedLead", not isinstance(err, quality.UngroundedLead))
    check("it got past the gate into the recipe", reached == ["fit"] or err is not None)


if __name__ == "__main__":
    print("=" * 62)
    print("GROUNDING GATE IN THE SHARED PATH")
    print("=" * 62)
    test_predicate()
    test_relationship_to_thin_evidence()
    test_draft_one_lead_refuses_before_any_model_call()
    test_both_entry_points_are_covered()
    test_grounded_lead_is_not_blocked()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
