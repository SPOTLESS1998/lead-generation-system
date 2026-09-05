"""Read-only live-sourcing preview — the "watch it find real leads" demo clip.

Runs the configured auto-discovery source (Google Maps or Yellow Pages) exactly
like the real agent's sourcing step, prints the businesses + emails it finds, and
STOPS. It never writes state, never drafts, never sends — so it's 100% safe to run
live on camera and can be re-run as many times as you like.

Usage (all env-driven, so no code edits between takes):

    # Google Maps (default source for the ejentic client)
    CLIENT=ejentic \
    PREVIEW_QUERY="digital marketing agencies in Lagos, Nigeria" \
    PREVIEW_MAX=5 \
    venv/bin/python scripts/discover_preview.py

    # Yellow Pages
    PREVIEW_SOURCE=yellowpages \
    PREVIEW_QUERY="accounting firms" PREVIEW_LOCATION="Lagos" \
    PREVIEW_MAX=5 \
    venv/bin/python scripts/discover_preview.py

Env knobs:
    CLIENT           which client config to borrow (default "ejentic")
    PREVIEW_SOURCE   "maps_firecrawl" | "yellowpages"  (default: the client's lead_source)
    PREVIEW_QUERY    a single search to run (keeps the clip short); omit to use the config's own queries
    PREVIEW_LOCATION geo filter, mainly for Yellow Pages (e.g. "Lagos")
    PREVIEW_SERVICE  the Ejentic offering to tag these leads with (shown per lead)
    PREVIEW_MAX      cap on leads to return (default 5 — keep the clip snappy)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config   # noqa: E402


def main():
    cfg = config.load_client()

    source = os.environ.get("PREVIEW_SOURCE") or cfg.get("lead_source") or "maps_firecrawl"
    query = os.environ.get("PREVIEW_QUERY", "").strip()
    location = os.environ.get("PREVIEW_LOCATION", "").strip()
    # Default to the active tenant's FIRST offering, never a hardcoded service name
    # (MULTITENANCY.md) — a house default here would tag another client's preview
    # leads with a service they don't sell.
    _own = config.tenant_offerings(cfg)
    service = (os.environ.get("PREVIEW_SERVICE") or (_own[0] if _own else "")).strip()
    max_leads = int(os.environ.get("PREVIEW_MAX", "5"))

    # A single-query override keeps the on-camera clip short and predictable.
    # Segments take precedence everywhere, so setting one is the safe way to scope.
    seg = [{"name": "Preview", "service": service, "queries": [query]}] if query else []

    print("=" * 64)
    print("  EJENTIC AI — LIVE LEAD SOURCING (read-only preview)")
    print("=" * 64)
    print(f"  Source   : {source}")
    if query:
        print(f"  Search   : {query}" + (f"  ({location})" if location else ""))
    print(f"  Max leads: {max_leads}")
    print("-" * 64)

    if source == "yellowpages":
        from core import yellowpages as src
        yp = cfg["yellowpages"]
        if seg:
            yp["segments"] = seg
            yp["queries"] = []
        if location:
            yp["location"] = location
        yp["max_leads"] = max_leads
        yp["max_listings_per_query"] = max(max_leads * 2, 10)
    else:
        from core import discovery as src
        dsc = cfg["discovery"]
        if seg:
            dsc["segments"] = seg
            dsc["queries"] = []
        dsc["max_leads"] = max_leads
        dsc["max_results"] = max(max_leads * 2, 8)

    leads, skipped = src.load_leads(cfg)

    print("-" * 64)
    print(f"  ✅ Sourced {len(leads)} real business(es); {skipped} skipped (no site/email).")
    print("=" * 64)
    for i, ld in enumerate(leads, 1):
        name = ld.get("company_name", "?")
        email = ld.get("email") or "(no public email)"
        site = ld.get("website_url", "")
        svc = ld.get("ejentic_service", "")
        print(f"  {i}. {name}")
        print(f"       email  : {email}")
        print(f"       website: {site}")
        if svc:
            print(f"       matched: {svc}")
    print("=" * 64)
    print("  (Preview only — nothing was saved, drafted, or sent.)")


if __name__ == "__main__":
    main()
