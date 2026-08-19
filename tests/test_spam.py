"""Offline tests for the spam linter (core/spam.py) + its wiring into review.

NO network. Run:  venv/bin/python tests/test_spam.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import spam, review          # noqa: E402

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
# Clean copy -> ok, no flags
# --------------------------------------------------------------------------
clean = spam.check(
    "A quick idea for Acme",
    "Hi Ada, I mapped a small fix for your order process. Here is a short audit: "
    "https://ejentic.ai/a/1. Reply if it is useful.",
)
check("clean copy scores 0", clean["score"] == 0)
check("clean copy level is ok", clean["level"] == "ok")
check("clean copy has no flags", clean["flags"] == [])


# --------------------------------------------------------------------------
# Spammy copy -> high, with suggested swaps
# --------------------------------------------------------------------------
spammy = spam.check(
    "ACT NOW — 100% FREE MONEY GUARANTEE!!!",
    "CLICK HERE to buy now! Limited time. Visit http://deal.co — cash winner!",
)
check("spammy copy is high risk", spammy["level"] == "high")
check("spammy copy scores well above threshold", spammy["score"] >= 4)
terms = {f.get("term") for f in spammy["flags"] if f["type"] == "word"}
check("flags 'free money'", "free money" in terms)
check("flags 'act now'", "act now" in terms)
check("flags 'click here'", "click here" in terms)
check("a word flag carries a suggested swap",
      any(f.get("suggest") for f in spammy["flags"] if f["type"] == "word"))
types = {f["type"] for f in spammy["flags"]}
check("detects ALL-CAPS shouting", "caps" in types or "caps-subject" in types)
check("detects excess exclamation marks", "exclaim" in types)
check("detects insecure http link", "insecure-link" in types)


# --------------------------------------------------------------------------
# Repetition rule: one "free" is fine, several is not
# --------------------------------------------------------------------------
one_free = spam.check("hi", "Here is a free audit for you.")
check("a single 'free' is NOT flagged",
      not any(f.get("term") == "free" for f in one_free["flags"]))
many_free = spam.check("hi", "free free free resources for free")
free_flag = [f for f in many_free["flags"] if f.get("term") == "free"]
check("repeated 'free' IS flagged", len(free_flag) == 1 and free_flag[0]["count"] >= 2)


# --------------------------------------------------------------------------
# Whole-word matching (no false positives inside longer words)
# --------------------------------------------------------------------------
substr = spam.check("hi", "Our freedom and product offerings are great.")
check("'freedom' does not trip 'free'",
      not any(f.get("term") == "free" for f in substr["flags"]))
check("'offerings' does not trip 'offer'",
      not any(f.get("term") == "offer" for f in substr["flags"]))


# --------------------------------------------------------------------------
# Legit acronyms are not treated as shouting
# --------------------------------------------------------------------------
acro = spam.check("Our AI + HTTPS setup", "We use an API and a PDF guide.")
check("known acronyms are not flagged as caps",
      not any(t in ("caps", "caps-subject") for t in {f["type"] for f in acro["flags"]}))

shout = spam.check("BIG SALE TODAY", "normal body text here")
check("a genuinely shouting subject is flagged",
      any(f["type"] == "caps-subject" for f in shout["flags"]))


# --------------------------------------------------------------------------
# Long subject
# --------------------------------------------------------------------------
long_subj = spam.check("x" * 80, "body")
check("overly long subject is flagged",
      any(f["type"] == "long-subject" for f in long_subj["flags"]))


# --------------------------------------------------------------------------
# describe()
# --------------------------------------------------------------------------
check("describe() labels a clean result OK", "OK" in spam.describe(clean))
check("describe() labels a spammy result HIGH", "HIGH" in spam.describe(spammy))
check("describe(None) is safe", "not run" in spam.describe(None))


# --------------------------------------------------------------------------
# Wiring: review.save_pending stashes the check; _spam_html renders it
# --------------------------------------------------------------------------
review.PENDING_FILE = os.path.join(tempfile.mkdtemp(), "pending.json")  # don't touch real queue
entry = {"kind": "cold", "drafted_subject": "ACT NOW!!!", "drafted_body": "FREE MONEY guaranteed"}
review.save_pending(entry)
check("save_pending stashes a spam result", entry.get("spam", {}).get("level") == "high")

html_block = review._spam_html(entry)
check("_spam_html renders a readout block", "border-left" in html_block and "score" in html_block)
check("_spam_html is empty when no check present", review._spam_html({"kind": "cold"}) == "")


# --------------------------------------------------------------------------
print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
