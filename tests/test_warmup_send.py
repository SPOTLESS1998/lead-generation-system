"""Offline tests for the seed warm-up sender — NO network, NO SMTP, temp files only.

scripts/warmup_send.py builds sending reputation for a cold domain by mailing a
small, ramped number of personal notes to a CONSENTING seed list. The properties
that matter, and that these tests pin:

  1. It only ever sends to addresses from the seed file — never a lead, never the
     prospect `sends` table, never `sending.mode`. (The fuse stays intact.)
  2. The ramp can only hold volume DOWN (a config typo cannot widen the tap), and
     the warm-up day clock is its own — independent of the prospect pipeline.
  3. Today's cap is honoured, counting only today's SUCCESSFUL sends, so re-running
     the job cannot blow past the cap.
  4. One failing address records the failure and does not abort the batch.
  5. --dry-run puts nothing on the wire.

Run:  venv/bin/python tests/test_warmup_send.py
"""

import os
import sys
import json
import tempfile
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import warmup_send as w                                   # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


class _Rng:
    """A deterministic stand-in for random.Random: choice->first, shuffle->noop,
    uniform->min. Keeps composition and ordering predictable in tests."""
    def choice(self, seq):
        return seq[0]

    def shuffle(self, seq):
        pass

    def uniform(self, a, b):
        return a


def _cfg(daily_cap=40, ramp=None, enabled=True, spacing=None):
    sending = {
        "mode": "controlled",                # must be irrelevant to warm-up
        "controlled_inbox": "safe@mine.test",
        "mailboxes": [{
            "address": "mail@warm.test", "smtp_host": "h", "smtp_port": 587,
            "user": "u", "password": "p", "daily_cap": daily_cap,
        }],
        "warmup_seed": {"enabled": enabled},
    }
    if ramp is not None:
        sending["warmup_seed"]["ramp"] = ramp
    if spacing is not None:
        sending["warmup_seed"]["spacing_seconds"] = spacing
    return {"client": "testco", "from_name": "Ada Lovelace", "sending": sending}


def _now(days_from_epoch_ref=0):
    """A fixed 'now' we can shift by days for ramp math."""
    base = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    return base + timedelta(days=days_from_epoch_ref)


RAMP = [{"through_day": 7, "cap": 3}, {"through_day": 14, "cap": 6}]


# --------------------------------------------------------------------------
def test_seed_parsing():
    print("\n[load_seeds: comments, blanks, Name<>, dedupe, invalid lines]")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    tmp.write("# a comment\n\n"
              "one@gmail.com\n"
              "Two Person <two@outlook.com>\n"
              "ONE@gmail.com\n"            # dup (case-insensitive)
              "not-an-email\n"            # invalid -> skipped
              "three@yahoo.com\n")
    tmp.close()
    warned = []
    seeds = w.load_seeds(tmp.name, warn=warned.append)
    check("keeps the three valid, unique addresses",
          seeds == ["one@gmail.com", "two@outlook.com", "three@yahoo.com"])
    check("warns about the invalid line (never silent)", len(warned) == 1)
    check("missing file => []", w.load_seeds("/no/such/file.txt") == [])
    os.unlink(tmp.name)


def test_warmup_day_and_cap():
    print("\n[warmup_day / cap_for_day: own clock, ramp holds down]")
    today = _now().date()
    check("no log => day 1", w.warmup_day([], today) == 1)

    log = [{"ok": True, "date": (today - timedelta(days=9)).isoformat()},
           {"ok": True, "date": (today - timedelta(days=2)).isoformat()}]
    check("earliest OK send 9 days ago => day 10", w.warmup_day(log, today) == 10)

    # Failed sends must not start the clock.
    only_fail = [{"ok": False, "date": (today - timedelta(days=5)).isoformat()}]
    check("a failed send does not age the domain => day 1",
          w.warmup_day(only_fail, today) == 1)

    check("day 1 on RAMP => cap 3", w.cap_for_day(RAMP, 1, 40) == 3)
    check("day 8 still within step 2 (through_day 14) => cap 6", w.cap_for_day(RAMP, 8, 40) == 6)
    check("day 15 past the last step => hard cap 40", w.cap_for_day(RAMP, 15, 40) == 40)
    check("ramp step above hard cap is clamped (cannot RAISE)",
          w.cap_for_day([{"through_day": 3, "cap": 500}], 1, 10) == 10)
    check("unsorted ramp still gives the day-1 step",
          w.cap_for_day(list(reversed(RAMP)), 1, 40) == 3)


def test_sent_today_counts_only_todays_ok():
    print("\n[sent_today: today's successful sends only]")
    today = _now().date()
    log = [
        {"ok": True, "date": today.isoformat()},
        {"ok": True, "date": today.isoformat()},
        {"ok": False, "date": today.isoformat()},                 # failed, uncounted
        {"ok": True, "date": (today - timedelta(days=1)).isoformat()},  # yesterday
    ]
    check("counts the 2 successful sends from today only", w.sent_today(log, today) == 2)


def test_choose_recipients_spreads():
    print("\n[choose_recipients: least-recently-contacted first, only from seeds]")
    today = _now().date()
    seeds = ["a@x.test", "b@x.test", "c@x.test"]
    log = [{"ok": True, "to": "a@x.test", "date": today.isoformat()}]   # a was just sent
    picks = w.choose_recipients(seeds, log, 2, _Rng())
    check("skips the just-contacted 'a', picks never-sent first",
          "a@x.test" not in picks and set(picks) == {"b@x.test", "c@x.test"})
    check("every pick comes from the seed list",
          all(p in seeds for p in w.choose_recipients(seeds, log, 3, _Rng())))
    check("n=0 => no recipients", w.choose_recipients(seeds, log, 0, _Rng()) == [])


def test_run_sends_and_records():
    print("\n[run: delivers to seeds, records a log entry per send, respects cap]")
    seeds = ["a@x.test", "b@x.test", "c@x.test", "d@x.test"]
    wire, recorded = [], []
    cfg = _cfg(daily_cap=40, ramp=RAMP)       # day 1 cap = 3
    summary = w.run(
        cfg, seeds, [],
        deliver=lambda *a: wire.append(a[5]),   # a[5] == to_addr
        now=_now(), rng=_Rng(),
        sleep_fn=lambda s: None,
        record_fn=recorded.append,
        log=lambda *a, **k: None,
    )
    check("sent exactly the day-1 cap (3), not all 4 seeds", summary["sent"] == 3)
    check("3 messages on the wire", len(wire) == 3)
    check("every wire recipient is a seed", all(r in seeds for r in wire))
    check("3 log entries recorded, all ok", len(recorded) == 3 and all(e["ok"] for e in recorded))
    check("log entries carry a message-id and today's date",
          all(e.get("message_id") and e["date"] == _now().date().isoformat() for e in recorded))


def test_run_respects_used_today():
    print("\n[run: a second run the same day honours what was already sent]")
    seeds = ["a@x.test", "b@x.test", "c@x.test", "d@x.test"]
    today = _now().date().isoformat()
    prior = [{"ok": True, "to": "a@x.test", "date": today},
             {"ok": True, "to": "b@x.test", "date": today}]   # 2 already sent today
    wire = []
    summary = w.run(
        _cfg(ramp=RAMP), seeds, prior,
        deliver=lambda *a: wire.append(a[5]),
        now=_now(), rng=_Rng(), sleep_fn=lambda s: None,
        log=lambda *a, **k: None,
    )
    check("cap 3 minus 2 already used => only 1 more goes out", summary["sent"] == 1)


def test_max_lowers_only():
    print("\n[--max lowers the cap but can never raise it]")
    seeds = [f"{c}@x.test" for c in "abcdef"]
    wire = []
    summary = w.run(
        _cfg(ramp=RAMP), seeds, [],
        deliver=lambda *a: wire.append(a[5]),
        now=_now(), rng=_Rng(), limit=1,
        sleep_fn=lambda s: None, log=lambda *a, **k: None,
    )
    check("--max 1 caps today at 1", summary["sent"] == 1)

    summary2 = w.run(
        _cfg(ramp=RAMP), seeds, [],
        deliver=lambda *a: wire.append(a[5]),
        now=_now(), rng=_Rng(), limit=99,       # above the ramp cap of 3
        sleep_fn=lambda s: None, log=lambda *a, **k: None,
    )
    check("--max 99 cannot exceed the ramp cap (3)", summary2["sent"] == 3)


def test_dry_run_sends_nothing():
    print("\n[--dry-run: a full plan, nothing on the wire, nothing recorded]")
    seeds = ["a@x.test", "b@x.test"]
    wire, recorded = [], []
    summary = w.run(
        _cfg(ramp=RAMP), seeds, [],
        deliver=lambda *a: wire.append(a[5]),
        now=_now(), rng=_Rng(), dry_run=True,
        sleep_fn=lambda s: None, record_fn=recorded.append,
        log=lambda *a, **k: None,
    )
    check("nothing delivered", wire == [])
    check("nothing recorded", recorded == [])
    check("summary reports 0 sent", summary["sent"] == 0)


def test_one_failure_does_not_abort():
    print("\n[run: a single failing address is logged, the rest still send]")
    seeds = ["good1@x.test", "boom@x.test", "good2@x.test"]
    recorded = []

    def flaky(host, port, user, pw, frm, to, msg):
        if to == "boom@x.test":
            raise RuntimeError("smtp said no")

    summary = w.run(
        _cfg(ramp=[{"through_day": 7, "cap": 10}]), seeds, [],
        deliver=flaky, now=_now(), rng=_Rng(),
        sleep_fn=lambda s: None, record_fn=recorded.append,
        log=lambda *a, **k: None,
    )
    check("2 sent, 1 failed", summary["sent"] == 2 and summary["failed"] == 1)
    check("the failure is recorded with ok=False + an error",
          any((not e["ok"]) and e.get("error") for e in recorded))
    check("the failed address does NOT count toward warm-up age",
          w.warmup_day([e for e in recorded], _now().date()) == 1)


def test_disabled_sends_nothing():
    print("\n[enabled=false: the whole warm-up is off]")
    wire = []
    summary = w.run(
        _cfg(enabled=False), ["a@x.test", "b@x.test"], [],
        deliver=lambda *a: wire.append(a[5]),
        now=_now(), rng=_Rng(), sleep_fn=lambda s: None,
        log=lambda *a, **k: None,
    )
    check("nothing on the wire when disabled", wire == [])
    check("summary notes it was skipped", summary.get("skipped") == "disabled")


def test_log_roundtrip():
    print("\n[append_log / read_log: durable JSONL, corrupt line tolerated]")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "sub", "warmup_log.jsonl")       # dir does not exist yet
    w.append_log(p, {"ok": True, "to": "a@x.test", "date": "2026-10-09"})
    w.append_log(p, {"ok": True, "to": "b@x.test", "date": "2026-10-09"})
    with open(p, "a") as f:
        f.write("{ this is not json\n")                  # a corrupt line
    rows = w.read_log(p)
    check("creates the dir and reads back the 2 good rows", len(rows) == 2)
    check("the corrupt line is skipped, not fatal", all(r.get("to") for r in rows))


if __name__ == "__main__":
    print("=" * 62)
    print("SEED WARM-UP SENDER — offline, no SMTP")
    print("=" * 62)
    test_seed_parsing()
    test_warmup_day_and_cap()
    test_sent_today_counts_only_todays_ok()
    test_choose_recipients_spreads()
    test_run_sends_and_records()
    test_run_respects_used_today()
    test_max_lowers_only()
    test_dry_run_sends_nothing()
    test_one_failure_does_not_abort()
    test_disabled_sends_nothing()
    test_log_roundtrip()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
