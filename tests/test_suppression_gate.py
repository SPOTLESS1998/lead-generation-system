"""The never-contact gate at the SEND chokepoint — offline, no SMTP, throwaway DBs.

The gap this closes: suppression used to be checked only at DRAFT time, so the
window between "draft queued" and "operator clicks Approve" was unguarded. A
prospect could click Unsubscribe, land on the never-contact list, and still be
emailed minutes later because their draft was already sitting in the queue and
the approve path never re-read the list. Demonstrated before the fix: a send to
an address with `is_suppressed() == True` completed and reached the wire.

Emailing someone who opted out is the single worst thing a cold-email system can
do — CAN-SPAM, and a promise broken in the footer of the very email that produced
the opt-out. So the gate has no config flag, and these tests pin that too.

Run:  venv/bin/python tests/test_suppression_gate.py
"""

import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import state, suppression, review            # noqa: E402
from core import sender as sender_mod                  # noqa: E402
from core.sender import SendingPool, SuppressedRecipient  # noqa: E402

PASS = FAIL = 0
CLIENT = "testco"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _conn():
    return state.connect(tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name)


def _pool(mode="live", verify_enabled=False):
    return SendingPool({
        "client": CLIENT, "from_name": "Test",
        "sending": {
            "mode": mode, "controlled_inbox": "safe@mine.test",
            "daily_global_cap": 100,
            "mailboxes": [{"address": "a@x.test", "daily_cap": 40, "smtp_host": "h",
                           "smtp_port": 587, "user": "u", "password": "p"}],
            "warmup": {"enabled": False},
            "verify_recipients": {"enabled": verify_enabled},
        },
    })


def _try_send(pool, conn, to_email, **kw):
    """Attempt a send with SMTP replaced. Returns (exception_or_None, wire_recipients)."""
    wire = []
    real = sender_mod.smtp_deliver
    sender_mod.smtp_deliver = lambda *a, **k: wire.append(a[5])
    try:
        pool.send(conn, to_email, "subj", "body", throttle=False, **kw)
        return None, wire
    except Exception as e:
        return e, wire
    finally:
        sender_mod.smtp_deliver = real


# --------------------------------------------------------------------------
def test_suppressed_never_reaches_the_wire():
    print("\n[a suppressed recipient is refused before SMTP]  <-- the whole point")
    for reason in ("unsubscribed", "bounced", "complained", "manual", "not_interested"):
        conn = _conn()
        victim = f"person-{reason}@real.test"
        suppression.add(conn, CLIENT, victim, reason=reason)
        err, wire = _try_send(_pool(), conn, victim)
        ok = isinstance(err, SuppressedRecipient) and wire == []
        check(f"reason={reason}: refused, nothing on the wire", ok)
        check(f"reason={reason}: nothing recorded as sent",
              state.sends_today(conn, CLIENT) == 0)
        conn.close()


def test_normal_recipient_still_sends():
    print("\n[the gate does not block everyone — a clean address still goes]")
    conn = _conn()
    err, wire = _try_send(_pool(), conn, "fine@real.test")
    check("no exception", err is None)
    check("exactly one message on the wire", len(wire) == 1)
    check("recorded as sent", state.sends_today(conn, CLIENT) == 1)

    # Suppressing a DIFFERENT person must not affect this one.
    suppression.add(conn, CLIENT, "other@real.test", reason="unsubscribed")
    err, wire = _try_send(_pool(), conn, "fine@real.test")
    check("suppressing someone else does not block this address", err is None)
    conn.close()


def test_scoped_to_the_right_tenant():
    print("\n[suppression is per-client, not global]")
    conn = _conn()
    suppression.add(conn, "othertenant", "shared@real.test", reason="unsubscribed")
    err, wire = _try_send(_pool(), conn, "shared@real.test")
    check("another tenant's opt-out does not block ours", err is None and len(wire) == 1)
    conn.close()


def test_controlled_mode_cannot_mask_it():
    print("\n[controlled mode: the LOGICAL recipient is what is checked]")
    conn = _conn()
    suppression.add(conn, CLIENT, "victim@real.test", reason="unsubscribed")
    err, wire = _try_send(_pool(mode="controlled"), conn, "victim@real.test")
    check("still refused in controlled mode", isinstance(err, SuppressedRecipient))
    check("nothing on the wire", wire == [])
    conn.close()


def test_blocks_replies_too():
    print("\n[threaded replies go through the same chokepoint]")
    conn = _conn()
    suppression.add(conn, CLIENT, "nope@real.test", reason="not_interested")
    err, wire = _try_send(_pool(), conn, "nope@real.test",
                          in_reply_to="<abc@x>", references="<abc@x>")
    check("a reply to a suppressed prospect is refused",
          isinstance(err, SuppressedRecipient))
    check("nothing on the wire", wire == [])
    conn.close()


def test_gate_runs_before_the_cap_check():
    print("\n[ordering: opt-out is reported even when caps are also exhausted]")
    conn = _conn()
    victim = "victim@real.test"
    suppression.add(conn, CLIENT, victim, reason="unsubscribed")
    pool = _pool()
    pool.global_cap = 0                      # caps ALSO exhausted
    err, wire = _try_send(pool, conn, victim)
    check("reports the opt-out, not the cap", isinstance(err, SuppressedRecipient))
    check("nothing on the wire", wire == [])
    conn.close()


def test_there_is_no_off_switch():
    print("\n[the gate is not configurable — a legal obligation has no flag]")
    conn = _conn()
    suppression.add(conn, CLIENT, "victim@real.test", reason="unsubscribed")
    # Try every plausible way an operator (or a future edit) might disable it.
    for cfgblock in ({"enabled": False}, {"suppression": False},
                     {"check_suppression": False}, {}):
        pool = SendingPool({
            "client": CLIENT, "from_name": "T",
            "sending": {"mode": "live", "controlled_inbox": "s@x.test",
                        "daily_global_cap": 100,
                        "mailboxes": [{"address": "a@x.test", "daily_cap": 40,
                                       "smtp_host": "h", "smtp_port": 587,
                                       "user": "u", "password": "p"}],
                        "warmup": {"enabled": False},
                        "verify_recipients": {"enabled": False},
                        "suppression": cfgblock},
        })
        err, wire = _try_send(pool, conn, "victim@real.test")
        check(f"config {cfgblock} cannot disable it",
              isinstance(err, SuppressedRecipient) and wire == [])
    conn.close()


# --- the queue side --------------------------------------------------------

def test_retire_drafts_for():
    print("\n[opting out also pulls the draft off the approval queue]")
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    orig = review.PENDING_FILE
    review.PENDING_FILE = tmp
    try:
        entries = {
            "aaa": {"client": CLIENT, "target_email": "victim@real.test", "status": "pending"},
            "bbb": {"client": CLIENT, "target_email": "victim@real.test", "status": "pending"},
            "ccc": {"client": CLIENT, "target_email": "other@real.test", "status": "pending"},
            "ddd": {"client": "othertenant", "target_email": "victim@real.test", "status": "pending"},
            "eee": {"client": CLIENT, "target_email": "victim@real.test",
                    "status": "approved_and_sent"},
        }
        with open(tmp, "w") as f:
            json.dump(entries, f)

        n = review.retire_drafts_for(CLIENT, "victim@real.test")
        check("retired exactly the 2 live drafts for that person", n == 2)

        after = review.load_pending()
        check("both are no longer 'pending'",
              after["aaa"]["status"] == "suppressed" and after["bbb"]["status"] == "suppressed")
        check("a different prospect is untouched", after["ccc"]["status"] == "pending")
        check("another tenant's draft is untouched", after["ddd"]["status"] == "pending")
        check("an ALREADY-SENT draft is not rewritten",
              after["eee"]["status"] == "approved_and_sent")

        check("they no longer count as having a live draft",
              "victim@real.test" not in review.emails_with_live_draft(CLIENT))
        check("the untouched prospect still does",
              "other@real.test" in review.emails_with_live_draft(CLIENT))

        check("re-running is a no-op", review.retire_drafts_for(CLIENT, "victim@real.test") == 0)
    finally:
        review.PENDING_FILE = orig
        os.unlink(tmp)


if __name__ == "__main__":
    print("=" * 62)
    print("NEVER-CONTACT GATE AT THE SEND CHOKEPOINT")
    print("=" * 62)
    test_suppressed_never_reaches_the_wire()
    test_normal_recipient_still_sends()
    test_scoped_to_the_right_tenant()
    test_controlled_mode_cannot_mask_it()
    test_blocks_replies_too()
    test_gate_runs_before_the_cap_check()
    test_there_is_no_off_switch()
    test_retire_drafts_for()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
