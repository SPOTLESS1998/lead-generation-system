#!/usr/bin/env bash
# The daily run, wrapped for unattended execution.
#
#     ./deploy/run_daily.sh                  # a real run
#     ./deploy/run_daily.sh --dry-run        # no writes, no email, no sync
#
# This exists because launchd (and cron) start a job with almost nothing: no shell
# profile, no PATH you would recognise, no virtualenv, no .env, and nowhere for output
# to go. A scheduled job that "works when I run it by hand" and silently fails at 8am
# is almost always one of those five things. So this script:
#
#   1. cd's to the repo, so relative paths behave
#   2. loads .env, so API keys and SMTP credentials exist
#   3. extends PATH for the Composio CLI (discovery shells out to it)
#   4. tees everything to logs/YYYY-MM-DD.log, so an unattended failure is diagnosable
#   5. prunes logs older than 30 days
#   6. exits with the run's real status, so launchd and the heartbeat see the truth
#
# It deliberately does NOT retry. A failed run leaves every lead banked and every
# claim released (see scripts/lead_agent.py), so tomorrow's run picks up cleanly —
# and a retry loop at 8am would just multiply a provider outage into four of them.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

CLIENT="${CLIENT:-ejentic}"
export CLIENT

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y-%m-%d).log"

# --- .env -------------------------------------------------------------------
# `set -a` exports everything defined while it is on, which is what the pipeline
# expects (it reads os.environ). Comments and blank lines are skipped by the shell.
if [ -f "$ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
fi

# --- PATH -------------------------------------------------------------------
# Discovery shells out to the Composio CLI, which lives in a user-level npm or
# homebrew prefix that launchd does not put on PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

PY="$ROOT/venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "[$(date '+%F %T')] FATAL: no venv python at $PY" | tee -a "$LOG_FILE"
    exit 127
fi

# --- run --------------------------------------------------------------------
{
    echo ""
    echo "################################################################"
    echo "# daily run  client=$CLIENT  $(date '+%F %T %Z')"
    echo "# host=$(hostname -s)  args=${*:-none}"
    echo "################################################################"
} >> "$LOG_FILE"

# PIPESTATUS, not $?, because the pipe through tee would otherwise report tee's
# success as the run's. This is the bug that makes a broken cron job look healthy.
"$PY" -u "$ROOT/scripts/daily_run.py" "$@" 2>&1 | tee -a "$LOG_FILE"
STATUS="${PIPESTATUS[0]}"

echo "[$(date '+%F %T')] exit status: $STATUS" >> "$LOG_FILE"

# --- prune ------------------------------------------------------------------
find "$LOG_DIR" -name '*.log' -type f -mtime +30 -delete 2>/dev/null || true

exit "$STATUS"
