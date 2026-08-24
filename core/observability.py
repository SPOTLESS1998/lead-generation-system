"""Observability ledger — the accounting + timing layer under every pipeline step.

This is the "did each step go according to plan, and what did it cost?" layer.
One idea: wrap any pipeline step in `track(...)` and, when the block exits, a row
lands in the `pipeline_events` table (see core/state.py) recording:

  - status: ok | error | skipped        (error is captured, not swallowed)
  - duration_ms:  wall-clock of the step
  - provider + prompt/completion tokens: the LLM spend inside the step, captured
    automatically from core.ai's thread-local accumulator — no call-site changes
  - cost_usd: tokens × the client's configured reference rate × margin

Cost model (deliberately simple, and pricing-oriented rather than billing-oriented):
the runtime LLMs are FREE (FreeLLMAPI → Gemini → NVIDIA), so real provider cost ≈ $0.
But to SET client pricing you want a defensible "what would this cost at market rate?"
number. So cost_usd = tokens priced at a configurable reference rate (per-million
input/output) × a margin multiplier. All three come from cfg["observability"]["cost"];
zeros there mean "cost tracking off, just count tokens".

Usage:

    from core import observability as obs

    with obs.track(conn, cfg, "draft", subject=lead_email, run_id=run) as ev:
        text, provider = ai.generate(cfg, prompt)   # tokens captured automatically
        ev.note(subject_line=subj)                  # optional extra meta

    # a step that has nothing to do with the LLM still gets timed + recorded:
    with obs.track(conn, cfg, "send", subject=lead_email, run_id=run):
        sender.smtp_deliver(...)

Nothing in here is allowed to break the pipeline it watches: a failure to write a
ledger row is printed and swallowed. The tracked block's OWN exception, though, is
recorded and then re-raised — observability reports reality, it doesn't hide it.
"""

import time
import uuid

from core import ai, state


def new_run_id():
    """A short id that groups every event emitted during one agent pass/run."""
    return uuid.uuid4().hex[:12]


# --- cost model -------------------------------------------------------------

def _cost_cfg(cfg):
    return (cfg.get("observability", {}) or {}).get("cost", {}) or {}


def compute_cost(cfg, prompt_tokens, completion_tokens):
    """Reference $ cost of a token spend, for pricing (NOT actual provider billing).

    cost = (prompt/1e6 · rate_in + completion/1e6 · rate_out) · margin_multiplier

    With the default zero rates this returns 0.0 — token counts are still recorded,
    so you can backfill a rate later and recompute from the raw ledger.
    """
    c = _cost_cfg(cfg)
    rate_in = float(c.get("rate_per_million_input", 0.0) or 0.0)
    rate_out = float(c.get("rate_per_million_output", 0.0) or 0.0)
    margin = float(c.get("margin_multiplier", 1.0) or 1.0)
    base = (int(prompt_tokens or 0) / 1_000_000.0) * rate_in \
         + (int(completion_tokens or 0) / 1_000_000.0) * rate_out
    return round(base * margin, 6)


# --- the track() context manager -------------------------------------------

class _Event:
    """Live handle for one tracked step; collects meta and the final outcome."""

    def __init__(self, conn, cfg, step, subject, run_id, attempt):
        self.conn = conn
        self.cfg = cfg
        self.client = cfg["client"]
        self.step = step
        self.subject = subject
        self.run_id = run_id
        self.attempt = attempt
        self.status = "ok"
        self.error = None
        self._meta = {}
        self._t0 = None

    def note(self, **kw):
        """Attach arbitrary key/values to this event's `meta` JSON blob."""
        self._meta.update(kw)
        return self

    def skip(self, reason=None):
        """Mark this step 'skipped' (e.g. nothing to do) instead of 'ok'."""
        self.status = "skipped"
        if reason:
            self._meta.setdefault("skip_reason", reason)
        return self


class track:
    """Context manager that times a pipeline step and writes a ledger row on exit.

    On entry it resets core.ai's per-thread token accumulator; on exit it collects
    whatever tokens generate()/generate_json() spent inside the block and prices
    them. If the block raises, the event is recorded status='error' with the message
    and the exception is re-raised (never swallowed).
    """

    def __init__(self, conn, cfg, step, subject=None, run_id=None, attempt=1):
        self.ev = _Event(conn, cfg, step, subject, run_id, attempt)

    def __enter__(self):
        ai.reset_usage()
        self.ev._t0 = time.monotonic()
        return self.ev

    def __exit__(self, exc_type, exc, tb):
        ev = self.ev
        duration_ms = int((time.monotonic() - ev._t0) * 1000) if ev._t0 is not None else None
        usage = ai.collect_usage()
        if exc is not None:
            ev.status = "error"
            ev.error = f"{exc_type.__name__}: {exc}"[:500]
        cost = compute_cost(ev.cfg, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        try:
            state.record_event(
                ev.conn, ev.client, ev.step, ev.status,
                subject=ev.subject, run_id=ev.run_id, attempt=ev.attempt,
                duration_ms=duration_ms, provider=usage.get("provider"),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                cost_usd=cost, error=ev.error,
                meta=ev._meta or None,
            )
        except Exception as e:  # observability must never break the pipeline
            print(f"⚠️  observability: failed to record '{ev.step}' event ({e})")
        return False  # never suppress the tracked block's own exception


# --- metrics rollup ---------------------------------------------------------

def metrics(conn, cfg, since=None):
    """Aggregate the ledger into the numbers a dashboard / client report wants.

    Returns a dict with totals, a per-step breakdown, health (error rate), and
    unit economics (cost per lead / per booked meeting). Pure reads; no mutation.
    """
    client = cfg["client"]
    rows = state.list_events(conn, client, since=since)
    total = len(rows)
    errors = sum(1 for r in rows if r["status"] == "error")
    ok = sum(1 for r in rows if r["status"] == "ok")
    skipped = sum(1 for r in rows if r["status"] == "skipped")
    prompt_tokens = sum(r["prompt_tokens"] or 0 for r in rows)
    completion_tokens = sum(r["completion_tokens"] or 0 for r in rows)
    cost = round(sum(r["cost_usd"] or 0.0 for r in rows), 6)

    by_step = {}
    for r in rows:
        s = by_step.setdefault(r["step"], {
            "events": 0, "ok": 0, "error": 0, "skipped": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
            "duration_ms": 0,
        })
        s["events"] += 1
        s[r["status"]] = s.get(r["status"], 0) + 1
        s["prompt_tokens"] += r["prompt_tokens"] or 0
        s["completion_tokens"] += r["completion_tokens"] or 0
        s["cost_usd"] = round(s["cost_usd"] + (r["cost_usd"] or 0.0), 6)
        s["duration_ms"] += r["duration_ms"] or 0

    leads = state.count_leads(conn, client)
    bookings = state.count_bookings(conn, client)
    return {
        "client": client,
        "since": since,
        "totals": {
            "events": total, "ok": ok, "error": errors, "skipped": skipped,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_usd": cost,
        },
        "by_step": by_step,
        "unit_economics": {
            "leads": leads,
            "bookings": bookings,
            "cost_per_lead": round(cost / leads, 6) if leads else 0.0,
            "cost_per_booking": round(cost / bookings, 6) if bookings else 0.0,
        },
    }
