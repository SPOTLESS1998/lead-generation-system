"""Curated lead source: reads clients/<name>/leads.csv into lead dicts.

CSV is used because it's the easiest format to fill from an Apollo/ContactOut
export or a spreadsheet. This module is the seam where a live Apollo
`mixed_people/search` integration plugs in later — swap load_leads() and the
rest of the pipeline is unchanged.
"""

import os
import csv
from urllib.parse import urlparse

from . import verify

FIELDS = ["first_name", "last_name", "title", "email",
          "company_name", "company_description", "website_url",
          "ejentic_service",   # which Ejentic offering this prospect was matched to
                               # (auto-discovery sets it; CSV rows may leave it blank)
          "company_facts"]     # richer grounding facts for the opener (services listed +
                               # one verifiable detail + a review signal); auto-discovery
                               # fills it, a missing CSV column safely degrades to ""


def _looks_like_email(value):
    return "@" in value and "." in value.split("@")[-1]


def domain_key(url):
    """Normalized host (no scheme, no leading www) — the de-duplication key for a
    business's website.

    Lives here rather than in core/discovery.py because two callers now need to agree
    on it exactly: discovery de-dupes the businesses found within one run, and
    core/state.py stores the same key so a LATER run can recognise a business it has
    already paid to scrape. If the two ever disagreed, the cross-run skip would
    silently stop working and we would quietly re-buy leads we already own.

    Returns "" for a missing or unparseable URL — an empty key never matches, so a
    lead with no website is simply never skipped as a duplicate.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    host = (urlparse(raw).netloc or raw).lower().strip()
    host = host.split("@")[-1].split(":")[0]      # drop any userinfo / :port
    return host[4:] if host.startswith("www.") else host


def load_leads(client_cfg):
    """Return (leads, skipped) — valid lead dicts, and count of ignored rows.

    Rows are ignored if the email is blank, a template placeholder
    (starts with 'REPLACE-ME' or '#'), not a plausible address, or on a reserved /
    unroutable domain.

    That last check used to be missing here, and the asymmetry mattered: the
    scraped paths (core/discovery.py, core/yellowpages.py) both refuse
    contact@example.com at extraction, but the CSV path validated only with
    _looks_like_email — "@" plus a dotted domain — which example.com passes
    happily. So a hand-curated or exported list could bank an address guaranteed
    to hard-bounce, and a bounce is exactly what costs a new sending domain its
    reputation. Same guard, same list, every entry route: core/verify.
    """
    path = client_cfg["paths"]["leads_csv"]
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Curated leads file not found: {path}\n"
            f"Add your real contacts there (see clients/_template/leads.csv)."
        )

    leads, skipped = [], 0
    # utf-8-sig tolerates the BOM that Excel/Sheets exports often include.
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            email = (row.get("email") or "").strip()
            if not email or email.startswith("#") or email.upper().startswith("REPLACE-ME") \
                    or not _looks_like_email(email) or verify.is_placeholder(email):
                skipped += 1
                continue
            leads.append({k: (row.get(k) or "").strip() for k in FIELDS})
    return leads, skipped
