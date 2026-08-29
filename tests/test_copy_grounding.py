"""Offline tests for facts-grounding in the copy path — NO network.

Milestone 3 threads the real scraped facts (company_facts, falling back to
company_description) all the way into the pitch. This proves the drafter and the
re-drafter actually carry those facts, WITHOUT any LLM: lead_agent.generate_json
is monkeypatched to capture the prompt it was handed, and _draft_lead_view is a
pure row->dict shaper.

Run:  venv/bin/python tests/test_copy_grounding.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                              # repo root -> `core`
sys.path.insert(0, os.path.join(ROOT, "scripts"))     # scripts/  -> `lead_agent`, `draft_queued`

# Dummy creds so importing the modules (which pull in core.ai / core.config) never
# trips on a missing key — no real credential is used, every LLM seam is patched.
for _k in ("GEMINI_API_KEY", "NVIDIA_API_KEY", "SMTP_USER", "SMTP_PASS", "UNSUB_SECRET"):
    os.environ.setdefault(_k, "test")

import lead_agent            # noqa: E402
import draft_queued          # noqa: E402
from core import config      # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


# A body with a proper sign-off, so finalize_body leaves it untouched.
_GOOD_BODY = "Hi Ada,\n\nSpecific pitch about your work.\n\nBest,\nEjentic AI"
CFG = {"client_name": "Ejentic AI", "from_name": "Ejentic AI"}


def _capture_generate_json():
    """Swap lead_agent.generate_json for a capturing fake; return the capture dict."""
    cap = {}

    def fake(cfg, prompt):
        cap["prompt"] = prompt
        return {"subject": "A quick idea", "body": _GOOD_BODY}, "fake"

    lead_agent.generate_json = fake
    return cap


# --------------------------------------------------------------------------
# generate_copy — the drafter now sees the facts DIRECTLY, not just the brief
# --------------------------------------------------------------------------
def test_generate_copy_grounds_in_facts():
    print("\n[generate_copy: real facts reach the draft prompt]")
    cap = _capture_generate_json()

    lead = {"first_name": "Ada", "last_name": "Obi", "company_name": "Acme",
            "company_facts": "Services: solar install. Notable: serves Maitama landlords."}
    subject, body, prov = lead_agent.generate_copy(CFG, lead, "STRATEGY-BRIEF-MARKER")
    check("draft prompt includes company_facts", "Maitama landlords" in cap["prompt"])
    check("draft prompt still includes the strategy brief", "STRATEGY-BRIEF-MARKER" in cap["prompt"])
    check("draft prompt labels the facts block", "WHAT WE ACTUALLY KNOW ABOUT THEM" in cap["prompt"])
    check("returned body keeps its sign-off", body.strip().endswith("Ejentic AI"))
    check("returned subject is non-empty", bool(subject.strip()))

    # No company_facts -> fall back to the older one-line description.
    cap2 = _capture_generate_json()
    lead2 = {"first_name": "Ada", "company_name": "Acme",
             "company_description": "sells solar kits to Lagos SMEs"}
    lead_agent.generate_copy(CFG, lead2, "BRIEF")
    check("draft prompt falls back to company_description when no facts",
          "sells solar kits to Lagos SMEs" in cap2["prompt"])

    # Nothing known at all -> the facts block is omitted (no dangling empty label).
    cap3 = _capture_generate_json()
    lead3 = {"first_name": "Ada", "company_name": "Acme"}
    lead_agent.generate_copy(CFG, lead3, "BRIEF")
    check("draft prompt omits the facts block when nothing is known",
          "WHAT WE ACTUALLY KNOW ABOUT THEM" not in cap3["prompt"])

    # Blank first name (role mailbox) -> the prompt greets safely as 'there', never a
    # literal placeholder the model would echo as "Hi Name,".
    cap4 = _capture_generate_json()
    lead4 = {"first_name": "", "company_name": "Acme"}
    lead_agent.generate_copy(CFG, lead4, "BRIEF")
    check("blank first name -> prompt uses a safe 'there' greeting name",
          "Name: there" in cap4["prompt"])


# --------------------------------------------------------------------------
# generate_strategy — the OBSERVATION is anchored to the same facts
# --------------------------------------------------------------------------
def test_generate_strategy_grounds_in_facts():
    print("\n[generate_strategy: real facts reach the strategist prompt]")
    cap = {}

    def fake_generate(cfg, prompt):
        cap["prompt"] = prompt
        return "OBSERVATION: ...", "fake"

    lead_agent.generate = fake_generate
    lead = {"first_name": "Ada", "company_name": "Acme",
            "company_facts": "Services: solar. Notable: serves Maitama landlords."}
    lead_agent.generate_strategy(CFG, lead)
    check("strategy prompt includes company_facts", "Maitama landlords" in cap["prompt"])
    check("strategy OBSERVATION must be flattering, never a negative opener",
          "NEVER open on a negative or operational detail" in cap["prompt"])


# --------------------------------------------------------------------------
# generate_strategy — the promised OUTCOME is a config constant, not an LLM guess.
# This is the fix for the "15-25 leads one run, 100 the next" inconsistency: the
# figure now comes from service_outcomes, keyed by the lead's matched service.
# --------------------------------------------------------------------------
def test_generate_strategy_standardizes_outcome():
    print("\n[generate_strategy: outcome number is a config constant, not an LLM guess]")
    cap = {}

    def fake_generate(cfg, prompt):
        cap["prompt"] = prompt
        return "OUTCOME: ...", "fake"

    lead_agent.generate = fake_generate

    # A service WITH a house-standard outcome -> the strategist is handed that exact figure.
    cfg = {**CFG, "service_outcomes": {
        "AI Lead Generation System": "10-20 additional qualified leads per month within the first 90 days"}}
    lead = {"first_name": "Ada", "company_name": "Acme",
            "ejentic_service": "AI Lead Generation System",
            "company_facts": "Services: property sales."}
    lead_agent.generate_strategy(cfg, lead)
    check("strategy injects the exact standard outcome figure",
          "10-20 additional qualified leads per month within the first 90 days" in cap["prompt"])
    check("strategy forbids inflating the standard figure", "never inflate" in cap["prompt"].lower())

    # A service with NO configured standard -> graceful fallback to the free 'plausible number'.
    cap2 = {}

    def fake_generate2(cfg, prompt):
        cap2["prompt"] = prompt
        return "OUTCOME: ...", "fake"

    lead_agent.generate = fake_generate2
    lead2 = {"first_name": "Ada", "company_name": "Acme",
             "ejentic_service": "Unlisted Service", "company_facts": "x"}
    lead_agent.generate_strategy({**CFG, "service_outcomes": {}}, lead2)
    check("no configured standard -> falls back to a plausible number",
          "plausible number or timeframe" in cap2["prompt"])


# --------------------------------------------------------------------------
# draft_queued._draft_lead_view — reads persisted facts, degrades gracefully
# --------------------------------------------------------------------------
def _row(**kw):
    base = {"email": "x@y.ng", "first_name": None, "last_name": None, "title": None,
            "company_name": None, "website_url": None, "niche": None,
            "company_description": None, "company_facts": None}
    base.update(kw)
    return base


def test_draft_lead_view_reads_facts():
    print("\n[_draft_lead_view: persisted facts preferred, company-name fallback kept]")
    v = draft_queued._draft_lead_view(_row(
        company_name="Acme", company_description="sells solar",
        company_facts="Services: solar. Notable: serves Maitama landlords."))
    check("uses persisted company_facts verbatim",
          v["company_facts"] == "Services: solar. Notable: serves Maitama landlords.")
    check("uses persisted company_description verbatim", v["company_description"] == "sells solar")

    v2 = draft_queued._draft_lead_view(_row(company_name="Acme", company_description="sells solar"))
    check("facts fall back to the description when facts are NULL", v2["company_facts"] == "sells solar")

    v3 = draft_queued._draft_lead_view(_row(company_name="Acme"))
    check("facts fall back to the company name when both are NULL", v3["company_facts"] == "Acme")
    check("description falls back to the company name too", v3["company_description"] == "Acme")
    check("blank first name becomes a safe 'there' greeting", v3["first_name"] == "there")


# --------------------------------------------------------------------------
# config — the gate default now drafts best-of-2
# --------------------------------------------------------------------------
def test_config_gate_default():
    print("\n[config: quality_gate default bumped to best_of=2]")
    gate = config.DEFAULTS["copy"]["quality_gate"]
    check("best_of default is 2", gate["best_of"] == 2)
    check("max_revisions still 1", gate["max_revisions"] == 1)
    check("gate still enabled by default", gate["enabled"] is True)


def main():
    test_generate_copy_grounds_in_facts()
    test_generate_strategy_grounds_in_facts()
    test_generate_strategy_standardizes_outcome()
    test_draft_lead_view_reads_facts()
    test_config_gate_default()
    print(f"\n{'='*50}\nRESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
