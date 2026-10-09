#!/usr/bin/env bash
# Daily domain warm-up send, wrapped for unattended cron execution.
#
#     ./deploy/run_warmup_box.sh              # today's warm-up batch (real send)
#     ./deploy/run_warmup_box.sh --dry-run    # show the plan, send nothing
#     ./deploy/run_warmup_box.sh --status     # day / cap / used / left
#
# WHY THIS EXISTS (same reasons as run_daily.sh): cron starts a job with almost no
# environment — no shell profile, no PATH, no virtualenv, no .env, nowhere for
# output to go. A job that "works by hand" but silently dies at 9am is nearly
# always one of those. So this wrapper:
#   1. cd's to the repo so relative paths behave
#   2. loads .env so the Zoho SMTP credentials exist
#   3. tees output to logs/warmup-YYYY-MM-DD.log so a failure is diagnosable
#   4. exits with the python's real status (not tee's) so cron/heartbeat see truth
#
# BOUNDARY: warm-up does NOT touch the prospect pipeline, the leads DB, the sends
# table, suppression, or Composio. It only mails the consenting seed list in
# clients/<client>/warmup_seeds.txt via core.sender.smtp_deliver. The "never email
# a stranger" fuse is unaffected. See scripts/warmup_send.py for the full contract.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

CLIENT="${CLIENT:-ejentic}"
export CLIENT

# --- .env (Zoho SMTP credentials) ------------------------------------------
set -a
# shellcheck disable=SC1091
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
set +a

# --- logging ---------------------------------------------------------------
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/warmup-$(date +%Y-%m-%d).log"

{
  echo "===== warm-up run $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
  "$ROOT/venv/bin/python3" scripts/warmup_send.py "$@"
} 2>&1 | tee -a "$LOG_FILE"
status="${PIPESTATUS[0]}"   # python's status, not tee's

# keep the log dir tidy: drop warm-up logs older than 30 days
find "$LOG_DIR" -name 'warmup-*.log' -mtime +30 -delete 2>/dev/null || true

exit "$status"
