"""Audit the banked lead list for deliverability — read-only unless told otherwise.

Before flipping `sending.mode` to 'live' you want one number: of the leads sitting
on the list, how many can actually receive mail? A bounce rate above a few percent
throttles or blocklists a sending domain, and on a BRAND-NEW domain — which has no
reputation to spend — the first few days of bounces are the most expensive ones you
will ever send. Finding the dead addresses now costs a DNS query each; finding them
after the flip costs the domain.

What this proves and what it does not:
  * It proves the DOMAIN has somewhere to put mail (MX, or an A record acting as an
    implicit MX per RFC 5321), and catches reserved/placeholder domains and RFC 7505
    null-MX domains that explicitly refuse mail.
  * It does NOT prove the individual mailbox exists. That needs SMTP probing, which
    core/verify.py deliberately refuses to do — see the note there.

'unknown' is not a failure grade. It means OUR lookup did not complete, and it is
reported separately from 'invalid' precisely so a flaky network never gets read as a
dead list.

Run:
    venv/bin/python scripts/verify_leads.py                 # report only
    venv/bin/python scripts/verify_leads.py --status sourced
    venv/bin/python scripts/verify_leads.py --suppress-invalid   # WRITES
"""

import warnings
warnings.filterwarnings('ignore')

import os
import sys
import argparse
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import config, state, verify, suppression   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Check the lead list for deliverability.")
    ap.add_argument("--status", default=None,
                    help="only leads in this lifecycle status (default: all)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N leads")
    ap.add_argument("--timeout", type=float, default=5.0, help="seconds per DNS query")
    ap.add_argument("--no-cache", action="store_true",
                    help="re-resolve every domain instead of trusting cached verdicts")
    ap.add_argument("--suppress-invalid", action="store_true",
                    help="WRITES: add every proven-undeliverable address to the "
                         "never-contact list so it is never drafted or sent again")
    ap.add_argument("--show", choices=["invalid", "unknown", "all", "none"],
                    default="invalid", help="which addresses to list individually")
    args = ap.parse_args()

    cfg = config.load_client()
    client = cfg["client"]
    conn = state.connect(cfg["paths"]["db"])

    sql = "SELECT email, company_name, status FROM leads WHERE client=?"
    params = [client]
    if args.status:
        sql += " AND status=?"
        params.append(args.status)
    sql += " ORDER BY company_name"
    rows = conn.execute(sql, tuple(params)).fetchall()
    if args.limit:
        rows = rows[:args.limit]

    print("=" * 62)
    print(f"📮 Deliverability audit — {cfg['client_name']}  ({len(rows)} lead(s))")
    print(f"   domain-level only: MX / implicit-MX. Mailbox existence is NOT checked.")
    print("=" * 62)
    if not rows:
        print("\nNo leads matched.")
        return 0

    counts = Counter()
    per_status = Counter()
    listed = {"invalid": [], "unknown": [], "ok": []}
    # Verdicts are cached per DOMAIN, so a list of 50 leads across 30 domains
    # costs 30 queries on the first pass and ~0 on the next.
    for r in rows:
        v = verify.verify_email(r["email"], conn, timeout=args.timeout,
                                use_cache=not args.no_cache)
        counts[v["status"]] += 1
        per_status[(r["status"], v["status"])] += 1
        listed[v["status"]].append((r["email"], r["company_name"], v["reason"]))

    total = len(rows)
    def pct(n):
        return f"{(100.0 * n / total):.0f}%" if total else "—"

    print(f"\n  ✅ deliverable : {counts['ok']:>4}  ({pct(counts['ok'])})")
    print(f"  ❌ undeliverable: {counts['invalid']:>4}  ({pct(counts['invalid'])})  "
          f"<- these WILL bounce")
    print(f"  ❓ unknown      : {counts['unknown']:>4}  ({pct(counts['unknown'])})  "
          f"<- our lookup failed; NOT a verdict on the lead")

    if args.show in ("invalid", "all") and listed["invalid"]:
        print("\n  Undeliverable:")
        for email, company, reason in listed["invalid"]:
            print(f"    ❌ {email:<38} {(company or '')[:24]:<24} {reason}")
    if args.show in ("unknown", "all") and listed["unknown"]:
        print("\n  Could not verify:")
        for email, company, reason in listed["unknown"]:
            print(f"    ❓ {email:<38} {(company or '')[:24]:<24} {reason}")

    if counts["unknown"]:
        print(f"\n  ⚠️  {counts['unknown']} address(es) could not be checked. That is a "
              f"statement about this machine's DNS, not about those leads — re-run "
              f"before drawing a conclusion. They are NOT suppressed.")

    if args.suppress_invalid:
        if not listed["invalid"]:
            print("\n  Nothing to suppress.")
        else:
            n = 0
            for email, _company, reason in listed["invalid"]:
                # 'bounced' is the existing never-contact reason that means
                # "this address cannot receive mail". Reusing it keeps one
                # vocabulary rather than inventing a parallel one.
                suppression.add(conn, client, email, reason="bounced")
                n += 1
            print(f"\n  🚫 Suppressed {n} undeliverable address(es) — they will never "
                  f"be drafted or sent again.")
    elif listed["invalid"]:
        print(f"\n  Nothing was changed. Re-run with --suppress-invalid to put those "
              f"{len(listed['invalid'])} address(es) on the never-contact list.")

    # The number that actually gates the go-live decision.
    if total:
        bounce_risk = 100.0 * counts["invalid"] / total
        verdict = ("🟢 well under the ~2% that hurts a new domain" if bounce_risk < 2
                   else "🟡 high enough to hurt a new domain — suppress before going live"
                   if bounce_risk < 5 else
                   "🔴 high enough to get a new domain blocklisted — do NOT flip live")
        print(f"\n  Projected hard-bounce rate if sent as-is: {bounce_risk:.1f}%  {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
