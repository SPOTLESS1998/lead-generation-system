"""Resend the operator approval email for every pending draft.

Notifications are best-effort: a draft is saved to the queue BEFORE the email is
attempted (so a lead is never lost), which means a flaky Gmail SMTP handshake can
leave drafts sitting in the queue with no alert in the operator's inbox. This
re-sends the APPROVE/DECLINE email for every lead still 'pending', retrying each
a few times to ride out a transient timeout. It only notifies — it never changes
queue state — so it is safe to run as often as needed.

Run:  venv/bin/python -u scripts/resend_alerts.py
"""

import warnings
warnings.filterwarnings("ignore")

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # repo root -> `core`

from core import config, review

ATTEMPTS = 3


def main():
    cfg = config.load_client()
    pending = {
        lid: e for lid, e in review.load_pending().items()
        if e.get("status") == "pending" and e.get("client") == cfg["client"]
    }
    if not pending:
        print("Nothing pending to notify.")
        return

    print(f"Resending approval alerts for {len(pending)} pending draft(s) "
          f"to {os.environ.get('SMTP_USER', '(SMTP_USER unset)')}\n")

    ok = fail = 0
    for lid, entry in pending.items():
        who = entry.get("company_name") or entry.get("target_email") or lid
        sent = False
        for i in range(ATTEMPTS):
            if review.notify_operator(cfg, lid, entry):
                sent = True
                break
            if i < ATTEMPTS - 1:
                wait = 2 * (i + 1)
                print(f"   ...retry {i + 2}/{ATTEMPTS} for {who} in {wait}s")
                time.sleep(wait)
        if sent:
            ok += 1
        else:
            fail += 1

    print(f"\nDone. {ok} alert(s) delivered, {fail} still failing.")
    if fail:
        print("The failed ones are still safely in the queue — re-run this "
              "script once the network settles.")


if __name__ == "__main__":
    main()
