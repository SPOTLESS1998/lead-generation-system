# Go-Live Runbook — cold outreach

**Status: 2026-09-20.** Everything that can be built without a sending domain is
built and running. What is left is not code — it is buying a domain, proving you
own it, and warming it up. That part takes about **five weeks of calendar time**
and roughly **two hours of actual work**, so start it before you feel ready.

`sending.mode` is `controlled`. Nothing in this system has ever emailed a
stranger. Only **Part 6** changes that, and only you can do it.

---

## The one decision everything else follows from

> **Do not send cold outreach from `ejentic.xyz`. Buy a second domain for it.**

This is not caution, it is arithmetic. Two separate reasons, and either one alone
would be enough.

### Reason 1 — right now, mail from `ejentic.xyz` via Gmail would be *rejected*

Not "land in spam". Rejected at the door. Here are your live records:

```
ejentic.xyz  SPF    v=spf1 include:_spf.porkbun.com ~all
ejentic.xyz  DMARC  v=DMARC1; p=reject; rua=mailto:mail@ejentic.xyz
```

And `_spf.porkbun.com` expands to Porkbun's own forwarding servers only —
no Google, no Gmail. So if you sent a cold email through Gmail SMTP with
`From: you@ejentic.xyz`:

| Check | Result | Why |
|---|---|---|
| SPF | ❌ fail | Gmail's servers are not in your SPF list |
| DKIM | ❌ not aligned | Gmail signs as `d=gmail.com`, your From says `ejentic.xyz` |
| DMARC | ❌ **reject** | Needs SPF *or* DKIM to pass **and** align. Neither does. |

`p=reject` is you instructing every receiving mail server to throw that message
away. It is the correct setting — it is what stops someone forging your domain —
and it was the right call to turn on early. But it means the domain is closed for
sending until you explicitly authorise a sender, and Gmail SMTP is not one.

### Reason 2 — even if you fixed that, you should not

Cold outreach earns spam complaints. That is normal; the job is keeping them
rare. But complaints attach to the **domain**, and `ejentic.xyz` is already
carrying things you cannot afford to lose:

- your website,
- `mail@ejentic.xyz`, the address Anthropic's startup programme has on file,
- transactional mail through Resend on `send.ejentic.xyz`, which is working.

A bad outreach month would take all of that down with it. A separate domain is a
**fuse**: if it burns, you buy another one for $10 and your real identity is
untouched.

One more thing, and it is a hard one: **Resend's acceptable-use policy forbids
cold outreach to scraped lists.** Violating it risks the whole account — which is
the account sending your transactional mail. Do not route outreach through Resend
to save the setup.

---

## Part 1 — Buy the outreach domain (~15 min, ~$10/yr)

Pick something a human would believe is yours, close to the real brand:

- `ejentic.co`, `ejentic.io`, `getejentic.com`, `ejentic-ai.com`, `tryejentic.com`

Rules:
- **Not** a subdomain of `ejentic.xyz`. Subdomains inherit the parent's
  reputation in both directions — that is exactly the coupling you are paying to
  avoid.
- Avoid the cheap novelty TLDs (`.top`, `.xyz`, `.click`). They are disproportionately
  spam-blocked. `.com` is safest; `.co`/`.io` are fine.
- Buy it **today even if you flip live in a month.** Domain age is itself a trust
  signal, and a domain registered last week sending cold email is a pattern
  filters already know.

## Part 2 — Put a real mailbox on it (~20 min, ~$1–7/month)

You need an actual **mailbox**, not an email API. This is a real constraint from
the code, not a preference: `core/inbox.py` reads replies over **IMAP**, and
`scripts/reply_agent.py` is what turns a reply into a booked meeting. An
API-only sender (Resend, Postmark, SendGrid) gives you no inbox to read, so the
entire reply-handling half of this system would go dark.

| Option | Cost | Notes |
|---|---|---|
| **Google Workspace** | ~$7/user/mo | Best deliverability reputation; DKIM in the admin console; Postmaster Tools included |
| **Zoho Mail** | ~$1/user/mo | Much cheaper, IMAP works, perfectly respectable |

Create one mailbox, something like `peter@<newdomain>`. Use a **real human name**,
not `info@` or `sales@` — role addresses get filtered harder and answered less.

Then generate an **app password** for it (Google: Security → 2-Step Verification →
App passwords). That is what goes in `SMTP_PASS`. Never the account password.

## Part 3 — DNS: prove you own it (~20 min + waiting)

Three records at the new domain's registrar. All three matter; DMARC without the
first two makes things worse, not better.

**SPF** — "these servers may send as me." One TXT record at the root:
```
Type: TXT   Host: @   Value: v=spf1 include:_spf.google.com ~all
```
(Zoho: `v=spf1 include:zohomail.com ~all`.) Never publish two SPF records on one
domain — that is an automatic fail. One record, merge the includes if you ever
need both.

**DKIM** — a cryptographic signature proving the message was not altered.
Google: Admin console → Apps → Google Workspace → Gmail → Authenticate email →
Generate new record → **2048-bit**. It gives you a host like
`google._domainkey` and a long `v=DKIM1; k=rsa; p=...` value. Publish it, wait
for it to resolve, then come back and click **Start authentication**. Forgetting
that last click is the single most common setup mistake.

**DMARC** — what receivers should do when the first two fail. Start permissive:
```
Type: TXT   Host: _dmarc   Value: v=DMARC1; p=none; rua=mailto:mail@ejentic.xyz
```

> ⚠️ **`p=none`, not `p=reject`.** On `ejentic.xyz` you went straight to `reject`
> and that was right — it was a domain that sent nothing, so there was no
> legitimate mail to break. This one is different: it is a domain you are about
> to send from, with a config you have not yet proven. `p=none` means "watch and
> report, reject nothing," so a DKIM mistake shows up in a report instead of
> silently binning every email you send. Move to `p=quarantine` after a couple of
> clean weeks, then `p=reject`. The reports go to your existing
> `mail@ejentic.xyz`.

**Verify before going further** — do not trust the registrar's UI:
```bash
dig +short TXT <newdomain>                  # the SPF line
dig +short TXT google._domainkey.<newdomain>  # the DKIM key
dig +short TXT _dmarc.<newdomain>           # the DMARC policy
```
All three must print. DNS can take up to 48h; usually it is minutes.

## Part 4 — Wire it into the system (~10 min)

On the box, `/home/ubuntu/leadgen/.env` — add the new mailbox. Keep the old
`SMTP_USER`/`SMTP_PASS`, because that is still your safe controlled-mode inbox:
```
OUTREACH_SMTP_USER=peter@<newdomain>
OUTREACH_SMTP_PASS=<the app password from Part 2>
```
Then `chmod 600 .env`. Never commit it; it is gitignored and rsync-excluded.

In `clients/ejentic/config.json`, point the mailbox at the new identity:
```json
"mailboxes": [
  {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "user_env": "OUTREACH_SMTP_USER",
    "pass_env": "OUTREACH_SMTP_PASS",
    "daily_cap": 40
  }
]
```
Also set `"from_name"` and `"reply_to"` to match the new mailbox.

**Leave `"mode": "controlled"`.** Changing the mailbox is not going live.

Now send yourself a test and read the headers. This is the step that catches a
broken setup before a stranger does:

1. Trigger one controlled-mode send (it goes to your own inbox).
2. In Gmail: open it → ⋮ → **Show original**.
3. You must see **SPF: PASS**, **DKIM: PASS**, **DMARC: PASS**.

If any says FAIL or NEUTRAL, stop and fix Part 3. Do not proceed on three-out-of-four.

## Part 5 — Warm it up (~30 days, mostly waiting)

A brand-new domain that opens at 40 emails/day gets filtered. Google's own
guidance is to start with low volume to engaged recipients and increase slowly,
avoiding bursts.

**This is now enforced in code**, not left to memory. `clients/ejentic/config.json`:
```json
"warmup": {
  "enabled": true,
  "ramp": [
    { "through_day": 3,  "cap": 5  },
    { "through_day": 7,  "cap": 10 },
    { "through_day": 14, "cap": 20 },
    { "through_day": 21, "cap": 30 },
    { "through_day": 30, "cap": 40 }
  ]
}
```

Three things about how this behaves, worth knowing so it does not surprise you:

- **Day 1 is the first *live* send.** Controlled-mode sends go to your own inbox,
  and they deliberately do **not** start the clock. Otherwise a fortnight of
  testing would leave the ramp reporting "day 15" on a domain no stranger had
  ever received mail from — the exact failure warmup exists to prevent.
- **The ramp can only hold volume down, never up.** A step larger than the
  mailbox's own `daily_cap` is clamped, so a typo cannot widen the tap.
- **It is per-mailbox.** Add a second mailbox in month three and it warms from
  its own first send, not the pool's age.

Watch it at **https://audit.ejentic.xyz/health** → *🌡️ Sending capacity & warmup*,
which shows the day, the cap in force, used and remaining, per mailbox.

**Do this by hand for the first week.** Before the ramp opens up, send 10–20
ordinary emails from the new mailbox to people who will actually reply — friends,
your other accounts, anyone. Replies and "not spam" marks are the strongest
positive signal there is, and a domain whose entire history is one-way cold mail
looks like exactly what it is.

## Part 6 — The flip (irreversible)

> **STOP.** Below this line real strangers get emailed. An email cannot be unsent.

**Pre-flight — every box must be ticked:**

- [ ] New domain registered, ≥2 weeks old
- [ ] `dig` shows SPF, DKIM and DMARC (`p=none`) all resolving
- [ ] A test message shows **SPF/DKIM/DMARC: PASS** in *Show original*
- [ ] 10–20 warm human emails sent and some replied to
- [ ] `venv/bin/python scripts/verify_leads.py` → bounce risk **under 2%**
- [ ] `physical_address` in config is real (CAN-SPAM) — ✅ already set
- [ ] One-click unsubscribe works end to end — ✅ already built
- [ ] Google Postmaster Tools set up (Part 7)
- [ ] `warmup.enabled` is `true` — ✅ already set
- [ ] You have said "go live" deliberately, not at the end of a long day

**The flip itself** — `clients/ejentic/config.json`:
```json
"mode": "controlled"   →   "mode": "live"
```
Restart the approval service. Config load prints a loud warning; that is expected.

**Then go slowly on purpose.** The ramp allows 5 on day one. Send five. Read what
came back before you send the sixth. The first week is information, not volume.

## Part 7 — Watch the things that actually matter

**Google Postmaster Tools** (https://postmaster.google.com) — free, and the only
place you can see what Gmail thinks of you. Add the new domain, verify by TXT,
then watch:

| Metric | Target | What it means |
|---|---|---|
| Spam rate | **< 0.10%** | Google's own threshold is 0.30%; above that, delivery collapses |
| Domain reputation | High/Medium | "Low" or "Bad" means stop sending and fix it |
| Authentication | ~100% | Anything less means SPF/DKIM is intermittently broken |

Data takes a few days to appear and needs some volume — another reason to set it
up before you need it.

**In this system:**
- `/health` → warmup + capacity, quality scores, faults
- `scripts/verify_leads.py` → bounce risk before you send, not after
- Every unsubscribe and bounce is already written to the suppression list and can
  never be contacted again

**Stop immediately if:** spam rate goes above 0.3%, reputation drops to Low, or
bounces exceed 2%. Fix the cause before sending again. Reputation takes weeks to
rebuild and days to destroy.

---

## If it goes wrong

Set `"mode": "controlled"` and restart. Sending stops at once; nothing else is
lost — the leads, the drafts, the history and the suppression list all stay.

If the new domain's reputation is damaged beyond repair, buy another one and
repeat Parts 1–5. That is the whole point of having kept it separate: it costs
$10 and a month, not your business identity.

---

## Already done (for reference — do not redo)

| | |
|---|---|
| `ejentic.xyz` registered, website live | ✅ |
| `audit.ejentic.xyz` → box, HTTPS, password-gated | ✅ |
| Approval service (`ejentic-approval`) running, `Restart=always` | ✅ |
| Pipeline on the box, cron 07:37 UTC daily | ✅ |
| Heartbeat (missed-run alarm) 10:12 UTC | ✅ |
| `physical_address` set (CAN-SPAM) | ✅ |
| `unsubscribe_base_url` → `https://audit.ejentic.xyz` | ✅ |
| One-click List-Unsubscribe headers | ✅ |
| Suppression list (unsubscribe / bounce / manual) | ✅ |
| Transactional mail via Resend on `send.ejentic.xyz`, DKIM verified | ✅ |
| DMARC `p=reject` on `ejentic.xyz` | ✅ |
| Daily caps, global cap, jittered throttle | ✅ |
| **Warmup ramp, enforced in code** | ✅ new |
| **Recipient MX verification before every send** | ✅ new |
| Quality gate (score, rewrite, refuse) + citation grounding | ✅ |
| Learning loop (critiques, scorecard, human-gated copy lessons) | ✅ |

**Still open, and all of it is Parts 1–7 above:** the outreach domain, its
mailbox, its DNS, its warmup, Postmaster Tools, and the flip.

---

## One thing worth double-checking

`send.ejentic.xyz` has a valid Resend DKIM key but **no SPF TXT record of its
own**. Mail still passes DMARC, because DMARC needs only one of SPF/DKIM to pass
and align — DKIM is carrying it alone. That works, and transactional mail is
proven to deliver, so this is not urgent. But it is single-stranded: if that DKIM
key is ever rotated or removed, transactional mail fails with no fallback. Worth
confirming against Resend's dashboard next time you are in there.
