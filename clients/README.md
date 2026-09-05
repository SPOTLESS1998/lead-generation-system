# Clients

Each client is a folder here. Everything specific to a client lives in its
folder — so standing up **Client #2** is: copy `_template/` to `clients/<name>/`,
fill in the two files, add that client's credentials to `.env`, then run with
`CLIENT=<name>`.

**No code changes needed, ever.** That is a rule, not a convenience — see
[`MULTITENANCY.md`](../MULTITENANCY.md). No business fact about a client (a service
name, an outcome figure, a brand string, a postal address) may live in Python. If you
find yourself editing code to take on a client, that's a bug in the architecture.

```
clients/
  _template/     # copy this to start a new client
    config.json
    leads.csv
  ejentic/       # the working demo client (CLIENT defaults to "ejentic")
    config.json
    leads.csv
```

Runtime state (who's been contacted, suppression list) is kept **out** of here,
in `data/<name>/state.sqlite` (git-ignored, machine-local).

## Onboarding a new client

One command copies the template and validates the result, so you find out
immediately whether the client is complete rather than mid-run:

```bash
venv/bin/python scripts/new_client.py acme \
    --name "Acme Ltd" --sender "Ada Obi" \
    --offerings "Bookkeeping Automation,Invoice Chasing" \
    --address "Acme Ltd, 4 Broad St, Lagos, Nigeria"

venv/bin/python scripts/new_client.py acme --check   # re-validate any time
```

Then, in order:

1. **Finish `clients/acme/config.json`** — the five things that are genuinely
   theirs: `offerings` (what they sell), `service_outcomes` (the real result +
   figure + timeframe for each — left as `REPLACE-ME` on purpose, because an
   invented figure would ship in real mail), `physical_address` (CAN-SPAM),
   `unsubscribe_base_url`, and `sending.mailboxes[]`.
2. **Add their SMTP credentials to `.env`.** Config only ever names env vars, so
   nothing secret is written into `clients/`.
3. **Fill `clients/acme/leads.csv`** (or set `lead_source` to `maps_firecrawl` /
   `yellowpages` and configure `discovery`).
4. **Dry run — sends nothing:**
   `CLIENT=acme venv/bin/python scripts/lead_agent.py --preview`

Every run prints the active tenant on startup (`🏢 tenant: acme (Acme Ltd) mode=controlled`)
so operating the wrong client is obvious immediately. Silence it with `ANNOUNCE_TENANT=0`.

**A client that isn't ready fails at config load, not at draft time.** Missing
`offerings` or an unfilled `client_name` raises `ConfigError` before a single LLM
call or email. There is deliberately **no default service menu** — inheriting one
would pitch someone else's services in this client's name.

## config.json

| field | meaning |
|---|---|
| `client_name` | Shown in the email footer ("You received this from …"). |
| `from_name` | Display name on the From line. |
| `reply_to` | Optional Reply-To address (`null` to omit). |
| `physical_address` | **Required by CAN-SPAM** — a real postal address, printed in every footer. |
| `website_url` | The client's own site. Informational; also used when introducing them. |
| `unsubscribe_base_url` | Base URL for unsubscribe + audit-link + one-click reply buttons. Its **port is also where the approval server binds** (`http://localhost:5002` for the demo), so links and server can't drift apart. |
| `offerings` | **Required.** The closed list of services this client sells — the *only* things the strategist may pitch. No default exists; an empty list is a `ConfigError` at load. |
| `service_outcomes` | `{service: "the concrete result, with a figure and timeframe"}`. The strategist quotes this **verbatim** instead of inventing a number, which is what kept the promise consistent between runs. Keys also count as `offerings` if you omit that list. |
| `lead_source` | Where leads come from: `"csv"` (reads `leads.csv`), `"maps_firecrawl"`, or `"yellowpages"`. |
| `discovery` | Settings for `maps_firecrawl`: `max_leads`, `max_results`, `location`, and `segments[]` — each `{name, service, queries[]}`, tagging every prospect it finds with the offering it matched. |
| `copy_provider` | `"gemini"` (mandated) or `"nvidia"` (fallback). |
| `gemini_model` / `nvidia_model` | Model IDs for each provider. |
| `freellmapi_model` | Model (or ordered list of models) for the FreeLLMAPI gateway. `null` = let the gateway auto-route. |
| `providers` | Optional explicit fallback chain, e.g. `["freellmapi","gemini","nvidia"]`. Omit to use the default chain. `anthropic` is added/removed automatically by the budget guard — don't list it here. |
| `copy.premium` | `true` = route cold-email copy to **real Claude** (the Anthropic Messages API) when today's spend is under the cap. `false` = free providers only. |
| `copy.anthropic_model` | Which Claude to use for premium copy (e.g. `claude-opus-4-8`). |
| `copy.premium_daily_usd_cap` | **Hard daily $ ceiling** on real-Claude spend, per client. Once today's metered spend reaches it, copy auto-falls back to the free chain until tomorrow. `null`/absent = uncapped. |
| `copy.quality_gate` | Optional draft-scoring gate — see below. `{enabled, min_score, max_revisions, best_of}`. |
| `demo_mode` | `false` = real AI generation. `true` = canned pitches (recording only). |
| `timezone` | IANA tz (e.g. `Africa/Lagos`) used when proposing and booking meeting times. |
| `booking.enabled` | `true` = create a real Google Calendar event when an *interested* reply is approved. |
| `booking.provider` | Booking backend — `composio_googlecalendar` (via the Composio CLI). |
| `booking.default_duration_min` | Default meeting length in minutes. |
| `booking.default_service` | Fallback service label when a lead has no `niche` tag (blank → "intro consultation"). |
| `booking.action_slug` | Composio create-event action (`GOOGLECALENDAR_CREATE_EVENT`). |
| `reminders.enabled` | `true` = the reminder agent pings you before each booked meeting. `false` = it does nothing. |
| `reminders.lead_times_min` | Minutes-before to remind, e.g. `[60, 30, 15]`. The most-urgent unsent one fires per pass. |
| `reminders.poll_seconds` | How often the agent re-checks the appointments list (loop mode). |
| `reminders.email` | `true` = email your own inbox at each reminder. |
| `reminders.desktop` | `true` = show a macOS desktop banner at each reminder (best-effort). |
| `observability.enabled` | `true` = the observer agent scans the ledger and heals faults. `false` = the pipeline still runs and is still metered, just not watched. |
| `observability.poll_seconds` | How often the observer re-scans the ledger (loop mode). |
| `observability.stall_minutes` | Per-status age thresholds, e.g. `{"queued":120}` — a lead stuck longer becomes a stall to heal. |
| `observability.max_heal_attempts` | Tries before the healer escalates a fault to you. |
| `observability.escalate_email` | `true` = email your own inbox when a fault can't be auto-healed. |
| `observability.cost` | Notional pricing model: `rate_per_million_input`/`_output` (reference $/1M tokens) × `margin_multiplier`. Zeros = count tokens only. |
| `sending.mode` | `"controlled"` = deliver to a safe inbox (demos). `"live"` = deliver to real prospects. |
| `sending.controlled_inbox_env` | Env var naming the safe inbox for controlled mode. |
| `sending.daily_global_cap` | Max sends per day across all mailboxes. |
| `sending.throttle_seconds` | `{min,max}` jittered delay between batch sends. |
| `sending.mailboxes[]` | One or more sending identities (see below). |
| `target_niches` | Informational for now; the seam for a future live Apollo pull. |

**Credentials are never stored here** — mailboxes reference env-var *names*:

```json
{ "smtp_host": "smtp.gmail.com", "smtp_port": 587,
  "user_env": "SMTP_USER", "pass_env": "SMTP_PASS", "daily_cap": 40 }
```

Add mailboxes to the array to rotate across several sending identities (each with
its own `daily_cap`). The demo runs with one.

Reply reading (Tier 2) reuses the *same* mailbox creds over IMAP, defaulting to
`imap.gmail.com:993`. Override per mailbox only if you're not on Gmail:

```json
{ "smtp_host": "smtp.gmail.com", "smtp_port": 587,
  "imap_host": "imap.gmail.com", "imap_port": 993,
  "user_env": "SMTP_USER", "pass_env": "SMTP_PASS", "daily_cap": 40 }
```

## Replies & meeting booking (Tier 2)

The reply agent reads inbound replies over **IMAP** using each mailbox's existing
`user_env`/`pass_env` — no new secret. Run it alongside the approval server:

```
python scripts/reply_agent.py     # fetch unseen replies → classify → draft or auto-suppress
```

- **Opt-outs** (`unsubscribe`, `not_interested`) are suppressed automatically — no reply, no approval.
- **interested / question / objection** replies are AI-drafted and queued for the same 1-click approval as cold pitches.
- Approving an **interested** reply books a **real Google Calendar event** (attendee follows `sending.mode`: the controlled inbox in demo, the real prospect when live) and sends a threaded confirmation.

Booking needs a one-time Google Calendar link via Composio:

```
composio link googlecalendar
```

Until that's done, approvals still send the reply — the calendar step degrades
gracefully and just asks you to book manually.

## Appointment reminders (Tier 2.5)

Once a meeting is booked, it lands on a **curated appointments list** — every
booking joined to its lead, so you see *who* is booked, *for what* (the service =
the lead's `niche`), *when*, and their *contact*. View it in the browser at
**`/appointments`** (linked from the top of the approval queue), split into
Upcoming vs Past, each row showing which of the reminders have fired.

A dedicated **reminder agent** oversees that list and pings you before each call —
at `60`, `30`, and `15` minutes before (configurable via `reminders.lead_times_min`),
on **both** an email to your own inbox **and** a macOS desktop banner:

```
python scripts/reminder_agent.py            # continuous loop (re-checks every reminders.poll_seconds)
python scripts/reminder_agent.py --once     # a single pass, then exit (for cron/launchd)
```

Each reminder fires **exactly once** (tracked in the DB), and an agent that was
offline across several thresholds sends a **single** catch-up ping — never a burst.
The reminder sentence is AI-composed via the free gateway (FreeLLMAPI → Gemini →
NVIDIA), with a fixed template fallback if every provider is down. Turn the whole
thing off per client with `reminders.enabled: false`.

## Observability & self-healing (Tier 3)

Every AI/send step in the pipeline is wrapped in an **accounting ledger**: each run
records status (ok / error / skipped), wall-clock time, the LLM provider, token
counts, and a **notional cost**. See it in the browser at **`/health`** (linked from
the top of the approval queue) — throughput, error rate, token spend, unit economics
(cost per lead / per booked meeting), a live fault list, and a per-step breakdown.

A dedicated **observer agent** watches that ledger and closes the loop:

```
python scripts/observer_agent.py            # continuous loop (re-scans every observability.poll_seconds)
python scripts/observer_agent.py --once     # a single pass, then exit (for cron/launchd)
```

- **Flag** — new error events (past a persisted watermark, so each is reacted to once)
  and **stalled** leads (stuck in a status past `stall_minutes`) become *faults*.
- **Heal** — the AI *diagnoses* each fault, but deterministic code *executes* exactly
  one action from a fixed allowlist (retry / requeue / quarantine / escalate / ignore).
  Auth/config problems always escalate; anything unresolved after `max_heal_attempts`
  escalates. **Escalation emails your own inbox** (once per fault) so a human can step in.
- **Resolve** — a fault auto-closes when its step later succeeds, or the stalled lead
  moves on. The observer never faults its own heal/observe work (no self-heal loop).

**Cost is for pricing, not billing** — the runtime LLMs are free (FreeLLMAPI → Gemini →
NVIDIA), so real provider cost ≈ $0. Set `observability.cost.rate_per_million_input`/
`_output` to a market reference rate and a `margin_multiplier` to see "what would this
cost at scale, and what should I charge?" Leave them at `0.0` to just count tokens.
Turn the whole layer off per client with `observability.enabled: false` (the pipeline
still runs and is still metered — only the watching/healing stops).

## Premium copy & the quality gate

The pitch is what converts, so the copy path can run on **real Claude** and be
**scored before it ships**. Both are optional and both degrade gracefully.

**Premium copy (`copy.premium`)** — when on, each draft first checks today's
real-Claude spend against `copy.premium_daily_usd_cap`. Under the cap, the copy LLM
tries **Anthropic first**, then the free chain; at/over the cap, `anthropic` is
dropped and it's the free chain only. Spend is reconstructed from the ledger
(`/health` shows *premium model · spent today · remaining*). No key in `.env` →
premium is simply **dormant** and the free chain answers — nothing breaks.

```json
"copy": {
  "premium": true,
  "anthropic_model": "claude-opus-4-8",
  "premium_daily_usd_cap": 5.0
}
```

> Add `ANTHROPIC_API_KEY=sk-ant-…` to `.env` (git-ignored) to activate it. The cap
> protects the shared dev-credit pool — a bulk run can never blow past it, because
> the check runs *before* every draft and unknown models are priced as Opus (the
> expensive tier), so the guard errs toward stopping early.

**Quality gate (`copy.quality_gate`)** — an LLM-as-judge scores every draft 1-10
against a fixed rubric and rewrites anything below `min_score`, up to `max_revisions`
times; `best_of` drafts N candidates and keeps the highest. If the judge LLM is
unavailable the draft simply ships **unscored** (never blocks the pipeline).

```json
"copy": {
  "premium": true,
  "anthropic_model": "claude-opus-4-8",
  "premium_daily_usd_cap": 5.0,
  "quality_gate": { "enabled": true, "min_score": 8, "max_revisions": 1, "best_of": 2 }
}
```

## leads.csv

Header row must be exactly:

```
first_name,last_name,title,email,company_name,company_description,website_url
```

Fill it from an Apollo/ContactOut export or a spreadsheet. Rows whose `email` is
blank, starts with `#`, or starts with `REPLACE-ME` are ignored — handy for
guidance/comment rows. `company_description` feeds the AI's personalization, so
make it specific.
