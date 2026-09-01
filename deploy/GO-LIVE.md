# Go-live: hosting the approval + audit server on the VPS

**Decision (2026-09-01):** the approval/audit server runs centrally on the Ejentic
VPS (`88.96.57.79`, same box as the website), behind Caddy, at **one subdomain
`audit.ejentic.ai`** that serves every client. Deploy when the domain's DNS is
live (planned this week). Nothing goes live — and no real emails are sent — until
that domain exists AND it's an explicit decision.

## Why central hosting
The audit links, the approve/decline dashboard, and the one-click reply buttons
must resolve 24/7 from anywhere, even when every laptop is off. A rented VPS is
always on; a laptop is not. So the server lives on the VPS, exactly like the
website Cline already deployed. Clients host **nothing** — a client with no
website and a machine that's never on still gets working audit links, because
their links are just a path on our always-on domain:

    https://audit.ejentic.ai/magnet/<client>/<token>

## ⚠️ The one wrinkle: where the data lives
The magnet (audit) pages and lead state are stored in a **SQLite database the
pipeline writes** (`core/state.py`). The approval server *reads* that same DB to
serve the pages. So the pipeline and the approval server MUST share one database.

Since the server has to be on the VPS, the DB has to be on the VPS too — which
means the **pipeline that generates the magnets must also run on the VPS** (or at
least write to the VPS's DB). Good news: doing that makes the *entire* outreach
machine laptop-independent, not just the links. The only open question is where
the API keys / freellmapi gateway live (see step 2).

## Go-live checklist (this week, once the domain is bought)
1. **DNS:** add `audit.ejentic.ai  A  88.96.57.79` (plus `ejentic.ai` / `www` for
   the site itself if not already done).
2. **Get the code + a venv onto the VPS** at `/home/ubuntu/leadgen`
   (rsync from the Mac, same pattern as the website's `scripts/deploy.sh`;
   exclude `.env`, `venv`, `*.db`, `backups`). On the VPS:
   `python3 -m venv venv && venv/bin/pip install -r requirements.txt`.
   Decide: does the whole pipeline (incl. the freellmapi gateway that holds the
   API keys) run here too? If yes, the keys live on the VPS — a security call.
3. **Install the service:**
   `sudo cp deploy/ejentic-approval.service /etc/systemd/system/`
   `sudo systemctl daemon-reload && sudo systemctl enable --now ejentic-approval`
   `systemctl is-active ejentic-approval`  → should print `active`.
4. **Caddy:** paste `deploy/Caddyfile.audit` into the VPS Caddyfile, set the
   dashboard password (`caddy hash-password`), then `sudo systemctl reload caddy`.
   Caddy auto-issues HTTPS for `audit.ejentic.ai`.
5. **Flip config** in `clients/ejentic/config.json`:
   - `unsubscribe_base_url` → `https://audit.ejentic.ai`
   - `website_url` → `https://ejentic.ai` (enables the "Explore what Ejentic
     builds →" link on every audit page)
6. **Smoke test (no sending):**
   - open a real `…/magnet/ejentic/<token>` link over HTTPS → the audit page loads
   - open `https://audit.ejentic.ai/` → it should demand the password
   - click an unsubscribe / interested link → it works
7. **Only then** consider real sends — a separate, explicit decision. Sending
   mode stays `controlled` and the `physical_address` placeholder must be filled
   first (CAN-SPAM). Neither is changed without your say-so.
