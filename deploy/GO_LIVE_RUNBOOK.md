# Go-Live Runbook (plain-English, do-this-in-order)

> **⚠️ DOMAIN STATUS (updated 2026-09-03).** `ejentic.xyz` **is not registered yet** —
> registering it is the #1 go-live blocker (`ejentic.ai` / `.com` belong to other
> people). Also: this runbook assumes the website is *already* live on the domain; it
> isn't yet on `ejentic.xyz`, so any "already serving / already points at the box" note
> here is part of **THIS** deploy, not a given. Full note in `deploy/GO-LIVE.md`.

**Companion to `deploy/GO-LIVE.md`.** That file explains *why* we host centrally
and where the data lives. This file is the *exactly-what-to-type*, in order, for
someone doing this the first time.

**Read this safety rule first:**
- **Nothing in Part A, B, or D sends a single real email.** Those steps only
  stand the server up, protect it with a password, and point the links at the
  real domain.
- **Part C is the only place real prospects get emailed.** Do **NOT** run Part C
  without an explicit "yes, go" from the owner (Ejeh Peter).
- **Never paste a real password, API key, or secret into any file that lives in
  the repo.** The repo is on GitHub. Secrets live only in the server's `.env`
  (which is never copied up) and in the Caddyfile as a one-way *hash*.

---

## PART A — Inputs you must get from the user *before you start*

You cannot finish go-live without these. Get all of them up front.

**A1. DNS record — NEED FROM YOU.**
Add this record at the domain registrar / DNS host:
```
audit.ejentic.xyz   A   88.96.57.79
```
(Also confirm `ejentic.xyz` and `www.ejentic.xyz` already point at the same box —
if the website is already live they do.) Caddy cannot get an HTTPS certificate
until this record exists and has propagated, so this must be done first.

**A2. Real business postal address — NEED FROM YOU.**
A real, physical mailing address. US anti-spam law (CAN-SPAM) requires it in the
footer of every marketing email. It replaces the `physical_address` placeholder
in `clients/ejentic/config.json` (currently the text `REPLACE-ME: ...`). No real
send is allowed until this is a real address.

**A3. The real leads list — NEED FROM YOU (decision + data).**
There are two possible lead sources in this system, so confirm which one we use:
- **Auto-discovery (currently active):** `lead_source` in the config is
  `maps_firecrawl`. This finds businesses automatically from the search queries
  already listed under `discovery.segments`. If we use this, "the leads list" is
  really "confirm the segment queries are the ones you want."
- **Curated CSV:** `clients/ejentic/leads.csv`. **Right now this file only holds
  placeholder/guidance rows** (`REPLACE-ME@...`), and the loader skips any row
  whose email is blank/placeholder — so today it would yield **zero** leads. If
  we use this path, you must give me a real list of contacts to paste in
  (columns: `first_name,last_name,title,email,company_name,company_description,website_url`).

**A4. Dashboard password — NEED FROM YOU.**
The password that protects the internal approve/decline dashboard. You will type
it **once** into the hashing command in Part D; it is never written to a file.
Have it ready but do not send it in chat or paste it anywhere.

**A5. Server secret values (`.env`) — NEED FROM YOU.**
The code reads all secrets from environment variables, never from the config.
The server's `.env` file is deliberately **not** copied up by rsync, so you must
create it on the server by hand (Part B3). You need the *values* for these
**names** (I only ever write the names, never the values):
- `SMTP_USER`, `SMTP_PASS` — the sending mailbox (Gmail app password).
- `UNSUB_SECRET` — signs the unsubscribe/one-click links; set it explicitly so
  the links keep working even if the mailbox password later changes.
- LLM keys used by the copywriter/gateway: `GEMINI_API_KEY`, `NVIDIA_API_KEY`,
  `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`),
  `FREELLMAPI_BASE_URL`, `FREELLMAPI_KEY`.
Use the same values you already have on the Mac's local `.env` — copy them, do
not invent new ones.

---

## PART B — Server steps, in exact order (safe / reversible — no emails sent)

> All of these are done from the Mac (rsync) and over SSH to the server. They set
> things up but do not contact any prospect.

**B1. Confirm DNS is live (do this before Caddy).**
From your Mac:
```
dig +short audit.ejentic.xyz
```
It must print `88.96.57.79`. If it prints nothing, DNS (A1) has not propagated
yet — wait and re-check. Do not continue until it resolves.

**B2. Copy the code to the server with rsync.**
Run this exactly (note the trailing slash on the source path, and the excludes
— they protect the server's live database and its secrets from being overwritten):
```
rsync -avz \
  --exclude '.git' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '.env' \
  --exclude 'data/' \
  --exclude '*.sqlite' --exclude '*.sqlite-wal' --exclude '*.sqlite-shm' \
  --exclude 'pending_leads.json' --exclude 'pending_leads.json.bak' \
  /Users/ejehrebecca/Antigravity/lead-generation-system/ \
  ubuntu@88.96.57.79:/home/ubuntu/leadgen/
```
Why these excludes: `data/` and `*.sqlite` are the live database (see Part E) —
copying the Mac's copy up would clobber the server's real state. `.env` is
secrets, which belong only on the server. (On *first* deploy there is nothing to
overwrite yet, but keep the excludes so re-deploys stay safe. Do **not** add
`--delete` unless you understand it — it removes server files not present on the Mac.)

**B3. Create the server's `.env` (secrets live only here).**
SSH in and create `/home/ubuntu/leadgen/.env` with the variable **names** from
A5, filling in the values you already hold. For example, the structure only:
```
SMTP_USER=...
SMTP_PASS=...
UNSUB_SECRET=...
GEMINI_API_KEY=...
# ...and the other keys from A5 as needed
```
Save it, then lock it down: `chmod 600 /home/ubuntu/leadgen/.env`.
(This file is git-ignored and rsync-excluded, so it never leaves the server.)

**B4. Build the Python environment.** On the server:
```
cd /home/ubuntu/leadgen
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

**B5. Install and start the approval server as a service.** On the server:
```
sudo cp deploy/ejentic-approval.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ejentic-approval
systemctl is-active ejentic-approval      # must print: active
```
This runs the Flask app bound to `127.0.0.1:5002` only (set by `APPROVAL_PORT`
in the service file). The public internet never reaches it directly — Caddy does
(next step). `Restart=always` means it survives crashes and reboots.

**B6. Add the Caddy block and set the dashboard password.**
1. Generate the password **hash** now — see **Part D** — and copy the hash it prints.
2. Open the server's main Caddyfile (the same one already serving `ejentic.xyz`).
3. Paste in the whole `audit.ejentic.xyz { ... }` block from
   `deploy/Caddyfile.audit`.
4. In that block, replace `REPLACE_WITH_BCRYPT_HASH` with the hash from step 1.
   Leave the username `ejentic` as-is.
5. Reload Caddy:
   ```
   sudo systemctl reload caddy
   ```
Caddy will automatically fetch a real HTTPS certificate for `audit.ejentic.xyz`
now that DNS (A1) is live. (If Caddy complains about `basic_auth`, this is an
older Caddy — change it to the one-word `basicauth` and reload again.)

**B7. Flip the safe config values.** Edit `clients/ejentic/config.json` and:
- Change `unsubscribe_base_url` from `http://localhost:5002` to
  `https://audit.ejentic.xyz` (so every link in every email points at the real,
  always-on domain).
- **Add** a `website_url` key set to `https://ejentic.xyz` (this key is not in the
  file yet; it turns on the "Explore what Ejentic builds →" link on each audit page).
- Set `physical_address` to the real address from A2 (delete the `REPLACE-ME` text).

These three edits change what the links and footer *say*; **none of them send
anything.** `sending.mode` stays `controlled` — do not touch it here.
Tip: make these edits on the Mac and re-run the B2 rsync, *or* edit them directly
on the server — but if you edit on the server, make the same edit on the Mac too,
or your next rsync will overwrite it.

**B8. Smoke test — still no real sends.**
- Open a real magnet link over HTTPS, e.g.
  `https://audit.ejentic.xyz/magnet/ejentic/<token>` → the audit page should load.
- Open `https://audit.ejentic.xyz/` → it should **demand the password**.
- Click an unsubscribe / interested link → it should work.
- `curl -s https://audit.ejentic.xyz/health` → should respond OK.
If all four pass, the server side of go-live is done. **Stop here** until the
owner says to proceed to Part C.

---

## PART C — Outward-facing / irreversible (DO NOT do without explicit user OK)

> **STOP. These steps email real strangers. Once an email is sent it cannot be
> unsent.** Do not run either step until the owner explicitly says "go live."

**C1. Flip sending from controlled to live — GATED.**
In `clients/ejentic/config.json`, under `sending`, change:
```
"mode": "controlled"   →   "mode": "live"
```
What this actually does:
- **`controlled` (now):** every email is redirected to your own safe inbox
  (`SMTP_USER`) instead of the prospect — safe for testing.
- **`live`:** emails go to the **real prospect addresses.**
(`controlled` and `live` are the only two valid values; the code rejects anything
else on startup.) After changing it, the code prints a loud warning that live
sending is on — that is expected.

**C2. The first real send — GATED.**
Before the first live send, confirm the whole checklist is true:
- [ ] Owner has explicitly said go-live.
- [ ] `physical_address` is a real address (B7 / A2).
- [ ] The intended leads are confirmed (A3).
- [ ] `sending.daily_global_cap` (currently 50) and throttle look sane for a first run.
- [ ] Part B8 smoke test passed.
Only then kick off the sending run. Start small; watch the dashboard.

---

## PART D — Prep the dashboard password hash (safe to do now, no secret stored)

Caddy's password protection stores a one-way **bcrypt hash**, never the password
itself. You generate the hash by typing the plaintext **once** at a prompt; the
tool prints only the hash; the plaintext is never saved anywhere.

**Preferred (run on the server, where Caddy is installed):**
```
caddy hash-password
```
It prompts for the password twice (nothing is echoed to the screen) and prints a
single line starting with `$2a$...`. That printed line is the hash.

**Alternative if you prefer `htpasswd`:**
```
htpasswd -nBC 12 ejentic
```
This prints `ejentic:$2y$...`. Caddy wants **only the hash part** — copy the text
*after* the colon.

**Then:** paste that hash into `deploy/Caddyfile.audit` (and the live Caddyfile)
in place of `REPLACE_WITH_BCRYPT_HASH`, keeping the username `ejentic`:
```
basic_auth @private {
    ejentic <the-hash-goes-here>
}
```
The plaintext password was typed once and discarded. It is never written to the
repo, the Caddyfile, or this runbook. Only the hash is stored, and a hash cannot
be turned back into the password.

---

## PART E — Why the pipeline and the approval server must be on the SAME box

The approval/audit server and the outreach pipeline both open the *same* SQLite
database file: every script (`lead_agent`, `reply_agent`, `observer_agent`,
`draft_queued`, `reminder_agent`) and the approval server all call
`state.connect(cfg["paths"]["db"])`, which resolves to
`data/ejentic/state.sqlite`. SQLite is a single file on local disk — it cannot be
safely shared between two machines over a network — so the process that *writes*
the leads/magnets (the pipeline) and the process that *reads* them to serve the
pages (the approval server) must run on the same box, with that one file between
them. That is why hosting the server on the VPS also pulls the pipeline onto the
VPS.

---

## BOTTOM LINE

**What I need FROM YOU, in order:**
1. **DNS:** confirm `audit.ejentic.xyz A 88.96.57.79` is added (A1).
2. **Postal address:** the real CAN-SPAM mailing address (A2).
3. **Leads:** confirm auto-discovery is the source, *or* hand over the real
   contacts to fill `clients/ejentic/leads.csv` (A3).
4. **Dashboard password:** have it ready to type once (A4) — do not send it in chat.
5. **`.env` secret values:** SMTP creds + LLM/gateway keys, same values as local (A5).

**Safe for me to prep now (no send, reversible):**
- Everything in **Part B** (rsync, venv, service, Caddy block + password, the
  safe config edits, smoke test) once inputs 1–5 are in hand.
- The password **hash** (**Part D**) — plaintext typed once, only the hash stored.

**Must WAIT for your explicit "go live" (irreversible):**
- **Part C1** — flipping `sending.mode` from `controlled` to `live`.
- **Part C2** — the first real send to real prospects.
