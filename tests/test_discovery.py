"""Offline tests for core/discovery.py — NO network, NO API credits.

Monkeypatches the two external calls discovery makes:
  - subprocess.run  -> fake Composio (Google Maps + Firecrawl) responses
  - core.discovery.generate_json -> fake AI extraction (regex-pulls an email
    out of the prompt's website text)

Run:  venv/bin/python tests/test_discovery.py
"""

import os
import sys
import json
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import discovery, config          # noqa: E402
from core.leads import FIELDS               # noqa: E402

# --------------------------------------------------------------------------
# Tiny assertion tally
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


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeProc:
    def __init__(self, rc, out, err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


SCRAPE_CALLS = []   # every Firecrawl URL requested, in order

# One "OPERATIONAL" place with a website unless noted.
def place(name, site=None, status="OPERATIONAL"):
    p = {"displayName": {"text": name}, "businessStatus": status,
         "formattedAddress": "Lagos", "nationalPhoneNumber": "0800"}
    if site is not None:
        p["websiteUri"] = site
    return p


MAPS = {
    "Q1": [
        place("Alpha Accounting", "https://alpha-acct.ng/"),
        place("Beta Books", "https://www.betabooks.com/"),
        place("Ghost Ltd", None),                                   # no website -> dropped at Maps
        place("Dead Firm", "https://delta.ng/", "CLOSED_PERMANENTLY"),  # closed -> dropped
    ],
    "Q2": [
        place("Beta Books", "https://www.betabooks.com/"),          # dup domain -> deduped
        place("NoEmail Corp", "https://noemail.ng/"),               # no email anywhere -> skipped
    ],
    "QFAIL": "__fail__",                                            # Maps returns a failure envelope
}

PAGES = {
    "https://alpha-acct.ng/": "Alpha Accounting. Contact us at info@alpha-acct.ng today.",
    "https://www.betabooks.com/": "Welcome to Beta Books, Lagos bookkeepers.",   # no email
    "https://www.betabooks.com/contact": "Reach the team at hello@betabooks.com.",
    "https://noemail.ng/": "No contact info here.",
    "https://noemail.ng/contact": "Still nothing.",
    "https://noemail.ng/about": "About us. No email.",
}


def fake_run(cmd, capture_output=True, text=True, timeout=None):
    slug, payload = cmd[2], json.loads(cmd[4])
    if "MAPS" in slug:
        q = payload["textQuery"]
        resp = MAPS.get(q, [])
        if resp == "__fail__":
            return FakeProc(0, json.dumps({"successful": False, "data": {}, "error": "quota exceeded"}))
        return FakeProc(0, json.dumps({"successful": True, "data": {"places": resp}, "error": None}))
    # Firecrawl
    url = payload["url"]
    SCRAPE_CALLS.append(url)
    md = PAGES.get(url, "")
    return FakeProc(0, json.dumps({"successful": True, "data": {"data": {"markdown": md}}, "error": None}))


def fake_generate_json(cfg, prompt, retries=1):
    # Pull the first real email out of the prompt's embedded website text.
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", prompt)
    return ({"email": m.group(0) if m else "",
             "first_name": "", "last_name": "", "title": "",
             "company_description": "A test firm."}, "fake")


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_helpers():
    print("\n[helpers]")
    check("_domain_key strips scheme + www", discovery._domain_key("https://www.Foo.com/x") == "foo.com")
    check("_candidate_urls homepage-first + paths",
          discovery._candidate_urls("https://x.ng", ["", "contact", "about"])
          == ["https://x.ng", "https://x.ng/contact", "https://x.ng/about"])
    check("_place_name dict form", discovery._place_name({"text": "Acme"}) == "Acme")
    check("_place_name string form", discovery._place_name("Acme") == "Acme")
    check("_place_name none form", discovery._place_name(None) == "")
    check("_queries prefers discovery.queries",
          discovery._queries({"discovery": {"queries": ["a"]}, "target_niches": ["b"]}) == ["a"])
    check("_queries falls back to target_niches",
          discovery._queries({"discovery": {"queries": []}, "target_niches": ["b"]}) == ["b"])
    check("_loads_tolerant ignores surrounding log lines",
          discovery._loads_tolerant('log line\n{"a": 1}\ntail') == {"a": 1})
    check("_clean_email strips trailing period", discovery._clean_email("a@b.com.") == "a@b.com")
    check("_clean_email strips mailto + angle brackets", discovery._clean_email("mailto:<a@b.com>") == "a@b.com")


def base_cfg(**overrides):
    disc = {"queries": ["Q1", "Q2"], "max_results": 10, "max_leads": 25,
            "require_email": True, "scrape_pages": ["", "contact", "about"]}
    disc.update(overrides)
    return {"copy_provider": "gemini", "gemini_model": "x", "nvidia_model": "y", "discovery": disc}


def test_full_pipeline():
    print("\n[pipeline: happy path + dedup + filters + early-stop]")
    SCRAPE_CALLS.clear()
    leads, skipped = discovery.load_leads(base_cfg())

    by_company = {l["company_name"]: l for l in leads}
    check("2 usable leads returned", len(leads) == 2)
    check("Alpha email from homepage", by_company.get("Alpha Accounting", {}).get("email") == "info@alpha-acct.ng")
    check("Beta email from contact page", by_company.get("Beta Books", {}).get("email") == "hello@betabooks.com")
    check("NoEmail Corp counted as skipped", skipped == 1)
    check("Ghost/Dead not returned", "Ghost Ltd" not in by_company and "Dead Firm" not in by_company)
    check("Beta not duplicated across queries", list(by_company).count("Beta Books") == 0 or len([l for l in leads if l["company_name"] == "Beta Books"]) == 1)

    # Early-stop: Alpha had an email on the homepage -> its /contact must NOT be scraped.
    check("early-stop skips Alpha /contact", "https://alpha-acct.ng/contact" not in SCRAPE_CALLS)
    # Beta had email on /contact -> /about must NOT be scraped.
    check("early-stop skips Beta /about", "https://www.betabooks.com/about" not in SCRAPE_CALLS)
    # Dedup: Beta's site scraped only in Q1, not again in Q2.
    check("dedup: Beta homepage scraped once", SCRAPE_CALLS.count("https://www.betabooks.com/") == 1)

    # Lead shape must exactly match the curated-CSV contract.
    check("lead keys == FIELDS", all(set(l.keys()) == set(FIELDS) for l in leads))


def test_require_email_false():
    print("\n[pipeline: require_email=False keeps no-email businesses]")
    SCRAPE_CALLS.clear()
    leads, skipped = discovery.load_leads(base_cfg(queries=["Q2"], require_email=False))
    names = {l["company_name"] for l in leads}
    check("NoEmail Corp kept when require_email=False", "NoEmail Corp" in names)
    check("its email is blank", any(l["company_name"] == "NoEmail Corp" and l["email"] == "" for l in leads))


def test_max_leads_cap():
    print("\n[pipeline: max_leads cap]")
    SCRAPE_CALLS.clear()
    leads, _ = discovery.load_leads(base_cfg(max_leads=1))
    check("max_leads=1 returns exactly 1", len(leads) == 1)


def test_graceful_maps_failure():
    print("\n[pipeline: a failing Maps query degrades, run continues]")
    SCRAPE_CALLS.clear()
    leads, _ = discovery.load_leads(base_cfg(queries=["QFAIL", "Q1"]))
    # QFAIL yields nothing; Q1 still produces Alpha + Beta.
    check("failed query contributes nothing, Q1 still works", len(leads) == 2)


def test_config_defaults():
    print("\n[config: defaults wired]")
    check("DEFAULTS.lead_source == 'csv'", config.DEFAULTS.get("lead_source") == "csv")
    check("DEFAULTS has discovery block", isinstance(config.DEFAULTS.get("discovery"), dict))
    check("discovery has maps + firecrawl slugs",
          "maps_search_slug" in config.DEFAULTS["discovery"] and
          "firecrawl_scrape_slug" in config.DEFAULTS["discovery"])


def main():
    # Patch the two external seams.
    discovery.subprocess.run = fake_run
    discovery.generate_json = fake_generate_json

    test_helpers()
    test_full_pipeline()
    test_require_email_false()
    test_max_leads_cap()
    test_graceful_maps_failure()
    test_config_defaults()

    print(f"\n{'='*50}\nRESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
