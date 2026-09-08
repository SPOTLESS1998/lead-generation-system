# Lead Generation System

An autonomous outbound SDR pipeline: it finds businesses that fit a client's offering, researches
each one, writes a personalised pitch, **holds every message for human approval**, then sends on a
throttled schedule and books the replies into meetings.

It is built on the **agency model** — one deployment per client, on that client's own
infrastructure. The same codebase serves any client unchanged, because everything that makes the
pipeline "theirs" lives in configuration rather than code (see [MULTITENANCY.md](MULTITENANCY.md)).

## The pipeline

| Stage | Module | What it does |
|---|---|---|
| Discovery | `core/discovery.py` | Google Maps discovery → Firecrawl enrichment → LLM extraction of contact + business detail |
| Lead intake | `core/leads.py`, `core/yellowpages.py` | Normalises leads from discovery or a curated CSV |
| Suppression | `core/suppression.py`, `core/spam.py` | Drops anyone already contacted, unsubscribed, or spam-trappy |
| Drafting | `scripts/lead_agent.py`, `core/ai.py` | Writes the pitch from a **closed menu** of what the client actually sells — the model cannot invent a service |
| Quality gate | `core/quality.py` | LLM-as-judge scores each draft and automatically rewrites weak copy |
| Compliance | `core/compliance.py` | CAN-SPAM footer plus a signed unsubscribe token per recipient |
| **Human approval** | `core/review.py`, `scripts/approval_server.py` | Every draft waits in a web queue. Nothing sends unapproved. |
| Sending | `core/sender.py` | Throttled and hard-capped, with sender rotation support |
| Booking | `core/scheduling.py`, `core/calendar.py`, `core/reminders.py` | Picks a strictly-future slot, books it, schedules reminders |
| Accounting | `core/observability.py`, `core/healer.py`, `core/budget.py` | Ledger + timing for every step, a self-healing pass over failures, and a hard spend cap |

## Design decisions worth knowing

**A human approves every send.** The approval queue is not a convenience feature, it is the
architecture. Fully automated cold outreach is how you burn a domain and a reputation at the same
time; the operator sees each message before a stranger does.

**The model works from a closed menu.** `_client_offerings()` reads the services list from the
client's own config, and the strategist may only pitch from it. This is what stops a language model
from confidently offering a service the client does not sell.

**Copy quality is scored, not assumed.** `core/quality.py` runs an LLM-as-judge pass and rewrites
anything weak, so a bad generation gets caught before it reaches the queue rather than after.

**Compliance is built in, not bolted on.** Every message carries the required footer and a signed,
per-recipient unsubscribe token.

**Spend is capped in code.** `core/budget.py` enforces a hard ceiling on premium-model calls, so a
runaway loop cannot quietly drain an API budget.

## Layout

```
core/            the pipeline stages (see table above)
scripts/         entry points — lead_agent.py (draft), approval_server.py (review queue)
clients/         per-client configuration
  _template/     copy this to onboard a new client
  demo/          fabricated demo tenant, safe to run
  ejentic/       the first-party tenant
deploy/          Caddy config, systemd unit, go-live runbook
tests/           pytest suite covering discovery, multi-tenancy, copy grounding,
                 appointments, reminders, observability and the self-healer
```

## Running it

```bash
cp .env.example .env          # then fill in your API keys — .env is gitignored
./run.sh                      # starts the approval server on :5001, then the drafting agent
```

Onboard a client by copying `clients/_template/` to `clients/<name>/`, filling in `config.json`
and `leads.csv`, and pointing the run at it. No Python changes — if a client ever needs a code
change, that is a bug in the architecture.

```bash
pytest                        # run the suite
```

## About the data in this repo

**Every lead in this repository is fabricated.** `clients/demo/leads.csv` holds invented companies,
`clients/ejentic/leads.csv` contains only `REPLACE-ME` guidance rows, and
`mock_apollo_database.json` is a mock fixture — the filename is not a euphemism. No real prospect's
contact details are committed here, and real credentials live only in the gitignored `.env`.

## Status

Working and deployed, with the approval queue running behind a reverse proxy (see
[deploy/GO_LIVE_RUNBOOK.md](deploy/GO_LIVE_RUNBOOK.md)). The pipeline itself currently runs from a
workstation rather than a server — moving it to always-on hosting is the next step.
