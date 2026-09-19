"""Offline tests for the sending warmup ramp — NO network, NO SMTP, throwaway DBs.

A new sending domain opened at full volume gets filtered. Google's guidance is to
start low and increase slowly, avoiding bursts. Before the ramp, `daily_cap` was
FLAT: whatever the config allowed on day one was allowed immediately, so the ramp
lived only in an operator's memory.

The three properties that actually matter, and that these tests pin:

  1. The ramp can only ever hold volume DOWN. A ramp step above the mailbox's own
     daily_cap must NOT raise it — otherwise a typo in config silently widens the
     tap past the limit the operator set.
  2. The clock starts at the first LIVE send, not the first send. Controlled-mode
     sends go to our own safe inbox; if they aged the domain, two weeks of testing
     would flip live reporting "day 15" on a domain no stranger has heard from.
  3. It is per-MAILBOX. A mailbox added in month three warms from its own first
     send rather than inheriting a warmed sibling's age.

Run:  venv/bin/python tests/test_warmup_ramp.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import state                                  # noqa: E402
from core import sender as sender_mod                   # noqa: E402
from core.sender import SendingPool, SendCapExceeded    # noqa: E402

PASS = FAIL = 0
CLIENT = "testco"

RAMP = [
    {"through_day": 3, "cap": 5},
    {"through_day": 7, "cap": 10},
    {"through_day": 14, "cap": 20},
]


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


def _conn():
    return state.connect(_tmpdb())


def _pool(mailboxes=None, warmup=None, global_cap=500, mode="live"):
    """A SendingPool built straight from a dict — no config.load_client, so these
    tests need no .env and cannot pick up the real tenant's caps."""
    boxes = mailboxes or [{"address": "a@x.test", "daily_cap": 40}]
    return SendingPool({
        "client": CLIENT,
        "from_name": "Test",
        "sending": {
            "mode": mode,
            "controlled_inbox": "safe@x.test",
            "daily_global_cap": global_cap,
            "mailboxes": boxes,
            "warmup": warmup if warmup is not None else {"enabled": True, "ramp": RAMP},
        },
    })


def _send_row(conn, mailbox, days_ago=0, redirected=None, n=1):
    """Write `n` raw send rows dated `days_ago` days back.

    Inserted directly rather than via record_send because record_send stamps
    _now() and the whole point here is controlling the calendar.
    """
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    for i in range(n):
        conn.execute(
            "INSERT INTO sends (client, lead_email, mailbox, subject, redirected_to, "
            "message_id, sent_at) VALUES (?,?,?,?,?,?,?)",
            (CLIENT, f"p{i}@lead.test", mailbox, "s", redirected, f"<m{i}>", when),
        )
    conn.commit()


# --------------------------------------------------------------------------
def test_first_send_at():
    print("\n[state.first_send_at: scoping and live_only]")
    conn = _conn()
    check("None when nothing sent", state.first_send_at(conn, CLIENT) is None)

    _send_row(conn, "a@x.test", days_ago=5)
    _send_row(conn, "b@x.test", days_ago=1)
    first_a = state.first_send_at(conn, CLIENT, "a@x.test")
    first_b = state.first_send_at(conn, CLIENT, "b@x.test")
    check("per-mailbox: a and b differ", first_a != first_b)
    check("per-mailbox: a is the older one", first_a < first_b)
    check("pool-wide returns the earliest of all", state.first_send_at(conn, CLIENT) == first_a)
    check("unknown mailbox is None", state.first_send_at(conn, CLIENT, "zz@x.test") is None)

    # live_only: controlled sends carry a redirected_to and must be invisible to it.
    conn2 = _conn()
    _send_row(conn2, "a@x.test", days_ago=10, redirected="safe@x.test")
    check("live_only=False sees a controlled send",
          state.first_send_at(conn2, CLIENT, "a@x.test", live_only=False) is not None)
    check("live_only=True ignores a controlled send",
          state.first_send_at(conn2, CLIENT, "a@x.test", live_only=True) is None)

    _send_row(conn2, "a@x.test", days_ago=2)       # a real one, newer
    check("live_only=True picks the first REAL send (not the older controlled one)",
          state.first_send_at(conn2, CLIENT, "a@x.test", live_only=True)
          > state.first_send_at(conn2, CLIENT, "a@x.test", live_only=False))
    conn.close()
    conn2.close()


def test_day_counting():
    print("\n[_warmup_day: 1-based, live sends only]")
    conn = _conn()
    p = _pool()
    check("no sends at all => day 1", p._warmup_day(conn, "a@x.test") == 1)

    _send_row(conn, "a@x.test", days_ago=0)
    check("first live send today => day 1", p._warmup_day(conn, "a@x.test") == 1)

    conn2 = _conn()
    _send_row(conn2, "a@x.test", days_ago=1)
    check("first live send yesterday => day 2", p._warmup_day(conn2, "a@x.test") == 2)

    conn3 = _conn()
    _send_row(conn3, "a@x.test", days_ago=9)
    check("first live send 9 days ago => day 10", p._warmup_day(conn3, "a@x.test") == 10)

    # THE TRAP: a fortnight of controlled testing must not age the domain.
    conn4 = _conn()
    _send_row(conn4, "a@x.test", days_ago=14, redirected="safe@x.test", n=20)
    check("14 days of CONTROLLED sends still => day 1",
          p._warmup_day(conn4, "a@x.test") == 1)
    for c in (conn, conn2, conn3, conn4):
        c.close()


def test_cap_follows_the_ramp():
    print("\n[warmup_cap: the step in force for the day]")
    mb = {"address": "a@x.test", "daily_cap": 40}
    p = _pool()
    for days_ago, expected, label in [
        (0, 5, "day 1"),
        (2, 5, "day 3 (last day of step 1)"),
        (3, 10, "day 4 (first day of step 2)"),
        (6, 10, "day 7 (last day of step 2)"),
        (7, 20, "day 8 (first day of step 3)"),
        (13, 20, "day 14 (last ramp day)"),
    ]:
        conn = _conn()
        _send_row(conn, "a@x.test", days_ago=days_ago)
        check(f"{label} => cap {expected}", p.warmup_cap(conn, mb) == expected)
        conn.close()

    conn = _conn()
    _send_row(conn, "a@x.test", days_ago=40)
    check("past the last step => the mailbox's own daily_cap (40)",
          p.warmup_cap(conn, mb) == 40)
    conn.close()


def test_ramp_only_holds_volume_down():
    print("\n[warmup_cap: a ramp can never RAISE a cap]  <-- mutation target")
    conn = _conn()
    _send_row(conn, "a@x.test", days_ago=0)              # day 1

    # A ramp step of 500 against a mailbox whose operator set daily_cap=10.
    # The step must be clamped: config typos must not widen the tap.
    p = _pool(warmup={"enabled": True, "ramp": [{"through_day": 3, "cap": 500}]})
    mb = {"address": "a@x.test", "daily_cap": 10}
    check("ramp cap 500 vs daily_cap 10 => 10, not 500", p.warmup_cap(conn, mb) == 10)

    # And the converse still holds: below the hard cap, the ramp wins.
    p2 = _pool(warmup={"enabled": True, "ramp": [{"through_day": 3, "cap": 2}]})
    check("ramp cap 2 vs daily_cap 10 => 2", p2.warmup_cap(conn, mb) == 2)

    check("never negative", p2.warmup_cap(conn, {"address": "a@x.test", "daily_cap": 0}) == 0)
    conn.close()


def test_disabled_and_malformed():
    print("\n[warmup_cap: off / absent / unsorted config]")
    conn = _conn()
    _send_row(conn, "a@x.test", days_ago=0)
    mb = {"address": "a@x.test", "daily_cap": 40}

    check("enabled=false => hard cap",
          _pool(warmup={"enabled": False, "ramp": RAMP}).warmup_cap(conn, mb) == 40)
    check("no warmup block at all => hard cap",
          _pool(warmup={}).warmup_cap(conn, mb) == 40)
    check("enabled but empty ramp => hard cap",
          _pool(warmup={"enabled": True, "ramp": []}).warmup_cap(conn, mb) == 40)

    # Steps are sorted before matching, so config order cannot change the answer.
    shuffled = _pool(warmup={"enabled": True, "ramp": list(reversed(RAMP))})
    check("ramp written out of order still gives day-1 cap 5",
          shuffled.warmup_cap(conn, mb) == 5)
    conn.close()


def test_per_mailbox_independence():
    print("\n[a new mailbox warms from ITS OWN first send]")
    conn = _conn()
    _send_row(conn, "old@x.test", days_ago=60)       # long warmed
    _send_row(conn, "new@x.test", days_ago=0)        # added today
    p = _pool(mailboxes=[
        {"address": "old@x.test", "daily_cap": 40},
        {"address": "new@x.test", "daily_cap": 40},
    ])
    caps = {m["address"]: p.warmup_cap(conn, m) for m in p.mailboxes}
    check("the warmed mailbox is at its full cap (40)", caps["old@x.test"] == 40)
    check("the new mailbox is still on day 1 (5)", caps["new@x.test"] == 5)
    conn.close()


def test_capacity_respects_the_ramp():
    print("\n[remaining_today / pick_mailbox use the ramp cap, not daily_cap]")
    conn = _conn()
    _send_row(conn, "a@x.test", days_ago=0, n=1)     # day 1, 1 live send used
    p = _pool()
    # daily_cap is 40; day-1 ramp cap is 5; 1 already used => 4 left, not 39.
    check("remaining_today = ramp cap - used (4)", p.remaining_today(conn) == 4)

    _send_row(conn, "a@x.test", days_ago=0, n=4)     # now 5 used, at the day-1 cap
    check("remaining_today = 0 at the ramp cap", p.remaining_today(conn) == 0)
    check("pick_mailbox returns None at the ramp cap (though 35 under daily_cap)",
          p.pick_mailbox(conn) is None)

    # The global cap is still an independent ceiling and the lower one wins.
    conn2 = _conn()
    _send_row(conn2, "a@x.test", days_ago=0, n=1)
    tight = _pool(global_cap=2)
    check("global cap wins when it is lower (2-1=1)", tight.remaining_today(conn2) == 1)
    conn.close()
    conn2.close()


def test_pick_mailbox_spreads_load():
    print("\n[pick_mailbox: fewest-sends-today among mailboxes with ramp headroom]")
    conn = _conn()
    _send_row(conn, "a@x.test", days_ago=0, n=3)
    _send_row(conn, "b@x.test", days_ago=0, n=1)
    p = _pool(mailboxes=[
        {"address": "a@x.test", "daily_cap": 40},
        {"address": "b@x.test", "daily_cap": 40},
    ])
    check("picks the less-used mailbox", p.pick_mailbox(conn)["address"] == "b@x.test")

    _send_row(conn, "b@x.test", days_ago=0, n=4)     # b now at 5 = day-1 cap
    check("skips a mailbox that hit its ramp cap", p.pick_mailbox(conn)["address"] == "a@x.test")

    _send_row(conn, "a@x.test", days_ago=0, n=2)     # a now at 5 too
    check("None once every mailbox is at its ramp cap", p.pick_mailbox(conn) is None)
    conn.close()


def test_send_refuses_before_smtp():
    print("\n[send(): a ramp-capped pool raises BEFORE any SMTP call]")
    conn = _conn()
    _send_row(conn, "a@x.test", days_ago=0, n=5)     # at the day-1 cap of 5
    p = _pool()

    calls = []
    real = sender_mod.smtp_deliver
    sender_mod.smtp_deliver = lambda *a, **k: calls.append(a)   # would record any wire attempt
    try:
        raised = False
        try:
            p.send(conn, "prospect@real.test", "hi", "body", throttle=False)
        except SendCapExceeded:
            raised = True
        except Exception as e:                        # noqa: BLE001 - report the wrong error
            print(f"     (unexpected {type(e).__name__}: {e})")
    finally:
        sender_mod.smtp_deliver = real

    check("SendCapExceeded raised", raised)
    check("smtp_deliver was never called", calls == [])
    check("no send row was written", state.sends_today(conn, CLIENT) == 5)
    conn.close()


def test_status_readout():
    print("\n[warmup_status: what the operator sees]")
    conn = _conn()
    _send_row(conn, "a@x.test", days_ago=3, n=2)     # day 4 => cap 10, 0 used today
    p = _pool()
    row = p.warmup_status(conn)[0]
    check("reports the address", row["address"] == "a@x.test")
    check("reports day 4", row["day"] == 4)
    check("reports today's cap (10)", row["cap_today"] == 10)
    check("reports the hard cap (40)", row["hard_cap"] == 40)
    check("used counts only TODAY (0, the sends were 3 days ago)", row["used"] == 0)
    check("left = cap - used", row["left"] == 10)

    _send_row(conn, "a@x.test", days_ago=0, n=4)
    row = p.warmup_status(conn)[0]
    check("used tracks today's sends (4)", row["used"] == 4)
    check("left shrinks to 6", row["left"] == 6)
    conn.close()


if __name__ == "__main__":
    print("=" * 62)
    print("WARMUP RAMP — offline, no SMTP")
    print("=" * 62)
    test_first_send_at()
    test_day_counting()
    test_cap_follows_the_ramp()
    test_ramp_only_holds_volume_down()
    test_disabled_and_malformed()
    test_per_mailbox_independence()
    test_capacity_respects_the_ramp()
    test_pick_mailbox_spreads_load()
    test_send_refuses_before_smtp()
    test_status_readout()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
