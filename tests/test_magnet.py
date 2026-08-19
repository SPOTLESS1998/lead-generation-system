"""Offline tests for the personalized lead magnet.

Covers core/magnet.py, the state `magnets` table round-trip, and the copy-link
wiring in scripts/lead_agent.py — with NO network and NO API credits.

Monkeypatches:
  - core.magnet.generate_json   -> canned structured content (+ failure modes)
  - scripts/lead_agent.generate -> canned email, capturing the built prompt

Run:  venv/bin/python tests/test_magnet.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from core import magnet, state          # noqa: E402
import lead_agent                        # noqa: E402  (scripts/ is on the path)

# --------------------------------------------------------------------------
# Tiny assertion tally (same convention as tests/test_discovery.py)
# --------------------------------------------------------------------------
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


LEAD = {
    "first_name": "Ada", "last_name": "Obi", "title": "COO",
    "company_name": "Acme Foods", "email": "ada@acme.ng",
    "company_description": "Food distributor",
    "ejentic_service": "Enterprise Workflow Automation",
}
CFG = {"client": "acme", "client_name": "Ejentic AI",
       "unsubscribe_base_url": "http://localhost:5001"}


# --------------------------------------------------------------------------
# build_content — happy path keeps the AI's structured fields
# --------------------------------------------------------------------------
def fake_json_ok(cfg, prompt):
    return ({
        "headline": "Audit for Acme",
        "bottleneck": "Manual order entry eats hours.",
        "steps": [
            {"title": "Map", "detail": "d1"},
            {"title": "Build", "detail": "d2"},
            {"title": "Ship", "detail": "d3"},
        ],
        "cta": "Reply to start.",
    }, "fake")


magnet.generate_json = fake_json_ok
c = magnet.build_content(CFG, LEAD, "brief text")
check("build_content keeps AI headline", c["headline"] == "Audit for Acme")
check("build_content keeps exactly 3 steps", len(c["steps"]) == 3)
check("build_content carries company name", c["company_name"] == "Acme Foods")
check("build_content carries prospect name", c["prepared_for"] == "Ada")


# --------------------------------------------------------------------------
# build_content — AI failure / junk both fall back to a valid page
# --------------------------------------------------------------------------
def fake_json_boom(cfg, prompt):
    raise RuntimeError("no provider available")


magnet.generate_json = fake_json_boom
fb = magnet.build_content(CFG, LEAD, "the brief opening line about their bottleneck")
check("fallback still returns 3 steps", len(fb["steps"]) == 3)
check("fallback uses the brief as bottleneck", "brief opening" in fb["bottleneck"])

magnet.generate_json = lambda cfg, p: ("not a dict at all", "fake")
jf = magnet.build_content(CFG, LEAD, "b")
check("junk AI response -> fallback shape", len(jf["steps"]) == 3)

magnet.generate_json = lambda cfg, p: ({"headline": "X", "steps": []}, "fake")  # no steps
nf = magnet.build_content(CFG, LEAD, "b")
check("empty steps -> fallback shape", len(nf["steps"]) == 3)


# --------------------------------------------------------------------------
# render_html — every dynamic field is HTML-escaped (no injected markup)
# --------------------------------------------------------------------------
evil = {
    "company_name": "<script>alert(1)</script>",
    "prepared_for": "Ada", "headline": "Safe Headline",
    "bottleneck": "Tom & Jerry <b>bold</b>",
    "steps": [{"title": "<i>hi", "detail": "d"}],
    "cta": "reply now",
}
html_out = magnet.render_html(CFG, evil)
check("render escapes a script tag", "<script>alert(1)</script>" not in html_out)
check("render emits escaped entity instead", "&lt;script&gt;" in html_out)
check("render includes the headline", "Safe Headline" in html_out)
check("render includes the cta", "reply now" in html_out)
check("render is a full HTML doc", html_out.strip().startswith("<!DOCTYPE html>"))


# --------------------------------------------------------------------------
# url + token
# --------------------------------------------------------------------------
check("url has the right shape",
      magnet.url(CFG, "acme", "abc123") == "http://localhost:5001/magnet/acme/abc123")
check("token is 12 chars", len(magnet.new_token()) == 12)
check("tokens are unique", magnet.new_token() != magnet.new_token())


# --------------------------------------------------------------------------
# state.save_magnet / get_magnet round-trip
# --------------------------------------------------------------------------
db = os.path.join(tempfile.mkdtemp(), "state.sqlite")
conn = state.connect(db)
magnet.generate_json = fake_json_ok
content = magnet.build_content(CFG, LEAD, "brief")
state.save_magnet(conn, "acme", "tok1", "ada@acme.ng", content)
got = state.get_magnet(conn, "acme", "tok1")
check("magnet round-trips through the DB", bool(got) and got["headline"] == "Audit for Acme")
check("unknown token returns None", state.get_magnet(conn, "acme", "missing") is None)
state.save_magnet(conn, "acme", "tok1", "ada@acme.ng", {**content, "headline": "Updated"})
check("re-saving a token updates its content",
      state.get_magnet(conn, "acme", "tok1")["headline"] == "Updated")
conn.close()


# --------------------------------------------------------------------------
# generate_copy — embeds the REAL link, never a dead placeholder
# --------------------------------------------------------------------------
captured = {}


def fake_generate(cfg, prompt):
    captured["prompt"] = prompt
    return ("Subject: Idea for Acme\nHi Ada,\nHere is your audit.", "fake")


lead_agent.generate = fake_generate

subj, body, _ = lead_agent.generate_copy(
    CFG, LEAD, "brief", magnet_url="http://localhost:5001/magnet/acme/tok1")
check("copy prompt contains the real URL",
      "http://localhost:5001/magnet/acme/tok1" in captured["prompt"])
check("copy prompt warns against placeholders",
      "placeholder" in captured["prompt"].lower())
check("copy prompt hardens deliverability",
      "spam-trigger" in captured["prompt"].lower())
check("copy parses the subject line", subj == "Idea for Acme")
check("copy body drops the Subject line", "Subject:" not in body)

subj2, _, _ = lead_agent.generate_copy(CFG, LEAD, "brief", magnet_url=None)
check("no-url path still drafts", subj2 == "Idea for Acme")
check("no-url prompt forbids inventing a link",
      "do not invent" in captured["prompt"].lower())


# --------------------------------------------------------------------------
print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
