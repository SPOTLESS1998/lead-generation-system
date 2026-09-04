"""Copy A/B: does the PREMIUM (Opus) writer beat the FREE chain — judged on the same bar?

A safe, offline-style experiment (no email is sent, no DB row is written, no config
file is touched). It answers ONE question: for the same prospect and the same strategy
brief, does real Claude Opus write cold-email copy that clears the quality gate's 8/10,
where today's free chain plateaus at ~5-7?

How it stays a FAIR test:
  - Same fictional lead + same brief for both writers.
  - Only the WRITER varies: FREE chain vs Opus (via whatever ANTHROPIC_BASE_URL points
    at — here the free AgentRouter proxy, so this costs $0).
  - The JUDGE is held CONSTANT (the free chain) for both, so we measure writing, not a
    writer grading its own homework.
  - We print which provider ACTUALLY drafted each email. If Opus is throttled, core.ai
    silently fails over to the free chain — this harness flags that so a "premium" number
    is never secretly a free-chain number.

Run:  CLIENT=ejentic venv/bin/python scripts/copy_ab.py
"""

import time

from core import config, ai, quality
from scripts.lead_agent import generate_copy

MIN_SCORE = 8            # the gate's bar we're trying to clear
MAX_REVISIONS = 1        # keep Opus calls low (AgentRouter throttles under load)
MAGNET_URL = "https://audit.ejentic.xyz/audit/demo-abc123"   # realistic link (text only; nothing is served)

# --- One representative prospect (fictional; NOT a real lead) ---------------
LEAD = {
    "first_name": "Ada",
    "last_name": "Okonkwo",
    "title": "Managing Director",
    "company_name": "Lekki Prime Realty",
    "company_facts": (
        "Boutique real-estate agency in Lekki Phase 1, Lagos. Lists ~40 luxury apartments "
        "and serviced shortlets on their own site; 4.6 stars across 180+ Google reviews; "
        "markets heavily on Instagram (28k followers) and runs open-house viewings on weekends. "
        "Contact is a single WhatsApp line and a web enquiry form."
    ),
}

# --- Strategy brief in the exact 5-line shape lead_agent produces -----------
# SERVICE is copied verbatim from ejentic's offerings; OUTCOME uses its exact figure.
BRIEF = (
    "OBSERVATION: Lekki Prime Realty has built real trust — 180+ Google reviews at 4.6 stars "
    "and a 28k-strong Instagram following for their Lekki Phase 1 luxury listings.\n"
    "PAIN: All that inbound interest funnels into a single WhatsApp line and a web form, so "
    "enquiries likely pile up unqualified and slow-to-answer at the weekend viewing rush.\n"
    'SERVICE: MUST be exactly "AI Lead Generation System".\n'
    "OUTCOME: 10-20 additional qualified leads per month within the first 90 days.\n"
    "AUDIT: show how many enquiries go unanswered >1 hour and what a qualified-lead pipeline "
    "would capture."
)

FREE_CHAIN = ["freellmapi", "gemini", "nvidia"]


def _writer_cfg(base, providers, opus=False):
    cfg = dict(base)
    cfg["providers"] = list(providers)
    if opus:
        cfg["anthropic_model"] = (base.get("copy", {}) or {}).get("anthropic_model") or "claude-opus-4-8"
        cfg["anthropic_attempts"] = 2   # small: don't hammer a throttling proxy
    return cfg


def _run_writer(label, writer_cfg, judge_cfg):
    """Draft with writer_cfg, score with judge_cfg (constant), revise up to MAX_REVISIONS.
    Returns a dict of results; never raises (a failed side still reports)."""
    print(f"\n{'='*70}\n  {label}\n{'='*70}")
    ai.reset_breakers()
    ai.reset_usage()
    t0 = time.time()
    try:
        subject, body, drafted_by = generate_copy(writer_cfg, LEAD, BRIEF, MAGNET_URL)
    except Exception as e:
        print(f"  ❌ draft failed: {e}")
        return {"label": label, "ok": False, "error": str(e)}
    usage = ai.collect_usage()
    elapsed = time.time() - t0

    sc = quality.score(judge_cfg, LEAD, subject, body, BRIEF)
    cur = sc["score"] if sc else None
    print(f"\n  Drafted by : {drafted_by}   ({elapsed:.1f}s, "
          f"{usage.get('total_tokens',0)} tokens)")
    print(f"  Word count : {len(body.split())}")
    print(f"  Judge score: {cur}/10" if cur is not None else "  Judge score: (judge unavailable)")
    if sc:
        print(f"  Issues     : {', '.join(sc['issues']) or '(none)'}")
        print(f"  Fix hint   : {sc['fix_hint']}")
    print(f"\n  --- SUBJECT ---\n  {subject}\n  --- BODY ---")
    for ln in body.splitlines():
        print(f"  {ln}")

    # Up to MAX_REVISIONS rewrites (writer rewrites; constant judge re-scores).
    best = {"subject": subject, "body": body, "score": cur if cur is not None else -1, "critique": sc}
    rev = 0
    sender = (writer_cfg.get("from_name") or writer_cfg.get("client_name") or "our team").strip()
    while best["score"] < MIN_SCORE and rev < MAX_REVISIONS and best["critique"]:
        rev += 1
        try:
            ns, nb = quality.revise(writer_cfg, LEAD, best["subject"], best["body"],
                                    best["critique"], MAGNET_URL, sender, BRIEF)
        except Exception as e:
            print(f"\n  🧪 revision {rev} failed ({e}); keeping best.")
            break
        nsc = quality.score(judge_cfg, LEAD, ns, nb, BRIEF)
        nval = nsc["score"] if nsc else -1
        print(f"\n  🧪 revision {rev} scored {nval}/10 (was {best['score']}/10)")
        if nval >= best["score"]:
            best = {"subject": ns, "body": nb, "score": nval, "critique": nsc}

    return {"label": label, "ok": True, "drafted_by": drafted_by,
            "draft_score": cur, "final_score": best["score"],
            "wants_opus": "anthropic" in writer_cfg.get("providers", [])}


def main():
    base = config.load_client("ejentic")
    # Make sure the gate settings we assume are what the client actually inherits.
    gate = (base.get("copy", {}) or {}).get("quality_gate", {}) or {}
    print(f"Client: {base.get('client_name')}   gate: min_score="
          f"{gate.get('min_score', '?')} max_revisions={gate.get('max_revisions','?')} "
          f"best_of={gate.get('best_of','?')}")
    print("Judge (constant): FREE chain.  Writers compared: FREE vs OPUS.")

    judge_cfg = _writer_cfg(base, FREE_CHAIN)
    free = _run_writer("WRITER = FREE chain", _writer_cfg(base, FREE_CHAIN), judge_cfg)
    opus = _run_writer("WRITER = OPUS (premium)", _writer_cfg(base, ["anthropic"] + FREE_CHAIN, opus=True), judge_cfg)

    print(f"\n{'#'*70}\n  VERDICT\n{'#'*70}")
    for r in (free, opus):
        if not r.get("ok"):
            print(f"  {r['label']}: FAILED ({r.get('error')})")
            continue
        line = f"  {r['label']}: draft {r['draft_score']}/10 -> final {r['final_score']}/10  (drafted by {r['drafted_by']})"
        if r.get("wants_opus") and r["drafted_by"] != "anthropic":
            line += "   ⚠️ OPUS THROTTLED — this is FREE-CHAIN copy, not Opus!"
        print(line)
    if opus.get("ok") and opus.get("drafted_by") == "anthropic":
        verdict = "CLEARS the 8/10 bar ✅" if opus["final_score"] >= MIN_SCORE else "still below 8/10"
        print(f"\n  Opus writer {verdict}. Free writer final: "
              f"{free.get('final_score') if free.get('ok') else 'n/a'}/10.")
    print("\n  (No email sent, no DB row written, no config changed.)")


if __name__ == "__main__":
    main()
