# Ejentic AI — Demo Recording (Simple Step-by-Step)

**Test mode is ON.** Every email and calendar invite goes to **your own inbox**.
No real company is ever contacted. You can redo the whole thing anytime.

The demo tells one story: **it finds real buyers → writes each a personal email →
you approve with one click → it sends → reads the reply → books the meeting.**

---

## Setup — do this once, before you record

1. Open a terminal and go to the project:
   ```
   cd ~/Antigravity/lead-generation-system
   ```
2. Clean slate for a fresh take:
   ```
   bash demo_reset.sh
   ```
3. Open a **second terminal tab**. From now on:
   - **Tab A** = the dashboard (start it, then leave it alone)
   - **Tab B** = where you run each step
4. In **Tab A**, start the dashboard and leave it running the whole time:
   ```
   CLIENT=demo venv/bin/python scripts/approval_server.py
   ```
5. Open two browser tabs: **your Gmail** and **http://localhost:5002**

Now press record. Every command below goes in **Tab B**.

---

## The recording — 8 steps

### 1. Show it finding real businesses (live)
```
CLIENT=ejentic PREVIEW_QUERY="digital marketing agencies in Lagos, Nigeria" PREVIEW_MAX=5 venv/bin/python scripts/discover_preview.py
```
**See:** it searches Google Maps and reads each company's website live, then lists ~5 real businesses with real emails. *(This is the only slow part, ~1 min — talk over it.)*
**Say:** *"This isn't a list I uploaded — it's finding real prospects live, right now."*

### 2. Let the AI write the emails
```
CLIENT=demo venv/bin/python scripts/lead_agent.py
```
**See:** for each of 3 companies it analyzes the business, builds a personal audit page, and writes a unique email. Ends with **"Drafted 3 pitches."**
**Say:** *"Now it writes a one-of-a-kind email for each company — no templates."*

### 3. Open the approval email
In **Gmail**, open the approval request for **Zuri Threads**.
**Show:** the personalized subject + body, and the color-coded **spam score**. Then click the **magnet link** → their personal audit page opens.
**Say:** *"Every prospect gets a personalized page — that's what earns the reply."*

### 4. Approve it
Click **Approve**. In **Gmail**, the real cold email arrives (in your own inbox — test mode).
**Say:** *"One click — sent through real email, safely routed to me."*

### 5. Reply as an interested prospect
In **Gmail**, hit reply on that email and send:
> This looks great — can we talk Thursday afternoon?

### 6. Let the AI read the reply
```
CLIENT=demo venv/bin/python scripts/reply_agent.py
```
**See:** it reads the reply over email, marks it **interested**, drafts a response proposing a time, and emails you to approve.
**Say:** *"It read the reply, saw they're interested, and proposed a meeting time."*

### 7. Approve the reply
Open the **new** approval email → click **Approve**. The threaded reply is sent **and** a Google Calendar event is created.

### 8. Show the calendar
Open **Google Calendar** → point to the new event.
**Say:** *"From a cold company name to a booked meeting — I clicked approve twice, the AI did the rest."*

---

## Want another take?
```
bash demo_reset.sh
```
Then start again from step 1. (Leave Tab A's dashboard running.)

## Good to know
- **Test mode:** everything lands in your own inbox. No real company is contacted.
- **Step 1 is the only slow part** (~1 min of live scraping) — narrate over it, or pre-record it and trim the wait.
- **If the calendar event doesn't appear in step 7**, the reply still sends fine — it just means Composio's Google Calendar needs reconnecting: `composio link googlecalendar`.
- **Don't** switch to live sending or use a real business address until you're truly ready for real outreach.
