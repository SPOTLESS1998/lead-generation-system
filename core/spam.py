"""Offline spam-trigger linter — automates the old manual "check the copy" step.

No network and no external service (this replaces pasting drafts into MailMeteor by
hand). It scans a draft's subject + body for the words and patterns that push cold
email into spam folders and returns:

    {"score": int, "level": "ok" | "warn" | "high", "flags": [ ... ]}

Each flag names the problem and, for trigger words, a softer suggested swap. The
operator sees this readout right in the approval email, and the copywriter prompt is
hardened against the same list — so most drafts come back clean.

Pure functions, no state: safe to call from the drafting agent and the review layer.

Design choice: common-but-fine words like "free" or "offer" are only flagged when
REPEATED (repetition is the real signal). One "free audit" is normal; three "free"s
is spammy. This keeps the linter useful instead of crying wolf on every draft.
"""

import re

# Trigger phrase -> softer suggested replacement ("" = just drop / rephrase).
# Matched case-insensitively, on whole words (so "freedom" won't match "free").
TRIGGER_WORDS = {
    "free money": "no-cost",
    "100% free": "no cost",
    "risk-free": "no-obligation",
    "risk free": "no-obligation",
    "act now": "when you're ready",
    "buy now": "get started",
    "order now": "get started",
    "click here": "see it here",
    "limited time": "this month",
    "limited offer": "current offer",
    "urgent": "quick",
    "guarantee": "aim to",
    "guaranteed": "reliably",
    "winner": "selected",
    "congratulations": "hello",
    "cash": "revenue",
    "make money": "grow revenue",
    "earn money": "grow revenue",
    "double your": "grow your",
    "extra income": "added revenue",
    "cheap": "affordable",
    "special promotion": "something for you",
    "no obligation": "no pressure",
    "why pay more": "a better option",
    "this is not spam": "",
    "miracle": "significant",
    "amazing": "useful",
    "incredible": "notable",
    # Common-but-fine words — only flagged when repeated (see REPEAT_ONLY):
    "free": "complimentary",
    "offer": "idea",
    "discount": "saving",
}

# Heavier signals (weight 2). Everything else counts as weight 1.
HEAVY = {
    "free money", "100% free", "risk-free", "risk free", "act now", "buy now",
    "guarantee", "guaranteed", "winner", "congratulations", "make money",
    "earn money", "cash", "this is not spam", "miracle",
}

# Fine in moderation — flag only when they appear 2+ times.
REPEAT_ONLY = {"free", "offer", "discount"}

# All-caps acronyms that are legitimate and should not be flagged as shouting.
CAPS_ALLOW = {"HTTP", "HTTPS", "HTML", "FAQ", "CEO", "CTO", "COO", "CFO",
              "AI", "API", "SaaS", "B2B", "B2C", "ROI", "PDF", "URL", "USD"}


def _count(phrase, text_low):
    """Whole-word occurrences of `phrase` in already-lowercased text."""
    pat = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
    return len(re.findall(pat, text_low))


def check(subject, body):
    """Score a draft for spam-trigger risk. Returns a dict (see module docstring)."""
    subject = subject or ""
    body = body or ""
    text = f"{subject}\n{body}"
    low = text.lower()

    flags = []
    score = 0

    # 1) Trigger words / phrases.
    for phrase, swap in TRIGGER_WORDS.items():
        count = _count(phrase, low)
        if count == 0:
            continue
        if phrase in REPEAT_ONLY and count < 2:
            continue  # one is fine; only repetition is a real signal
        weight = 2 if phrase in HEAVY else 1
        score += weight * count
        flags.append({
            "type": "word",
            "term": phrase,
            "count": count,
            "suggest": swap,
            "detail": (f"\"{phrase}\"" + (f" x{count}" if count > 1 else "")
                       + (f" → try \"{swap}\"" if swap else " → rephrase / drop")),
        })

    # 2) SHOUTING — 4+ letter all-caps words (excluding known acronyms).
    caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", text) if w not in CAPS_ALLOW]
    if caps:
        seen = sorted(set(caps))
        score += min(len(seen), 2)
        flags.append({"type": "caps",
                      "detail": "ALL-CAPS word(s): " + ", ".join(seen[:5])})

    # 3) A subject line shouting is a strong signal on its own.
    subj_caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", subject) if w not in CAPS_ALLOW]
    if subj_caps:
        score += 2
        flags.append({"type": "caps-subject",
                      "detail": "Subject uses ALL-CAPS — reads as shouting"})

    # 4) Exclamation marks.
    bangs = text.count("!")
    if bangs >= 2:
        score += 2 if bangs >= 4 else 1
        flags.append({"type": "exclaim",
                      "detail": f"{bangs} exclamation marks — keep at most one"})

    # 5) Insecure (http://) links hurt deliverability + trust. Loopback/localhost
    #    URLs are local-only (dev/demo previews) and never reach a real inbox, so
    #    they are exempt — only genuine external http:// links are flagged.
    insecure = len(re.findall(r"http://(?!localhost[:/]|127\.|0\.0\.0\.0|\[::1\]|::1)", low))
    if insecure:
        score += 2
        flags.append({"type": "insecure-link",
                      "detail": f"{insecure} insecure http:// link(s) — use https://"})

    # 6) Overly long subject lines get truncated / look spammy.
    if len(subject) > 70:
        score += 1
        flags.append({"type": "long-subject",
                      "detail": f"Subject is {len(subject)} chars — aim for under 60"})

    level = "ok" if score == 0 else ("warn" if score <= 3 else "high")
    return {"score": score, "level": level, "flags": flags}


def describe(result):
    """A compact one-line human summary of a check() result."""
    if not result:
        return "spam check: (not run)"
    if result["level"] == "ok":
        return "spam check: OK — no trigger words or patterns found."
    label = {"warn": "WARN", "high": "HIGH"}[result["level"]]
    items = "; ".join(f["detail"] for f in result["flags"][:8])
    return f"spam check: {label} (score {result['score']}) — {items}"
