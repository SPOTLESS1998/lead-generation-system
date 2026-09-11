"""Automated lead source: Yellow Pages discovery -> Firecrawl enrichment -> AI extraction.

A second drop-in source alongside core/discovery.py (Google Maps). It keeps the exact
same public contract —

    load_leads(client_cfg) -> (leads, skipped)

with each lead a dict keyed by core.leads.FIELDS — so scripts/lead_agent.py switches to
it purely from config (`"lead_source": "yellowpages"`).

Yellow Pages has no Composio "search" action, so Step 1 is itself a Firecrawl scrape:
for each configured query we build a Yellow Pages search-results URL (query + location),
scrape it to markdown, and AI-extract the businesses shown (name + their own website when
the listing exposes one). Steps 2-3 — scrape each business's site and extract a public
email — are IDENTICAL to Google Maps discovery, so this module REUSES core.discovery for
them and only owns the YP-specific "find the businesses" step.

Composio (Firecrawl) is the runtime backend, same as discovery.py — no extra key needed.
Every external call degrades gracefully: a failure skips one item (counted in `skipped`)
and never crashes the run. A missing Composio CLI is the one fatal case (surfaced as
discovery.DiscoveryError, since the scrape plumbing is shared).

    from core import yellowpages
    leads, skipped = yellowpages.load_leads(cfg)
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus

from core import discovery
from core import state
from core.leads import FIELDS, _looks_like_email   # noqa: F401 (FIELDS documents the shape)
from core.ai import generate_json

DEFAULT_FIRECRAWL_SLUG = "FIRECRAWL_SCRAPE"
# {query} and {location} are URL-encoded before substitution. Default is the US site;
# a client targeting another country overrides this (e.g. yellowpages.com.ng, vconnect).
DEFAULT_SEARCH_URL_TEMPLATE = (
    "https://www.yellowpages.com/search?search_terms={query}&geo_location_terms={location}"
)
MAX_MARKDOWN_CHARS = 8000   # results pages are long; bound token cost
DEFAULT_MAX_WORKERS = 6


class YellowPagesError(Exception):
    """A fatal setup problem the operator must fix (e.g. no queries configured).

    Per-query / per-business failures are NOT fatal — they are logged and skipped,
    mirroring core/discovery.py.
    """


# --------------------------------------------------------------------------
# Query plan (mirrors discovery's segment -> query -> niche precedence, but reads
# the client's `yellowpages` config block so the two sources stay independent).
# --------------------------------------------------------------------------

def _yp_specs(cfg):
    """[{query, segment, service}] from yellowpages.segments -> .queries -> target_niches.

    Each segment carries the Ejentic `service` its prospects need; that tag rides on
    every business and lead so the strategist can pitch the matched offering.
    """
    yp = cfg.get("yellowpages") or {}
    specs = []
    for seg in (yp.get("segments") or []):
        if not isinstance(seg, dict):
            continue
        name = str(seg.get("name") or "").strip()
        service = str(seg.get("service") or seg.get("name") or "").strip()
        for q in (seg.get("queries") or []):
            if str(q).strip():
                specs.append({"query": str(q).strip(), "segment": name, "service": service})
    if specs:
        return specs
    flat = [q for q in (yp.get("queries") or []) if str(q).strip()]
    if not flat:
        flat = [q for q in (cfg.get("target_niches") or []) if str(q).strip()]
    return [{"query": q, "segment": "", "service": ""} for q in flat]


def _search_url(template, query, location):
    """Build a YP search-results URL, URL-encoding the query + location.

    Falls back to the default template if a custom one is malformed OR omits the
    {query} placeholder — a template that ignores the query would silently scrape
    the same page for every search, which is never what the operator wants.
    """
    q, loc = quote_plus(query), quote_plus(location or "")
    if "{query}" in (template or ""):
        try:
            return template.format(query=q, location=loc)
        except (KeyError, IndexError, ValueError):
            pass
    return DEFAULT_SEARCH_URL_TEMPLATE.format(query=q, location=loc)


# --------------------------------------------------------------------------
# Step 1 — extract the business list from a scraped results page
# --------------------------------------------------------------------------

def _listings_prompt(query, location, markdown):
    text = (markdown or "")[:MAX_MARKDOWN_CHARS]
    return f"""You are reading a Yellow Pages search-results page for "{query}" in "{location}".
Extract the list of REAL businesses shown on the page.

PAGE TEXT (markdown, possibly truncated):
\"\"\"
{text}
\"\"\"

Return ONLY a JSON object with this exact shape:
{{"businesses": [
  {{"company_name": "<name as shown>", "website_url": "<the business's OWN website if the listing shows one, else \\"\\">", "phone": "<phone if shown, else \\"\\">"}}
]}}

Rules: include only businesses actually present in the text; do NOT invent names, URLs, or
phone numbers; use "" for any field not shown. A Yellow Pages profile/detail link is NOT the
business's website — only report a real external company site. Reply with ONLY the JSON object."""


def _discover_from_query(cfg, spec, template, location, fc_slug, max_listings):
    """Scrape one YP results page and AI-extract its businesses (tagged w/ the service)."""
    url = _search_url(template, spec["query"], location)
    markdown = discovery._firecrawl_markdown(url, fc_slug)   # shared Firecrawl plumbing
    if not markdown:
        print(f"      ⚠️  YP results page empty/failed for {spec['query']!r}.")
        return []
    try:
        obj, _ = generate_json(cfg, _listings_prompt(spec["query"], location, markdown))
    except Exception as e:
        print(f"      ⚠️  YP listing extraction failed for {spec['query']!r}: {e}")
        return []

    items = obj.get("businesses") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return []

    out = []
    for it in items[:max_listings]:
        if not isinstance(it, dict):
            continue
        name = (it.get("company_name") or "").strip()
        website = (it.get("website_url") or "").strip()
        if not name or not website:
            continue  # need the business's own site to find an email downstream
        if not website.lower().startswith(("http://", "https://")):
            website = "https://" + website
        out.append({
            "company_name": name,
            "website_url": website,
            "address": location,
            "phone": (it.get("phone") or "").strip(),
            "segment": spec.get("segment", ""),
            "ejentic_service": spec.get("service", ""),
        })
    return out


# --------------------------------------------------------------------------
# Orchestration — the public seam
# --------------------------------------------------------------------------

def load_leads(client_cfg, conn=None):
    """Discover leads via Yellow Pages -> Firecrawl -> AI, in parallel. Returns (leads, skipped).

    Phase 1 scrapes every configured YP results page concurrently and AI-extracts the
    businesses. Phase 2 scrapes each unique business's site and extracts a public email —
    reusing core.discovery's enrichment verbatim. `skipped` counts businesses that could
    not become a usable lead (no site, scrape failed, or — when require_email is set — no
    email found), mirroring core/leads.py + core/discovery.py.

    `conn` (optional): when supplied, businesses already on this client's lead list are
    dropped BEFORE Phase 2, so a daily run does not re-scrape companies it already owns.
    Same reasoning as core.discovery.load_leads — see the comment there.
    """
    yp = client_cfg.get("yellowpages") or {}
    template = yp.get("search_url_template") or DEFAULT_SEARCH_URL_TEMPLATE
    location = yp.get("location", "")
    fc_slug = yp.get("firecrawl_scrape_slug") or DEFAULT_FIRECRAWL_SLUG
    max_leads = int(yp.get("max_leads", 25))
    require_email = yp.get("require_email", True)
    scrape_pages = yp.get("scrape_pages", ["", "contact"])
    workers = max(1, int(yp.get("max_workers", DEFAULT_MAX_WORKERS)))
    max_listings = int(yp.get("max_listings_per_query", 20))

    specs = _yp_specs(client_cfg)
    if not specs:
        raise YellowPagesError(
            "lead_source is 'yellowpages' but no search queries are configured. "
            "Set yellowpages.segments or yellowpages.queries (or target_niches) in your "
            "client config."
        )

    # --- Phase 1: scrape each YP results page + extract businesses (concurrently). ---
    print(f"   📖 Running {len(specs)} Yellow Pages search(es) ({workers} at a time)...")
    businesses = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_discover_from_query, client_cfg, spec, template, location,
                          fc_slug, max_listings): spec for spec in specs}
        for fut in as_completed(futs):
            q = futs[fut]["query"]
            try:
                found = fut.result()
            except discovery.DiscoveryError:
                raise  # missing CLI etc. — a real setup problem, surface it
            except Exception as e:
                print(f"      ⚠️  YP query {q!r} failed: {e}")
                found = []
            print(f"      • {q!r} → {len(found)} listing(s) with a website.")
            businesses.extend(found)

    # De-dupe by domain (reuse discovery's normalizer).
    seen, unique = set(), []
    for biz in businesses:
        dkey = discovery._domain_key(biz["website_url"])
        if dkey in seen:
            continue
        seen.add(dkey)
        unique.append(biz)
    print(f"   🔎 {len(unique)} unique business(es) to enrich (from {len(businesses)} hits).")

    # Skip businesses already on the lead list BEFORE the expensive per-site scrape,
    # so a daily run stops re-buying companies it already owns. Same rule as
    # core.discovery.load_leads; a no-op when no DB handle was supplied.
    if conn is not None:
        known = state.known_domains(conn, client_cfg["client"])
        if known:
            fresh = [b for b in unique if discovery._domain_key(b["website_url"]) not in known]
            if len(fresh) != len(unique):
                print(f"   💾 {len(unique) - len(fresh)} business(es) already on the lead "
                      f"list — skipped without re-scraping.")
            unique = fresh

    # --- Phase 2: scrape + extract every unique business — reused from discovery. ---
    leads, skipped = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(discovery._process_business, client_cfg, biz, fc_slug, scrape_pages): biz
                for biz in unique}
        for fut in as_completed(futs):
            biz = futs[fut]
            try:
                _, lead = fut.result()
            except Exception as e:
                print(f"      ⚠️  {biz['company_name']}: enrichment error: {e}")
                lead = None

            if not lead:
                skipped += 1
                continue
            if require_email and not _looks_like_email(lead["email"]):
                print(f"      ⏭️  {biz['company_name']}: no public email found; skipping.")
                skipped += 1
                continue

            leads.append(lead)
            print(f"      ✅ {biz['company_name']} — {lead['email'] or '(no email)'}")
            if len(leads) >= max_leads:
                for f in futs:      # hit the cap — cancel work not yet started
                    f.cancel()
                break

    return leads, skipped
