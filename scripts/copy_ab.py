"""Copy A/B (multi-run): does the PREMIUM (Opus) writer reliably beat the FREE chain?

Upgrade of the single-shot version: LLM copy is stochastic, so ONE draft each can't
rank two writers. This runs N draws per side and reports the SCORE SPREAD, so we can
see whether Opus *reliably* clears the gate's 8/10 or the free chain just got a lucky 9.

Fairness rules (unchanged):
  - Same fictional lead + same brief for both writers.
  - Only the WRITER varies (FREE chain vs Opus via ANTHROPIC_BASE_URL = AgentRouter, $0).
  - The JUDGE is held CONSTANT (free chain) for every score — never a writer grading itself.
  - Each draw prints which provider ACTUALLY wrote it, so a silent Opus->free failover
    (AgentRouter throttled) is never miscounted as an Opus result.
  - LENGTH-GUARD: any draft over 90 words gets ONE "tighten to <=90" pass (same writer),
    applied to BOTH sides — so a writer isn't scored down for a purely mechanical overrun.

Nothing is sent, no DB row is written, no config file is touched.

Run:  CLIENT=ejentic RUNS=4 venv/bin/python -m scripts.copy_ab
"""

import os
import sys
import time
import statistics

from core import config, ai, quality
from scripts.lead_agent import generate_copy

MIN_SCORE = 8
WORD_CEILING = 90
FREE_CHAIN = ["freellmapi", "gemini", "nvidia"]
MAGNET_URL = "https://audit.ejentic.xyz/audit/demo-abc123"   # realistic link (text only; nothing served)

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

def _brief(cfg):
    """The fixed strategy brief both writers get, with SERVICE/OUTCOME taken from the
    bench tenant's own config — never a hardcoded service name (MULTITENANCY.md)."""
    own = config.tenant_offerings(cfg)
    service = own[0] if own else ""
    outcome = ((cfg.get("service_outcomes") or {}).get(service)
               or "a concrete, plausible result within the first 90 days")
    return (
        "OBSERVATION: Lekki Prime Realty has built real trust — 180+ Google reviews at 4.6 stars "
        "and a 28k-strong Instagram following for their Lekki Phase 1 luxury listings.\n"
        "PAIN: All that inbound interest funnels into a single WhatsApp line and a web form, so "
        "enquiries likely pile up unqualified and slow-to-answer at the weekend viewing rush.\n"
        f'SERVICE: MUST be exactly "{service}".\n'
        f"OUTCOME: {outcome}.\n"
        "AUDIT: show how many enquiries go unanswered >1 hour and what a qualified-lead pipeline "
        "would capture."
    )


def _wc(body):
    return len((body or "").split())


def _writer_cfg(base, providers, opus=False):
    cfg = dict(base)
    cfg["providers"] = list(providers)
    if opus:
        cfg["anthropic_model"] = (base.get("copy", {}) or {}).get("anthropic_model") or "claude-opus-4-8"
        cfg["anthropic_attempts"] = 2
    return cfg


def _tighten(cfg, subject, body, sender):
    """One best-effort pass to bring an over-long email to <=90 words, keeping the hook,
    the audit link, and the sign-off. Returns (subject, body); the original on any failure."""
    prompt = f"""Tighten this cold email to {WORD_CEILING} words or FEWER. Keep the opening hook,
the exact audit link, the single CTA question, and the sign-off. Same warm, plain voice — just cut flab.

Subject: {subject}
{body}

Reply with ONLY this JSON, no prose: {{"subject": "<subject>", "body": "<full body>"}}"""
    try:
        obj, _ = ai.generate_json(cfg, prompt)
        ns = (obj.get("subject") or subject).strip()
        nb = quality.finalize_body(obj.get("body") or "", sender, MAGNET_URL)
        return (ns, nb) if nb else (subject, body)
    except Exception:
        return subject, body


def _one_draw(i, writer_cfg, judge_cfg, sender, brief):
    """One draft -> length-guard if needed -> score with the CONSTANT judge."""
    t0 = time.time()
    try:
        subject, body, drafted_by = generate_copy(writer_cfg, LEAD, brief, MAGNET_URL)
    except Exception as e:
        print(f"    draw {i}: draft FAILED ({str(e)[:70]})")
        return None
    tightened = False
    if _wc(body) > WORD_CEILING:
        subject, body = _tighten(writer_cfg, subject, body, sender)
        tightened = True
    sc = quality.score(judge_cfg, LEAD, subject, body, brief)
    score = sc["score"] if sc else None
    flag = "" if score is not None else " (judge unavailable)"
    tg = " tightened" if tightened else ""
    print(f"    draw {i}: {drafted_by:9s} score={score if score is not None else '—'}/10  "
          f"{_wc(body)}w{tg}  {time.time()-t0:.0f}s{flag}")
    return {"drafted_by": drafted_by, "score": score, "words": _wc(body),
            "subject": subject, "body": body, "tightened": tightened}


def _run_side(label, writer_cfg, judge_cfg, n, brief, gentle=False):
    print(f"\n{'='*70}\n  {label}   (N={n})\n{'='*70}")
    ai.reset_breakers()          # once per side: a truly-dead provider then trips and is skipped
    sender = (writer_cfg.get("from_name") or writer_cfg.get("client_name") or "our team").strip()
    draws = []
    for i in range(1, n + 1):
        ai.reset_usage()
        d = _one_draw(i, writer_cfg, judge_cfg, sender, brief)
        if d:
            draws.append(d)
        if gentle and i < n:
            # Let a budget-pooled proxy refill between Opus draws. AgentRouter answers
            # a single draft fine but returns HTTP 402 "Budget pool quota has been
            # exhausted" when a run bursts ~15 premium calls back to back — so the
            # gap has to be long enough for its window to roll, not just polite.
            # Override with PACE_SECONDS when measuring against a different proxy.
            time.sleep(float(os.environ.get("PACE_SECONDS") or 3))
    return draws


def _summ(label, draws, wants):
    scored = [d for d in draws if d["score"] is not None]
    served = [d for d in draws if d["drafted_by"] == wants] if wants else draws
    print(f"\n  {label}")
    if wants:
        print(f"    actually written by '{wants}': {len(served)}/{len(draws)} draws "
              + ("(rest failed over to the free chain)" if len(served) < len(draws) else ""))
    if not scored:
        print("    no scored draws (judge was unavailable).")
        return None
    sc = [d["score"] for d in scored]
    cleared = sum(1 for s in sc if s >= MIN_SCORE)
    print(f"    scores : {sorted(sc, reverse=True)}")
    print(f"    spread : min {min(sc)}  median {statistics.median(sc):.1f}  "
          f"mean {statistics.mean(sc):.1f}  max {max(sc)}")
    print(f"    cleared 8/10: {cleared}/{len(sc)} draws")
    # "Best" should showcase THIS writer: prefer a draft it actually wrote (not a failover).
    pool = [d for d in scored if d["drafted_by"] == wants] if wants else scored
    best = max(pool or scored, key=lambda d: d["score"])
    return {"scores": sc, "cleared": cleared, "n": len(sc), "best": best,
            "median": statistics.median(sc), "served": len(served), "draws": len(draws)}


def main():
    n = int(os.environ.get("RUNS") or (sys.argv[1] if len(sys.argv) > 1 else 4))
    base = config.load_client("ejentic")
    gate = (base.get("copy", {}) or {}).get("quality_gate", {}) or {}
    print(f"Client: {base.get('client_name')}   gate min_score={gate.get('min_score','?')}   "
          f"word ceiling={WORD_CEILING}   runs/side={n}")
    print("Judge held CONSTANT (free chain). Length-guard applied to BOTH sides.")

    brief = _brief(base)
    judge_cfg = _writer_cfg(base, FREE_CHAIN)
    free_draws = _run_side("WRITER = FREE chain", _writer_cfg(base, FREE_CHAIN), judge_cfg, n, brief)
    opus_draws = _run_side("WRITER = OPUS (premium)", _writer_cfg(base, ["anthropic"] + FREE_CHAIN, opus=True),
                           judge_cfg, n, brief, gentle=True)

    print(f"\n{'#'*70}\n  VERDICT ({n} draws/side, same judge)\n{'#'*70}")
    free = _summ("FREE chain:", free_draws, wants=None)
    opus = _summ("OPUS (premium):", opus_draws, wants="anthropic")

    # Show the best email each side produced, so the numbers have copy behind them.
    for tag, s in (("FREE", free), ("OPUS", opus)):
        if s and s["best"]:
            b = s["best"]
            print(f"\n  --- best {tag} draft ({b['score']}/10, {b['words']}w, by {b['drafted_by']}) ---")
            print(f"  Subject: {b['subject']}")
            for ln in b["body"].splitlines():
                print(f"  {ln}")

    if free and opus:
        print(f"\n  Median: FREE {free['median']:.1f}/10  vs  OPUS {opus['median']:.1f}/10.")
        print(f"  Cleared 8+: FREE {free['cleared']}/{free['n']}  vs  OPUS {opus['cleared']}/{opus['n']}.")
        if opus["served"] < opus["draws"]:
            print(f"  ⚠️ Opus only truly served {opus['served']}/{opus['draws']} draws "
                  "— it is still flaky under repeated load.")
    print("\n  (No email sent, no DB row written, no config changed.)")


if __name__ == "__main__":
    main()
