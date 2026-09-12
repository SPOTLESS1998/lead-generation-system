"""The daily run — one command, every step, unattended.

    CLIENT=ejentic venv/bin/python scripts/daily_run.py
    CLIENT=ejentic venv/bin/python scripts/daily_run.py --dry-run
    CLIENT=ejentic venv/bin/python scripts/daily_run.py --skip-sync

WHAT A RUN DOES, IN ORDER
  1. preflight   — refuse to run against a config that could email a real prospect
  2. source      — discover leads and BANK every qualified one (the lead list grows)
  3. draft       — write copy for up to drafting.daily_cap of them, into the queue
  4. observe     — one self-healing pass over the ledger (faults, stalls, recovery)
  5. sync        — push the drafts up to the always-on approval dashboard
  6. digest      — one email summarising the run

WHY IT IS ONE PROCESS AND NOT A SHELL CHAIN
Each step is called in-process so a failure is caught, attributed to a named step, and
reported honestly in the digest and the exit code. A shell `&&` chain would stop at the
first failure and tell you nothing beyond a non-zero status; a `;` chain would hide it
entirely. Steps that fail are recorded and the run continues to the ones that can still
usefully run — a sync failure should not stop the digest that tells you the sync failed.

SAFETY
This job NEVER sends email to a prospect. It drafts, queues, and notifies. The only
route from a draft to a prospect's inbox is a human clicking approve on the dashboard.
preflight() enforces that by refusing to run unless sending.mode is 'controlled' — so a
config left in 'live' by accident stops the run loudly instead of quietly mailing
strangers.

WHERE IT RUNS
Built to run anywhere; today it is scheduled on the Mac (see deploy/), because Composio
and the freellmapi gateway are authenticated there. --skip-sync exists so the same
command works unchanged once the pipeline moves to the always-on box, where discovery
and the dashboard share a filesystem and there is nothing to sync.
"""

import warnings
warnings.filterwarnings('ignore')

import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # repo root -> `core`
sys.path.insert(0, HERE)                     # scripts/  -> `lead_agent`, `observer_agent`

from core import config, state, budget
from core import observability as obs
import lead_agent
import observer_agent


# Environment variables the pipeline genuinely cannot run without. Names only — the
# values are never read here, and never logged.
REQUIRED_ENV = ("SMTP_USER", "SMTP_PASS")


def print_step(step):
    print(f"\n{'='*62}\n{step}\n{'='*62}")


class RunReport:
    """What happened, assembled for the digest and the exit code.

    Kept deliberately dumb: a list of steps with a status each, plus the counts the
    digest shows. Anything that failed carries its error text so the digest can tell
    the operator what broke rather than just that something did.
    """

    def __init__(self):
        self.steps = []          # [{name, status, detail, error}]
        self.sourced = 0
        self.drafted = 0
        self.lead_counts = {}
        self.faults = {}

    def add(self, name, status, detail="", error=""):
        self.steps.append({"name": name, "status": status,
                           "detail": detail, "error": error})
        icon = {"ok": "✅", "failed": "❌", "skipped": "⏭️"}.get(status, "•")
        line = f"{icon} {name}"
        if detail:
            line += f" — {detail}"
        print(line)
        if error:
            print(f"     {error}")

    @property
    def failed(self):
        return [s for s in self.steps if s["status"] == "failed"]

    @property
    def ok(self):
        return not self.failed


# --------------------------------------------------------------------------
# 1. Preflight
# --------------------------------------------------------------------------

def preflight(cfg):
    """Refuse to run in any configuration that could reach a real prospect.

    Returns a list of problem strings; empty means safe to proceed. Each check is a
    hard stop rather than a warning: an unattended job that quietly emails strangers
    because a config said `live` is the worst outcome this system has.
    """
    problems = []

    mode = (cfg.get("sending") or {}).get("mode")
    if mode != "controlled":
        problems.append(
            f"sending.mode is '{mode}', not 'controlled'. This job drafts for human "
            f"approval and must never deliver to a prospect unattended. Set it back to "
            f"'controlled' in clients/{cfg['client']}/config.json."
        )

    for key in REQUIRED_ENV:
        if not os.environ.get(key):
            problems.append(f"{key} is not set in the environment — the pipeline cannot "
                            f"send its approval/digest mail without it.")

    return problems


# --------------------------------------------------------------------------
# 2/3. The pipeline itself (discovery + banking + drafting)
# --------------------------------------------------------------------------

def run_pipeline(cfg, report, conn):
    """Run lead_agent.main(), then read back what it did.

    main() does its own discovery, banking and drafting (it is the one shared recipe —
    see lead_agent.draft_one_lead). We call it rather than re-implementing the order
    here, and read the resulting counts from the DB afterwards so the digest reports
    reality rather than our expectations.
    """
    print_step("📇 Source + draft")
    run_id = obs.new_run_id()

    # Count what exists BEFORE, so "sourced"/"drafted" are deltas for this run and not
    # cumulative totals that grow forever.
    before = state.count_leads_by_status(conn, cfg["client"])
    lead_agent.main()
    after = state.count_leads_by_status(conn, cfg["client"])

    report.sourced = max(0, (after.get(state.SOURCED, 0) + after.get(state.AWAITING_APPROVAL, 0))
                         - (before.get(state.SOURCED, 0) + before.get(state.AWAITING_APPROVAL, 0)))
    report.drafted = max(0, after.get(state.AWAITING_APPROVAL, 0)
                         - before.get(state.AWAITING_APPROVAL, 0))
    report.lead_counts = after
    report.add("source+draft", "ok",
               f"sourced {report.sourced}, drafted {report.drafted}; "
               f"list now {sum(after.values())} lead(s)")
    return run_id


# --------------------------------------------------------------------------
# 4. Observability / self-healing pass
# --------------------------------------------------------------------------

def run_observe(cfg, conn):
    """One observer pass: flag faults and stalls, resolve recovered, heal the rest.

    Called directly rather than shelling out to observer_agent --once, so a fault here
    is attributed to this run instead of vanishing into a subprocess.
    """
    print_step("🔭 Observability + self-healing pass")
    summary = observer_agent.run_once(cfg, conn)
    counts = state.fault_counts(conn, cfg["client"])
    open_like = sum(v for k, v in counts.items() if k in ("open", "healing", "escalated"))
    return summary, counts, open_like


# --------------------------------------------------------------------------
# 5. Sync to the always-on dashboard
# --------------------------------------------------------------------------

def run_sync(cfg, report, dry_run=False, skip=False):
    """Push drafts + DB to the box so the operator can approve from anywhere.

    Failure here is NOT fatal: the drafts exist and the run did its job. It is
    recorded and surfaced, because a sync that silently stopped working would leave
    you approving nothing while the pipeline looked healthy.
    """
    print_step("☁️  Sync to the approval dashboard")
    if skip:
        report.add("sync", "skipped", "--skip-sync (running where the dashboard lives)")
        return

    script = os.path.join(cfg["paths"]["root"], "deploy", "sync_to_box.sh")
    if not os.path.exists(script):
        report.add("sync", "skipped", "no deploy/sync_to_box.sh in this checkout")
        return
    if dry_run:
        report.add("sync", "skipped", "--dry-run")
        return

    import subprocess
    try:
        proc = subprocess.run(["bash", script], capture_output=True, text=True, timeout=600)
        if proc.returncode == 0:
            tail = [l for l in (proc.stdout or "").strip().splitlines() if l.strip()][-1:]
            report.add("sync", "ok", tail[0].strip() if tail else "pushed")
        else:
            report.add("sync", "failed", "sync_to_box.sh exited non-zero",
                       (proc.stderr or proc.stdout or "").strip()[-400:])
    except Exception as e:
        report.add("sync", "failed", "sync failed", str(e)[:400])


# --------------------------------------------------------------------------
# 6. Digest
# --------------------------------------------------------------------------

def run_digest(cfg, report, dry_run=False):
    print_step("✉️  Morning digest")
    if dry_run:
        report.add("digest", "skipped", "--dry-run")
        return
    try:
        from core import digest
        sent = digest.send_run_digest(cfg, report)
        report.add("digest", "ok" if sent else "failed",
                   "emailed to the operator" if sent else "not sent")
    except Exception as e:
        report.add("digest", "failed", "digest failed", str(e)[:400])


# --------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    skip_sync = "--skip-sync" in sys.argv
    started = time.time()

    cfg = config.load_client()
    print("=" * 62)
    print(f"🌅 {cfg['client_name']} — Daily Run" + ("   [DRY RUN]" if dry_run else ""))
    print(f"   client={cfg['client']}  send_mode={cfg['sending']['mode']}  "
          f"timezone={cfg.get('timezone')}")
    print(f"   started {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 62)

    report = RunReport()

    # --- 1. preflight ---
    print_step("🔒 Preflight")
    problems = preflight(cfg)
    if problems:
        for p in problems:
            print(f"   ❌ {p}")
        report.add("preflight", "failed", f"{len(problems)} blocking problem(s)",
                   "; ".join(problems))
        print(f"\n⛔ Refusing to run. Fix the above and re-run.")
        _finish(cfg, report, started, dry_run=True)   # no digest on a refused run
        return 2

    report.add("preflight", "ok",
               f"controlled mode, {len(REQUIRED_ENV)} env key(s) present")

    conn = state.connect(cfg["paths"]["db"])
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        # --- 2 + 3. source and draft ---
        try:
            run_pipeline(cfg, report, conn)
        except Exception as e:
            report.add("source+draft", "failed", "pipeline raised", str(e)[:400])
            traceback.print_exc()

        # --- 4. observe ---
        try:
            _summary, faults, open_like = run_observe(cfg, conn)
            report.faults = faults
            report.add("observe", "ok",
                       f"faults {faults or '{}'} (open/healing/escalated: {open_like})")
        except Exception as e:
            report.add("observe", "failed", "observer pass raised", str(e)[:400])

        # --- 5. sync ---
        run_sync(cfg, report, dry_run=dry_run, skip=skip_sync)

        # --- 6. digest ---
        run_digest(cfg, report, dry_run=dry_run)

        # Record the run LAST, so the row reflects the whole outcome. This is the row
        # scripts/heartbeat.py looks for: without it, a run that never happened writes
        # nothing at all and looks exactly like a healthy quiet morning.
        if not dry_run:
            try:
                state.record_run(
                    conn, cfg["client"],
                    "ok" if report.ok else "failed",
                    sourced=report.sourced, drafted=report.drafted,
                    failed_steps=[s["name"] for s in report.failed],
                    detail="; ".join(f"{s['name']}: {s['detail']}" for s in report.failed) or None,
                    started_at=started_iso)
            except Exception as e:
                print(f"⚠️  could not record the run ({e}) — heartbeat may report a missed run.")
    finally:
        conn.close()

    _finish(cfg, report, started, dry_run=dry_run)
    return 0 if report.ok else 1


def _finish(cfg, report, started, dry_run=False):
    elapsed = time.time() - started
    print("\n" + "=" * 62)
    if report.ok:
        print(f"🌅 Daily run complete in {elapsed:.0f}s — {len(report.steps)} step(s), no failures.")
    else:
        print(f"⚠️  Daily run finished in {elapsed:.0f}s with {len(report.failed)} failure(s):")
        for s in report.failed:
            print(f"     ❌ {s['name']}: {s['detail']}{(' — ' + s['error']) if s['error'] else ''}")
    print("=" * 62)


if __name__ == "__main__":
    sys.exit(main())
