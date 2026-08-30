"""Personalized lead-magnet pages — the real "free gift" behind every cold email.

Each cold pitch links to a page auto-generated for THAT prospect: a short, AI-mapped
mini-audit anchored on the bottleneck the strategist found and the Ejentic service the
lead matched. The page is stored (keyed by an unguessable token) in the client's state
DB and served by the approval server at:

    {unsubscribe_base_url}/magnet/{client}/{token}

so the link in the email actually resolves to something valuable — fixing the old bug
where the email promised a "free gift" but only contained a dead ``[Link to Free Gift]``
placeholder.

Design notes:
  * The LLM only returns STRUCTURED JSON (headline, bottleneck, steps, cta). The HTML is
    rendered here from a fixed template with every field HTML-escaped, so nothing the
    model returns is injected as raw markup.
  * Generation degrades gracefully: if the AI call fails, we fall back to a page built
    from the strategy brief, so a prospect never lands on a broken link.
  * Storage helpers live in core/state.py (the `magnets` table); this module only builds
    content + renders HTML, so it stays free of DB wiring.
"""

from __future__ import annotations

import html
import json
import uuid

from .ai import generate_json


def new_token() -> str:
    """An unguessable, URL-safe token for one prospect's magnet page."""
    return uuid.uuid4().hex[:12]


def url(cfg, client: str, token: str) -> str:
    """The public link the email points at (same base as the dashboard/unsub)."""
    base = (cfg.get("unsubscribe_base_url") or "http://localhost:5001").rstrip("/")
    return f"{base}/magnet/{client}/{token}"


def build_content(cfg, lead, strategy_brief: str) -> dict:
    """Ask the LLM for the page's structured content; fall back to the brief on failure.

    Returns a dict with a stable shape (see _fallback_content) that render_html expects.
    """
    company = (lead.get("company_name") or "your company").strip()
    name = (lead.get("first_name") or "").strip()
    service = (lead.get("ejentic_service") or "").strip()

    prompt = f"""
    You are an AI solutions consultant at '{cfg.get('client_name', 'our company')}'.
    Using the strategy brief below, produce the content for a short, personalized
    "mini-audit" web page we are giving this prospect as a free resource.

    PROSPECT: {name} at {company}
    {"MATCHED SERVICE: " + service if service else ""}

    STRATEGY BRIEF:
    {strategy_brief}

    Return ONLY a JSON object with EXACTLY these keys:
    {{
      "headline": "a specific, non-hyped title for the audit (<= 12 words)",
      "bottleneck": "2-3 sentences naming the ONE concrete operational bottleneck they face",
      "steps": [
        {{"title": "Step 1 short title", "detail": "1-2 sentences, concrete"}},
        {{"title": "Step 2 short title", "detail": "1-2 sentences, concrete"}},
        {{"title": "Step 3 short title", "detail": "1-2 sentences, concrete"}}
      ],
      "cta": "one warm sentence inviting them to reply to the email to go further"
    }}
    Keep it grounded and specific to {company}. No buzzwords, no ALL-CAPS, no exclamation marks.
    """

    try:
        obj, _ = generate_json(cfg, prompt)
    except Exception:
        obj = None

    if not isinstance(obj, dict) or not obj.get("steps"):
        return _fallback_content(cfg, lead, strategy_brief)

    steps = []
    for s in (obj.get("steps") or [])[:3]:
        if isinstance(s, dict):
            steps.append({
                "title": str(s.get("title") or "").strip() or "Step",
                "detail": str(s.get("detail") or "").strip(),
            })
    if not steps:
        return _fallback_content(cfg, lead, strategy_brief)

    return {
        "company_name": company,
        "prepared_for": name,
        "service": service,
        "headline": str(obj.get("headline") or "").strip() or f"AI Opportunity Audit for {company}",
        "bottleneck": str(obj.get("bottleneck") or "").strip(),
        "steps": steps,
        "cta": str(obj.get("cta") or "").strip()
               or "Reply to our email and we'll map the full build with you.",
    }


def _fallback_content(cfg, lead, strategy_brief: str) -> dict:
    """A safe page built without a fresh AI call (used if generation fails)."""
    company = (lead.get("company_name") or "your company").strip()
    name = (lead.get("first_name") or "").strip()
    service = (lead.get("ejentic_service") or "").strip()
    # Use the brief's opening as the bottleneck text; keep it short.
    brief = (strategy_brief or "").strip().replace("\n", " ")
    bottleneck = (brief[:280] + "…") if len(brief) > 280 else brief
    focus = service or "an AI automation"
    return {
        "company_name": company,
        "prepared_for": name,
        "service": service,
        "headline": f"AI Opportunity Audit for {company}",
        "bottleneck": bottleneck or f"A recurring manual process at {company} is holding back growth.",
        "steps": [
            {"title": "Map the workflow", "detail": f"We document the exact steps behind the bottleneck at {company}."},
            {"title": "Design the AI layer", "detail": f"We design {focus} that removes the manual work while keeping a human in control."},
            {"title": "Deploy & measure", "detail": "We ship it on your infrastructure and track the hours and revenue recovered."},
        ],
        "cta": "Reply to our email and we'll map the full build with you.",
    }


def render_html(cfg, content: dict) -> str:
    """Render the stored content into a clean, branded, self-contained HTML page.

    Every dynamic value is HTML-escaped — the model never emits raw markup.
    """
    def esc(v):
        return html.escape(str(v or ""))

    client_name = esc(cfg.get("client_name") or "Ejentic AI")
    company = esc(content.get("company_name") or "your company")
    prepared_for = esc(content.get("prepared_for") or "")
    headline = esc(content.get("headline") or f"AI Opportunity Audit for {company}")
    bottleneck = esc(content.get("bottleneck") or "")
    cta = esc(content.get("cta") or "")
    website = esc((cfg.get("website_url") or "").strip())

    prepared_line = f"Prepared for {prepared_for} at {company}" if prepared_for else f"Prepared for {company}"
    # Optional: let the prospect jump to the main site and explore (set client config "website_url").
    brand_top = (f'<a href="{website}" style="color:#7f8c8d;text-decoration:none;">{client_name} · Free AI Audit</a>'
                 if website else f"{client_name} · Free AI Audit")
    explore_cta = (f'<p style="text-align:center;margin:22px 0 0;">'
                   f'<a href="{website}" style="color:#2980b9;font-weight:bold;text-decoration:none;">'
                   f'Explore what {client_name} builds →</a></p>' if website else "")

    steps_html = ""
    for i, step in enumerate(content.get("steps") or [], start=1):
        steps_html += (
            f'<div style="margin:18px 0;padding-left:16px;border-left:3px solid #4CAF50;">'
            f'<h3 style="color:#2980b9;margin:0 0 6px;">Step {i}: {esc(step.get("title"))}</h3>'
            f'<p style="margin:0;color:#444;">{esc(step.get("detail"))}</p>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{headline}</title>
</head>
<body style="font-family:'Inter',Arial,sans-serif;background:#f4f7f6;color:#333;margin:0;padding:40px 16px;">
  <div style="max-width:760px;margin:auto;background:#fff;padding:40px;border-radius:12px;box-shadow:0 4px 15px rgba(0,0,0,0.08);border-top:5px solid #4CAF50;">
    <p style="color:#7f8c8d;font-size:13px;text-transform:uppercase;letter-spacing:1px;margin:0 0 8px;">{brand_top}</p>
    <h1 style="color:#2c3e50;margin:0 0 6px;">{headline}</h1>
    <p style="color:#7f8c8d;font-size:17px;margin:0 0 24px;">{prepared_line}</p>
    <hr style="border:none;border-top:1px solid #eee;margin:0 0 24px;">
    <h2 style="color:#333;font-size:19px;">The bottleneck we see</h2>
    <p style="color:#444;line-height:1.6;">{bottleneck}</p>
    <h2 style="color:#333;font-size:19px;margin-top:28px;">A step-by-step AI solution</h2>
    {steps_html}
    <div style="margin-top:32px;padding:20px;background:#e8f4f8;border-radius:8px;">
      <strong style="color:#2c3e50;">{cta}</strong>
    </div>
    {explore_cta}
    <p style="margin-top:28px;color:#aaa;font-size:12px;">This page was prepared specifically for {company} by {client_name}.</p>
  </div>
</body>
</html>"""
