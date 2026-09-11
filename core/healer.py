"""Self-healing agent — the second half of the observability layer.

When a pipeline step faults (an error row in pipeline_events) or a lead stalls, a
`faults` row is opened (see core/state.py). This module works those faults:

    1. diagnose()      — the AI reads the fault and PROPOSES one remediation
    2. decide_action() — a deterministic safety governor makes the FINAL call
    3. _execute()      — deterministic code carries out that one action

THE SAFETY BOUNDARY (important): the AI only ever *suggests*; it can never execute
anything, and its suggestion is clamped to a fixed allowlist. Every actual action is
plain code doing one of a few safe things — re-queue an item, quarantine bad data,
note a retry, or escalate to a human. The healer NEVER composes or sends anything to
a prospect, never books, never touches credentials. Two classes of fault ALWAYS go
to a human no matter what the AI thinks: auth/credential problems and configuration
problems. And once a fault has been retried `max_heal_attempts` times, it escalates.

Escalation = mark the fault 'escalated' and email the operator's own inbox once
(smtp_user -> smtp_user, the same safe channel reminders use). Idempotent.

    from core import healer
    healer.attempt_heal(cfg, conn, dict(fault_row))   # returns the action taken
"""

import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from core import ai, state, sender
from core import observability as obs


# The ONLY remediations that may ever be taken. Nothing outside this set runs.
SAFE_ACTIONS = ("retry", "requeue", "quarantine", "escalate", "ignore")

# What the AI is permitted to suggest (a subset — it may not choose "ignore",
# which is reserved for deterministic code closing a known-benign fault).
_AI_ACTIONS = ("retry", "requeue", "quarantine", "escalate")

# Fault kinds that must ALWAYS go to a human — never auto-healed.
HARD_ESCALATE_KINDS = ("auth", "config")

# Deterministic default remediation for each kind (used when the AI is unavailable
# or suggests something invalid).
DEFAULT_ACTION = {
    "transient": "retry",       # timeouts, 5xx, dropped connections — re-run
    "rate_limit": "retry",      # 429/quota — next pass is a natural backoff
    "stall": "requeue",         # stuck in a state too long — put it back in the queue
    "data": "quarantine",       # bad/invalid data — stop churning on it
    "auth": "escalate",         # bad/missing credentials — a human must fix
    "config": "escalate",       # misconfiguration — a human must fix
    "unknown": "escalate",      # unclear — never guess; hand to a human
}


def classify_fault(detail, step=None, kind_hint=None):
    """Deterministic first-pass classification from the error text.

    `kind_hint` (e.g. 'stall', supplied by the observer when a lead sits too long)
    wins when given. Otherwise we pattern-match the error string. This runs BEFORE
    any AI call, so classification never depends on a working LLM.
    """
    if kind_hint in DEFAULT_ACTION:
        return kind_hint
    t = (detail or "").lower()

    def has(*subs):
        return any(s in t for s in subs)

    if has("401", "403", "unauthorized", "invalid api key", "authentication",
           "api key not", "not set", "forbidden"):
        # creds/config both surface as "missing"/"unauthorized"; split by wording
        if has("not set", "missing", "no mailboxes", "no config", "configerror"):
            return "config"
        return "auth"
    if has("429", "rate limit", "quota", "resource exhausted", "too many requests"):
        return "rate_limit"
    if has("timeout", "timed out", "temporarily", "connection", "502", "503",
           "504", "500", "network", "reset by peer", "unavailable"):
        return "transient"
    if has("invalid email", "malformed", "no email", "cannot parse", "could not parse",
           "decode", "not a valid", "missing email"):
        return "data"
    if has("no mailboxes", "no config", "configerror", "misconfigured"):
        return "config"
    return "unknown"


def decide_action(kind, suggestion, attempts, max_attempts):
    """The safety governor: the FINAL action, constraining the AI's suggestion.

    - auth/config always escalate (a human must act).
    - out of retry budget => escalate.
    - otherwise honor the AI suggestion IF it is an allowed safe action; else fall
      back to the deterministic default for the kind.

    The AI can never widen the action beyond the allowlist, and can never keep a
    hard-escalate fault away from a human.
    """
    if kind in HARD_ESCALATE_KINDS:
        return "escalate"
    if attempts >= max_attempts:
        return "escalate"
    if suggestion in _AI_ACTIONS:
        return suggestion
    return DEFAULT_ACTION.get(kind, "escalate")


def diagnose(cfg, fault):
    """Ask the healing AI to classify the fault and propose ONE safe action.

    Returns (kind, suggestion, reason). Advisory only — decide_action() has the
    final say. Falls back to the deterministic heuristic if the gateway is down, so
    the healer keeps working even with no LLM available.
    """
    kind = classify_fault(fault.get("detail"), fault.get("step"), fault.get("kind"))
    prompt = f"""You are the self-healing operations agent for an automated cold-email pipeline.
A pipeline step faulted. Classify it, then choose EXACTLY ONE remediation from this
fixed list — no other value is permitted:
- retry: a transient/temporary error (timeout, rate limit, 5xx); re-running will likely work
- requeue: the item stalled or was skipped; put it back in the queue to try again
- quarantine: the item's data is bad (e.g. an invalid email address); stop processing it
- escalate: a human is required (credentials, configuration, or anything you are unsure about)

Fault:
- step: {fault.get('step')}
- heuristic kind: {kind}
- detail: {fault.get('detail')}
- attempts so far: {fault.get('attempts', 0)}

Reply with ONLY a JSON object: {{"kind": "...", "action": "...", "reason": "<=20 words"}}"""
    try:
        obj, _ = ai.generate_json(cfg, prompt)
        k = obj.get("kind") if obj.get("kind") in DEFAULT_ACTION else kind
        reason = (obj.get("reason") or "").strip()[:300]
        return k, obj.get("action"), reason
    except Exception as e:
        return kind, DEFAULT_ACTION.get(kind), f"AI diagnosis unavailable ({e}); used heuristic."


def attempt_heal(cfg, conn, fault):
    """Run one heal cycle on an open fault. Returns the action actually taken.

    `fault` is a dict (dict(row) from state.open_faults). The whole cycle is wrapped
    in observability.track(), so the healer's OWN token/time cost is metered too.
    """
    fault = dict(fault)
    max_attempts = int((cfg.get("observability", {}) or {}).get("max_heal_attempts", 3))
    attempts = int(fault.get("attempts", 0)) + 1

    with obs.track(conn, cfg, "heal", subject=fault.get("subject"),
                   run_id=fault.get("run_id")) as ev:
        kind, suggestion, reason = diagnose(cfg, fault)
        action = decide_action(kind, suggestion, attempts, max_attempts)
        ev.note(fault_id=fault.get("id"), kind=kind, ai_suggestion=suggestion,
                action=action, attempt=attempts, reason=reason)
        _execute(cfg, conn, fault, kind, action, reason, attempts)
    return action


def _execute(cfg, conn, fault, kind, action, reason, attempts):
    """Carry out exactly one safe action. Plain deterministic code — no AI here."""
    client = cfg["client"]
    fid = fault["id"]
    subject = fault.get("subject")

    if action == "requeue":
        if subject:
            # A stalled lead is claimed-but-unfinished (status_changed_at tells us how
            # long). Returning it to `sourced` genuinely puts it back in the drafting
            # pool; setting it to "queued" — what this used to do — assigned the status
            # it was already stuck in, so the fault could never resolve and the healer
            # re-diagnosed it (an LLM call per pass) until it escalated to a human.
            state.set_status(conn, client, subject, state.SOURCED)
        state.update_fault(conn, fid, status="healing", action=action, kind=kind,
                           attempts=attempts, resolution=f"requeued: {reason}"[:300])

    elif action == "quarantine":
        if subject:
            # Terminal, safe: the pipeline won't re-pick a quarantined lead. This is
            # about bad DATA, not an opt-out, so it is distinct from suppression.
            state.set_status(conn, client, subject, "quarantined")
        state.update_fault(conn, fid, status="resolved", action=action, kind=kind,
                           attempts=attempts, resolution=f"quarantined bad data: {reason}"[:300])

    elif action == "retry":
        # No state change: the pipeline re-attempts this step on its next pass. We
        # just record the attempt; the observer resolves the fault once a later
        # success (an 'ok' event for this subject/step) shows the retry worked.
        state.update_fault(conn, fid, status="healing", action=action, kind=kind,
                           attempts=attempts, resolution=f"retry scheduled: {reason}"[:300])

    elif action == "ignore":
        state.update_fault(conn, fid, status="resolved", action=action, kind=kind,
                           attempts=attempts, resolution=f"ignored (benign): {reason}"[:300])

    else:  # escalate (default for anything unrecognized — fail safe to a human)
        escalate(cfg, conn, fault, reason=reason, attempts=attempts, kind=kind)


def escalate(cfg, conn, fault, reason="", attempts=0, kind=None):
    """Hand a fault to a human: mark it 'escalated' and email the operator once.

    Idempotent — a fault already 'escalated' is not re-emailed. Honors the
    observability.escalate_email toggle (off => mark escalated without emailing).
    """
    fid = fault["id"]
    if fault.get("status") == "escalated":
        return
    emailed = False
    if (cfg.get("observability", {}) or {}).get("escalate_email", True):
        try:
            _email_operator(cfg, fault, reason, attempts)
            emailed = True
        except Exception as e:
            print(f"⚠️  healer: operator escalation email failed ({e})")
    note = f"escalated to human{' (emailed)' if emailed else ''}: {reason}"
    state.update_fault(conn, fid, status="escalated", action="escalate",
                       kind=kind or fault.get("kind"), attempts=attempts,
                       resolution=note[:300])


def _email_operator(cfg, fault, reason, attempts):
    """Plain-text alert to the operator's own inbox (smtp_user -> smtp_user)."""
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    subject = f"🛠️ Manual fix needed — {fault.get('step')} fault ({cfg['client']})"
    body = (
        "The self-healing agent could not automatically resolve a pipeline fault "
        "and is escalating it to you.\n\n"
        f"Client:    {cfg.get('client_name') or cfg['client']}\n"
        f"Step:      {fault.get('step')}\n"
        f"Subject:   {fault.get('subject') or '(none)'}\n"
        f"Kind:      {fault.get('kind')}\n"
        f"Attempts:  {attempts}\n"
        f"Detail:    {fault.get('detail')}\n\n"
        f"Why escalated: {reason}\n\n"
        "Please investigate and repair this manually."
    )
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{cfg.get('client_name') or cfg['client']} Ops <{smtp_user}>"
    msg["To"] = smtp_user
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    sender.smtp_deliver("smtp.gmail.com", 587, smtp_user, smtp_pass,
                        smtp_user, smtp_user, msg.as_string())
