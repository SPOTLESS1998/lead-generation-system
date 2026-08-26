"""Draft-quality gate — LLM-as-judge scoring + automatic rewrite of weak copy.

The conversion step is the whole game: a bland pitch wastes the lead. So after the
copywriter drafts an email we run a strict editor over it (score 1-10 against a fixed
rubric) and, if it falls short, feed the critique back for one rewrite pass — optionally
drafting a few candidates and keeping the best. This is the same LLM-as-judge pattern
the RAG system uses for eval, applied to outbound copy.

The single source of truth for what "good" means lives here (`copy_instructions` +
`RUBRIC`), so the drafter (scripts/lead_agent.generate_copy), the reviser, and the judge
all hold the copy to the identical standard — they can't drift apart.

Everything degrades gracefully: if the judge LLM is unavailable the draft simply ships
unscored (house pattern — never block the pipeline on a quality-of-life feature). All LLM
calls go through the cfg handed in, so premium-vs-free is already decided by the caller's
budget check (core/budget.copy_cfg) before we get here.
"""

from core.ai import generate_json

# Template signatures a model sometimes leaves behind; swapped for the real sender.
_PLACEHOLDERS = ("[Your Name]", "[Your name]", "[YOUR NAME]", "[Name]", "[name]",
                 "[Your Company]", "[Company]", "[your name]")

# Closing words that count as a real sign-off; if none is present we add one.
_SIGNOFFS = ("best,", "best regards", "regards,", "cheers,", "thanks,", "thank you,",
             "sincerely,", "warmly,", "talk soon", "speak soon")


def finalize_body(body, sender_name, magnet_url=None):
    """Shared safety net for any body (fresh draft OR rewrite): kill placeholder
    signatures, guarantee the promised audit link is present, and guarantee a real
    sign-off. A draft that drops the sign-off otherwise ships looking unfinished and
    gets marked down — fixing it here covers BOTH the draft and the revise path."""
    body = (body or "").strip()
    for ph in _PLACEHOLDERS:
        body = body.replace(ph, sender_name)
    if magnet_url and magnet_url not in body:
        body = f"{body}\n\n{magnet_url}"
    # Append a sign-off only when the body has neither a closing word nor the
    # sender's name near the end — never double an existing one. An empty body is
    # left empty on purpose so the caller's emptiness check still fires.
    low = body.lower()
    if body and sender_name and (not any(s in low for s in _SIGNOFFS)
                                 or sender_name.lower() not in low):
        body = f"{body}\n\nBest,\n{sender_name}"
    return body


def copy_instructions(sender_name, magnet_url):
    """The one framework the drafter AND the reviser must follow (kept identical so a
    rewrite is held to the same bar as a first draft). `magnet_url` toggles the link rule."""
    if magnet_url:
        link_rule = (f"Put this exact audit link on its OWN line, written in full and "
                     f"unchanged — never a placeholder: {magnet_url}")
    else:
        link_rule = ("Offer a free personalized AI audit and invite a one-word reply to get it. "
                     "Do NOT invent or include any link.")
    return f"""Follow this framework exactly:
- Greet them by first name.
- Sentence 1 — a specific, TRUE observation about THEIR business, drawn only from the facts you were given (never a generic compliment, never an invented fact).
- Sentence 2 — the concrete operational pain that observation implies, in plain words.
- Sentence 3 — one believable outcome tied to the offer (a plausible number or timeframe beats hype).
- Never state a statistic as a fact about THEIR current business unless it appears in the facts you were given (do NOT assert things like "you lose 30% of leads" or "you waste 10 hours a week"). If you need a number to size the problem, frame it as a benchmark ("firms your size typically..."). The ONE allowed projection is the outcome in Sentence 3.
- {link_rule}
- End with ONE specific, low-friction question that proposes a concrete next step — never "let me know" or "let me know your thoughts".
- 60-90 words total. Read like a sharp human wrote it in two minutes for this one person.
- No buzzwords, no "I hope this finds you well", no "I wanted to reach out", no "in today's fast-paced world", no "leverage/seamless/cutting-edge". Warm, confident, plain.
- Deliverability: no spam-trigger words (free money, guarantee, act now, limited time, click here, 100%, cash, urgent, risk-free); no ALL-CAPS words; at most one "!"; https links only.
- Sign off with "Best," on one line, then "{sender_name}" on the next. Never leave a placeholder like "[Your Name]".
Subject line: specific and curiosity-driving, UNDER 6 words, no clickbait, and with no "Subject:" prefix."""


RUBRIC = """Score 1-10, where 10 is a top-1% cold email a sharp founder would actually reply to.
Most competent first drafts are a 6 or 7 — reserve 8+ for genuinely specific, human copy.
Reward copy where sentence 1 cites a concrete, verifiable fact about THIS prospect (a real
service they list, a named detail, genuine review volume) rather than a generic guess.
Deduct hard for any of these:
- a generic sentence that could be sent to any company (not specific to THIS prospect)
- any statistic stated as the prospect's CURRENT reality that wasn't given as a fact (an invented "you lose X%" / "you waste Y hours") — a fabricated number is worse than no number
- hype, vague value, or an unbelievable claim
- AI/robotic tells or buzzwords ("I hope this finds you well", "leverage", "seamless", "cutting-edge", "in today's...")
- wrong length (must be 60-90 words), a missing https audit link, or a weak/vague CTA ("let me know")
- any "[placeholder]", a broken sign-off, or a subject over 5 words / clickbait / vague"""


def score(cfg, lead, subject, body, brief=""):
    """Judge one draft. Returns {"score": int 1-10, "issues": [...], "fix_hint": str},
    or None if the judge is unavailable (caller then ships the draft unscored)."""
    wc = len((body or "").split())
    facts = lead.get("company_facts") or lead.get("company_description") or ""
    prompt = f"""You are a ruthless cold-email editor working for {cfg.get('client_name','the agency')}.
Judge this cold email. Be specific and hard to impress.

PROSPECT (the email must be specifically about THIS business, not generic):
Name: {lead.get('first_name','')} {lead.get('last_name','')}
Company: {lead.get('company_name','')}
What we actually know about them (the email must be grounded in THESE facts, and must not invent others): {facts}

STRATEGY BRIEF the writer was given:
{brief or '(none)'}

EMAIL UNDER REVIEW ({wc} words):
Subject: {subject}
{body}

{RUBRIC}

Reply with ONLY this JSON, no prose, no markdown fences:
{{"score": <integer 1-10>, "issues": ["<short phrase>", ...], "fix_hint": "<ONE concrete, actionable instruction that would raise the score the most>"}}"""
    try:
        obj, _ = generate_json(cfg, prompt)
    except Exception:
        return None
    try:
        s = int(round(float(obj.get("score"))))
    except (TypeError, ValueError):
        return None
    s = max(1, min(10, s))
    issues = obj.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    return {"score": s, "issues": [str(i) for i in issues][:6],
            "fix_hint": str(obj.get("fix_hint") or "").strip()}


def revise(cfg, lead, subject, body, critique, magnet_url, sender_name):
    """Rewrite a draft to address the editor's critique. Returns (subject, body)."""
    fix_hint = (critique or {}).get("fix_hint") or "Make it more specific and human."
    issues = ", ".join((critique or {}).get("issues") or []) or "generic / not specific enough"
    facts = lead.get("company_facts") or lead.get("company_description") or ""
    prompt = f"""You are a world-class B2B cold-email copywriter for {cfg.get('client_name','the agency')}.
A strict editor rejected the draft below. Rewrite it so it fully earns a reply. Keep what already works.

PROSPECT: {lead.get('first_name','')} {lead.get('last_name','')} at {lead.get('company_name','')}
What we actually know about them (ground it in THESE facts; keep the specific one the draft already cites, don't invent new ones): {facts}

EDITOR'S ISSUES: {issues}
EDITOR'S #1 FIX: {fix_hint}

CURRENT DRAFT:
Subject: {subject}
{body}

{copy_instructions(sender_name, magnet_url)}

Reply with ONLY this JSON, no prose, no markdown fences:
{{"subject": "<subject, no 'Subject:' prefix>", "body": "<full body, greeting through sign-off, real newlines>"}}"""
    obj, _ = generate_json(cfg, prompt)
    new_subject = (obj.get("subject") or subject).strip()
    new_body = finalize_body(obj.get("body") or "", sender_name, magnet_url)
    if not new_body:
        raise RuntimeError("reviser returned an empty body")
    return new_subject, new_body


def draft_and_polish(cfg, lead, brief, magnet_url, draft_fn):
    """Draft (optionally best-of-N), score, and rewrite weak copy up to the configured
    number of passes. Returns (subject, body, provider) — a drop-in for generate_copy().

    `draft_fn(cfg, lead, brief, magnet_url) -> (subject, body, provider)` is injected so
    this module never imports the drafter (no cycle). Reads cfg['copy']['quality_gate'].
    """
    gate = (cfg.get("copy", {}) or {}).get("quality_gate", {}) or {}
    enabled = bool(gate.get("enabled"))
    min_score = int(gate.get("min_score", 8))
    max_rev = int(gate.get("max_revisions", 1))
    best_of = max(1, int(gate.get("best_of", 1)))
    sender_name = (cfg.get("from_name") or cfg.get("client_name") or "our team").strip()

    # 1) Draft one (or, if the gate + best_of are on, a few) candidate(s).
    n = best_of if enabled else 1
    candidates, last_err = [], None
    for _ in range(n):
        try:
            candidates.append(draft_fn(cfg, lead, brief, magnet_url))
        except Exception as e:
            last_err = e
    if not candidates:
        raise last_err or RuntimeError("no draft produced")

    if not enabled:
        return candidates[0]

    # 2) Score each candidate; keep the best. If the judge is down, ship as-is.
    scored = [(s, b, prov, score(cfg, lead, s, b, brief)) for (s, b, prov) in candidates]
    if all(sc is None for (_, _, _, sc) in scored):
        s, b, prov, _ = scored[0]
        print("   🧪 [Quality] judge unavailable — shipping draft unscored.")
        return s, b, prov
    s, b, prov, sc = max(scored, key=lambda x: (x[3]["score"] if x[3] else -1))
    cur = sc["score"] if sc else min_score
    print(f"   🧪 [Quality] draft scored {cur}/10" + (f" (best of {n})" if n > 1 else "") + ".")

    # 3) Rewrite while below the bar, keeping the highest-scoring version seen.
    best = (s, b, cur)
    rev = 0
    while cur < min_score and rev < max_rev:
        rev += 1
        try:
            ns, nb = revise(cfg, lead, s, b, sc, magnet_url, sender_name)
        except Exception as e:
            print(f"   🧪 [Quality] revision {rev} failed ({e}); keeping best draft.")
            break
        nsc = score(cfg, lead, ns, nb, brief)
        nval = nsc["score"] if nsc else cur
        print(f"   🧪 [Quality] revision {rev} scored {nval}/10.")
        if nval >= best[2]:
            best = (ns, nb, nval)
        s, b, sc, cur = ns, nb, (nsc or sc), nval
    print(f"   🧪 [Quality] final {best[2]}/10 (min {min_score}, {rev} revision(s)).")
    return best[0], best[1], prov
