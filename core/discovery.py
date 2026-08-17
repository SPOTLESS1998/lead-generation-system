"""Automated lead source: Google Maps discovery -> Firecrawl enrichment -> AI extraction.

A drop-in alternative to core/leads.py (the curated-CSV source). It keeps the
exact same public contract —

    load_leads(client_cfg) -> (leads, skipped)

with each lead a dict keyed by core.leads.FIELDS — so scripts/lead_agent.py can
switch sources purely from config (`"lead_source": "maps_firecrawl"`) with
nothing else in the pipeline changing.

Pipeline (every external call degrades gracefully; a failure skips one item and
is counted in `skipped`, it never crashes the run — the house pattern from
core/calendar.py):

    1. Google Maps text search (Composio) per configured query
       -> businesses with a name + website.
    2. Firecrawl scrape (Composio) of each site -> markdown.
    3. AI extraction (core.ai.generate_json) -> a public contact email, a named
       contact if the page identifies one, and a one-line company description.

Composio is the runtime backend — the same `composio execute <slug>` mechanism
core/calendar.py uses. Maps + Firecrawl are already connected there, so no extra
API key is needed. Google Maps never returns an email, so the email always comes
from the scraped page; businesses with no discoverable email are counted in
`skipped` (when require_email is set), mirroring how core/leads.py skips
placeholder rows.

    from core import discovery
    leads, skipped = discovery.load_leads(cfg)
"""

import json
import subprocess
from urllib.parse import urljoin, urlparse

from core.leads import FIELDS, _looks_like_email
from core.ai import generate_json

# Composio action slugs (overridable per client via the "discovery" config block).
DEFAULT_MAPS_SLUG = "GOOGLE_MAPS_TEXT_SEARCH"
DEFAULT_FIRECRAWL_SLUG = "FIRECRAWL_SCRAPE"

# Only these Maps fields are requested — keeps the response small and cheap.
MAPS_FIELD_MASK = ("places.id,places.displayName,places.formattedAddress,"
                   "places.websiteUri,places.nationalPhoneNumber,places.businessStatus")

# Markdown handed to the extractor is truncated to bound token cost.
MAX_MARKDOWN_CHARS = 6000


class DiscoveryError(Exception):
    """A fatal setup problem (e.g. Composio CLI missing) the operator must fix.

    Per-query / per-business failures are NOT fatal — they are logged and the
    business is skipped, so one bad site never sinks the whole run.
    """


# --------------------------------------------------------------------------
# Composio plumbing (mirrors core/calendar.py)
# --------------------------------------------------------------------------

def _loads_tolerant(text):
    """composio may print log lines around the JSON; parse the outermost object."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def _composio_execute(slug, payload, timeout=90):
    """Run `composio execute <slug> -d <json>` and return the parsed response dict.

    Raises DiscoveryError only for a missing CLI (a setup problem). Every other
    failure returns None so the caller can skip this item and continue.
    """
    cmd = ["composio", "execute", slug, "-d", json.dumps(payload)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DiscoveryError(
            "composio CLI not found on PATH. Install it and connect Google Maps + "
            "Firecrawl, or set lead_source back to 'csv'."
        )
    except subprocess.TimeoutExpired:
        print(f"   ⚠️  composio {slug} timed out; skipping.")
        return None
    if proc.returncode != 0:
        print(f"   ⚠️  composio {slug} rc={proc.returncode}: "
              f"{(proc.stderr or proc.stdout or '').strip()[:200]}")
        return None

    obj = _loads_tolerant((proc.stdout or "").strip())
    if not isinstance(obj, dict):
        print(f"   ⚠️  composio {slug} returned unparseable output; skipping.")
        return None
    ok = obj.get("successful", obj.get("successfull", True))
    if ok is False or obj.get("error"):
        print(f"   ⚠️  composio {slug} reported failure: {str(obj.get('error'))[:200]}")
        return None
    return obj


# --------------------------------------------------------------------------
# Step 1 — Google Maps discovery
# --------------------------------------------------------------------------

def _place_name(display):
    """displayName may be {'text': '...'} or a bare string."""
    if isinstance(display, dict):
        return (display.get("text") or "").strip()
    return (display or "").strip() if isinstance(display, str) else ""


def _maps_search(query, max_results, slug):
    """Return a list of business dicts for one text query (already website-filtered)."""
    payload = {
        "textQuery": query,
        "fieldMask": MAPS_FIELD_MASK,
        "maxResultCount": max(1, min(20, int(max_results or 10))),
    }
    obj = _composio_execute(slug, payload)
    if not obj:
        return []
    data = obj.get("data") or {}
    places = data.get("places") if isinstance(data, dict) else None
    if not isinstance(places, list):
        return []

    out = []
    for p in places:
        if not isinstance(p, dict):
            continue
        if str(p.get("businessStatus", "")).upper() == "CLOSED_PERMANENTLY":
            continue
        name = _place_name(p.get("displayName"))
        website = (p.get("websiteUri") or "").strip()
        if not name or not website:
            continue  # need both a name and a site to enrich; counted as skipped upstream
        out.append({
            "company_name": name,
            "website_url": website,
            "address": (p.get("formattedAddress") or "").strip(),
            "phone": (p.get("nationalPhoneNumber") or "").strip(),
        })
    return out


# --------------------------------------------------------------------------
# Step 2 — Firecrawl enrichment
# --------------------------------------------------------------------------

def _candidate_urls(website_url, scrape_pages):
    """Homepage first, then configured sub-paths (e.g. contact/about), de-duped."""
    base = website_url if website_url.endswith("/") else website_url + "/"
    urls, seen = [], set()
    for page in (scrape_pages or [""]):
        u = website_url if not page else urljoin(base, str(page).lstrip("/"))
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _firecrawl_markdown(url, slug):
    """Scrape one URL to markdown via Firecrawl; '' on any failure."""
    obj = _composio_execute(slug, {"url": url, "formats": ["markdown"]})
    if not obj:
        return ""
    data = obj.get("data")
    # Firecrawl nests the result under data.data.markdown; tolerate data.markdown too.
    for candidate in (data.get("data") if isinstance(data, dict) else None, data):
        if isinstance(candidate, dict):
            md = candidate.get("markdown")
            if isinstance(md, str) and md.strip():
                return md
    return ""


# --------------------------------------------------------------------------
# Step 3 — AI extraction
# --------------------------------------------------------------------------

def _extraction_prompt(company_name, website_url, markdown):
    text = (markdown or "")[:MAX_MARKDOWN_CHARS]
    return f"""You are extracting B2B contact details for a cold-outreach system from a company's own website text.

COMPANY: {company_name}
WEBSITE: {website_url}
WEBSITE TEXT (markdown, possibly truncated):
\"\"\"
{text}
\"\"\"

Return ONLY a JSON object with exactly these keys:
- "email": the best PUBLIC business contact email that literally appears in the text (e.g. info@, hello@, or a named person's address). Use "" if none appears. NEVER invent or guess an email.
- "first_name", "last_name", "title": a specific contact person ONLY if the text clearly names one with their role (founder, partner, manager, etc.); otherwise use "" for all three.
- "company_description": ONE concise sentence (max 25 words) describing what the company does, based only on the text.

Rules: use ONLY information present in the text; never fabricate an email or a person; if unsure, use "".
Reply with ONLY the JSON object — no prose, no code fence."""


def _clean_email(raw):
    """Trim artifacts an LLM or scraped page may wrap around an address.

    Handles a leading 'mailto:', angle brackets, surrounding whitespace, and
    trailing sentence punctuation (a real email never ends in .,;:>)). Defensive
    regardless of extractor — the provider isn't guaranteed to return it clean.
    """
    e = (raw or "").strip()
    if e.lower().startswith("mailto:"):
        e = e[len("mailto:"):]
    return e.strip().strip("<>").strip().rstrip(".,;:>)")


def _extract_lead(cfg, business, markdown):
    """Turn one scraped business into a lead dict (or None if unusable)."""
    prompt = _extraction_prompt(business["company_name"], business["website_url"], markdown)
    try:
        obj, _ = generate_json(cfg, prompt)
    except Exception as e:
        print(f"   ⚠️  extraction failed for {business['company_name']}: {e}")
        return None
    if not isinstance(obj, dict):
        return None

    email = _clean_email(obj.get("email"))
    lead = {
        "first_name": (obj.get("first_name") or "").strip(),
        "last_name": (obj.get("last_name") or "").strip(),
        "title": (obj.get("title") or "").strip(),
        "email": email,
        "company_name": business["company_name"],
        "company_description": (obj.get("company_description") or "").strip(),
        "website_url": business["website_url"],
    }
    return {k: lead.get(k, "") for k in FIELDS}


# --------------------------------------------------------------------------
# Orchestration — the public seam
# --------------------------------------------------------------------------

def _domain_key(url):
    """Normalized host (drops scheme + leading www) for de-duplication."""
    host = (urlparse(url).netloc or url).lower()
    return host[4:] if host.startswith("www.") else host


def _queries(cfg):
    """Explicit discovery.queries, else fall back to the client's target_niches."""
    disc = cfg.get("discovery") or {}
    queries = [q for q in (disc.get("queries") or []) if str(q).strip()]
    if queries:
        return queries
    return [q for q in (cfg.get("target_niches") or []) if str(q).strip()]


def load_leads(client_cfg):
    """Discover leads via Maps -> Firecrawl -> AI. Returns (leads, skipped).

    `skipped` counts businesses that were found but could not be turned into a
    usable lead (no website, scrape failed, or — when require_email is set — no
    email could be extracted), mirroring core/leads.py's skip semantics.
    """
    disc = client_cfg.get("discovery") or {}
    maps_slug = disc.get("maps_search_slug") or DEFAULT_MAPS_SLUG
    fc_slug = disc.get("firecrawl_scrape_slug") or DEFAULT_FIRECRAWL_SLUG
    max_results = disc.get("max_results", 10)
    max_leads = int(disc.get("max_leads", 25))
    require_email = disc.get("require_email", True)
    scrape_pages = disc.get("scrape_pages", ["", "contact"])

    queries = _queries(client_cfg)
    if not queries:
        raise DiscoveryError(
            "lead_source is 'maps_firecrawl' but no search queries are configured. "
            "Set discovery.queries (or target_niches) in your client config."
        )

    leads, skipped = [], 0
    seen_domains = set()

    for query in queries:
        if len(leads) >= max_leads:
            break
        print(f"   \U0001f5fa️  Maps search: {query!r}")
        businesses = _maps_search(query, max_results, maps_slug)
        print(f"      → {len(businesses)} business(es) with a website.")

        for biz in businesses:
            if len(leads) >= max_leads:
                break
            dkey = _domain_key(biz["website_url"])
            if dkey in seen_domains:
                continue  # same site surfaced by another query
            seen_domains.add(dkey)

            # Scrape homepage first; only fetch extra pages if no email yet (cost control).
            markdown, lead = "", None
            for url in _candidate_urls(biz["website_url"], scrape_pages):
                page_md = _firecrawl_markdown(url, fc_slug)
                if page_md:
                    markdown = page_md
                    lead = _extract_lead(client_cfg, biz, markdown)
                    if lead and _looks_like_email(lead["email"]):
                        break  # got a usable email — stop spending scrapes on this site

            if not lead:
                skipped += 1
                continue
            if require_email and not _looks_like_email(lead["email"]):
                print(f"      ⏭️  {biz['company_name']}: no public email found; skipping.")
                skipped += 1
                continue

            leads.append(lead)
            print(f"      ✅ {biz['company_name']} — {lead['email'] or '(no email)'}")

    return leads, skipped
