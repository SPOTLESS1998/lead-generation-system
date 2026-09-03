# Ejentic AI Lead-Gen — Screen-Recording Strategy

**Goal of this recording:** show a client the *whole* system — the stack it's built on, every
feature, and (most important) the **measurement + monitoring layer** — so they can see we are
**watching every step and writing down what happens**. This is the "advertise-the-prototype" cut.

> This is the richer companion to `DEMO_SCRIPT.md` (which is the short 8-step sales story).
> Use this one when the point is "look how much is under the hood and how closely it's watched."

---

## The one idea that makes this recording win

A client's real fear isn't "can AI write an email." It's **"can I trust a machine to run outreach
without me babysitting it."** So the spine of this recording is **monitoring**: we keep a
**mission-control view on screen the entire time**, and everything else — finding leads, writing,
booking — happens *inside that watched frame*. Every act, we glance back at the dashboard and the
ledger and say some version of *"and it wrote that step down."*

---

## Record with confidence — the safety facts

- **Controlled mode is ON.** Every email and calendar invite is redirected to **your own inbox**.
  No real company is ever contacted. (Verified: `sending.mode: "controlled"` in the demo config.)
- **`discover_preview.py` and `lead_agent.py --preview` send and save *nothing*** — 100% safe,
  re-runnable on camera.
- **Reset anytime:** `bash demo_reset.sh` clears only the demo client. Your real `ejentic` data is
  a separate file and is never touched.
- The dashboard header reads **"Ejentic AI"** (the client's brand name), not "demo" — don't let
  that confuse the narration.

---

## Say this, not that — 3 honesty rules (a client could fact-check these)

| Don't say | Say instead | Why |
|---|---|---|
| "It's **stdlib-only** Python" | "**Minimal-dependency** — no heavy frameworks" | There are 6 small packages: `requests`, `beautifulsoup4`, `python-dotenv`, `googlesearch-python`, `google-genai`, `Flask`. The data/email/crypto/scheduling **core** is pure standard library — that's the true, strong claim. |
| "It costs **$0 per lead**" (as a hard fact) | "Routine copy runs on **free providers at zero marginal cost**; the dashboard's cost-per-lead is a **pricing model you set per client**" | Free providers genuinely cost nothing, but the dashboard's dollar figures default to $0 until you set a reference rate. Only **premium (Claude) spend is real metered money**. |
| "**Claude Opus** wrote these emails" | "This runs on the **free provider chain**; premium **Claude is on tap** when a client wants top-tier copy" | For the demo client, premium is **off** — the free chain writes the copy. Opus is real but dormant until a key + `premium: true` are set. |

---

## Screen setup — build "mission control" BEFORE you press record

Arrange **four zones** so the "we're watching everything" story is visible the whole time.

**First, one line for clean output in every terminal (kills a harmless OpenSSL warning):**
```
export PYTHONWARNINGS=ignore
cd ~/Antigravity/lead-generation-system
```

**(Optional) Start the FreeLLMAPI gateway** if you want to *show* it on camera and use it as the
primary copy engine. It's currently **stopped** — the demo still works without it (it falls back to
Gemini), so this is optional:
```
npm --prefix ~/Antigravity/freellmapi run dev -w server
```

**Zone 1 — Browser tab: the health dashboard.** Start the server, then open the page and leave it up:
```
CLIENT=demo venv/bin/python scripts/approval_server.py     # leave running
```
Open **`http://localhost:5002/health`**. It's static HTML — **reload it after each step** so the
numbers visibly climb. This is your anchor visual. (Queue = `/`, meetings = `/appointments`.)

**Zone 2 — Terminal: the watchdog (the "always watching" heartbeat).**
```
CLIENT=demo venv/bin/python scripts/observer_agent.py      # leave running (polls every 60s)
```
Prints `🔍 Scanning the pipeline ledger…` every minute. On the demo's queued leads it will visibly
**flag a stall and auto-requeue it** — a real self-healing moment (safe: a requeue never sends
anything, and it only emails you if it *can't* fix something).

**Zone 3 — Terminal: where you run each step** (the "Tab B" of the demo).

**Zone 4 (optional but killer) — Terminal: the live ledger tail.** The audience literally watches
rows being written:
```
while true; do clear; CLIENT=demo PYTHONWARNINGS=ignore venv/bin/python -c "
import sqlite3
c=sqlite3.connect('file:data/demo/state.sqlite?mode=ro',uri=True); c.row_factory=sqlite3.Row
print('PIPELINE LEDGER — one permanent note per step\n')
for r in c.execute('SELECT id,step,status,provider,prompt_tokens+completion_tokens tok,round(duration_ms/1000.0,1) s,substr(created_at,12,8) t FROM pipeline_events ORDER BY id DESC LIMIT 15'):
    print(f\"#{r['id']:>3}  {r['step']:<7} {r['status']:<7} {(r['provider'] or '-'):<11} {r['tok']:>6} tok {r['s']:>6}s  {r['t']}\")"; sleep 2; done
```

Also open browser tabs for **Gmail** and **Google Calendar**.

---

## Pick your take

- **PATH A — "The finished machine, watched live" (RECOMMENDED).** Reliable, ~6–8 min, no
  dependence on a live 4-minute drafting run. Uses the 3 drafts already in the queue (verified live).
  The monitoring surfaces are already populated and the watchdog heals live.
- **PATH B — "Watch it build from empty."** Fuller and more impressive (you see it draft + grade
  live and the dashboard climb in real time), ~10–12 min, needs the free providers behaving. Trim
  the slow drafting in edit. See the short delta at the end.

---

# PATH A — shot by shot

Each act: **the command** → **See** (what appears) → **Point** (where to aim the camera) →
**Say** (the line). Glance at Zone 1/2/4 between acts and note the numbers moving.

### Act 1 — "What it's built on" (stack, ~45s)
```
cat requirements.txt
ls clients/
cat clients/demo/config.json
```
**See:** six small dependencies; one folder per client; a readable JSON config.
**Point:** the short `requirements.txt`; the `clients/` list.
**Say:** *"It's minimal-dependency — no heavy frameworks. And it's built agency-style: one folder
per client, so onboarding a new client is copying a template and filling in two files. Each client's
data, sending limits and credentials stay completely separate."*
*(Optional, only if you started the gateway:)* `curl http://localhost:3001/v1/models`
**Say:** *"Routine copy is powered by our own gateway that pools about a dozen free AI providers
behind one endpoint — and premium Claude is on tap when a client wants top-tier writing."*

### Act 2 — "It finds real buyers, live" (sourcing, ~1 min)
```
CLIENT=ejentic PREVIEW_QUERY="digital marketing agencies in Lagos, Nigeria" PREVIEW_MAX=5 venv/bin/python scripts/discover_preview.py
```
**See:** it searches Google Maps, reads each company's own website, and prints ~5 real businesses
with real public emails + the matched Ejentic service. *(This is the slow part, ~1 min — talk over
it. It writes nothing.)*
**Point:** the list of real businesses + emails as they appear.
**Say:** *"This isn't a list I uploaded — it's finding real prospects right now and reading each
company's website to enrich them. Notice it also picks which one of our services fits each one."*

### Act 3 — "It writes, grades its own work, and logs every step" (the heart, ~90s)
```
CLIENT=demo venv/bin/python scripts/lead_agent.py --preview --limit=1
```
**See:** on ONE lead, the full real pipeline runs on a throwaway database (nothing queued or sent):
`🧠 [Strategist]` picks the angle → `🎁 [Magnet]` builds a personal audit page → the copywriter
drafts → **`🧪 [Quality]` scores it 1–10 and rewrites weak copy** → a **`📊 RUN METRICS`** receipt
prints at the end.
**Point:** the `🧪 [Quality] draft scored X/10 … revision … final Y/10` lines, then the `📊 RUN
METRICS` block (drafts, tokens, time).
**Say:** *"Watch what it's doing: it decides the strategy, builds them a personalized page, writes
the email — and then a **second AI editor scores that email out of ten and rewrites it** to push the
quality up. Weak copy never reaches me. And at the end it hands me an itemized receipt — how many
tokens, how long, every step."*
> Then glance at **Zone 4 (ledger)** and **Zone 1 (/health)**: *"Every one of those steps is also
> written to a permanent ledger — that's the 'taking notes' part."* *(These reflect the real queued
> run, not the throwaway preview — that's fine, it's the same ledger.)*

### Act 4 — "Every prospect gets a bespoke page" (the magnet, ~45s)
Open these three in tabs and flip between them:
```
http://localhost:5002/magnet/demo/a02dea805edc     # Zuri Threads — support audit
http://localhost:5002/magnet/demo/599bcacf69f3     # Lekki Prime Realty — lead-gen audit
http://localhost:5002/magnet/demo/84bc70a0ece3     # Adebayo & Co. Legal — knowledge-base audit
```
**See:** three totally different branded audit pages — each names the company, a real bottleneck,
and a 3-step solution written for *that* business.
**Point:** the headline + "Prepared for {name} at {company}" line on each; flip between all three.
**Say:** *"Three companies, three completely different audit pages — this is the free gift the email
links to, and it's what earns the reply. No two are the same."*

### Act 5 — "Nothing sends without a human" (approval, ~45s)
Open **`http://localhost:5002/`**.
**See:** three white cards, one per company, each with a **color-coded spam badge** (`✅ Spam: clean
(0)`), the personalized subject + body, the magnet link, and green **Approve** / red **Decline**
buttons.
**Point:** the spam badges; then click **✅ Approve & Send** on the Zuri Threads card → the
**"✅ Pitch Approved & Sent!"** confirmation → switch to **Gmail**, where the real cold email lands
**in your own inbox** (with a "Yes, I'm interested" button + an unsubscribe footer).
**Say:** *"Before I ever see it, it spam-checks its own copy — green means safe to send. Nothing
goes out until I click approve. One click, and it's sent through real email — safely routed to me in
this demo."*

### Act 6 — "One click → a booked meeting" (booking, ~1 min)
In the email that just arrived (Gmail), click the **"Yes, I'm interested"** button.
**See:** a celebratory **"You're booked! 🎉"** page with a real date/time → switch to **Google
Calendar**, where the event now exists.
**Point:** the "You're booked" page, then the new Google Calendar event.
**Say:** *"From a cold company name to a real meeting on my calendar — the prospect clicked one
button, and the system booked it. No back-and-forth."*
> *(If the calendar event doesn't appear, the flow still worked — Composio's Calendar just needs
> `composio link googlecalendar`. See `DEMO_SCRIPT.md`.)*

### Act 7 — "It babysits the calendar" (reminders + appointments, ~45s)
```
CLIENT=demo venv/bin/python scripts/reminder_agent.py --once
```
Then open **`http://localhost:5002/appointments`**.
**See:** the console prints the upcoming meeting with a live countdown; `/appointments` now shows
the booking with **reminder pills** (60 / 30 / 15-min). A macOS desktop banner may pop.
**Point:** the appointments row + the reminder pills; the desktop banner if it fires.
**Say:** *"Once a meeting's booked, it babysits it — every minute it re-checks the calendar and
fires reminders at 60, 30 and 15 minutes out, by email and a desktop alert, so a call it booked
never gets missed."*

### Act 8 — "The watchtower" (monitoring finale, ~90s)
Reload **`http://localhost:5002/health`**, then run the read-only receipt:
```
CLIENT=demo venv/bin/python -c "
import sqlite3
from core import config, observability as obs
cfg=config.load_client('demo')
c=sqlite3.connect('file:data/demo/state.sqlite?mode=ro',uri=True); c.row_factory=sqlite3.Row
row=c.execute('SELECT run_id FROM pipeline_events ORDER BY id DESC LIMIT 1').fetchone()
print(obs.format_run_summary(obs.run_metrics(c,cfg,row['run_id'])))"
```
**See:** the `/health` dashboard — **Events**, **Error rate 0%**, **Tokens** (in/out), **Notional
cost**, **Unit economics** (cost per lead / per booking), a **Faults** panel, and a **by-step**
table. Then the printed **RUN METRICS** receipt. Glance at **Zone 2 (watchdog)** — its heartbeat,
and any stall it healed during the take.
**Point:** slow pan over the KPI cards and the by-step table; then the watchdog heartbeat line.
**Say:** *"This is mission control. Every run's tokens, error rate, cost and unit economics update
here — and a separate watchdog agent reads that ledger every minute. If any step fails or a lead
gets stuck, it opens a fault and fixes it automatically, and only escalates to a human when it
genuinely can't. So the honest pitch isn't 'trust the AI' — it's **'you can see exactly what it did,
step by step, and it's watching itself.'**"*

**Retake:** `bash demo_reset.sh`, then start again from Act 2. (Leave Zone 1's dashboard running.)

---

# PATH B — the delta ("watch it build from empty")

Same acts, three changes:
1. **Before recording:** `bash demo_reset.sh` (empties the demo). Start the gateway (setup) so the
   free chain is fast.
2. **Replace Act 3** with the *full* real run so the dashboard climbs live:
   ```
   CLIENT=demo venv/bin/python -u scripts/lead_agent.py
   ```
   Now it drafts all 3 leads (~4 min — narrate or trim), and **Zone 1 (/health) + Zone 4 (ledger)
   climb in real time**, ending with the `📊 RUN METRICS` receipt and 3 `[ACTION REQUIRED]`
   approval emails in Gmail. This is the strongest "taking notes" visual because the notes appear as
   you watch.
3. **Act 4 magnet links change** after a reset — grab the fresh URLs from the dashboard cards
   (`http://localhost:5002/`, the "🎁 View their personalized page" link on each card) instead of
   the three fixed URLs above.

Everything else (approve → book → reminders → health) is identical.

---

# Appendix A — the measurement / monitoring catalog (your "we watch everything" cheat-sheet)

Nine surfaces you can point to. Each is real and verified in code.

1. **The pipeline ledger (`pipeline_events`)** — one immutable row per step: which model answered,
   tokens in/out, milliseconds, pass/fail, cost, timestamp. *The literal "notes on every step."*
   See it: Zone 4 tail, or `/health` by-step table.
2. **The `/health` dashboard** — the always-on web page that rolls the ledger into KPIs: events,
   error rate, tokens, cost, unit economics, faults, per-step timing. `http://localhost:5002/health`.
3. **The `📊 RUN METRICS` receipt** — an itemized per-run scorecard printed at the end of every run
   (drafts / errors / tokens / time / by-step). Replayable read-only (Act 8 command).
4. **The `🧪 Quality` gate (LLM-as-judge)** — a second AI scores every draft 1–10 against a fixed
   rubric and rewrites weak copy (best-of-2, up to 2 rewrites, target 8). Shows live in Act 3.
5. **The self-healing watchdog (`observer_agent.py`)** — scans the ledger every 60s, opens a fault
   for any error or stalled lead, heals it from a **safe allowlist** (retry / requeue / quarantine /
   escalate), auto-closes faults that recover, emails you only when it can't. Zone 2.
6. **The spam-lint score** — an offline linter grades every draft for deliverability (trigger words,
   shouting, unsafe links) → the color-coded badge on each card and in the approval email. Act 5.
7. **The reminders agent** — re-checks booked meetings every minute with a live countdown and fires
   60/30/15-min reminders (email + desktop). Act 7.
8. **The `/appointments` board** — every booking with its service, time, and which reminders have
   fired (green ✓ / grey ○ pills). Act 7.
9. **The premium budget guard** — when premium Claude is on, it reconstructs the day's real spend
   from the ledger and auto-drops to free models the moment a daily cap is hit (shown on `/health`
   for a premium client). Not active for the demo (free chain) — mention, don't show.

---

# Appendix B — the stack in one breath (honest version)

- **Language/core:** Python, **minimal-dependency** (6 small packages) — the data (`sqlite3`),
  email (`smtplib`/`imaplib`), crypto (`hmac`), and scheduling core is pure standard library.
- **Data:** one **WAL-mode SQLite** database **per client** (10 tables incl. the `pipeline_events`
  ledger); idempotent, safe to re-run without double-sending.
- **AI layer:** one LLM module with a **provider fallback chain** (FreeLLMAPI → Gemini → NVIDIA) and
  a **circuit breaker** that skips a dead provider; **premium Claude Opus** prepended when a client
  turns it on and is under budget.
- **FreeLLMAPI gateway:** a self-hosted Node/Express proxy that pools ~11 free LLM providers behind
  one OpenAI-compatible endpoint (routine copy runs here at zero marginal cost).
- **Integrations (via the Composio CLI, no heavy SDK):** Google Maps + Firecrawl (discovery),
  Google Calendar (booking), IMAP (reply reading).
- **Web:** a small **Flask** app = the human-in-the-loop approval dashboard, magnet pages, and the
  `/health` monitor.
- **Compliance:** CAN-SPAM footer + **self-verifying HMAC unsubscribe tokens** + `List-Unsubscribe`
  one-click headers + a suppression list.
- **Safety:** **controlled sending** (redirects to your own inbox), per-mailbox/global daily caps,
  the quality gate, the spam linter, and the self-healing watchdog.

---

## Gotchas checklist (read once before recording)

- [ ] `export PYTHONWARNINGS=ignore` in every terminal (clean output).
- [ ] Dashboard started with **`CLIENT=demo`** (or every page is empty).
- [ ] Header says "Ejentic AI" — that's the brand name, not a bug.
- [ ] Gateway (:3001) is **optional** and currently **stopped** — start it only if you want to show
      it; otherwise don't run the `curl` in Act 1.
- [ ] Magnet URLs in Act 4 are the **current** ones — if you `demo_reset.sh`, get fresh links from
      the dashboard cards.
- [ ] Calendar not linking? The flow still works; `composio link googlecalendar` fixes the event.
- [ ] Never say "stdlib-only," never present the notional cost as a hard cost, never say Opus wrote
      the demo copy. (See the honesty table.)
