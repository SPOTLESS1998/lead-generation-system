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

import re

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
    # Greeting safety net (before the placeholder swap): a model told to "greet by
    # first name" but given none sometimes writes the literal token — "Hi Name,",
    # "Hi First Name,", "Hi [Name],". Rewrite ONLY that opening greeting to a safe
    # "Hi there,"; a real name ("Hi Ada,") or an already-safe "Hi there," is untouched.
    if body:
        body = re.sub(
            r'^\s*(?:hi|hello|hey|dear)\s+\[?\{?(?:first[\s_-]*)?name\}?\]?\s*,',
            'Hi there,', body, count=1, flags=re.IGNORECASE)
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
- Greet them by first name if one is given; if the name is unknown or a generic mailbox, open with exactly "Hi there," — NEVER write a literal placeholder like "Name" or "[First Name]".
- Sentence 1 — a specific, TRUE, FLATTERING observation about THEIR business, drawn only from the facts you were given (never a generic compliment, never an invented fact). Lead with their strongest, most differentiating fact; NEVER open by highlighting a negative or operational detail (a complaints line, a support/error number, a disclaimer, a problem) — that insults the reader.
- Make that opener so specific to THIS prospect that it could NOT be copy-pasted to any other company — name the real service, product, market, or detail from the facts. A line that would fit any business in their industry is the gap between a 7 and an 8; earn the 8.
- Do NOT just recite a fact they already know (their own review count, founding year, office list) back at them — add YOUR read of it: the implication, the tension, or what it says about them. Restating the brief's observation verbatim reads as machine-assembled; naming the fact AND what it reveals reads human.
- Sentence 2 — the operational pain that observation implies. Frame it as YOUR inference or an open question, NEVER as a stated fact about their internals: hedge with "likely", "my guess is", "I imagine", or ask it ("...is that still manual?"). Do NOT assert a specific internal process as if you know it (e.g. never write "your team manually sources and qualifies" as fact — you don't know that).
- Sentence 3 — the outcome from the brief, stated with its EXACT figure and timeframe (never inflate it or swap in a bigger number).
- Pitch ONLY the single service named in the strategy brief's SERVICE line — never offer a different or extra service, even if their industry suggests one (do not pitch ad-spend, SEO, or web design). Sell exactly what the brief names.
- Never state a statistic as a fact about THEIR current business unless it appears in the facts you were given (do NOT assert things like "you lose 30% of leads" or "you waste 10 hours a week"). If you need a number to size the problem, frame it as a benchmark ("firms your size typically..."). The ONE allowed projection is the outcome in Sentence 3.
- {link_rule}
- End with ONE specific, low-friction question that proposes a concrete next step — never "let me know" or "let me know your thoughts".
- Keep ONE consistent frame: the subject, the pain, and the closing question should all point at the SAME angle or market — never drift (e.g. subject "six-country", body "four markets", CTA "North America" reads as three different emails stitched together).
- 60-90 words total, but aim for ~70-80 — 90 is a HARD ceiling you must never cross; one word over reads as bloated and gets cut. Read like a sharp human wrote it in two minutes for this one person.
- No buzzwords, no "I hope this finds you well", no "I wanted to reach out", no "in today's fast-paced world", no "leverage/seamless/cutting-edge". Warm, confident, plain.
- Deliverability: no spam-trigger words (free money, guarantee, act now, limited time, click here, 100%, cash, urgent, risk-free); no ALL-CAPS words; at most one "!"; https links only.
- Sign off with "Best," on one line, then "{sender_name}" on the next. Never leave a placeholder like "[Your Name]".
Subject line: specific and curiosity-driving, UNDER 6 words, no clickbait, and with no "Subject:" prefix."""


RUBRIC = """Score 1-10, where 10 is a top-1% cold email a sharp founder would actually reply to.
Most competent first drafts are a 6 or 7 — reserve 8+ for genuinely specific, human copy.
Reward copy where sentence 1 cites a concrete, verifiable fact about THIS prospect (a real
service they list, a named detail, genuine review volume) rather than a generic guess.
Deduct hard for any of these:
- pitches a service the agency doesn't offer, or a different service than the strategy brief's SERVICE line names (the email must sell exactly that one service)
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
{{"score": <integer 1-10>, "issues": ["<short phrase>", ...], "fix_hint": "<the SINGLE change that raises the score the most — name the biggest rubric deduction and exactly how to fix it>"}}"""
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


def revise(cfg, lead, subject, body, critique, magnet_url, sender_name, brief=""):
    """Rewrite a draft to address the editor's critique. Returns (subject, body).

    `brief` is the SAME strategy brief the judge scores against. The reviser MUST see it,
    or it can't obey copy_instructions' "sell exactly the brief's SERVICE / use its exact
    OUTCOME figure" rules — and the judge then marks it down for a brief it never read."""
    fix_hint = (critique or {}).get("fix_hint") or "Make it more specific and human."
    issues = ", ".join((critique or {}).get("issues") or []) or "generic / not specific enough"
    facts = lead.get("company_facts") or lead.get("company_description") or ""
    prompt = f"""You are a world-class B2B cold-email copywriter for {cfg.get('client_name','the agency')}.
A strict editor rejected the draft below. Rewrite it so it fully earns a reply. Keep what already works.

PROSPECT: {lead.get('first_name','')} {lead.get('last_name','')} at {lead.get('company_name','')}
What we actually know about them (ground it in THESE facts; keep the specific one the draft already cites, don't invent new ones): {facts}

STRATEGY BRIEF you must follow (sell EXACTLY the SERVICE it names — never a different or extra one; state its OUTCOME with the EXACT figure, never a bigger number):
{brief or '(none)'}

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

    # 3) Rewrite while below the bar. Each pass rewrites from the BEST draft so far
    #    (the champion) using that draft's own critique — never from a revision that
    #    scored worse, so a bad pass can't compound. Repeated passes still vary because
    #    the free chain is non-deterministic. We always return the champion.
    best = (s, b, cur, sc)                        # (subject, body, score, critique)
    rev = 0
    while best[2] < min_score and rev < max_rev:
        rev += 1
        try:
            ns, nb = revise(cfg, lead, best[0], best[1], best[3],
                            magnet_url, sender_name, brief)
        except Exception as e:
            print(f"   🧪 [Quality] revision {rev} failed ({e}); keeping best draft.")
            break
        nsc = score(cfg, lead, ns, nb, brief)
        if nsc is None:
            print(f"   🧪 [Quality] revision {rev} could not be scored; keeping champion.")
            continue
        nval = nsc["score"]
        print(f"   🧪 [Quality] revision {rev} scored {nval}/10.")
        if nval >= best[2]:
            best = (ns, nb, nval, nsc)
    print(f"   🧪 [Quality] final {best[2]}/10 (min {min_score}, {rev} revision(s)).")
    return best[0], best[1], prov
