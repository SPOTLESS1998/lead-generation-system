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

# A bracketed stand-in for the audit link — "[Link to Free Gift]", "(insert link here)",
# "[your audit page]". Models write these even when told not to: when no magnet was built
# the drafter is instructed "do NOT invent or include any link", and it produces one anyway.
# A dangling placeholder in a real prospect's inbox is worse than no link at all — it
# advertises that the mail was machine-written — so finalize_body repairs it unconditionally.
# The lookahead leaves a markdown link "[label](https://…)" alone.
_LINK_PLACEHOLDER = re.compile(
    r'[\[\(\{]\s*[^\]\)\}\n]*?'
    r'(?:link|url|gift|audit page|landing page|attachment|download|insert)'
    r'[^\]\)\}\n]*?\s*[\]\)\}](?!\s*\()',
    re.IGNORECASE)

# Closing words that count as a real sign-off; if none is present we add one.
_SIGNOFFS = ("best,", "best regards", "regards,", "cheers,", "thanks,", "thank you,",
             "sincerely,", "warmly,", "talk soon", "speak soon")


# --------------------------------------------------------------------------
# Structural sanity net — runs on every candidate, gate on or off
# --------------------------------------------------------------------------
# The LLM judge below would usually catch a broken draft, but "usually" is not a
# guarantee and the judge can be offline. A reasoning model once leaked its entire
# scratchpad — "We need to produce email subject line and body... Word count? Let's
# count:" — into a draft that reached the approval queue for a real prospect. So a
# cheap, deterministic check gates every candidate before any of that.

# A body this far past copy_instructions' 90-word ceiling isn't a cold email that
# ran long; it's a model thinking out loud. Deliberately generous.
_MAX_BODY_WORDS = 200

# Phrases that belong to a model's scratchpad and never to a cold email.
_REASONING_TELLS = (
    "we need to produce", "let's craft", "let us craft", "word count",
    "let's count", "the instruction:", "the instructions:", "prospect details:",
    "as an ai", "i need to write", "the user wants", "let me write",
)


def reject_reason(subject, body):
    """Why this candidate is unusable as an email, or None if it looks like one.

    Structural only — this judges nothing about quality, just that the model
    returned an email rather than its own notes. Rejecting leaves the lead for a
    later retry, which is always better than queueing garbage for a real prospect.
    """
    text = (body or "").strip()
    if not text:
        return "empty body"
    if not (subject or "").strip():
        return "empty subject"
    words = len(text.split())
    if words > _MAX_BODY_WORDS:
        return (f"body is {words} words (max {_MAX_BODY_WORDS}) — "
                f"the model most likely leaked its reasoning")
    low = text.lower()
    for tell in _REASONING_TELLS:
        if tell in low:
            return f"body contains the reasoning tell {tell!r}"
    # The drafter strips a leading "Subject:" line, so one surviving mid-body means
    # the model wrote its whole answer as prose instead of the JSON we asked for.
    if re.search(r'(?m)^\s*subject\s*:', text, re.IGNORECASE):
        return "body still contains a 'Subject:' line"
    return None


# --------------------------------------------------------------------------
# Citation check — every specific the copy asserts must trace to real evidence
# --------------------------------------------------------------------------
# The grounding gate (scripts/draft_queued) refuses to draft a prospect we know
# NOTHING about. This is the other half of the same rule: given facts, does the draft
# actually stay inside them? Both are needed. After the grounding gate shipped, live
# drafts still cited things our data has never contained — "over N50 billion in
# recoveries", "three Lagos training centres (Ikeja, Ketu, Ajah)" for a firm whose
# facts list one service and a review score, "storing 660,000 tons" for one whose
# facts say 85,731 sqm. Every one of those prospects had RICH facts. The model did not
# invent because it was starved; it invented because it was asked to sound specific and
# reached past the evidence to do it.
#
# So this is deliberately MECHANICAL, not another LLM call. The judge meant to catch
# invention is the same kind of system that produced it, its rubric only ever deducted
# for invented *statistics*, and it can be offline. A regex cannot be argued round, and
# it costs nothing to run on every candidate.
#
# The strategy brief is NOT evidence, however tempting it looks: it is model-written, so
# it is precisely where an invented "fact" first appears — counting it would let the
# fabrication launder itself into a citation. Only two things count as evidence: what
# discovery actually scraped about THIS prospect, and values a HUMAN wrote in the
# client's own config. Note the consequence for a new tenant: a client with no
# `service_outcomes` configured leaves the strategist to invent its own outcome figure,
# and that figure will be flagged here. That is the correct answer — configure the
# promise you intend to make (see MULTITENANCY.md) rather than let a model improvise it.

# Acronyms any business writer uses generically; they assert nothing about the prospect.
_SAFE_ACRONYMS = {
    "AI", "ML", "RAG", "US", "USA", "UK", "EU", "UN", "CEO", "CTO", "CFO", "COO",
    "HR", "IT", "API", "APIS", "FAQ", "FAQS", "CRM", "ERP", "SEO", "SEM", "B2B",
    "B2C", "SAAS", "PPC", "KPI", "KPIS", "ROI", "VAT", "PAYE", "SME", "SMES",
    "NGO", "PDF", "PDFS", "URL", "OK", "Q1", "Q2", "Q3", "Q4", "WFH", "LLC",
    "LTD", "INC", "CO", "ADR", "PPP", "IP", "HQ", "ID",
}

# A number followed by one of these is meeting logistics ("a 15-minute call"), not a
# claim about the prospect — the copy is TOLD to propose a concrete next step.
_TIME_UNIT = re.compile(r'^\s*[-‑–—‐]?\s*(min\b|mins\b|minute|hour|hr\b|hrs\b)',
                        re.IGNORECASE)

# Capitalised words that are grammar or courtesy, never a claim about the prospect.
_STOP_CAPS = {
    "the", "a", "an", "i", "we", "you", "your", "our", "their", "they", "he",
    "she", "it", "this", "that", "these", "those", "hi", "hello", "hey", "dear",
    "best", "regards", "thanks", "thank", "sincerely", "cheers", "warmly", "and",
    "but", "or", "so", "if", "as", "at", "by", "for", "from", "in", "of", "on",
    "to", "with", "is", "are", "was", "here", "there", "let", "would", "could",
    "should", "can", "will", "may", "might", "since", "when", "while", "after",
    "before", "most", "many", "few", "some", "any", "google",
}

# Bare integers below this are inference or shape-of-sentence ("3 offices", "one team"),
# not the kind of hard figure a prospect could catch us inventing. Anything with a
# comma, a decimal point, a currency symbol or a scale suffix is checked regardless.
_CITE_MIN_INT = 10

# Words the copy is always free to use: the next-step vocabulary the framework asks for.
_CITE_UNIVERSAL = "audit call week month monday tuesday wednesday thursday friday minute review process team"


def _denorm(s):
    """Fold the typographic characters models like into their ASCII equivalents, so
    '10‑15' (non-breaking hyphen) matches '10-15' in the facts. Without this the check
    reports invention every time the model reaches for a prettier dash."""
    s = s or ""
    for dash in "‑–—‐":
        s = s.replace(dash, "-")
    return s.replace(" ", " ").replace("’", "'")


def _cite_words(s):
    return set(re.findall(r'[a-z0-9]+', _denorm(s).lower()))


def _cite_numbers(s):
    return {m.replace(",", "") for m in re.findall(r'\d[\d,]*\.\d+|\d[\d,]*', _denorm(s))}


def _strip_urls(s):
    """Links are ours and carry a random token; never read them as claims."""
    return re.sub(r'https?://\S+', ' ', _denorm(s))


def _anchored(word, evidence_words):
    """True if `word` — or a light morphological variant of it — is in the evidence.

    'Africa' in the facts entitles the copy to write 'African'; a plural, or a
    close prefix ('logistic'/'logistics'), is the same fact in another grammatical
    coat. This is intentionally loose: the check exists to catch invented *content*,
    not to police word endings.
    """
    if word in evidence_words:
        return True
    for suffix in ("n", "s", "an", "ian", "ese", "ic", "al"):
        if word.endswith(suffix) and word[: -len(suffix)] in evidence_words:
            return True
    for known in evidence_words:
        if len(known) > 4 and abs(len(known) - len(word)) <= 3 and (
                word.startswith(known) or known.startswith(word)):
            return True
    return False


def evidence_corpus(cfg, lead):
    """Everything this draft is entitled to cite, as one blob of text.

    Two sources, both trustworthy: what discovery actually scraped about THIS
    prospect, and values a human wrote in the client's own config (offering names,
    the standard outcome promises, the sender and brand names). Never the brief.
    """
    cfg = cfg or {}
    lead = lead or {}
    outcomes = cfg.get("service_outcomes") or {}
    offerings = cfg.get("offerings") or []
    parts = [
        lead.get("company_facts"), lead.get("company_description"),
        lead.get("company_name"), lead.get("first_name"), lead.get("last_name"),
        lead.get("title"), lead.get("email"), lead.get("website_url"),
        lead.get("ejentic_service"),
        cfg.get("client_name"), cfg.get("from_name"), cfg.get("unsubscribe_base_url"),
        # Every configured outcome, not just this lead's: the fitted service can be
        # re-chosen after the lead dict was built (lead_agent.select_offering), and
        # pitching the wrong one is the RUBRIC's job to punish, not a fabrication.
        " ".join(str(v) for v in outcomes.values()),
        " ".join(o if isinstance(o, str) else (o or {}).get("name") or "" for o in offerings),
        _CITE_UNIVERSAL,
    ]
    return " ".join(str(p) for p in parts if p)


def citation_issues(cfg, lead, subject, body):
    """Specifics the copy asserts that our evidence does not support.

    Returns a list of short human-readable strings; empty means everything it claims
    is traceable. Four shapes of claim are checked, each both mechanically detectable
    and the kind of thing a prospect would immediately know to be wrong:

        figures          "660,000", "4.9", "25%"     — a number we never recorded
        money/magnitude  "N50bn", "$2 million"       — a currency or scale claim
        acronyms/codes   "ISO 9001", "MTN"           — a named standard or brand
        proper nouns     "Ikeja Ketu Ajah"           — places, people, companies

    Validated against 42 real drafts before it was allowed to gate anything: it
    flagged 4 (all genuine fabrications) and cleared 38, including every draft whose
    star rating, review count and office list were real. Deliberately NOT checked:
    bare integers under 10 (inference, not claim), meeting logistics ("a 15-minute
    call"), and single capitalised words — a lone capital is usually a sentence start,
    and flagging those buried the real catches in noise. A multi-word phrase is
    reported only when NOTHING in it is anchored in the evidence: one real anchor
    means the model is elaborating on a fact, which is what good copy does.
    """
    corpus = evidence_corpus(cfg, lead)
    ev_words = _cite_words(corpus)
    ev_numbers = _cite_numbers(corpus)
    ev_text = _denorm(corpus).lower()
    subject_text = _strip_urls(subject or "")
    body_text = _strip_urls(body or "")
    both = f"{subject_text}\n{body_text}"
    issues = []

    # (1) Figures, money and magnitudes — subject line included, since a fabricated
    #     number in the subject is the first thing the prospect reads.
    for m in re.finditer(
            r'([$₦£€]\s?)?(\d[\d,]*\.\d+|\d[\d,]*)\s?(%|bn|billion|m\b|million|k\b|thousand)?', both):
        symbol, raw, scale = m.group(1), m.group(2), (m.group(3) or "")
        number = raw.replace(",", "")
        if _TIME_UNIT.match(both[m.end():m.end() + 10]):
            continue
        try:
            value = float(number)
        except ValueError:
            continue
        if symbol or scale:
            # A magnitude claim: the figure AND its scale have to appear together in
            # the evidence, so facts reading "50 reviews" can't license "N50 billion".
            if any(f in ev_text for f in (_denorm(raw + scale).lower(),
                                          _denorm(raw + " " + scale).lower().strip())):
                continue
            issues.append(f"the figure {m.group(0).strip()!r} is not in the facts")
            continue
        if number in ev_numbers:
            continue
        # Decimals are ratings and percentages — always worth checking, since a draft
        # that rounds a 4.3 star average up to 4.9 is wrong in a way the reader can see.
        if "," in raw or "." in number or value >= _CITE_MIN_INT:
            issues.append(f"the figure {m.group(0).strip()!r} is not in the facts")

    # (2) Acronyms and codes — body only, like the proper-noun check below. An all-caps
    #     token in a SUBJECT is nearly always emphasis or house style ("NEW", "Q3", a
    #     stylised product word), and the framework already bans ALL-CAPS words there and
    #     the rubric deducts for them; reading those as claims about the prospect flagged
    #     ordinary subject lines. In the body, "ISO 9001" or a bare brand name reads as a
    #     named standard or company, which is exactly the claim worth checking.
    for token in re.findall(r'\b[A-Z][A-Z0-9]{1,}\b', body_text):
        base = re.sub(r'[^A-Z0-9]', '', token)
        if not base or base in _SAFE_ACRONYMS or base.lower() in ev_words:
            continue
        issues.append(f"{token!r} is not in the facts")

    # (3) Multi-word proper nouns — body only. Subject lines are stylistically
    #     Title Cased ("Cut Lookup Time"), which says nothing about the prospect.
    for segment in re.split(r'[.!?\n]+', body_text):
        tokens = re.findall(r"[A-Za-z][A-Za-z'&.-]*", segment)
        run, at_start = [], True
        for token in tokens:
            title_case = bool(re.match(r'[A-Z][a-z]', token)) or (token[:1].isupper() and "-" in token)
            if title_case and not (at_start and token.lower() in _STOP_CAPS):
                # Drop a sentence's first word: capitalised by grammar, not by meaning.
                if at_start and not run:
                    at_start = False
                    continue
                run.append(token)
            else:
                _flush_proper_run(run, ev_words, issues)
                run = []
            at_start = False
        _flush_proper_run(run, ev_words, issues)

    # Same claim can surface twice (subject and body, or "660k" and "660,000");
    # report each once so a fix hint names distinct problems.
    seen, unique = set(), []
    for issue in issues:
        key = re.sub(r'[^a-z0-9]', '', issue.lower())[:40]
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def _flush_proper_run(run, evidence_words, issues):
    """Judge one run of consecutive capitalised words; append an issue if it is
    entirely unsupported. Split on punctuation the same way the evidence is indexed,
    so "Ikeja-Arepo" matches facts that name Ikeja and Arepo separately."""
    if len(run) < 2:
        return
    significant = []
    for word in run:
        for piece in re.findall(r'[a-z0-9]+', word.lower()):
            if piece not in _STOP_CAPS and len(piece) > 2:
                significant.append(piece)
    if not significant:
        return
    if all(not _anchored(piece, evidence_words) for piece in significant):
        issues.append(f"{' '.join(run)!r} is not in the facts")


# Facts that amount to nothing but a star rating. The review signal has the same shape
# for every business on Google, so it is not differentiating evidence: a prospect whose
# facts are only that is one we can say almost nothing specific about, and the copy
# should be ALLOWED to say little rather than pressured into inventing more.
_REVIEW_CLAUSE = re.compile(r'well[- ]reviewed:.*?(?=(?:services:|notable:)|$)', re.IGNORECASE | re.DOTALL)
_THIN_EVIDENCE_WORDS = 12


def evidence_is_thin(lead):
    """True when we hold almost nothing citable about this prospect.

    Drives the one honest answer to a thin file: tell the writer AND the judge that a
    plain opener is correct here. Without it the framework demands an opener that
    "could NOT be copy-pasted to any other company" and the judge marks honest copy
    down as generic — which is the pressure that produced the invented detail.
    """
    facts = ((lead or {}).get("company_facts") or (lead or {}).get("company_description") or "")
    return len(_REVIEW_CLAUSE.sub(" ", facts).split()) < _THIN_EVIDENCE_WORDS


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
    # Link-placeholder safety net. With a real magnet, the placeholder BECOMES the
    # link (the model already put it in the right sentence). Without one, the promise
    # is deleted rather than left dangling — then any line left blank by the deletion
    # is collapsed so the mail doesn't ship with a hole in it.
    if magnet_url:
        body = _LINK_PLACEHOLDER.sub(magnet_url, body)
    else:
        body = _LINK_PLACEHOLDER.sub("", body)
    body = re.sub(r'[ \t]+(\n|$)', r'\1', body)      # trailing spaces the cut left behind
    body = re.sub(r'\n{3,}', '\n\n', body).strip()   # and any blank line it opened up
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


def copy_instructions(sender_name, magnet_url, thin_evidence=False):
    """The one framework the drafter AND the reviser must follow (kept identical so a
    rewrite is held to the same bar as a first draft). `magnet_url` toggles the link
    rule; `thin_evidence` scales what we ask for to what we can actually prove.

    That second switch exists because of a real failure. The framework used to demand,
    unconditionally, an opener that "could NOT be copy-pasted to any other company" and
    told the writer to "earn the 8" — while the judge scored honest copy 5 or 6 for
    being generic. On a prospect whose file is one line, no honest sentence can satisfy
    that, so the rewrite loop pushed for specificity the facts could not supply and
    invention was the only lever left. Asking for less when we know less is not lowering
    the bar; it is removing the incentive to lie.
    """
    if magnet_url:
        link_rule = (f"Put this exact audit link on its OWN line, written in full and "
                     f"unchanged — never a placeholder: {magnet_url}")
    else:
        link_rule = ("Offer a free personalized AI audit and invite a one-word reply to get it. "
                     "Do NOT invent or include any link.")
    if thin_evidence:
        specificity_rules = (
            "- We hold very little on this prospect, and that is fine. Use the little there is "
            "plainly and exactly, and then STOP. Do NOT reach for extra colour to make the opener "
            "feel researched — no invented offices, cities, clients, awards, volumes, or figures. "
            "A short, plain, TRUE opener is the correct answer here and is scored as such; a "
            "specific-sounding detail we did not give you is the one unforgivable error.\n"
            "- You may add your own read of the fact (what it implies), as long as it is clearly "
            "your inference and not presented as something you know about them."
        )
    else:
        specificity_rules = (
            "- Make that opener so specific to THIS prospect that it could NOT be copy-pasted to "
            "any other company — name the real service, product, market, or detail FROM THE FACTS "
            "YOU WERE GIVEN. Specific means 'drawn from the evidence', never 'sounds impressive': "
            "if the detail is not in the facts, it does not go in the email, no matter how much "
            "better it would read.\n"
            "- Do NOT just recite a fact they already know (their own review count, founding year, "
            "office list) back at them — add YOUR read of it: the implication, the tension, or what "
            "it says about them. Restating the brief's observation verbatim reads as machine-"
            "assembled; naming the fact AND what it reveals reads human."
        )
    return f"""Follow this framework exactly:
- Greet them by first name if one is given; if the name is unknown or a generic mailbox, open with exactly "Hi there," — NEVER write a literal placeholder like "Name" or "[First Name]".
- Sentence 1 — a specific, TRUE, FLATTERING observation about THEIR business, drawn only from the facts you were given (never a generic compliment, never an invented fact). Lead with their strongest, most differentiating fact; NEVER open by highlighting a negative or operational detail (a complaints line, a support/error number, a disclaimer, a problem) — that insults the reader.
{specificity_rules}
- EVERY concrete thing you state about them — a number, an amount, a place, an office, a client, a certification, a named brand or standard — must appear in the facts you were given. This is checked mechanically after you write, and a draft that cites anything we did not give you is thrown away rather than sent. If you want to say something and the facts don't support it, leave it out.
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

The single most serious fault is a FABRICATION: any concrete detail about the prospect that
does not appear in the facts above. Not just invented statistics — an office, a city, a
client, a partner, a certification, an award, a named brand or standard, a volume, a sum of
money, a headcount, a number of branches or years. If the facts do not contain it, it is
invented, however plausible or flattering it sounds, and however well the sentence reads.
Check every concrete noun and every figure in the email against the facts one by one. Any
email containing even one such detail scores 3 or below and you must name the invented
detail in "issues" — a fabricated specific is far worse than a plain, honest sentence,
because we cannot verify it and the prospect can.

Deduct hard for any of these:
- pitches a service the agency doesn't offer, or a different service than the strategy brief's SERVICE line names (the email must sell exactly that one service)
- a generic sentence that could be sent to any company (not specific to THIS prospect) — but ONLY when the facts held something more specific that it failed to use
- hype, vague value, or an unbelievable claim
- AI/robotic tells or buzzwords ("I hope this finds you well", "leverage", "seamless", "cutting-edge", "in today's...")
- wrong length (must be 60-90 words), a missing https audit link, or a weak/vague CTA ("let me know")
- any "[placeholder]", a broken sign-off, or a subject over 5 words / clickbait / vague"""

# Told to the judge when the prospect's file is nearly empty. Without it the judge marks an
# honest plain opener down as "generic", the rewrite loop chases a score the evidence cannot
# support, and the writer invents detail to reach it — the exact loop that shipped invented
# offices and figures. The bar does not drop: a fabrication still scores 3 or below.
_THIN_EVIDENCE_NOTE = """
EVIDENCE BUDGET — IMPORTANT: we hold very little about this prospect (that is the whole of
what is above). Judge this email against the best email that could HONESTLY be written from
those facts alone, not against an ideal email. Do NOT deduct for "not specific enough",
"generic", or "could be sent to anyone" when the facts simply contain nothing more specific
to cite — in that situation a short, plain, accurate opener is the correct answer and should
score 7 or 8. Conversely, any specific-sounding detail NOT in the facts above is invention,
not research: score it 3 or below and name it."""


def score(cfg, lead, subject, body, brief=""):
    """Judge one draft. Returns {"score": int 1-10, "issues": [...], "fix_hint": str},
    or None if the judge is unavailable (caller then ships the draft unscored)."""
    wc = len((body or "").split())
    facts = lead.get("company_facts") or lead.get("company_description") or ""
    evidence_note = _THIN_EVIDENCE_NOTE if evidence_is_thin(lead) else ""
    prompt = f"""You are a ruthless cold-email editor working for {cfg.get('client_name','the agency')}.
Judge this cold email. Be specific and hard to impress.

PROSPECT (the email must be specifically about THIS business, not generic):
Name: {lead.get('first_name','')} {lead.get('last_name','')}
Company: {lead.get('company_name','')}
What we actually know about them (the email must be grounded in THESE facts, and must not invent others): {facts}
{evidence_note}
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


def revise(cfg, lead, subject, body, critique, magnet_url, sender_name, brief="",
           citation_problems=None):
    """Rewrite a draft to address the editor's critique. Returns (subject, body).

    `brief` is the SAME strategy brief the judge scores against. The reviser MUST see it,
    or it can't obey copy_instructions' "sell exactly the brief's SERVICE / use its exact
    OUTCOME figure" rules — and the judge then marks it down for a brief it never read.

    `citation_problems` (from citation_issues) takes priority over the editor's critique
    when present: a draft that invented a detail must lose the detail before it is made
    prettier, and saying so explicitly salvages a draft that is otherwise good rather
    than throwing the whole candidate away.
    """
    thin = evidence_is_thin(lead)
    # A rewrite instruction of "make it more specific" against an empty file is an
    # instruction to invent — it is the request that produced the fabrications. When we
    # have nothing more to be specific WITH, ask for the achievable thing instead.
    default_hint = ("Tighten the wording and sharpen the closing question — do NOT add "
                    "any detail about them that isn't in the facts."
                    if thin else "Make it more specific and human.")
    fix_hint = (critique or {}).get("fix_hint") or default_hint
    issues = ", ".join((critique or {}).get("issues") or []) or "generic / not specific enough"
    facts = lead.get("company_facts") or lead.get("company_description") or ""
    citation_block = ""
    if citation_problems:
        problems = "; ".join(citation_problems)
        citation_block = (
            f"\nSTOP — THIS DRAFT INVENTED FACTS. Our records do not support: {problems}.\n"
            f"Your FIRST and most important job is to remove or replace every one of those "
            f"claims. Do not substitute a different invented detail, and do not try to rescue "
            f"the sentence by making it vaguer but still implying the claim — cut it and build "
            f"the opener from something that IS in the facts above, or write a plainer opener. "
            f"This is checked automatically after you reply, and a draft that still contains an "
            f"unsupported detail is discarded.\n")
        fix_hint = f"Remove the unsupported claims ({problems}) first, then: {fix_hint}"
    prompt = f"""You are a world-class B2B cold-email copywriter for {cfg.get('client_name','the agency')}.
A strict editor rejected the draft below. Rewrite it so it fully earns a reply. Keep what already works.

PROSPECT: {lead.get('first_name','')} {lead.get('last_name','')} at {lead.get('company_name','')}
What we actually know about them (ground it in THESE facts; keep the specific one the draft already cites, don't invent new ones): {facts}
{citation_block}
STRATEGY BRIEF you must follow (sell EXACTLY the SERVICE it names — never a different or extra one; state its OUTCOME with the EXACT figure, never a bigger number):
{brief or '(none)'}

EDITOR'S ISSUES: {issues}
EDITOR'S #1 FIX: {fix_hint}

CURRENT DRAFT:
Subject: {subject}
{body}

{copy_instructions(sender_name, magnet_url, thin_evidence=thin)}

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

    RAISES rather than returning weak or invented copy. Two hard refusals:

      * a draft citing anything our evidence doesn't support (citation_issues), and
      * a draft scoring below `floor_score`.

    Both are new, and both replace the same old behaviour: this function used to return
    `best` unconditionally, so the min_score it printed was an aspiration, not a gate —
    every draft scoring 5 or 6 against a min_score of 8 shipped anyway, with the log
    line "final 6/10 (min 8)" as the only trace. Raising is safe and already handled:
    both callers (lead_agent.main, scripts/draft_queued.main) catch it and leave the
    lead in `sourced` for a later retry, which is the correct outcome for "we could not
    write this one honestly yet".
    """
    gate = (cfg.get("copy", {}) or {}).get("quality_gate", {}) or {}
    enabled = bool(gate.get("enabled"))
    min_score = int(gate.get("min_score", 8))
    # The floor is what we will actually ship, as distinct from what we aim for. Kept
    # separate on purpose: aiming at 8 still drives the rewrite loop, while the floor
    # decides what reaches a real person. Defaults BELOW min_score — a floor set to the
    # aspiration would reject most honest drafts and produce no volume at all.
    floor = int(gate.get("floor_score", 6))
    max_rev = int(gate.get("max_revisions", 1))
    best_of = max(1, int(gate.get("best_of", 1)))
    sender_name = (cfg.get("from_name") or cfg.get("client_name") or "our team").strip()

    def _cite(subject, body):
        return citation_issues(cfg, lead, subject, body)

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

    # Structural sanity net — deliberately BEFORE the `enabled` check, so a model
    # that returned its scratchpad instead of an email is discarded even when the
    # quality gate is switched off. If nothing survives we raise: the caller then
    # leaves the lead for a retry, which beats queueing garbage for a prospect.
    kept, why_last = [], None
    for (s, b, prov) in candidates:
        why = reject_reason(s, b)
        if why:
            why_last = why
            print(f"   🚫 [Quality] discarded an unusable candidate: {why}.")
        else:
            kept.append((s, b, prov))
    if not kept:
        raise RuntimeError(f"every draft candidate was unusable ({why_last})")
    candidates = kept

    # Citation gate — also before the `enabled` check, and for the same reason: a draft
    # that invents a detail about a real business is not a quality-of-life problem, it is
    # the one error we cannot detect after the fact and cannot take back once sent.
    # With the gate off there is no rewrite loop to repair it, so an uncitable draft is
    # simply refused; with the gate on, the loop below gets a chance to strip the claim.
    clean = []
    dirty_problems = None
    for (s, b, prov) in candidates:
        problems = _cite(s, b)
        if problems:
            dirty_problems = problems
            print(f"   🚫 [Citation] candidate cites what our facts don't support: "
                  f"{'; '.join(problems[:3])}.")
        else:
            clean.append((s, b, prov))
    if clean:
        candidates = clean
    elif not enabled:
        raise RuntimeError(
            f"every draft candidate cited unverifiable specifics "
            f"({'; '.join(dirty_problems or [])})")

    if not enabled:
        return candidates[0]

    # 2) Score each candidate; keep the best. If the judge is down, ship as-is.
    scored = [(s, b, prov, score(cfg, lead, s, b, brief), _cite(s, b))
              for (s, b, prov) in candidates]
    if all(sc is None for (_, _, _, sc, _) in scored):
        # Judge unavailable: still never ship an uncitable draft. The structural and
        # citation nets don't need an LLM, which is exactly why they carry this case.
        for (s, b, prov, _, problems) in scored:
            if not problems:
                print("   🧪 [Quality] judge unavailable — shipping a citation-clean draft unscored.")
                return s, b, prov
        raise RuntimeError("judge unavailable and every candidate cited unverifiable specifics")
    # Rank citation-clean FIRST, score second: a beautifully written draft that invented
    # a figure must never beat an honest one, whatever the judge made of the prose.
    s, b, prov, sc, problems = max(
        scored, key=lambda x: (not x[4], x[3]["score"] if x[3] else -1))
    cur = sc["score"] if sc else min_score
    print(f"   🧪 [Quality] draft scored {cur}/10" + (f" (best of {n})" if n > 1 else "") + ".")

    # 3) Rewrite while below the bar OR still citing something unsupported. Each pass
    #    rewrites from the BEST draft so far (the champion) using that draft's own
    #    critique — never from a revision that scored worse, so a bad pass can't
    #    compound. Repeated passes still vary because the free chain is non-deterministic.
    best = (s, b, cur, sc, problems)       # (subject, body, score, critique, citation problems)
    rev = 0
    while (best[2] < min_score or best[4]) and rev < max_rev:
        rev += 1
        try:
            ns, nb = revise(cfg, lead, best[0], best[1], best[3],
                            magnet_url, sender_name, brief, citation_problems=best[4])
        except Exception as e:
            print(f"   🧪 [Quality] revision {rev} failed ({e}); keeping best draft.")
            break
        nproblems = _cite(ns, nb)
        nsc = score(cfg, lead, ns, nb, brief)
        if nsc is None:
            print(f"   🧪 [Quality] revision {rev} could not be scored; keeping champion.")
            continue
        nval = nsc["score"]
        note = (f" but still cites {'; '.join(nproblems[:2])}" if nproblems else "")
        print(f"   🧪 [Quality] revision {rev} scored {nval}/10{note}.")
        # Accept on the same two-key comparison used to pick the champion, so a revision
        # that fixed an invented claim wins even if the judge liked the prose slightly
        # less — and one that introduces a new invented claim can never win.
        if (not nproblems, nval) >= (not best[4], best[2]):
            best = (ns, nb, nval, nsc, nproblems)

    # 4) Refuse rather than ship. Both of these used to be advisory.
    if best[4]:
        raise RuntimeError(
            f"draft cites specifics our facts don't support after {rev} revision(s): "
            f"{'; '.join(best[4])}")
    if best[2] < floor:
        raise RuntimeError(
            f"draft scored {best[2]}/10, below the hard floor of {floor} "
            f"(aiming for {min_score}, {rev} revision(s))")
    print(f"   🧪 [Quality] final {best[2]}/10 (min {min_score}, floor {floor}, "
          f"{rev} revision(s)) — citations check out.")
    return best[0], best[1], prov
