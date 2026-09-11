"""Offline tests for scripts/draft_queued.py — the re-draft entry point.

THE REGRESSION THIS EXISTS FOR: draft_queued used to spell out its own drafting
steps instead of calling the shared recipe, and drifted. It never built a magnet
page and never ran the quality gate, so every draft it produced shipped a dead
"[Link to Free Gift]" and was never scored — one queued the model's raw internal
monologue for a real prospect. These tests assert it routes through
lead_agent.draft_one_lead, so the two entry points can never silently diverge again.

The inner seams (offering fit, strategist, magnet, quality gate) are monkeypatched
but draft_one_lead itself runs for real — that wiring is exactly what broke.
No network, no LLM, no API credits, and the real pending queue is never touched.

Run:  venv/bin/python tests/test_draft_queued.py
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

for _k in ("GEMINI_API_KEY", "NVIDIA_API_KEY", "SMTP_USER", "SMTP_PASS", "UNSUB_SECRET"):
    os.environ.setdefault(_k, "test")
os.environ.setdefault("CLIENT", "ejentic")

from core import state, review, suppression   # noqa: E402
import lead_agent                              # noqa: E402
import draft_queued                            # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


_LEAD = {
    "email": "info@acme.ng", "first_name": "", "last_name": "", "title": "",
    "company_name": "Acme Solar", "website_url": "https://acme.ng",
    "company_description": "Solar installer",
    "company_facts": "Services: solar install. Notable: serves Maitama landlords.",
}
_BODY = ("Hi there,\n\nAcme Solar's Maitama install work stands out. My guess is "
         "qualifying enquiries is still manual.\n\nWorth a look?\n\nBest,\nEjentic AI")


def _fresh_env(tmpdir):
    """A temp DB holding one banked-but-undrafted lead, plus a cfg pointed at it."""
    db = os.path.join(tmpdir, "state.sqlite")
    conn = state.connect(db)
    # 'sourced' is the state draft_queued exists to serve: banked by discovery, never
    # drafted (either it was past the draft cap, or an earlier draft failed and
    # released its claim).
    state.upsert_lead(conn, "acme", dict(_LEAD), niche="AI Lead Generation System",
                      status=state.SOURCED)
    conn.commit()
    cfg = {
        "client": "acme", "client_name": "Ejentic AI", "from_name": "Ejentic AI",
        "paths": {"db": db},
        "unsubscribe_base_url": "https://audit.ejentic.xyz",
        "copy": {"quality_gate": {"enabled": True, "min_score": 8}},
        "observability": {"enabled": False},
    }
    return conn, cfg


def _patch_seams(saved, magnet_url="https://audit.ejentic.xyz/magnet/acme/tok123"):
    """Patch every LLM/network/persistence seam; draft_one_lead itself stays real."""
    calls = {"select_offering": 0, "strategy": 0, "magnet": 0, "gate": 0, "notify": 0}

    def _sel(cfg, lead):
        calls["select_offering"] += 1
        return lead.get("ejentic_service") or ""

    def _strategy(cfg, lead):
        calls["strategy"] += 1
        return ("SERVICE: AI Lead Generation System. OUTCOME: 20 leads/mo.", "fake")

    def _build(cfg, lead, brief):
        calls["magnet"] += 1
        return {"headline": "Audit", "steps": [{"title": "t", "detail": "d"}]}

    def _gate(cfg, lead, brief, url, fn):
        calls["gate"] += 1
        calls["gate_url"] = url          # what the drafter was handed
        return ("A quick idea for Acme", _BODY, "fake-provider")

    lead_agent.select_offering = _sel
    lead_agent.generate_strategy = _strategy
    lead_agent.budget.copy_cfg = lambda conn, cfg: cfg
    lead_agent.magnet.build_content = _build
    lead_agent.magnet.url = lambda cfg, client, token: magnet_url
    lead_agent.quality.draft_and_polish = _gate

    # Never touch the real pending_leads.json, and never send an operator email.
    review.load_pending = lambda: {}
    review.save_pending = lambda entry: (saved.append(entry), "id123")[1]

    def _notify(cfg, lead_id, entry):
        calls["notify"] += 1

    review.notify_operator = _notify
    draft_queued.review = review
    return calls


# --------------------------------------------------------------------------
# The wiring: a re-draft goes through the ONE shared recipe
# --------------------------------------------------------------------------
print("\n[draft_queued: routes through the shared draft_one_lead recipe]")
check("draft_queued imports the shared recipe, not its own copy",
      draft_queued.draft_one_lead is lead_agent.draft_one_lead)
check("draft_queued uses the shared pending-entry builder",
      draft_queued.pending_entry is lead_agent.pending_entry)

with tempfile.TemporaryDirectory() as td:
    conn, cfg = _fresh_env(td)
    conn.close()
    saved = []
    calls = _patch_seams(saved)
    draft_queued.config.load_client = lambda: cfg

    buf = io.StringIO()
    with redirect_stdout(buf):
        draft_queued.main()
    out = buf.getvalue()

    check("it drafted exactly one lead", len(saved) == 1)
    entry = saved[0] if saved else {}

    # The four steps that used to be skipped entirely.
    check("the offering is re-fitted before drafting", calls["select_offering"] == 1)
    check("a strategy brief is generated", calls["strategy"] == 1)
    check("a personalized magnet page IS built", calls["magnet"] == 1)
    check("the quality gate DOES run", calls["gate"] == 1)
    check("the drafter is handed the real magnet link (not None)",
          calls.get("gate_url") == "https://audit.ejentic.xyz/magnet/acme/tok123")

    # The bug as the operator saw it: the dashboard had no link to show.
    check("the queued entry carries magnet_url",
          entry.get("magnet_url") == "https://audit.ejentic.xyz/magnet/acme/tok123")
    check("the queued entry carries magnet_token", bool(entry.get("magnet_token")))
    check("the queued entry has no dead link placeholder",
          "[Link to Free Gift]" not in entry.get("drafted_body", ""))
    check("the operator is notified", calls["notify"] == 1)

    # The magnet must actually be persisted, or the link 404s when clicked.
    conn2 = state.connect(cfg["paths"]["db"])
    stored = state.get_magnet(conn2, "acme", entry.get("magnet_token") or "x")
    check("the magnet page is persisted so the link resolves", bool(stored))
    conn2.close()


# --------------------------------------------------------------------------
# Entry shape parity — the drift that hid the bug
# --------------------------------------------------------------------------
print("\n[pending_entry: both entry points produce the identical shape]")
_cfg = {"client": "acme"}
_e = lead_agent.pending_entry(_cfg, _LEAD, "Subj", _BODY, "https://x/magnet/a/b", "b")
for _k in ("kind", "client", "company_name", "target_email", "first_name", "last_name",
           "title", "drafted_subject", "drafted_body", "magnet_url", "magnet_token"):
    check(f"entry carries {_k!r}", _k in _e)
check("entry kind is 'cold'", _e["kind"] == "cold")
check("entry targets the lead's email", _e["target_email"] == "info@acme.ng")
# Absent magnet keys are what let the dead-link bug hide, so they are always present.
_e2 = lead_agent.pending_entry(_cfg, _LEAD, "Subj", _BODY)
check("magnet_url key exists even when no magnet was built", "magnet_url" in _e2)
check("magnet_url is None when no magnet was built", _e2["magnet_url"] is None)


# --------------------------------------------------------------------------
# Recovery: a failed draft leaves the lead queued for retry (never deleted)
# --------------------------------------------------------------------------
print("\n[failure: the lead stays queued so it can be retried]")
with tempfile.TemporaryDirectory() as td:
    conn, cfg = _fresh_env(td)
    conn.close()
    saved = []
    _patch_seams(saved)

    def _boom(cfg_, lead, brief, url, fn):
        raise RuntimeError("provider down")

    lead_agent.quality.draft_and_polish = _boom
    draft_queued.config.load_client = lambda: cfg

    buf = io.StringIO()
    with redirect_stdout(buf):
        draft_queued.main()

    check("nothing is queued when generation fails", len(saved) == 0)
    conn3 = state.connect(cfg["paths"]["db"])
    row = conn3.execute("SELECT status FROM leads WHERE client='acme' AND email=?",
                        (_LEAD["email"],)).fetchone()
    check("the lead row still exists after a failure", row is not None)
    # The claim is released back to the pool rather than deleted, so the lead is
    # retryable and its contact details survive the failure.
    check("the lead is back in the drafting pool ('sourced') for a retry",
          row and row["status"] == state.SOURCED)
    conn3.close()


# --------------------------------------------------------------------------
# Suppression still vetoes a re-draft (never re-contact an opt-out)
# --------------------------------------------------------------------------
print("\n[suppression: an opted-out address is never re-drafted]")
with tempfile.TemporaryDirectory() as td:
    conn, cfg = _fresh_env(td)
    suppression.add(conn, "acme", _LEAD["email"], reason="unsubscribed")
    conn.commit()
    conn.close()
    saved = []
    _patch_seams(saved)
    draft_queued.config.load_client = lambda: cfg

    buf = io.StringIO()
    with redirect_stdout(buf):
        draft_queued.main()
    check("a suppressed lead is skipped, not drafted", len(saved) == 0)


print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
