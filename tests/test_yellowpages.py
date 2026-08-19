"""Offline tests for the Yellow Pages lead source (core/yellowpages.py) + its wiring.

NO network. Yellow Pages reuses core.discovery's Firecrawl + extraction plumbing,
so we monkeypatch at those seams:
  * discovery._firecrawl_markdown  -> canned markdown (routed by URL)
  * yellowpages.generate_json      -> the business list for a results page
  * discovery.generate_json        -> the per-site email extraction

Run:  venv/bin/python tests/test_yellowpages.py
"""

import io
import os
import sys
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import yellowpages, discovery   # noqa: E402
from core.leads import FIELDS             # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def quiet(fn, *a, **k):
    """Run a chatty function with stdout suppressed; return its result."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


# --------------------------------------------------------------------------
# Pure helpers — no patching needed
# --------------------------------------------------------------------------
u = yellowpages._search_url(yellowpages.DEFAULT_SEARCH_URL_TEMPLATE, "tax accountants", "Lagos")
check("_search_url URL-encodes the query", "search_terms=tax+accountants" in u)
check("_search_url fills the location", "geo_location_terms=Lagos" in u)

u2 = yellowpages._search_url("https://x.example/no-placeholders", "a b", "C")
check("_search_url falls back when the template lacks placeholders",
      "search_terms=a+b" in u2)

specs_seg = yellowpages._yp_specs(
    {"yellowpages": {"segments": [{"name": "Acc", "service": "Books", "queries": ["q1", "q2"]}]}})
check("segments expand to one spec per query, tagged with the service",
      len(specs_seg) == 2 and all(s["service"] == "Books" for s in specs_seg))

specs_flat = yellowpages._yp_specs({"yellowpages": {"queries": ["only-one"]}})
check("flat queries are used when there are no segments",
      len(specs_flat) == 1 and specs_flat[0]["query"] == "only-one" and specs_flat[0]["service"] == "")

specs_niche = yellowpages._yp_specs({"yellowpages": {}, "target_niches": ["niche-a"]})
check("target_niches is the final fallback",
      len(specs_niche) == 1 and specs_niche[0]["query"] == "niche-a")

check("no queries anywhere -> no specs", yellowpages._yp_specs({}) == [])

try:
    quiet(yellowpages.load_leads, {"client": "t", "yellowpages": {}})
    raised = False
except yellowpages.YellowPagesError:
    raised = True
check("load_leads with no queries raises YellowPagesError", raised)


# --------------------------------------------------------------------------
# Fakes for the full pipeline
# --------------------------------------------------------------------------
YP_RESULTS_MD = "Alpha Accounting | Beta Books | Gamma | Delta Dead — Yellow Pages results."

SITE_MD = {
    "alpha.com":   "Questions? Email hello@alpha.com. Founder Ada Lovelace leads the practice.",
    "beta.com":    "Get in touch: info@beta.com any time.",
    "noemail.com": "Call 0800-NOTHING. (This site lists no email address at all.)",
}

# What the AI would "read" off a results page: 5 listings exercising every branch.
LISTINGS = {"businesses": [
    {"company_name": "Alpha Accounting", "website_url": "https://alpha.com", "phone": "111"},
    {"company_name": "Beta Books",       "website_url": "beta.com",          "phone": ""},   # bare -> https://
    {"company_name": "Gamma NoSite",     "website_url": "",                   "phone": "333"},  # no site -> dropped
    {"company_name": "Delta Dead",       "website_url": "https://noemail.com", "phone": ""},  # site, no email
    {"company_name": "Alpha Dup",        "website_url": "https://www.alpha.com", "phone": ""},  # dup domain
]}


def fake_fc_markdown(url, slug):
    if "yellowpages.com" in url:
        return YP_RESULTS_MD
    for dom, md in SITE_MD.items():
        if dom in url:
            return md
    return ""


def fake_yp_json(cfg, prompt):
    return LISTINGS, "fake"


def fake_extract_json(cfg, prompt):
    # discovery._extract_lead embeds the scraped markdown (incl. any email) in the prompt.
    if "hello@alpha.com" in prompt:
        return ({"email": "hello@alpha.com", "first_name": "Ada", "last_name": "Lovelace",
                 "title": "Founder", "company_description": "Accounting firm."}, "fake")
    if "info@beta.com" in prompt:
        return ({"email": "info@beta.com", "first_name": "", "last_name": "",
                 "title": "", "company_description": "Independent bookshop."}, "fake")
    return ({"email": "", "first_name": "", "last_name": "", "title": "",
             "company_description": ""}, "fake")


discovery._firecrawl_markdown = fake_fc_markdown
yellowpages.generate_json = fake_yp_json
discovery.generate_json = fake_extract_json

BASE_CFG = {
    "client": "test",
    "yellowpages": {
        "location": "Lagos",
        "segments": [{"name": "Accountants", "service": "AI Bookkeeping", "queries": ["accountants"]}],
        "max_workers": 2,
        "require_email": True,
        "scrape_pages": ["", "contact"],
    },
}


# --------------------------------------------------------------------------
# Full pipeline — require_email = True (the default)
# --------------------------------------------------------------------------
leads, skipped = quiet(yellowpages.load_leads, BASE_CFG)

check("returns exactly the 2 businesses with a public email", len(leads) == 2)
check("the site with no email is counted as skipped", skipped == 1)

emails = {lead["email"] for lead in leads}
check("Alpha's email was extracted", "hello@alpha.com" in emails)
check("Beta's email was extracted", "info@beta.com" in emails)

check("every lead carries all FIELDS keys", all(set(FIELDS) <= set(lead) for lead in leads))
check("the segment's service tag rides onto every lead",
      all(lead["ejentic_service"] == "AI Bookkeeping" for lead in leads))

alpha_leads = [lead for lead in leads if "alpha.com" in lead["website_url"]]
check("the www. duplicate was de-duped (Alpha appears once)", len(alpha_leads) == 1)
check("a listing with no website never becomes a lead",
      not any("Gamma" in lead["company_name"] for lead in leads))

alpha = next(lead for lead in leads if lead["email"] == "hello@alpha.com")
check("a named contact on the page is captured",
      alpha["first_name"] == "Ada" and alpha["title"] == "Founder")

beta = next(lead for lead in leads if lead["email"] == "info@beta.com")
check("a bare website_url gets an https:// scheme", beta["website_url"].startswith("https://"))


# --------------------------------------------------------------------------
# require_email = False keeps the email-less business
# --------------------------------------------------------------------------
cfg_keep = {"client": "test", "yellowpages": dict(BASE_CFG["yellowpages"], require_email=False)}
leads2, skipped2 = quiet(yellowpages.load_leads, cfg_keep)
check("require_email=False keeps the email-less business (3 leads, 0 skipped)",
      len(leads2) == 3 and skipped2 == 0)


# --------------------------------------------------------------------------
# Step-1 edge cases: empty page and non-dict AI output both yield no businesses
# --------------------------------------------------------------------------
spec = {"query": "x", "segment": "", "service": ""}
tmpl = yellowpages.DEFAULT_SEARCH_URL_TEMPLATE

discovery._firecrawl_markdown = lambda url, slug: ""
biz_empty = quiet(yellowpages._discover_from_query, BASE_CFG, spec, tmpl, "Lagos", "FIRECRAWL_SCRAPE", 20)
check("an empty results page yields no businesses", biz_empty == [])

discovery._firecrawl_markdown = fake_fc_markdown
yellowpages.generate_json = lambda cfg, prompt: ("not-a-dict", "fake")
biz_bad = quiet(yellowpages._discover_from_query, BASE_CFG, spec, tmpl, "Lagos", "FIRECRAWL_SCRAPE", 20)
check("non-dict AI output yields no businesses", biz_bad == [])
yellowpages.generate_json = fake_yp_json  # restore (harmless at end)


# --------------------------------------------------------------------------
print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
