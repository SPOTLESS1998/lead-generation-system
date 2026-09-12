"""Offline tests for the daily orchestrator, the digest and the heartbeat.

NO network, no email, no subprocesses. The three things pinned here are the ones that
decide whether unattended operation is safe:

  1. PREFLIGHT REFUSES. The job must not run if the config could reach a prospect.
     This is the single most important assertion in the repo: everything else is a
     cost or tidiness concern, this one is "did we email strangers by accident".
  2. THE SENDER IS NEVER CALLED. Verified by exploding on contact rather than by
     reading the code and trusting it.
  3. A MISSED RUN IS DETECTABLE. The `runs` table exists precisely because every
     other signal is emitted BY a run, so absence is otherwise invisible.

Run:  venv/bin/python tests/test_daily_run.py
"""

import os
import sys
import json
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from core import state, digest, sender                            # noqa: E402
import daily_run                                                  # noqa: E402
import heartbeat                                                  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _tmpdb():
    return tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name


def _cfg(mode="controlled", **over):
    cfg = {"client": "t", "client_name": "T Co", "from_name": "Tester",
           "unsubscribe_base_url": "https://audit.example.com",
           "sending": {"mode": mode},
           "heartbeat": {"enabled": True, "max_age_hours": 26, "alert_email": True},
           "notify": {"mode": "digest"},
           "paths": {"db": _tmpdb(), "root": ROOT}}
    cfg.update(over)
    return cfg


class _Report:
    """Minimal stand-in matching daily_run.RunReport's read surface."""
    def __init__(self, steps=(), sourced=0, drafted=0, counts=None, faults=None):
        self.steps = list(steps)
        self.sourced = sourced
        self.drafted = drafted
        self.lead_counts = counts or {}
        self.faults = faults or {}

    def add(self, name, status, detail="", error=""):
        self.steps.append({"name": name, "status": status,
                           "detail": detail, "error": error})

    @property
    def failed(self):
        return [s for s in self.steps if s["status"] == "failed"]


# --------------------------------------------------------------------------
def test_preflight_refuses_live_mode():
    print("\n[preflight REFUSES any config that could reach a prospect]")
    os.environ.setdefault("SMTP_USER", "x@y.com")
    os.environ.setdefault("SMTP_PASS", "secret")

    problems = daily_run.preflight(_cfg(mode="controlled"))
    check("controlled mode passes preflight", problems == [])

    problems = daily_run.preflight(_cfg(mode="live"))
    check("live mode is REFUSED", len(problems) == 1)
    # Guarded so a regression FAILS the assertion rather than crashing the suite
    # before it can report (an IndexError here would hide every later test).
    joined = " ".join(problems)
    check("the refusal names sending.mode", "sending.mode" in joined)
    check("the refusal names the offending value", "'live'" in joined)

    # Any unexpected value must also be refused — fail closed, not open.
    for bad in ("LIVE", "Controlled", "", None, "test"):
        problems = daily_run.preflight(_cfg(mode=bad))
        check(f"mode {bad!r} is refused (fails closed)", len(problems) >= 1)


def test_preflight_requires_credentials():
    print("\n[preflight requires the credentials the run needs]")
    saved = {k: os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS")}
    try:
        for k in ("SMTP_USER", "SMTP_PASS"):
            os.environ.pop(k, None)
        problems = daily_run.preflight(_cfg())
        check("missing SMTP_USER is caught", any("SMTP_USER" in p for p in problems))
        check("missing SMTP_PASS is caught", any("SMTP_PASS" in p for p in problems))
        check("no credential VALUE is echoed in the problem text",
              all("secret" not in p for p in problems))
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_the_sender_is_never_called():
    print("\n[the daily job never puts mail on the wire to a prospect]")
    # Explode if anything reaches SMTP, then run the steps that could plausibly try.
    original = sender.smtp_deliver
    calls = []

    def exploding(*a, **k):
        calls.append(a)
        raise AssertionError("smtp_deliver was called during a dry run!")

    sender.smtp_deliver = exploding
    try:
        cfg = _cfg()
        report = _Report(steps=[{"name": "source+draft", "status": "ok",
                                 "detail": "sourced 3", "error": ""}],
                         sourced=3, drafted=1, counts={"sourced": 2, "awaiting_approval": 1})
        # build_digest is the pure half — it must never send.
        subject, html_body, text = digest.build_digest(cfg, report)
        check("building a digest sends nothing", calls == [])
        check("the digest subject reports the counts", "3 sourced" in subject)
        check("the digest states nothing reached a prospect",
              "Nothing was sent to a prospect" in html_body)

        # run_sync with --dry-run must not shell out or send.
        r2 = _Report()
        daily_run.run_sync(cfg, r2, dry_run=True, skip=False)
        check("sync is skipped in dry-run", r2.steps[0]["status"] == "skipped")
        check("still nothing on the wire", calls == [])
    finally:
        sender.smtp_deliver = original


def test_digest_reports_failures_honestly():
    print("\n[the digest tells the truth about a failed run]")
    cfg = _cfg()
    report = _Report(steps=[
        {"name": "source+draft", "status": "ok", "detail": "sourced 5", "error": ""},
        {"name": "sync", "status": "failed", "detail": "sync exited non-zero",
         "error": "ssh: connect to host timed out"},
    ], sourced=5, drafted=2)
    subject, html_body, text = digest.build_digest(cfg, report)
    check("a failure puts ACTION REQUIRED in the subject", "ACTION REQUIRED" in subject)
    check("the failure count is in the subject", "1 failure" in subject)
    check("the failing step is named in the body", "sync" in html_body)
    check("the underlying error is shown, not swallowed",
          "timed out" in html_body and "timed out" in text)
    check("the successful step is still reported", "source+draft" in html_body)

    clean = _Report(steps=[{"name": "source+draft", "status": "ok", "detail": "", "error": ""}],
                    sourced=4, drafted=2)
    subject2, _, _ = digest.build_digest(cfg, clean)
    check("a clean run does NOT say ACTION REQUIRED", "ACTION REQUIRED" not in subject2)
    check("a clean run is marked with a tick", subject2.startswith("✅"))


def test_digest_mode_suppresses_per_draft_email():
    print("\n[notify.mode routes per-draft mail correctly]")
    check("digest is the default", digest.notify_mode({}) == "digest")
    check("per_draft is honoured", digest.notify_mode({"notify": {"mode": "per_draft"}}) == "per_draft")
    check("case/whitespace tolerant",
          digest.notify_mode({"notify": {"mode": " DIGEST "}}) == "digest")

    # A cold draft in digest mode must not email; a REPLY always must.
    from core import review
    original = sender.smtp_deliver
    sent = []
    sender.smtp_deliver = lambda *a, **k: sent.append(a)
    try:
        cfg = _cfg()
        review.notify_operator(cfg, "id1", {"kind": "cold", "company_name": "Acme",
                                            "target_email": "a@a.com"})
        check("a cold draft sends no individual email in digest mode", sent == [])

        cfg_pd = _cfg(notify={"mode": "per_draft"})
        review.notify_operator(cfg_pd, "id2", {"kind": "cold", "company_name": "Acme",
                                               "target_email": "a@a.com"})
        check("per_draft mode DOES send for a cold draft", len(sent) == 1)

        review.notify_operator(cfg, "id3", {"kind": "reply", "company_name": "Acme",
                                            "target_email": "a@a.com", "intent": "interested"})
        check("a REPLY always sends, even in digest mode", len(sent) == 2)
    finally:
        sender.smtp_deliver = original


def test_run_ledger_and_heartbeat():
    print("\n[a missed run is detectable]")
    conn = state.connect(_tmpdb())

    ok, status, detail, age = heartbeat.check(conn, "t", 26)
    check("never having run is NOT reported as healthy", ok is False)
    check("...and is named 'never_ran'", status == "never_ran")

    state.record_run(conn, "t", "ok", sourced=12, drafted=5)
    ok, status, detail, age = heartbeat.check(conn, "t", 26)
    check("a fresh successful run is healthy", ok is True and status == "fresh")
    row = state.last_run(conn, "t")
    check("the run recorded its counts", row["sourced"] == 12 and row["drafted"] == 5)

    # Backdate past the threshold.
    old = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
    conn.execute("UPDATE runs SET finished_at=?", (old,))
    conn.commit()
    ok, status, detail, age = heartbeat.check(conn, "t", 26)
    check("a 40h-old run is stale against a 26h threshold", ok is False and status == "stale")
    check("the alert says how old it is", "40" in detail)

    # A newer FAILED run after an older success must not read as healthy.
    conn.execute("DELETE FROM runs")
    conn.commit()
    state.record_run(conn, "t", "ok", sourced=3, drafted=1)
    state.record_run(conn, "t", "failed", failed_steps=["source+draft"], detail="provider down")
    ok, status, detail, age = heartbeat.check(conn, "t", 26)
    check("a fresh FAILED run is not healthy", ok is False and status == "failed_last")
    check("the failure detail is surfaced", "provider down" in detail)

    check("failed_steps round-trips as JSON",
          json.loads(state.last_run(conn, "t")["failed_steps"]) == ["source+draft"])
    check("recent_runs returns newest first",
          [r["status"] for r in state.recent_runs(conn, "t", limit=2)] == ["failed", "ok"])


def test_report_status_accounting():
    print("\n[the run report's pass/fail accounting]")
    r = daily_run.RunReport()
    r.add("preflight", "ok", "fine")
    check("a clean report is ok", r.ok is True and r.failed == [])
    r.add("sync", "skipped", "--dry-run")
    check("a skipped step is not a failure", r.ok is True)
    r.add("observe", "failed", "boom", "detail")
    check("a failed step makes the report not-ok", r.ok is False)
    check("the failure is listed", [s["name"] for s in r.failed] == ["observe"])


test_preflight_refuses_live_mode()
test_preflight_requires_credentials()
test_the_sender_is_never_called()
test_digest_reports_failures_honestly()
test_digest_mode_suppresses_per_draft_email()
test_run_ledger_and_heartbeat()
test_report_status_accounting()

print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
