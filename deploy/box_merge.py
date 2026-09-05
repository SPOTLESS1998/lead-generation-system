#!/usr/bin/env python3
"""Additively merge a laptop snapshot of the lead-gen state into the live VPS state.

RUN THIS ON THE VPS. `deploy/sync_to_box.sh` (on the laptop) takes the snapshot,
ships it here, and invokes this.

WHY A MERGE AND NOT A COPY
--------------------------
The two machines are authorities over different things:

  the VPS owns what HAS HAPPENED     sends, suppression (unsubscribes), bookings,
                                     and the approve/decline decisions made in the
                                     browser (the `status` in pending_leads.json)
  the laptop owns what was FOUND     newly discovered leads, freshly drafted copy,
                                     and the magnet pages generated alongside them

A blind copy in either direction destroys the other side's truth. Copying the
laptop over the VPS is the dangerous one: it would wipe the suppression list and
re-queue people who had already clicked unsubscribe — a CAN-SPAM violation, not
merely a lost row — and erase the send history the daily cap is computed from,
which invites double-sends.

So every operation here is an INSERT of something missing. Nothing is ever
updated or deleted, and whatever the VPS already knows always wins.

NEVER TOUCHED: sends, suppression, appointments/bookings, and the observability
ledger. Those are VPS-only by definition.

    python3 box_merge.py --incoming-db /tmp/in.sqlite \
                         --incoming-queue /tmp/pending_leads.json \
                         --live-db  /home/ubuntu/leadgen/data/ejentic/state.sqlite \
                         --live-queue /home/ubuntu/leadgen/pending_leads.json
    # add --dry-run to see the plan without writing anything
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile


def cols(conn, table):
    """Column names of `table`, or an empty list if it doesn't exist."""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def shared_cols(a, b, table, drop=("id",)):
    """Columns present in BOTH databases, so a schema drift between the laptop
    and the VPS narrows the copy instead of raising. `id` is dropped so the
    destination assigns its own primary keys."""
    common = [c for c in cols(a, table) if c in set(cols(b, table)) and c not in drop]
    return common


def merge_table(src, dst, table, key_cols, skip_emails=(), email_col=None, dry=False):
    """Insert rows from src whose key is absent in dst. Returns (added, skipped_existing,
    skipped_suppressed). Existing destination rows are never modified."""
    use = shared_cols(src, dst, table)
    if not use:
        return 0, 0, 0

    existing = {tuple(r) for r in dst.execute(f"SELECT {','.join(key_cols)} FROM {table}")}
    added = seen = suppressed = 0
    sel = ",".join(use)
    placeholders = ",".join("?" * len(use))

    for row in src.execute(f"SELECT {sel} FROM {table}"):
        rec = dict(zip(use, row))
        key = tuple(rec[k] for k in key_cols)
        if key in existing:
            seen += 1
            continue
        # Never resurrect someone who opted out on the VPS.
        if email_col and str(rec.get(email_col, "")).lower() in skip_emails:
            suppressed += 1
            continue
        if not dry:
            dst.execute(f"INSERT INTO {table} ({sel}) VALUES ({placeholders})", row)
        existing.add(key)
        added += 1
    return added, seen, suppressed


def write_json_atomic(path, data):
    """Write via temp file + rename so a reader (the running Flask app) never
    observes a half-written queue."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pending_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        os.path.exists(tmp) and os.unlink(tmp)
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incoming-db", required=True)
    ap.add_argument("--incoming-queue", required=True)
    ap.add_argument("--live-db", required=True)
    ap.add_argument("--live-queue", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    for p in (a.incoming_db, a.incoming_queue, a.live_db):
        if not os.path.exists(p):
            sys.exit(f"missing: {p}")

    src = sqlite3.connect(a.incoming_db)
    dst = sqlite3.connect(a.live_db)

    # The opt-out list is the VPS's, and it is the veto on everything below.
    suppressed = {str(e).lower() for (e,) in dst.execute("SELECT email FROM suppression")}
    print(f"  VPS suppression list: {len(suppressed)} address(es) — these are never re-queued")

    mode = "DRY RUN — nothing will be written" if a.dry_run else "merging"
    print(f"  {mode}\n")

    print("  database (insert-only):")
    for table, keys, ecol in (("leads", ("client", "email"), "email"),
                              ("magnets", ("client", "token"), "lead_email")):
        added, seen, supp = merge_table(src, dst, table, keys,
                                        skip_emails=suppressed, email_col=ecol, dry=a.dry_run)
        note = f", {supp} skipped (unsubscribed)" if supp else ""
        print(f"    {table:<10} +{added} new, {seen} already on the VPS{note}")

    # The approve/decline queue. An id already on the VPS keeps the VPS's copy:
    # its `status` may already be approved_and_sent or declined.
    incoming = json.load(open(a.incoming_queue))
    live = json.load(open(a.live_queue)) if os.path.exists(a.live_queue) else {}
    q_added = q_kept = q_supp = 0
    for lead_id, entry in incoming.items():
        if lead_id in live:
            q_kept += 1
        elif str(entry.get("target_email", "")).lower() in suppressed:
            q_supp += 1
        else:
            live[lead_id] = entry
            q_added += 1
    note = f", {q_supp} skipped (unsubscribed)" if q_supp else ""
    print(f"\n  approval queue:")
    print(f"    drafts     +{q_added} new, {q_kept} already on the VPS{note}")

    if a.dry_run:
        print("\n  dry run — no changes written.")
        return

    dst.commit()
    write_json_atomic(a.live_queue, live)
    pending = sum(1 for e in live.values() if e.get("status") == "pending")
    print(f"\n  ✅ merged. queue now holds {len(live)} entries, {pending} pending approval.")


if __name__ == "__main__":
    main()
