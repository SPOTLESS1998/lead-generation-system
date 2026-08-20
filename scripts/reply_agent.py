import warnings
warnings.filterwarnings('ignore')

import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Make the project root importable so `core` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, state, suppression, review, inbox
from core.ai import generate_json

# The intents the classifier may assign. Anything else is treated as "question"
# (route to a human) so an odd model response never triggers a wrong auto-action.
INTENTS = ("interested", "question", "objection", "not_interested", "unsubscribe", "auto_reply")

# Safety valve per run.
MAX_PER_RUN = 50


def print_step(step):
    print(f"\n[{time.strftime('%H:%M:%S')}] {step}")


# --------------------------------------------------------------------------
# AI: classify intent, then (for a human-worthy reply) draft the response.
# Both degrade safely — if the model/keys are unavailable we route to a human
# with no drafted text rather than crash or mis-suppress.
# --------------------------------------------------------------------------

def classify_reply(cfg, reply):
    prompt = f"""
You are an SDR assistant for '{cfg['client_name']}', which sells AI automation
services. A prospect replied to our cold outreach email. Classify their intent.

PROSPECT REPLY (from {reply.get('from_name') or reply['from_email']}):
\"\"\"
{reply['body_text'][:1500]}
\"\"\"

Choose exactly ONE intent:
- "interested": wants a call/demo/more info, or signals they want to move forward.
- "question": has a genuine question but has not committed yet.
- "objection": skeptical or raises a concern (price, timing, fit) — a soft push-back, not a hard no.
- "not_interested": a polite/soft no; not interested right now.
- "unsubscribe": asks to stop contact, be removed, opt out, or "do not email me".
- "auto_reply": an out-of-office autoresponder, delivery/bounce notice, or non-human reply.

Reply with ONLY a JSON object, no prose:
{{"intent": "<one intent above>", "reason": "<=12 words"}}
"""
    obj, provider = generate_json(cfg, prompt)
    intent = (obj.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = "question"
    return intent, obj, provider


def draft_reply(cfg, reply, intent):
    """Draft the outbound reply. For 'interested', also propose a meeting."""
    tz = cfg.get("timezone", "UTC")
    default_dur = int(cfg.get("booking", {}).get("default_duration_min", 30))
    # Anchor the model to the real current date in the client's timezone — without
    # this it invents a date (and picked one two years in the past in testing).
    try:
        today = datetime.now(ZoneInfo(tz))
    except Exception:
        today = datetime.now()
    today_str = today.strftime("%A, %Y-%m-%d")  # e.g. "Thursday, 2026-08-20"
    if intent == "interested":
        schema = ('{{"reply_body": "<the email reply>", '
                  '"meeting_title": "<short calendar title>", '
                  '"proposed_start_local": "YYYY-MM-DD HH:MM", '
                  '"duration_min": %d}}' % default_dur)
        extra = (f"The prospect is interested. Propose ONE specific meeting time 2-4 business "
                 f"days AFTER today, on a real future calendar date (never today or a past date), "
                 f"during business hours in the {tz} timezone, and mention that day and time in "
                 f"the reply. proposed_start_local MUST be a date strictly after {today_str}. "
                 f"Keep it warm, concise (<120 words), human, no buzzwords.")
    else:
        schema = '{"reply_body": "<the email reply>"}'
        extra = ("Write a helpful, concise (<120 words), human reply that addresses their "
                 "message and gently moves toward a short intro call. No buzzwords.")
    prompt = f"""
You are a friendly SDR for '{cfg['client_name']}'. Today's date is {today_str} ({tz}).
Write a reply to this prospect message. Sign off as
'{cfg.get('from_name') or cfg['client_name']}'. Do not include a Subject line. {extra}

Deliverability: use plain, natural language. Avoid spam-trigger words (free money,
guarantee, act now, limited time, click here, urgent), no ALL-CAPS words, at most one
exclamation mark, and only ever use https links.

PROSPECT MESSAGE (from {reply.get('from_name') or reply['from_email']}):
\"\"\"
{reply['body_text'][:1500]}
\"\"\"

Reply with ONLY a JSON object, no prose:
{schema}
"""
    obj, provider = generate_json(cfg, prompt)
    return obj, provider


def _safe_future_slot(proposed, tz, min_days=2, hour=11):
    """Return a valid 'YYYY-MM-DD HH:MM' that is strictly in the future.

    The model can still propose a past or unparseable date despite the prompt;
    never let that reach the calendar. Keeps a good proposal as-is, otherwise
    falls back to `min_days` days out at `hour`:00 local, skipping weekends.
    """
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.now()
    if proposed:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
            try:
                dt = datetime.strptime(proposed.strip(), fmt).replace(tzinfo=now.tzinfo)
                if dt > now:
                    return dt.strftime("%Y-%m-%d %H:%M")
                break
            except ValueError:
                continue
    dt = (now + timedelta(days=min_days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    while dt.weekday() >= 5:  # nudge Sat/Sun → Monday
        dt += timedelta(days=1)
    return dt.strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# Threading helpers
# --------------------------------------------------------------------------

def _reply_subject(subject):
    s = (subject or "").strip()
    if not s:
        return "Re: your message"
    return s if s.lower().startswith("re:") else f"Re: {s}"


def _thread_headers(reply):
    """Compute (in_reply_to, references) for OUR outgoing reply.

    We are replying to the prospect's message, so In-Reply-To is their Message-ID
    and References is the full chain (our original send + their message).
    """
    refs = list(reply.get("references") or [])
    if reply.get("in_reply_to") and reply["in_reply_to"] not in refs:
        refs.append(reply["in_reply_to"])
    if reply.get("message_id") and reply["message_id"] not in refs:
        refs.append(reply["message_id"])
    return reply.get("message_id"), (" ".join(refs) if refs else reply.get("message_id"))


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def main():
    cfg = config.load_client()
    print("=========================================================")
    print(f"📥 {cfg['client_name']} - Reply Reader & Responder")
    print(f"   client={cfg['client']}  provider={cfg['copy_provider']}  "
          f"send_mode={cfg['sending']['mode']}")
    print("=========================================================")

    conn = state.connect(cfg["paths"]["db"])
    processed = 0

    for mailbox in cfg["sending"]["mailboxes"]:
        print_step(f"🔌 Checking {mailbox['address']} for new replies...")
        try:
            replies = inbox.fetch_replies(cfg, mailbox)
        except Exception as e:
            print(f"   ❌ IMAP fetch failed for {mailbox['address']}: {e}")
            continue
        print(f"   Found {len(replies)} unseen message(s).")

        for r in replies:
            if processed >= MAX_PER_RUN:
                print_step(f"⏹️  Reached MAX_PER_RUN ({MAX_PER_RUN}); stopping this run.")
                break
            if not r["message_id"]:
                continue  # can't dedup without a Message-ID; skip defensively
            if state.reply_seen(conn, cfg["client"], r["message_id"]):
                continue  # already processed on a previous run

            # Match this reply back to the send that triggered it.
            lookup_ids = ([r["in_reply_to"]] if r["in_reply_to"] else []) + list(r["references"])
            lead_email = state.lead_for_message_id(conn, cfg["client"], lookup_ids)
            if not lead_email:
                print(f"   ⏭️  Reply from {r['from_email']} matches no known send; skipping.")
                continue

            # Classify (safe default = 'question' → human review, never auto-suppress).
            try:
                intent, meta, provider = classify_reply(cfg, r)
            except Exception as e:
                print(f"   ⚠️  Classification failed ({e}); routing to human as 'question'.")
                intent, meta, provider = "question", {}, "none"

            state.record_reply(conn, cfg["client"], lead_email, r["message_id"],
                               r["in_reply_to"], r["subject"], r["body_text"], intent)
            processed += 1
            print(f"   📨 {lead_email}: intent={intent} (via {provider}).")

            # --- route by intent ---------------------------------------------
            if intent in ("unsubscribe", "not_interested"):
                reason = "unsubscribed" if intent == "unsubscribe" else "not_interested"
                suppression.add(conn, cfg["client"], lead_email, reason=reason)
                print(f"      🔕 Auto-suppressed ({reason}); no reply sent, no approval needed.")
                continue
            if intent == "auto_reply":
                print("      🤖 Auto-reply / OOO — ignored.")
                continue

            # interested / question / objection → draft a reply for 1-click approval.
            try:
                draft, dprovider = draft_reply(cfg, r, intent)
                reply_body = (draft.get("reply_body") or "").strip()
            except Exception as e:
                print(f"      ⚠️  Draft generation failed ({e}); queuing for manual reply.")
                draft, reply_body = {}, ""

            in_reply_to, references = _thread_headers(r)
            entry = {
                "kind": "reply",
                "client": cfg["client"],
                "target_email": lead_email,
                "intent": intent,
                "incoming_snippet": r["body_text"][:800],
                "drafted_subject": _reply_subject(r["subject"]),
                "drafted_body": reply_body,
                "in_reply_to": in_reply_to,
                "references": references,
            }
            if intent == "interested":
                entry["meeting"] = {
                    "title": (draft.get("meeting_title") or f"Intro call — {cfg['client_name']}"),
                    "start_local": _safe_future_slot(draft.get("proposed_start_local"),
                                                     cfg.get("timezone", "UTC")),
                    "duration_min": int(draft.get("duration_min")
                                        or cfg.get("booking", {}).get("default_duration_min", 30)),
                    "attendee_email": lead_email,
                }

            lead_id = review.save_pending(entry)
            review.notify_operator(cfg, lead_id, entry)
            state.set_status(conn, cfg["client"], lead_email,
                             "interested" if intent == "interested" else "replied")
            print(f"      ✉️  Draft reply queued for approval ({lead_id}).")

    print(f"\n🎉 Done. Processed {processed} new repl(y/ies). "
          f"Approve queued replies at {cfg.get('unsubscribe_base_url')}.")


if __name__ == "__main__":
    main()
