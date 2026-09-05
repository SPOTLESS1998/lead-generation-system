#!/usr/bin/env bash
# Push newly drafted leads from this laptop up to the live approval dashboard.
#
#     ./deploy/sync_to_box.sh              # do it
#     ./deploy/sync_to_box.sh --dry-run    # show what would change, write nothing
#
# WHY THIS EXISTS
# The pipeline (discovery + drafting) runs here, because Composio/Firecrawl and
# the freellmapi gateway are authenticated on this machine. The approval server
# runs on the VPS, so it can answer prospect clicks with the laptop shut. The two
# therefore hold different halves of the state, and this moves the laptop's half
# up — WITHOUT trampling the VPS's half.
#
# It is an additive merge, never a copy: the VPS keeps its send history, its
# suppression (unsubscribe) list, its bookings, and any approve/decline you have
# already clicked. Anything already up there wins. See deploy/box_merge.py for
# the rules, including the check that refuses to re-queue an address that has
# unsubscribed.
#
# Safe to run repeatedly — running it twice in a row changes nothing the second
# time. No email is ever sent by this script.
set -euo pipefail

BOX="${BOX:-ubuntu@88.96.57.79}"
CLIENT="${CLIENT:-ejentic}"
REMOTE_DIR=/home/ubuntu/leadgen
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LOCAL_DB="$ROOT/data/$CLIENT/state.sqlite"
LOCAL_QUEUE="$ROOT/pending_leads.json"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

[ -f "$LOCAL_DB" ]    || { echo "no local DB at $LOCAL_DB"; exit 1; }
[ -f "$LOCAL_QUEUE" ] || { echo "no local queue at $LOCAL_QUEUE — draft something first"; exit 1; }

echo "==> Snapshotting the local state (SQLite backup API: consistent even mid-write)"
python3 - "$LOCAL_DB" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect("/tmp/leadgen_snapshot.sqlite")
src.backup(dst); dst.close()
n = sqlite3.connect("/tmp/leadgen_snapshot.sqlite").execute("SELECT COUNT(*) FROM leads").fetchone()[0]
print(f"    snapshot taken — {n} lead(s)")
PY

echo "==> Shipping to $BOX"
scp -q /tmp/leadgen_snapshot.sqlite "$BOX:/tmp/leadgen_snapshot.sqlite"
scp -q "$LOCAL_QUEUE"              "$BOX:/tmp/incoming_queue.json"
scp -q "$ROOT/deploy/box_merge.py" "$BOX:/tmp/box_merge.py"

echo "==> Backing up the VPS state, then merging"
# shellcheck disable=SC2029  # $DRY/$CLIENT are meant to expand locally
ssh "$BOX" "
  set -e
  TS=\$(date +%Y%m%d-%H%M%S)
  cp -a $REMOTE_DIR/data/$CLIENT/state.sqlite $REMOTE_DIR/data/$CLIENT/state.sqlite.bak.\$TS
  [ -f $REMOTE_DIR/pending_leads.json ] && cp -a $REMOTE_DIR/pending_leads.json $REMOTE_DIR/pending_leads.json.bak.\$TS || true
  echo \"    backups: *.bak.\$TS\"
  $REMOTE_DIR/venv/bin/python3 /tmp/box_merge.py $DRY \
    --incoming-db    /tmp/leadgen_snapshot.sqlite \
    --incoming-queue /tmp/incoming_queue.json \
    --live-db        $REMOTE_DIR/data/$CLIENT/state.sqlite \
    --live-queue     $REMOTE_DIR/pending_leads.json
  rm -f /tmp/leadgen_snapshot.sqlite /tmp/incoming_queue.json /tmp/box_merge.py
"

rm -f /tmp/leadgen_snapshot.sqlite
echo ""
echo "Done. Review the drafts at https://audit.ejentic.xyz  (user: ejentic)"
