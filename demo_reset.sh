#!/bin/bash
# Repeatable, SAFE reset between demo takes.
# Clears ONLY the demo client's state + its pending drafts. Your real ejentic
# data and queue are never touched.
set -e
export R="$(cd "$(dirname "$0")" && pwd)"

# 1) Fresh demo state DB (isolated per-client; ejentic's DB is a different file).
rm -f "$R/data/demo/state.sqlite" "$R/data/demo/state.sqlite-wal" "$R/data/demo/state.sqlite-shm"

# 2) Remove only client=="demo" entries from the shared pending queue.
"$R/venv/bin/python" - <<'PY'
import json, os
root = os.environ["R"]
p = os.path.join(root, "pending_leads.json")
if os.path.exists(p):
    try:
        d = json.load(open(p))
    except Exception:
        d = {}
    before = len(d)
    d = {k: v for k, v in d.items() if v.get("client") != "demo"}
    json.dump(d, open(p, "w"), indent=2)
    print(f"[reset] pending queue: removed {before - len(d)} demo entr(ies); {len(d)} other entr(ies) kept.")
else:
    print("[reset] no pending_leads.json yet — nothing to clear.")
PY

echo "[reset] demo state cleared. Ready for a fresh take. (ejentic data untouched.)"
