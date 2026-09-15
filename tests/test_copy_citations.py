"""Offline tests for the anti-fabrication half of the copy gate — NO network.

Two guards ship together here, and they answer different failure modes:

  * `citation_issues` — a mechanical check that every concrete thing the copy asserts
    (figure, money, acronym, place, brand) appears in the evidence we actually hold.
  * `floor_score` — a HARD refusal to ship copy the judge scored below the floor.
    `min_score` was only ever an aspiration: the old loop rewrote a weak draft, then
    returned it anyway, printing "final 6/10 (min 8)" as the only trace.

Both exist because the grounding gate wasn't enough. It stops us drafting a prospect we
know NOTHING about; it cannot stop a draft with rich facts from reaching past them. The
fixtures below are the SHAPES of four fabrications that actually reached the approval
queue, kept deliberately close to the originals so this suite fails if any of them
becomes possible again:

    "over N50 billion in recoveries"          — money we never recorded
    "three Lagos training centres (Ikeja,
     Ketu, Ajah)"                             — offices invented for a firm whose
                                                facts list one service and a rating
    "storing 660,000 tons"                    — for a firm whose facts say 85,731 sqm
    "ISO 9001, 14001, 45001 and Ecovadis"     — certifications never scraped

Every one of those prospects had RICH facts, which is the point: the model did not
invent because it was starved, it invented because it was asked to sound specific.

Before this checker was allowed to gate anything it was run in report-only mode over
all 42 real drafts then sitting in the queue: it flagged 4 (every one a genuine
fabrication, the four above) and cleared 38, including every draft whose star rating,
review count and office list were real. That measurement is why the honest-copy cases
below matter as much as the fabrication ones — a guard that rejects good copy would
quietly stop the pipeline instead of improving it.

Run:  venv/bin/python tests/test_copy_citations.py
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


# A tenant config shaped like a real one: the outcome promises and offering names are
# HUMAN-authored values, so figures taken from them are citable. Nothing here is
# hardcoded in core/ — it all arrives from clients/<client>/config.json.
CFG = {
    "client": "acme",
    "client_name": "Acme AI",
    "from_name": "Ada Obi",
    "unsubscribe_base_url": "https://audit.example.test",
    "offerings": ["Internal Knowledge Base", "Workflow Automation"],
    "service_outcomes": {
        "Internal Knowledge Base": "trusted answers from your own documents in seconds, "
                                   "cutting lookup time ~50% within 60 days",
        "Workflow Automation": "reclaim 10-15 staff-hours per week within 90 days",
    },
    "copy": {"quality_gate": {}},
}


def gate_cfg(gate):
    return {**CFG, "copy": {"quality_gate": gate}}


# --- The four real fabrication shapes, each with the facts it was drafted from -------

LAW = {
    "first_name": "Tunde", "company_name": "Harbour & Vale Law Firm",
    "ejentic_service": "Internal Knowledge Base",
    "company_facts": ("Services: Alternative Dispute Resolution, dispute management, "
                      "Corporate Law, Litigation, Commercial Law, Property Law, "
                      "International Law. Notable: Full-service international law firm "
                      "with offices in Nigeria, South Africa, UAE, and Spain.."),
}
TRAINER = {
    "company_name": "Bright Path Training",
    "ejentic_service": "Internal Knowledge Base",
    "company_facts": ("Services: training. Notable: They are committed to achieving the "
                      "highest results in their industry.. Well-reviewed: 4.8-star "
                      "average across 202 Google reviews."),
}
LOGISTICS = {
    "first_name": "Nia", "company_name": "Meridian Logistics",
    "ejentic_service": "Workflow Automation",
    "company_facts": ("Services: clearing and forwarding, warehousing, distribution, "
                      "cold chain operations. Notable: They manage over 85,731sqm of "
                      "warehousing space across 19 distribution centres in Nigeria for "
                      "more than 60 principals.. Well-reviewed: 4.3-star average across "
                      "147 Google reviews."),
}
FREIGHT = {
    "company_name": "Continental Freight",
    "ejentic_service": "Workflow Automation",
    "company_facts": ("Services: port solutions, maritime shipping, multimodal transport, "
                      "warehousing. Notable: Operates 58,970 m2 of dedicated logistics "
                      "warehousing across Africa and maintains a professional workforce of "
                      "655 employees across 10 sites.. Well-reviewed: 4.5-star average "
                      "across 126 Google reviews."),
}
# A prospect whose whole file is a star rating — the thin-evidence case.
BARE = {
    "company_name": "Stonebridge Solicitors",
    "ejentic_service": "Internal Knowledge Base",
    "company_facts": "Well-reviewed: 4.9-star average across 111 Google reviews.",
}
# Rich facts, and copy that stays inside them (the honest control).
RICH = {
    "company_name": "Riverside Law Practice",
    "ejentic_service": "Internal Knowledge Base",
    "company_facts": ("Services: Property & real estate law, litigation & dispute "
                      "resolution, immigration services, diaspora legal services. "
                      "Notable: Maintains offices across both Ikeja, Lagos and Arepo, "
                      "Ogun State, offering dedicated diaspora legal services.. "
                      "Well-reviewed: 4.9-star average across 331 Google reviews."),
}

SIGN = "\n\nBest,\nAda Obi"


# --------------------------------------------------------------------------
# citation_issues — catches the fabrications that actually shipped
# --------------------------------------------------------------------------
print("\n[citation_issues catches the four fabrications that reached the queue]")

_money = ("Hi Tunde,\n\nSecuring over N50 billion in recoveries points to a rare "
          "command of high-stakes corporate litigation." + SIGN)
_flags = quality.citation_issues(CFG, LAW, "Scaling research", _money)
check("an invented sum of money is caught", bool(_flags))
check("the invented sum is named in the issue", any("50" in f for f in _flags))

_places = ("Hi there,\n\nYour three Lagos training centres (Ikeja, Ketu, Ajah) have "
           "earned a stellar 4.8-star average across 202 Google reviews." + SIGN)
_flags = quality.citation_issues(CFG, TRAINER, "Training docs in seconds", _places)
check("invented office locations are caught", bool(_flags))
check("the invented locations are named in the issue",
      any("Ikeja" in f for f in _flags))
check("...even though the star rating in the SAME sentence is real and allowed",
      not any("4.8" in f or "202" in f for f in _flags))

_tons = ("Hi Nia,\n\nStoring 660,000 tons across 19 distribution centres for 60+ "
         "principals is a testament to your operational mastery." + SIGN)
_flags = quality.citation_issues(CFG, LOGISTICS, "660k tons, 19 centres", _tons)
check("an invented volume is caught even when the facts are rich", bool(_flags))
check("the invented volume is named in the issue", any("660" in f for f in _flags))
check("...while the real 19 centres and 60 principals are not flagged",
      not any("19" in f or "60 " in f for f in _flags))

_certs = ("Hi there,\n\nYour ISO 9001, 14001, 45001 and Ecovadis certifications, paired "
          "with a 4.5-star rating from 126 Google reviews, signal real quality." + SIGN)
_flags = quality.citation_issues(CFG, FREIGHT, "Certified and efficient", _certs)
check("invented certifications are caught", bool(_flags))
check("the invented standard is named in the issue",
      any("9001" in f or "ISO" in f for f in _flags))


# --------------------------------------------------------------------------
# ...without rejecting honest copy. This half is the reason it can gate at all.
# --------------------------------------------------------------------------
print("\n[honest copy grounded in the facts passes untouched]")

_honest = ("Hi there,\n\nMaintaining a 4.9-star rating across 331 reviews while running "
           "offices in both Ikeja and Arepo suggests a high bar for internal "
           "coordination. I imagine keeping precedents accessible between two offices is "
           "still manual. Our internal knowledge base delivers trusted answers from your "
           "own documents in seconds, cutting lookup time ~50% within 60 days.\n\n"
           "https://audit.example.test/magnet/acme/abc123\n\nWould a 15-minute audit of "
           "your retrieval process work?" + SIGN)
check("a draft citing only real facts is clean",
      quality.citation_issues(CFG, RICH, "Ikeja-Arepo document retrieval", _honest) == [])
check("the configured outcome figures (~50%, 60 days) are citable",
      not any("50" in f or "60" in f
              for f in quality.citation_issues(CFG, RICH, "s", _honest)))
check("a 15-minute meeting proposal is logistics, not a claim",
      quality.citation_issues(CFG, BARE, "s", f"Hi there,\n\nWorth a 15-minute call?{SIGN}") == [])
check("the audit link's random token is never read as a claim",
      quality.citation_issues(
          CFG, BARE, "s",
          f"Hi there,\n\nHere it is:\nhttps://audit.example.test/magnet/acme/9f8e7d6c5b4a{SIGN}") == [])
check("a typographic dash in a cited figure still matches the facts",
      quality.citation_issues(
          CFG, LOGISTICS, "s",
          f"Hi Nia,\n\nWe reclaim 10‑15 staff‑hours per week within 90 days.{SIGN}") == [])
# Morphology: the facts say "Africa", so the copy is entitled to say "African". This
# needs a MULTI-word phrase to mean anything — a lone capitalised word is never checked
# (it is usually a sentence start), so "Your African ports" would pass whether or not
# stemming worked. "African Corridor" is the real test: both words are absent from the
# facts verbatim, and only the africa->african stem keeps it from being flagged.
check("a morphological variant of a cited fact is allowed (Africa -> African)",
      quality.citation_issues(
          CFG, FREIGHT, "s",
          f"Hi there,\n\nYour African Corridor network stands out.{SIGN}") == [])
check("...while a two-word phrase with NO anchor in the facts is still flagged",
      bool(quality.citation_issues(
          CFG, FREIGHT, "s",
          f"Hi there,\n\nYour Sterling Bank partnership stands out.{SIGN}")))
check("a small bare integer is treated as inference, not a claim",
      quality.citation_issues(
          CFG, RICH, "s", f"Hi there,\n\nKeeping 2 offices in step is hard.{SIGN}") == [])
check("a Title Cased subject line is not read as a claim about them",
      quality.citation_issues(CFG, BARE, "Cut Lookup Time In Half",
                              f"Hi there,\n\nA plain honest line.{SIGN}") == [])
check("the client's own offering name is citable",
      quality.citation_issues(
          CFG, BARE, "s",
          f"Hi there,\n\nOur Workflow Automation could help.{SIGN}") == [])

# A fabricated figure in the SUBJECT is the first thing a prospect reads, so the
# subject is checked for figures even though it is exempt from the style checks.
check("a fabricated figure in the subject line is caught",
      bool(quality.citation_issues(CFG, BARE, "Cut 3,400 lookups a week",
                                   f"Hi there,\n\nA plain honest line.{SIGN}")))
# A rating that drifts from the facts is wrong in a way the reader can see.
check("a star rating inflated above the facts is caught",
      bool(quality.citation_issues(
          CFG, LOGISTICS, "s",
          f"Hi Nia,\n\nYour 4.9-star reputation speaks for itself.{SIGN}")))


# --------------------------------------------------------------------------
# The strategy brief is NOT evidence — the laundering hole
# --------------------------------------------------------------------------
print("\n[a model-written brief cannot launder a fabrication into a citation]")
# The brief is generated by the strategist, so it is exactly where an invented "fact"
# first appears. If it counted as evidence, the copywriter could cite the invention
# straight back and the check would wave it through. evidence_corpus must ignore it.
_brief = ("OBSERVATION: Harbour & Vale secured over N50 billion in recoveries.\n"
          "PAIN: precedent lookup is manual.\nSERVICE: Internal Knowledge Base\n"
          "OUTCOME: cutting lookup time ~50% within 60 days\nAUDIT: retrieval latency.")
check("the brief's own text is not part of the citable evidence",
      "50 billion" not in quality.evidence_corpus(CFG, LAW))
check("a fabrication repeated from the brief is still caught",
      bool(quality.citation_issues(CFG, LAW, "s", _money)))
check("evidence_corpus does contain the prospect's real scraped facts",
      "85,731sqm" in quality.evidence_corpus(CFG, LOGISTICS))
check("evidence_corpus does contain the human-authored outcome promises",
      "10-15 staff-hours" in quality.evidence_corpus(CFG, LOGISTICS))


# --------------------------------------------------------------------------
# evidence_is_thin — ask for what the evidence can support
# --------------------------------------------------------------------------
print("\n[thin evidence changes what we ASK for, never the honesty rule]")
check("a file that is only a star rating is thin", quality.evidence_is_thin(BARE))
check("rich scraped facts are not thin", not quality.evidence_is_thin(RICH))
check("a lead with no facts at all is thin", quality.evidence_is_thin({}))
check("the review signal alone does not make a file rich",
      quality.evidence_is_thin({"company_facts": "Well-reviewed: 5-star average across "
                                                 "44 Google reviews."}))

_thin_rules = quality.copy_instructions("Ada Obi", None, thin_evidence=True)
_rich_rules = quality.copy_instructions("Ada Obi", None, thin_evidence=False)
check("thin evidence drops the 'could NOT be copy-pasted' demand",
      "could NOT be copy-pasted" not in _thin_rules)
check("rich evidence keeps the specificity demand",
      "could NOT be copy-pasted" in _rich_rules)
check("thin evidence explicitly permits a plain, true opener",
      "plain" in _thin_rules.lower() and "TRUE opener" in _thin_rules)
check("thin evidence still forbids inventing detail",
      "invented offices" in _thin_rules or "did not give you" in _thin_rules)
for _label, _rules in (("thin", _thin_rules), ("rich", _rich_rules)):
    check(f"{_label}: the writer is warned the citation check is mechanical",
          "checked mechanically" in _rules)
check("the old 'earn the 8' score-chasing instruction is gone from both",
      "earn the 8" not in _thin_rules and "earn the 8" not in _rich_rules)


# --------------------------------------------------------------------------
# The judge — taught to catch invention, and told the evidence budget
# --------------------------------------------------------------------------
print("\n[the judge deducts for ANY unsourced specific, not just statistics]")
check("the rubric names fabrication as the most serious fault",
      "FABRICATION" in quality.RUBRIC)
for _kind in ("office", "client", "certification", "award", "headcount"):
    check(f"the rubric names an invented {_kind} as fabrication", _kind in quality.RUBRIC)
check("the rubric caps a fabricating email at 3 or below",
      "3 or below" in quality.RUBRIC)
check("the rubric no longer limits the deduction to statistics",
      "any statistic stated as the prospect's CURRENT reality" not in quality.RUBRIC)
check("'generic' is only a deduction when the facts held something better",
      "ONLY when the facts held something more specific" in quality.RUBRIC)

_prompts = []


def _capture_gj(scores):
    """A judge/reviser stand-in that records every prompt it is handed."""
    st = {"i": 0}

    def gj(cfg, prompt):
        _prompts.append(prompt)
        if "ruthless cold-email editor" in prompt:
            s = scores[min(st["i"], len(scores) - 1)]
            st["i"] += 1
            return {"score": s, "issues": ["too generic"], "fix_hint": "be specific"}, "judge"
        if "strict editor rejected" in prompt:
            return {"subject": "Rev", "body": f"Hi there,\n\nA plainer honest line.{SIGN}"}, "rev"
        raise AssertionError("unexpected prompt")

    return gj


quality.generate_json = _capture_gj([7])
_prompts.clear()
quality.score(CFG, BARE, "s", f"Hi there,\n\nPlain line.{SIGN}", "brief")
check("a thin file tells the judge its evidence budget",
      "EVIDENCE BUDGET" in _prompts[0])
check("...and tells it not to deduct for 'generic' when nothing better exists",
      "nothing more specific" in _prompts[0])
_prompts.clear()
quality.score(CFG, RICH, "s", f"Hi there,\n\nPlain line.{SIGN}", "brief")
check("a rich file does not get the evidence-budget note",
      "EVIDENCE BUDGET" not in _prompts[0])


# --------------------------------------------------------------------------
# revise — an invented claim must be removed before the copy is polished
# --------------------------------------------------------------------------
print("\n[the reviser is told exactly which claims to strip]")
quality.generate_json = _capture_gj([7])
_prompts.clear()
quality.revise(CFG, LAW, "s", _money, {"issues": ["x"], "fix_hint": "y"}, None, "Ada Obi",
               brief="", citation_problems=["the figure '50 billion' is not in the facts"])
check("the revise prompt names the unsupported claim",
      "50 billion" in _prompts[0])
check("the revise prompt says the draft invented facts",
      "INVENTED FACTS" in _prompts[0])
check("the revise prompt forbids swapping in a different invention",
      "different invented detail" in _prompts[0])
_prompts.clear()
quality.revise(CFG, BARE, "s", f"Hi there,\n\nPlain.{SIGN}", {}, None, "Ada Obi")
check("with a thin file, the default fix hint does NOT ask for more specificity",
      "Make it more specific" not in _prompts[0])
check("...it asks for the achievable thing instead",
      "isn't in the facts" in _prompts[0] or "sharpen the closing question" in _prompts[0])


# --------------------------------------------------------------------------
# draft_and_polish — the citation gate and the HARD floor
# --------------------------------------------------------------------------
print("\n[draft_and_polish refuses rather than ships]")


class DraftFn:
    def __init__(self, results):
        self.results = results
        self.calls = 0

    def __call__(self, cfg, lead, brief, magnet_url):
        r = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return r


_CLEAN_BODY = f"Hi there,\n\nA plain, honest, grounded line about your work.{SIGN}"

# A) The hard floor: a draft the judge scores below it is REFUSED, not queued. This is
#    the behaviour change — the old loop returned `best` unconditionally.
quality.generate_json = _capture_gj([5, 5])
df = DraftFn([("S", _CLEAN_BODY, "provA")])
try:
    quality.draft_and_polish(
        gate_cfg({"enabled": True, "min_score": 8, "floor_score": 6, "max_revisions": 1}),
        BARE, "brief", None, df)
    check("a draft below the floor raises instead of shipping", False)
except RuntimeError as e:
    check("a draft below the floor raises instead of shipping", True)
    check("the refusal says what the score and floor were",
          "5/10" in str(e) and "floor of 6" in str(e))

# B) At the floor it ships: the floor is a floor, not the aspiration. This is the case
#    that keeps volume flowing — most honest drafts score 6-7, not 8.
quality.generate_json = _capture_gj([6])
df = DraftFn([("S", _CLEAN_BODY, "provA")])
out = quality.draft_and_polish(
    gate_cfg({"enabled": True, "min_score": 8, "floor_score": 6, "max_revisions": 0}),
    BARE, "brief", None, df)
check("a draft AT the floor still ships (volume is preserved)", out == ("S", _CLEAN_BODY, "provA"))

check("the floor defaults to 6, below the min_score aspiration",
      __import__("core.config", fromlist=["config"]).DEFAULTS["copy"]["quality_gate"]["floor_score"] == 6)

# C) The citation gate: a fabricating draft is never returned, even when the judge
#    loves it — and a revision that strips the invented claim rescues the lead.
_FAB_BODY = ("Hi Nia,\n\nStoring 660,000 tons across your centres is a testament to "
             "your operational mastery." + SIGN)
quality.generate_json = _capture_gj([9, 9])
df = DraftFn([("S", _FAB_BODY, "provA")])
out = quality.draft_and_polish(
    gate_cfg({"enabled": True, "min_score": 8, "floor_score": 6, "max_revisions": 1}),
    LOGISTICS, "brief", None, df)
check("a 9/10 draft that invented a figure is NOT shipped as drafted", out[1] != _FAB_BODY)
check("the revision that dropped the invention is shipped instead", out[0] == "Rev")

# D) If no revision can clean it, refuse outright rather than queue it.
quality.generate_json = _capture_gj([9])
quality.revise = lambda *a, **k: ("S", _FAB_BODY)   # every rewrite reinvents the claim
df = DraftFn([("S", _FAB_BODY, "provA")])
try:
    quality.draft_and_polish(
        gate_cfg({"enabled": True, "min_score": 8, "floor_score": 6, "max_revisions": 1}),
        LOGISTICS, "brief", None, df)
    check("an unfixable fabrication raises instead of shipping", False)
except RuntimeError as e:
    check("an unfixable fabrication raises instead of shipping", True)
    check("the refusal names the unsupported figure", "660" in str(e))

print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
