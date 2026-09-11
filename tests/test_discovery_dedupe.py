"""Offline tests for cross-run discovery de-duplication + query rotation.

The money question these pin: does a daily run still PAY to scrape a business it
already owns? Phase 2 of discovery is a Firecrawl scrape plus an LLM extraction per
business, so a skip that happens after Phase 2 saves nothing. The guard here is
ordered, not just present — a test that only asserted "the lead was skipped" would
still pass if the skip moved to the wrong side of the scrape.

Run:  venv/bin/python tests/test_discovery_dedupe.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import discovery, state, leads as leads_mod            # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _tmpdb():
    return tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name


def _biz(name, url, query="q1"):
    return {"company_name": name, "website_url": url, "address": "", "phone": "",
            "rating": None, "review_count": None, "types": [], "query": query,
            "segment": "seg", "ejentic_service": "Svc"}


def _run_load_leads(conn, known, hits_per_query, monkeypatch_state):
    """Drive load_leads with Maps + scrape + extract all stubbed.

    Returns (leads, skipped, scraped_urls) — scraped_urls is the evidence of what we
    actually paid Firecrawl to visit.
    """
    scraped = []

    def fake_maps(spec, max_results, slug):
        return hits_per_query.get(spec["query"], [])

    def fake_scrape(url, slug):
        scraped.append(url)
        return "# markdown"

    def fake_extract(cfg, business, markdown):
        return {"first_name": "", "last_name": "", "title": "",
                "email": f"info@{business['website_url'].split('//')[-1].split('/')[0]}",
                "company_name": business["company_name"],
                "company_description": "d", "company_facts": "f",
                "website_url": business["website_url"],
                "ejentic_service": business.get("ejentic_service", "")}

    cfg = {"client": "t",
           "discovery": {"max_results": 10, "max_leads": 50, "require_email": True,
                         "scrape_pages": [""], "max_workers": 1,
                         "segments": [{"name": "seg", "service": "Svc",
                                       "queries": list(hits_per_query.keys())}]}}

    # Patch the network seams only; load_leads itself runs for real.
    discovery._maps_search = fake_maps
    discovery._firecrawl_markdown = fake_scrape
    discovery._extract_lead = fake_extract
    discovery._extraction_cfg = lambda c, conn=None: c
    try:
        out = discovery.load_leads(cfg, conn=conn)
    finally:
        pass
    return out[0], out[1], scraped


def test_skip_happens_before_scraping():
    print("\n[a known business is skipped BEFORE the scrape — that is where the cost is]")
    # Two businesses on the list already, one genuinely new.
    conn = state.connect(_tmpdb())
    state.bank_leads(conn, "t", [
        {"email": "a@acme.com", "website_url": "https://acme.com", "company_name": "Acme"},
        {"email": "b@beta.com", "website_url": "https://www.beta.com", "company_name": "Beta"},
    ])

    hits = {"q1": [_biz("Acme", "https://acme.com"),
                   _biz("Beta", "https://www.beta.com"),
                   _biz("Gamma", "https://gamma.com")]}
    found, skipped, scraped = _run_load_leads(conn, {"acme.com"}, hits, None)

    check("only the NEW business produced a lead", len(found) == 1)
    check("that lead is Gamma", found[0]["company_name"] == "Gamma")
    check("already-owned sites were NOT scraped",
          "https://acme.com" not in scraped and "https://www.beta.com" not in scraped)
    check("the new site WAS scraped", "https://gamma.com" in scraped)
    check("exactly one scrape happened (not three)", len(scraped) == 1)


def test_no_conn_means_no_skip():
    print("\n[without a DB handle, behaviour is unchanged (back-compat)]")
    hits = {"q1": [_biz("Acme", "https://acme.com")]}
    found, skipped, scraped = _run_load_leads(None, set(), hits, None)
    check("the business was still scraped", "https://acme.com" in scraped)
    check("and produced a lead", len(found) == 1)


def test_within_run_dedupe_still_works():
    print("\n[the same site from two queries is scraped once]")
    conn = state.connect(_tmpdb())
    hits = {"q1": [_biz("Acme", "https://www.acme.com", "q1")],
            "q2": [_biz("Acme", "https://acme.com", "q2")]}
    found, skipped, scraped = _run_load_leads(conn, set(), hits, None)
    check("scraped exactly once", len(scraped) == 1)
    check("one lead produced", len(found) == 1)


def test_second_run_is_cheap():
    print("\n[a second run over the same searches scrapes nothing]")
    conn = state.connect(_tmpdb())
    hits = {"q1": [_biz("Acme", "https://acme.com"), _biz("Beta", "https://beta.com")]}

    found1, _, scraped1 = _run_load_leads(conn, set(), hits, None)
    check("first run scrapes both", len(scraped1) == 2)

    # Bank what run 1 found, exactly as lead_agent does.
    state.bank_leads(conn, "t", found1)

    found2, _, scraped2 = _run_load_leads(conn, set(), hits, None)
    check("second run scrapes NOTHING (all already owned)", scraped2 == [])
    check("second run yields no new leads", len(found2) == 0)


def test_rotation():
    print("\n[query rotation runs a rolling subset per day]")
    specs = [{"query": f"q{i}", "segment": "s", "service": "svc"} for i in range(12)]
    on = {"discovery": {"rotation": {"enabled": True, "queries_per_run": 4}}}

    day0, rot0 = discovery._rotate_specs(specs, on, day_ordinal=100)
    day1, _ = discovery._rotate_specs(specs, on, day_ordinal=101)
    day2, _ = discovery._rotate_specs(specs, on, day_ordinal=102)
    check("rotation reports itself active", rot0 is True)
    check("4 queries per run", len(day0) == 4)
    check("consecutive days differ", [s["query"] for s in day0] != [s["query"] for s in day1])
    check("days 0-2 together cover the whole plan",
          {s["query"] for s in day0 + day1 + day2} == {f"q{i}" for i in range(12)})
    check("same day is deterministic (a re-run repeats, not rerolls)",
          [s["query"] for s in discovery._rotate_specs(specs, on, day_ordinal=100)[0]]
          == [s["query"] for s in day0])

    # Every off-switch must be a clean no-op.
    for cfg, label in (({"discovery": {}}, "absent"),
                       ({"discovery": {"rotation": {"enabled": False}}}, "disabled"),
                       ({"discovery": {"rotation": {"enabled": True, "queries_per_run": 12}}},
                        "per_run >= total"),
                       ({"discovery": {"rotation": {"enabled": True, "queries_per_run": 0}}},
                        "per_run 0")):
        out, rot = discovery._rotate_specs(specs, cfg, day_ordinal=100)
        check(f"rotation {label}: all 12 kept, not rotated", len(out) == 12 and rot is False)

    # A single-query plan must still work.
    one, rot = discovery._rotate_specs([specs[0]], on, day_ordinal=100)
    check("single-query plan is left alone", len(one) == 1 and rot is False)


def test_domain_key_is_shared():
    print("\n[discovery and state agree on the de-dupe key]")
    for url in ("https://www.Acme.com/x", "http://acme.com", "acme.com"):
        check(f"both agree on {url!r}",
              discovery._domain_key(url) == leads_mod.domain_key(url) == "acme.com")


test_skip_happens_before_scraping()
test_no_conn_means_no_skip()
test_within_run_dedupe_still_works()
test_second_run_is_cheap()
test_rotation()
test_domain_key_is_shared()

print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
