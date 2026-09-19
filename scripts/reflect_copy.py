"""Reflection pass: turn recurring judge critiques into PROPOSED copy lessons.

The drafter scores every email and rewrites the weak ones, but that reflection only
ever lived for one run. The critique was used for a single rewrite and discarded, so
the same weakness was rediscovered every morning and run 11 was no better than run 1.
This reads the accumulated critiques, finds what keeps going wrong, and writes it down.

TWO RULES MAKE THIS SAFE, AND BOTH ARE LOAD-BEARING:

1. IT MAY PROPOSE; IT MAY NEVER PROMOTE.
   Everything written here lands with status='proposed'. Nothing reaches a live prompt
   until a human approves it (`--approve <id>`). This is the same gate the website's
   Nova learning loop uses, for the same reason: a drafter that can silently rewrite
   its own instructions is a drafter that can talk itself back into bad habits, and
   nobody would see the change happen.

2. A LESSON IS A STYLE RULE, NEVER A FACT.
   "Open with the bottleneck, not a compliment" is a lesson. "Mention their 20 years
   in business" is a FACT, and a fact in the prompt is fabrication with extra steps —
   it would be asserted about every prospect regardless of evidence, and it would enter
   the copy ABOVE the citation checker rather than below it. `lesson_problems()` rejects
   anything carrying a number or a proper noun, and it runs before storage, not after.

Run:
  CLIENT=ejentic venv/bin/python -u scripts/reflect_copy.py            # propose
  CLIENT=ejentic venv/bin/python -u scripts/reflect_copy.py --list     # review
  CLIENT=ejentic venv/bin/python -u scripts/reflect_copy.py --approve 3
  CLIENT=ejentic venv/bin/python -u scripts/reflect_copy.py --reject 4
"""
import warnings
warnings.filterwarnings("ignore")

import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import config, state          # noqa: E402
from core.ai import generate_json       # noqa: E402

# How many critiques to look back over, and how weak a draft must be to be worth
# learning from. A 9/10 draft's nitpick is noise; a refusal is the real signal.
LOOKBACK = 300
WEAK_AT_OR_BELOW = 7
MIN_OCCURRENCES = 3      # a one-off is a bad day, not a pattern
MAX_PROPOSALS = 5

# Words that may legitimately start a lesson in capitalised form.
_SENTENCE_START_OK = re.compile(r'^[A-Z][a-z]+')


def lesson_problems(text):
    """Why this proposed lesson is unsafe to ever put in a prompt, or [] if it is fine.

    Deliberately mechanical and deliberately strict. A lesson is injected into EVERY
    draft for this client, so anything specific in it becomes a claim made about every
    prospect — which is precisely the fabrication the citation checker exists to stop,
    except introduced upstream of it where the checker cannot see it.
    """
    problems = []
    t = (text or "").strip()
    if not t:
        return ["empty"]
    if len(t) > 160:
        problems.append("too long to be a rule (max 160 chars)")
    if len(t.split()) < 3:
        problems.append("too short to be meaningful")
    if re.search(r'\d', t):
        problems.append("contains a number — a lesson must not carry a figure")
    if re.search(r'[%$£€₦]', t):
        problems.append("contains a currency or percent sign")
    # Capitalised words mid-sentence read as names of real things.
    words = t.split()
    for w in words[1:]:
        bare = re.sub(r'[^A-Za-z]', '', w)
        if len(bare) > 1 and bare[0].isupper() and not bare.isupper():
            problems.append(f"contains a proper noun ({bare!r}) — lessons must be generic")
            break
    if re.search(r'\b(always say|claim|assert|tell them they)\b', t, re.IGNORECASE):
        problems.append("instructs the writer to assert something rather than to write better")
    return problems


def gather(conn, client):
    """Recurring weaknesses, most common first."""
    rows = state.recent_critiques(conn, client, limit=LOOKBACK)
    phrases, refusals, scored = Counter(), Counter(), []
    for r in rows:
        if r["score"] is not None:
            scored.append(r["score"])
        if r["attempt"] == "final" and r["outcome"] in (
                "refused_floor", "refused_citation"):
            refusals[r["outcome"]] += 1
        weak = r["score"] is None or r["score"] <= WEAK_AT_OR_BELOW
        if not weak:
            continue
        try:
            for issue in json.loads(r["issues"] or "[]"):
                key = re.sub(r'\s+', ' ', str(issue).strip().lower())[:80]
                if key:
                    phrases[key] += 1
        except Exception:
            continue
    return phrases, refusals, scored


def propose(cfg, conn, client):
    phrases, refusals, scored = gather(conn, client)
    total = sum(phrases.values())
    if not total:
        print("No critiques recorded yet — nothing to reflect on.")
        print("Run the drafter a few times first; this reads what the judge said.")
        return 0

    recurring = [(p, c) for p, c in phrases.most_common(25) if c >= MIN_OCCURRENCES]
    print(f"Read {total} issue mention(s) across the last {LOOKBACK} critique rows.")
    if scored:
        print(f"Mean judge score over that window: {sum(scored)/len(scored):.2f}")
    if refusals:
        print("Refusals: " + ", ".join(f"{k}={v}" for k, v in refusals.items()))
    print()
    if not recurring:
        print(f"No issue recurred {MIN_OCCURRENCES}+ times yet — too early to call "
              f"anything a pattern. Nothing proposed (this is the correct outcome, "
              f"not a failure).")
        return 0

    print("Recurring weaknesses:")
    for p, c in recurring[:12]:
        print(f"   {c:>3}x  {p}")
    print()

    listed = "\n".join(f"- ({c}x) {p}" for p, c in recurring[:15])
    prompt = f"""You are reviewing an automated cold-email writer's own quality reports.
Below are the weaknesses its editor flagged most often, with counts.

{listed}

Write at most {MAX_PROPOSALS} short RULES that would prevent these weaknesses.

HARD CONSTRAINTS — a rule breaking any of these is useless and will be discarded:
- A rule is about HOW TO WRITE, never about what is true of any company.
- NEVER include a number, percentage, figure, currency, date or statistic.
- NEVER include a company name, person's name, place, or any proper noun.
- NEVER instruct the writer to claim or assert anything about a prospect.
- Each rule must be one imperative sentence under 140 characters.

Good: "Open on the specific bottleneck the facts imply, not on a compliment."
Bad:  "Mention their 15 years of experience."   (a fact, and invented)
Bad:  "Reference their Lagos office."           (a proper noun)

Reply with ONLY this JSON, no prose:
{{"rules": ["<rule>", "..."]}}"""

    try:
        obj, _ = generate_json(cfg, prompt)
    except Exception as e:
        print(f"❌ Could not reach the model to summarise lessons: {e}")
        return 1

    rules = obj.get("rules") or []
    if not isinstance(rules, list):
        rules = [str(rules)]

    stored = rejected = 0
    print("Proposed lessons (NOT active — each needs your approval):\n")
    for raw in rules[:MAX_PROPOSALS]:
        text = str(raw).strip().strip('"')
        problems = lesson_problems(text)
        if problems:
            rejected += 1
            print(f"   🚫 discarded: {text!r}")
            for p in problems:
                print(f"        — {p}")
            continue
        ev = {"recurring": recurring[:6], "window": LOOKBACK}
        lid = state.propose_lesson(conn, client, text, ev)
        if lid is None:
            print(f"   ⏭️  already on file: {text}")
        else:
            stored += 1
            print(f"   💡 #{lid}  {text}")

    print()
    print(f"{stored} new proposal(s) stored, {rejected} discarded as unsafe.")
    if stored:
        print("None of these affect drafting yet. Review them, then approve the good "
              "ones:\n   CLIENT=%s venv/bin/python -u scripts/reflect_copy.py --list"
              % client)
    return 0


def main():
    args = sys.argv[1:]
    cfg = config.load_client()
    client = cfg["client"]
    conn = state.connect(cfg["paths"]["db"])

    if "--list" in args:
        rows = state.list_lessons(conn, client)
        if not rows:
            print("No lessons on file.")
            return 0
        for r in rows:
            mark = {"approved": "✅", "rejected": "❌"}.get(r["status"], "💡")
            print(f"{mark} #{r['id']:<4} [{r['status']}] {r['lesson']}")
        active = state.approved_lessons(conn, client)
        print(f"\n{len(active)} lesson(s) currently influencing drafting.")
        return 0

    for flag, status in (("--approve", "approved"), ("--reject", "rejected")):
        if flag in args:
            try:
                lid = int(args[args.index(flag) + 1])
            except (IndexError, ValueError):
                print(f"Usage: {flag} <lesson id>")
                return 2
            state.decide_lesson(conn, client, lid, status)
            print(f"Lesson #{lid} is now {status}.")
            if status == "approved":
                print("It will be included in the drafting instructions from the next run.")
            return 0

    return propose(cfg, conn, client)


if __name__ == "__main__":
    sys.exit(main())
