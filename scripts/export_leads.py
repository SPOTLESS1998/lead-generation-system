"""Export the lead list to CSV — the "good source later" the list is being built for.

    CLIENT=ejentic venv/bin/python scripts/export_leads.py
    CLIENT=ejentic venv/bin/python scripts/export_leads.py --status sourced
    CLIENT=ejentic venv/bin/python scripts/export_leads.py --out /tmp/leads.csv

Read-only: it never writes to the database and never sends anything.

WHERE IT WRITES, AND WHY IT MATTERS
The default destination is data/<client>/leads_export.csv, because `data/` is
gitignored. Real prospects' names and email addresses must never be committed —
this repo's own README promises that every lead in it is fabricated. Note that
clients/<client>/leads.csv is a TRACKED input file (the curated-CSV lead source),
so this deliberately does not write there.
"""

import os
import sys
import csv
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state

# Contact details first (what you'd actually use), provenance after.
COLUMNS = ["company_name", "first_name", "last_name", "title", "email",
           "website_url", "website_domain", "niche", "status",
           "company_description", "company_facts", "created_at"]


def export(conn, client, out_path, status=None):
    """Write the lead list to `out_path`. Returns (rows_written, {status: count})."""
    sql = "SELECT * FROM leads WHERE client=?"
    params = [client]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at ASC, id ASC"
    rows = conn.execute(sql, params).fetchall()

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: (dict(r).get(c) or "") for c in COLUMNS})

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return len(rows), counts


def main():
    ap = argparse.ArgumentParser(description="Export the lead list to CSV.")
    ap.add_argument("--status", help="only this status (e.g. sourced, queued, sent)")
    ap.add_argument("--out", help="destination CSV (default: data/<client>/leads_export.csv)")
    args = ap.parse_args()

    cfg = config.load_client()
    client = cfg["client"]
    out = args.out or os.path.join(os.path.dirname(cfg["paths"]["db"]), "leads_export.csv")

    conn = state.connect(cfg["paths"]["db"])
    try:
        n, counts = export(conn, client, out, args.status)
    finally:
        conn.close()

    print(f"📤 Exported {n} lead(s) → {out}")
    if counts:
        print(f"   by status: {counts}")
    if not n:
        print("   (the list is empty — run the pipeline to source some leads first)")


if __name__ == "__main__":
    main()
