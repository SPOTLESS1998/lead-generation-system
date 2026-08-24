import warnings
warnings.filterwarnings('ignore')

import os
import sys
import time
from datetime import datetime, timedelta, timezone

# Make the project root importable so `core` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state, healer
from core import observability as obs

# Safety valve: most faults worked in a single pass — a runaway backstop far above
# any realistic number of simultaneous problems.
MAX_PER_PASS = 50

# Steps the observer must NOT open faults for. 'heal' is the healer's own metered
# work and 'observe' is this agent — flagging those would let the watcher try to
# heal itself in a loop. A persistent heal failure still surfaces in the ledger.
_NON_FAULT_STEPS = {"heal", "observe"}


def print_step(step):
    print(f"\n[{time.strftime('%H:%M:%S')}] {step}")


# --------------------------------------------------------------------------
# 1. FLAG — turn error events and stalled leads into faults to work.
# --------------------------------------------------------------------------

def flag_error_faults(cfg, conn):
    """Scan new error events (past the watermark) and open one fault each.

    Idempotent via the persisted watermark: an event is reacted to exactly once,
    so a resolved incident is never re-opened from its stale error row, while a
    genuinely new error (the problem recurred) gets a fresh fault. record_fault
    itself dedups repeated errors on the same step+subject into one open fault.
    """
    client = cfg["client"]
    wm = state.get_observer_watermark(conn, client)
    if wm is None:
        # First run for this client: start watching NOW — ignore the historical
        # backlog so a long-lived DB doesn't trigger a cold-start fault storm.
        wm = state.max_event_id(conn, client)
        state.set_observer_watermark(conn, client, wm)
        print(f"   observer starting fresh at event #{wm} (ignoring earlier backlog)")
        return 0

    events = state.unscanned_error_events(conn, client, wm)
    opened = 0
    highest = wm
    for ev in events:
        highest = max(highest, ev["id"])
        if ev["step"] in _NON_FAULT_STEPS:
            continue
        kind = healer.classify_fault(ev["error"], ev["step"])
        fid = state.record_fault(conn, client, kind, ev["step"], ev["subject"],
                                 ev["error"] or "(no detail)", event_id=ev["id"],
                                 run_id=ev["run_id"])
        opened += 1
        print(f"   ⚑ error on '{ev['step']}' ({ev['subject'] or 'n/a'}) "
              f"→ fault #{fid} [{kind}]")
    if highest > wm:
        state.set_observer_watermark(conn, client, highest)
    return opened


def flag_stalls(cfg, conn):
    """Flag leads stuck in a state too long (a silent misstep with no error row).

    Driven by observability.stall_minutes, e.g. {"queued": 120}. The primary,
    unambiguous case is 'queued but never sent' — a queued lead older than its
    threshold means the drafting/sending pipeline never processed it (a crashed
    agent, a dead cron). One fault per stalled lead (record_fault dedups).
    """
    client = cfg["client"]
    stall_cfg = (cfg.get("observability", {}) or {}).get("stall_minutes", {}) or {}
    now = datetime.now(timezone.utc)
    flagged = 0
    for status, minutes in stall_cfg.items():
        cutoff = (now - timedelta(minutes=int(minutes))).isoformat()
        for lead in state.stalled_leads(conn, client, status, cutoff):
            detail = f"lead stuck in '{status}' for over {minutes} min (no progress)"
            fid = state.record_fault(conn, client, "stall", status, lead["email"], detail)
            flagged += 1
            print(f"   ⚑ stall: {lead['email']} in '{status}' > {minutes}min → fault #{fid}")
    return flagged


# --------------------------------------------------------------------------
# 2. RESOLVE — close faults whose underlying problem has cleared.
# --------------------------------------------------------------------------

def resolve_recovered(cfg, conn):
    """Auto-resolve faults that fixed themselves — the other half of self-healing.

    - error faults: resolved once a later 'ok' event for the same step+subject
      shows the step succeeded (this is how a 'retry' remediation closes out).
    - stall faults: resolved once the lead has left the stalled status.
    """
    client = cfg["client"]
    resolved = 0
    for f in state.open_faults(conn, client, limit=MAX_PER_PASS):
        if f["kind"] == "stall":
            # step holds the stalled status (e.g. 'queued'); did the lead move on?
            cur = state.lead_status(conn, client, f["subject"]) if f["subject"] else None
            if cur is not None and cur != f["step"]:
                state.resolve_fault(conn, f["id"], f"lead advanced to '{cur}'")
                resolved += 1
                print(f"   ✔ fault #{f['id']} resolved (stall cleared → {cur})")
        else:
            if f["subject"] and state.has_ok_event_since(
                    conn, client, f["step"], f["subject"], f["created_at"]):
                state.resolve_fault(conn, f["id"], "step succeeded after the fault")
                resolved += 1
                print(f"   ✔ fault #{f['id']} resolved ('{f['step']}' succeeded on retry)")
    return resolved


# --------------------------------------------------------------------------
# 3. HEAL — hand each still-open fault to the healing agent.
# --------------------------------------------------------------------------

def heal_open_faults(cfg, conn):
    """Work every still-open fault through the healer (AI diagnoses; deterministic
    code applies one safe action or escalates). Each heal meters its own cost."""
    client = cfg["client"]
    healed = 0
    for f in state.open_faults(conn, client, limit=MAX_PER_PASS):
        try:
            action = healer.attempt_heal(cfg, conn, dict(f))
            healed += 1
            print(f"   🩹 fault #{f['id']} ({f['kind']}/{f['step']}) → {action}")
        except Exception as e:
            print(f"   ⚠️  heal failed on fault #{f['id']}: {e}")
    return healed


# --------------------------------------------------------------------------
# The pass + the loop.
# --------------------------------------------------------------------------

def run_once(cfg, conn):
    print_step("🔍 Scanning the pipeline ledger...")
    opened = flag_error_faults(cfg, conn)
    stalled = flag_stalls(cfg, conn)
    recovered = resolve_recovered(cfg, conn)
    healed = heal_open_faults(cfg, conn)

    counts = state.fault_counts(conn, cfg["client"])
    m = obs.metrics(conn, cfg)["totals"]
    print(f"   flagged {opened} error(s) + {stalled} stall(s); "
          f"resolved {recovered}; healed {healed}.")
    print(f"   faults: {counts or '{}'}  |  ledger: {m['events']} events, "
          f"error_rate={m['error_rate']:.0%}, cost=${m['cost_usd']:.4f}")
    return {"opened": opened, "stalled": stalled, "recovered": recovered, "healed": healed}


def main():
    once = "--once" in sys.argv
    cfg = config.load_client()
    ocfg = cfg.get("observability", {})
    poll = int(ocfg.get("poll_seconds", 60))
    print("=========================================================")
    print(f"🔭 {cfg['client_name']} - Observability + Self-Healing Agent")
    print(f"   client={cfg['client']}  max_heal_attempts={ocfg.get('max_heal_attempts', 3)}  "
          f"escalate_email={ocfg.get('escalate_email', True)}")
    print(f"   mode={'single pass (--once)' if once else f'polling every {poll}s (Ctrl-C to stop)'}")
    print("=========================================================")

    if not ocfg.get("enabled", True):
        print("Observability is disabled for this client (observability.enabled=false). Nothing to do.")
        return

    conn = state.connect(cfg["paths"]["db"])
    try:
        if once:
            run_once(cfg, conn)
        else:
            while True:
                run_once(cfg, conn)
                time.sleep(poll)
    except KeyboardInterrupt:
        print("\n👋 Observer agent stopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
