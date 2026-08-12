# Clients

Each client is a folder here. Everything specific to a client lives in its
folder — so standing up **Client #2** is: copy `_template/` to `clients/<name>/`,
fill in the two files, add that client's credentials to `.env`, then run with
`CLIENT=<name>`.

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

## config.json

| field | meaning |
|---|---|
| `client_name` | Shown in the email footer ("You received this from …"). |
| `from_name` | Display name on the From line. |
| `reply_to` | Optional Reply-To address (`null` to omit). |
| `physical_address` | **Required by CAN-SPAM** — a real postal address, printed in every footer. |
| `unsubscribe_base_url` | Base URL for unsubscribe links (`http://localhost:5001` for the demo). |
| `copy_provider` | `"gemini"` (mandated) or `"nvidia"` (fallback). |
| `gemini_model` / `nvidia_model` | Model IDs for each provider. |
| `demo_mode` | `false` = real AI generation. `true` = canned pitches (recording only). |
| `timezone` | IANA tz (e.g. `Africa/Lagos`) used when proposing and booking meeting times. |
| `booking.enabled` | `true` = create a real Google Calendar event when an *interested* reply is approved. |
| `booking.provider` | Booking backend — `composio_googlecalendar` (via the Composio CLI). |
| `booking.default_duration_min` | Default meeting length in minutes. |
| `booking.action_slug` | Composio create-event action (`GOOGLECALENDAR_CREATE_EVENT`). |
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

## leads.csv

Header row must be exactly:

```
first_name,last_name,title,email,company_name,company_description,website_url
```

Fill it from an Apollo/ContactOut export or a spreadsheet. Rows whose `email` is
blank, starts with `#`, or starts with `REPLACE-ME` are ignored — handy for
guidance/comment rows. `company_description` feeds the AI's personalization, so
make it specific.
