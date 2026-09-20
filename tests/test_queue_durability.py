"""The approval queue must never destroy itself — offline, temp files only.

The bug this closes had two halves that combined into silent data loss:

  1. `_write` did `open(PENDING_FILE, "w")`, which TRUNCATES before json.dump
     runs. Any crash, kill or full disk mid-write left invalid or zero-byte JSON.
  2. `load_pending` swallowed JSONDecodeError and returned `{}`.

Together: a corrupt file read as an empty queue, and then `save_pending` did
`leads = load_pending()` -> {}, added one entry, and wrote — **replacing the
entire approval queue with a single draft**, silently. `emails_with_live_draft`
returned an empty set at the same time, disabling the duplicate guard whose
absence previously shipped 20 prospects two approvable drafts each.

13 pending_leads.json.bak files on the box — six from one day — are what that
looked like in practice.

Run:  venv/bin/python tests/test_queue_durability.py
"""

import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import review                                 # noqa: E402
from core.review import QueueCorrupt                    # noqa: E402

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


class _queue:
    """Point review.PENDING_FILE at a throwaway file with the given raw contents."""

    def __init__(self, raw=None):
        self.raw = raw

    def __enter__(self):
        d = tempfile.mkdtemp()
        self.path = os.path.join(d, "pending_leads.json")
        if self.raw is not None:
            with open(self.path, "w") as f:
                f.write(self.raw)
        self._orig = review.PENDING_FILE
        review.PENDING_FILE = self.path
        return self

    def read(self):
        with open(self.path) as f:
            return f.read()

    def __exit__(self, *a):
        review.PENDING_FILE = self._orig


def _entry(email, status="pending"):
    return {"client": CLIENT, "target_email": email, "status": status,
            "drafted_subject": "s", "drafted_body": "b"}


# --------------------------------------------------------------------------
def test_missing_vs_corrupt():
    print("\n[a MISSING file is empty; a CORRUPT one is an error]")
    with _queue() as q:            # no file at all
        os.path.exists(q.path) or None
        check("absent file => {} (fresh install)", review.load_pending() == {})

    with _queue('{"a": {"x": 1}}') as q:
        check("valid file loads", review.load_pending() == {"a": {"x": 1}})

    for label, raw in [("truncated mid-write", '{"a": {"target_em'),
                       ("garbage", "not json at all"),
                       ("zero bytes", ""),
                       ("whitespace only", "   \n  ")]:
        with _queue(raw):
            try:
                review.load_pending()
                caught = False
            except QueueCorrupt:
                caught = True
            check(f"{label} => raises QueueCorrupt", caught)

    # The zero-byte branch is NOT load-bearing for behaviour — json.loads("")
    # raises anyway. It exists purely to name the most common cause, because a
    # 0-byte file is the exact signature of the old truncate-then-die write. So
    # what is pinned here is the DIAGNOSTIC, which is the only thing it adds.
    with _queue(""):
        try:
            review.load_pending()
            msg = ""
        except QueueCorrupt as e:
            msg = str(e)
        check("a 0-byte file says so explicitly ('empty')", "empty" in msg.lower())
        check("...and names the likely cause (an interrupted write)",
              "interrupted" in msg.lower())
    with _queue("not json"):
        try:
            review.load_pending()
            msg = ""
        except QueueCorrupt as e:
            msg = str(e)
        check("a garbage file gets the parser's reason instead",
              "0 bytes" not in msg and "not valid JSON" in msg)


def test_corrupt_queue_is_never_overwritten():
    print("\n[THE BUG: a corrupt queue must not be replaced by one draft]  <-- key")
    corrupt = '{"aaa": {"target_email": "real@prospect.test", "stat'
    with _queue(corrupt) as q:
        try:
            review.save_pending(_entry("new@prospect.test"))
            raised = False
        except QueueCorrupt:
            raised = True
        check("save_pending refuses rather than overwriting", raised)
        check("the file on disk is byte-for-byte untouched", q.read() == corrupt)
        check("...so the original queue is still recoverable",
              "real@prospect.test" in q.read())


def test_dedupe_guard_is_not_silently_disabled():
    print("\n[a corrupt queue must not silently disable the duplicate guard]")
    with _queue('{"aaa": {"broke'):
        try:
            review.emails_with_live_draft(CLIENT)
            raised = False
        except QueueCorrupt:
            raised = True
        check("emails_with_live_draft raises instead of returning an empty set", raised)
        # An empty set here would mean "nobody has a live draft" -> every lead is
        # re-drafted -> the same prospect gets two approvable cold emails.

    with _queue('{"aaa": {"trunc'):
        for fn, label in [(lambda: review.set_pending_status("aaa", "declined"),
                           "set_pending_status"),
                          (lambda: review.retire_drafts_for(CLIENT, "x@y.test"),
                           "retire_drafts_for")]:
            try:
                fn(); raised = False
            except QueueCorrupt:
                raised = True
            check(f"{label} raises rather than acting on a phantom empty queue", raised)


def test_write_is_atomic():
    print("\n[_write never leaves a partial file, even if it dies mid-write]")
    good = {"aaa": _entry("keep@prospect.test")}
    with _queue(json.dumps(good)) as q:
        # Simulate a crash DURING the write: json.dump raises part-way through.
        class Boom(Exception):
            pass

        real_dump = review.json.dump

        def exploding_dump(obj, fp, **kw):
            fp.write('{"partial": ')      # some bytes land in the temp file
            raise Boom("killed mid-write")

        review.json.dump = exploding_dump
        try:
            try:
                review._write({"bbb": _entry("new@prospect.test")})
                died = False
            except Boom:
                died = True
        finally:
            review.json.dump = real_dump

        check("the write did fail", died)

        # Read defensively: with a truncate-in-place write the file is now garbage,
        # and this assertion must report RED rather than abort the whole script —
        # otherwise a mutation that reintroduces the bug looks like it passed.
        try:
            survived = json.loads(q.read()) == good
        except Exception:
            survived = False
        check("the ORIGINAL file is intact after a mid-write crash", survived)

        try:
            still_loads = review.load_pending() == good
        except Exception:
            still_loads = False
        check("...and is still valid JSON", still_loads)

        leftovers = [f for f in os.listdir(os.path.dirname(q.path))
                     if f.startswith(".pending_")]
        check("no temp file left behind", leftovers == [])


def test_write_replaces_cleanly():
    print("\n[a successful write fully replaces the file]")
    with _queue(json.dumps({"aaa": _entry("a@x.test")})) as q:
        review._write({"bbb": _entry("b@x.test")})
        on_disk = json.loads(q.read())
        check("new content written", "bbb" in on_disk)
        check("old content gone", "aaa" not in on_disk)
        check("readable through load_pending", review.load_pending() == on_disk)
        leftovers = [f for f in os.listdir(os.path.dirname(q.path))
                     if f.startswith(".pending_")]
        check("no temp file left behind", leftovers == [])


def test_normal_operation_unaffected():
    print("\n[the happy path still works exactly as before]")
    with _queue("{}"):
        lid = review.save_pending(_entry("first@x.test"))
        check("save_pending returns an id", isinstance(lid, str) and len(lid) == 8)
        q = review.load_pending()
        check("entry stored", lid in q)
        check("status set to pending", q[lid]["status"] == "pending")

        lid2 = review.save_pending(_entry("second@x.test"))
        q = review.load_pending()
        check("a SECOND save keeps the first (no clobber)", lid in q and lid2 in q)
        check("both are live drafts",
              review.emails_with_live_draft(CLIENT) ==
              {"first@x.test", "second@x.test"})

        review.set_pending_status(lid, "declined")
        check("declining drops it from live drafts",
              review.emails_with_live_draft(CLIENT) == {"second@x.test"})


if __name__ == "__main__":
    print("=" * 62)
    print("APPROVAL QUEUE DURABILITY")
    print("=" * 62)
    test_missing_vs_corrupt()
    test_corrupt_queue_is_never_overwritten()
    test_dedupe_guard_is_not_silently_disabled()
    test_write_is_atomic()
    test_write_replaces_cleanly()
    test_normal_operation_unaffected()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
