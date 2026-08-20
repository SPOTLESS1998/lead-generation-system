# Ejentic AI — Lead-Gen Demo Recording Script

A step-by-step guide for recording a client-facing demo of the lead-generation
system. Follow it top to bottom. Everything runs in **controlled mode** — every
email and calendar invite goes to **your own inbox**, so no real stranger is ever
contacted during the recording.

---

## What you're proving (the story)
A **24/7 AI SDR** that:
**finds real buyers → writes a personalized email + mini-audit page for each →
checks itself for spam → waits for your 1-click approval → sends → reads the reply
→ books the meeting on Google Calendar.** Human-in-the-loop the whole way.

---

## Before you hit record (one-time, ~2 min)
- Open a terminal at the repo: `cd ~/Antigravity/lead-generation-system`
- `.env` already has `SMTP_USER`, `SMTP_PASS`, `GEMINI_API_KEY` ✅
- Google Calendar is connected in Composio ✅ (verified ACTIVE)
- Have two browser tabs ready: **your Gmail inbox** and a blank tab for **localhost:5001**
- Increase your terminal font size so it's readable on video

## Reset before EVERY take (fully repeatable, safe)
```bash
bash demo_reset.sh
```
Clears only the demo's data. Your real `ejentic` client data is never touched.

---

# PART 1 — "It finds real buyers" (live proof clip, ~60–90s)

**Purpose:** prove the sourcing is *real*, not a spreadsheet you uploaded. This runs
live against Google Maps + Firecrawl and is **read-only** — nothing is saved or sent.

**Command** (one short search keeps the clip snappy):
```bash
CLIENT=ejentic \
PREVIEW_QUERY="digital marketing agencies in Lagos, Nigeria" \
PREVIEW_SERVICE="AI Lead Generation System" \
PREVIEW_MAX=5 \
venv/bin/python scripts/discover_preview.py
```

**What the audience sees:** it searches Google Maps live, visits each company's
website via Firecrawl, extracts a public contact email, and prints 3–5 real
businesses — name, email, website, and the Ejentic service each was matched to.

**Suggested narration:**
> "This isn't a list I uploaded. Watch — it's searching Google Maps live, visiting
> each company's site, and pulling a real contact email. In under a minute it's
> found real prospects and matched each one to the service they actually need."

**Tip:** the scraping takes ~30–60s. Either narrate over it, or pre-run it once and
trim the wait in editing. Safe to run as many times as you like.

**Optional — show the second source (Yellow Pages):**
```bash
PREVIEW_SOURCE=yellowpages PREVIEW_QUERY="accounting firms" \
PREVIEW_LOCATION="Lagos" PREVIEW_MAX=5 \
venv/bin/python scripts/discover_preview.py
```

---

# PART 2 — "It writes, checks & sends — with you in control" (polished walkthrough)

**Purpose:** the reliable core. Uses 3 curated sample companies so it's fast and
always looks great on camera.

### Step 1 — Start the approval dashboard (leave it running)
Terminal tab A:
```bash
CLIENT=demo venv/bin/python scripts/approval_server.py
```
This serves the Approve buttons and the personalized magnet pages on localhost:5001.

### Step 2 — Run the agent
Terminal tab B:
```bash
CLIENT=demo venv/bin/python scripts/lead_agent.py
```
**What the audience sees, per company:** 🧠 Strategist analyzes it → 🎁 builds a
personalized audit page → ✍️ Copywriter drafts a unique email → ✅ approval request
sent to your inbox. Ends with **"Drafted 3 pitches."**

**Narration:**
> "Now the AI takes over. For each company it works out their bottleneck, writes a
> one-of-a-kind email — no templates — and builds them a personalized mini-audit
> page as a free gift. Three companies, three completely different emails, in about
> a minute."

### Step 3 — Show a drafted email + the personalized magnet
Open your inbox → the **Approve** notification for **Zuri Threads**. Point out:
- The personalized subject + body (names their business + their real pain).
- The **color-coded spam score**. Talking point:
  > "It even grades its own deliverability. That yellow flag is just telling us the
  > free-gift link is `http` on my local test machine — on a client's real domain
  > it's `https` and this goes green. The point is it's actually checking, every time."
- Click the **magnet link** → the personalized audit page opens (their name, their service).

**Narration:**
> "Every prospect gets this — a personalized page, not a generic PDF. That's what earns the reply."

### Step 4 — One-click approve → it sends for real
Click **Approve**. Show your inbox: the actual cold email arrives. (In controlled
mode it lands in your own inbox — a real send through real email infrastructure,
just safely routed to you.)

**Narration:**
> "One click. That email is now sent — through real email infrastructure, safely
> routed to my own inbox in test mode."

### Step 5 — The reply loop (the 24/7 SDR close)
**Reply** to that email as an interested prospect, e.g.:
> "This looks great — can we talk Thursday afternoon?"

(Reply from the same Gmail account, to the email that just arrived, so it threads.)

Then run — Terminal tab B:
```bash
CLIENT=demo venv/bin/python scripts/reply_agent.py
```
It reads the reply over IMAP, classifies it as **interested**, drafts a response
with a proposed meeting time, and sends you an approval request.

**Narration:**
> "The prospect replied. The system reads it, sees they're interested, and drafts a
> response proposing a time — then waits for my okay."

### Step 6 — Approve the reply → real Google Calendar booking
Open the new approval email → **Approve**. The system sends the threaded reply **and
creates a real Google Calendar event** (attendee = your inbox in test mode). Switch
to Google Calendar and show the event.

**Narration:**
> "I approve — and it books the meeting. A real Google Calendar invite goes out.
> From a cold company name to a booked meeting, the AI did the whole thing; I clicked
> approve twice."

---

## Closing line (to camera)
> "That's the system: it finds your buyers, writes to each one personally, protects
> your sender reputation, and books meetings around the clock — and you stay in
> control with one-click approvals. Every piece runs on your own infrastructure."

---

## Safety notes (for you, not the camera)
- Stays in **`controlled`** mode — every email + calendar invite goes to your own inbox.
- Reset for another take anytime: `bash demo_reset.sh`.
- The `demo` client is fully separate from your real `ejentic` client.
- Do **not** flip `sending.mode` to `live` or fill a real `physical_address` until
  you're genuinely ready for real outreach.

## Quick reference — the whole flow in commands
```bash
bash demo_reset.sh                                          # 0. clean slate

# 1. LIVE sourcing clip (read-only)
CLIENT=ejentic PREVIEW_QUERY="digital marketing agencies in Lagos, Nigeria" \
  PREVIEW_MAX=5 venv/bin/python scripts/discover_preview.py

# 2. Polished walkthrough
CLIENT=demo venv/bin/python scripts/approval_server.py      # tab A (leave running)
CLIENT=demo venv/bin/python scripts/lead_agent.py           # tab B
#   → approve in browser → reply to the email that arrives
CLIENT=demo venv/bin/python scripts/reply_agent.py          # tab B
#   → approve in browser → Google Calendar event is created
```
