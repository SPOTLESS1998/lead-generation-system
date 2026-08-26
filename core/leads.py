"""Curated lead source: reads clients/<name>/leads.csv into lead dicts.

CSV is used because it's the easiest format to fill from an Apollo/ContactOut
export or a spreadsheet. This module is the seam where a live Apollo
`mixed_people/search` integration plugs in later — swap load_leads() and the
rest of the pipeline is unchanged.
"""

import os
import csv

FIELDS = ["first_name", "last_name", "title", "email",
          "company_name", "company_description", "website_url",
          "ejentic_service",   # which Ejentic offering this prospect was matched to
                               # (auto-discovery sets it; CSV rows may leave it blank)
          "company_facts"]     # richer grounding facts for the opener (services listed +
                               # one verifiable detail + a review signal); auto-discovery
                               # fills it, a missing CSV column safely degrades to ""


def _looks_like_email(value):
    return "@" in value and "." in value.split("@")[-1]


def load_leads(client_cfg):
    """Return (leads, skipped) — valid lead dicts, and count of ignored rows.

    Rows are ignored if the email is blank, a template placeholder
    (starts with 'REPLACE-ME' or '#'), or not a plausible address.
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
                    or not _looks_like_email(email):
                skipped += 1
                continue
            leads.append({k: (row.get(k) or "").strip() for k in FIELDS})
    return leads, skipped
