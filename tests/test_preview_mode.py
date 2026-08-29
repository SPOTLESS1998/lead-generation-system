"""Offline test for `lead_agent.main(preview=True)` — the trust contract.

Preview must run the REAL pipeline shape (strategist → magnet → copywriter →
quality gate → metrics) but write nothing permanent and send nothing: no
save_pending, no notify_operator. It uses a throwaway DB and prints the full
email instead. Every network/LLM seam is monkeypatched, so this never touches
Composio, Firecrawl, or any provider.

Run:  venv/bin/python tests/test_preview_mode.py
"""

import io
import os
import sys
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

for _k in ("GEMINI_API_KEY", "NVIDIA_API_KEY", "SMTP_USER", "SMTP_PASS", "UNSUB_SECRET"):
    os.environ.setdefault(_k, "test")
os.environ.setdefault("CLIENT", "ejentic")

import lead_agent          # noqa: E402
from core import discovery  # noqa: E402  (patched below; main re-imports this same object)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


_FAKE_LEAD = {
    "email": "hi@acme.ng", "first_name": "Ada", "last_name": "Obi",
    "title": "Founder", "company_name": "Acme Realty",
    "ejentic_service": "AI Lead Generation System",
    "company_facts": "Services: property sales. Notable: serves Lekki.",
    "company_description": "Acme sells property in Lekki.",
    "website_url": "https://acme.ng",
}
_FAKE_BODY = "Hi Ada,\n\nBODY-MARKER pitch.\n\nhttp://localhost:5002/magnet/ejentic/tok123\n\nBest,\nEjentic AI"


def test_preview_runs_real_path_but_persists_nothing():
    print("\n[main(preview=True): real path, throwaway db, nothing queued/sent]")
    calls = {"save_pending": 0, "notify_operator": 0}

    # Patch every seam that would hit the network or persist beyond the throwaway db.
    discovery.load_leads = lambda cfg, conn=None: ([dict(_FAKE_LEAD)], 0)
    lead_agent.generate_strategy = lambda cfg, lead: ("OUTCOME: standard", "fake")
    lead_agent.budget.copy_cfg = lambda conn, cfg: cfg
    lead_agent.magnet.build_content = lambda cfg, lead, brief: {"headline": "h", "steps": []}
    lead_agent.magnet.save_magnet = lambda *a, **k: None
    lead_agent.magnet.url = lambda cfg, client, token: "http://localhost:5002/magnet/ejentic/tok123"
    lead_agent.quality.draft_and_polish = lambda cfg, lead, brief, url, fn: ("A quick idea", _FAKE_BODY, "fake")

    def _spy(name):
        def f(*a, **k):
            calls[name] += 1
            return 1
        return f

    lead_agent.review.save_pending = _spy("save_pending")
    lead_agent.review.notify_operator = _spy("notify_operator")

    buf = io.StringIO()
    with redirect_stdout(buf):
        lead_agent.main(preview=True, limit=1)
    out = buf.getvalue()

    check("preview NEVER queues a draft (save_pending not called)", calls["save_pending"] == 0)
    check("preview NEVER notifies the operator (no email)", calls["notify_operator"] == 0)
    check("preview prints the full email", "FULL EMAIL" in out)
    check("preview shows the real drafted body", "BODY-MARKER" in out)
    check("preview shows the embedded magnet link", "magnet/ejentic/tok123" in out)
    check("preview still prints the live run metrics", "RUN METRICS" in out)
    check("preview announces it sent nothing", "PREVIEW complete" in out and "Nothing was queued" in out)


def main():
    test_preview_runs_real_path_but_persists_nothing()
    print(f"\n{'='*50}\nRESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
