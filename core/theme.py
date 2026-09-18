"""Shared look-and-feel for every HTML page this system serves.

Two pages face a human: the operator's approval queue (scripts/approval_server.py)
and the prospect's personalised magnet page (core/magnet.py). Before this module
they were ~40 hand-written inline styles apiece, in two different visual languages,
light-only. They now share one stylesheet and one page shell.

⚠️ DELIBERATE, DOCUMENTED DEVIATION FROM MULTITENANCY.md ⚠️
--------------------------------------------------------------------------------
That rule says no business fact about a client may live in code, and a brand
palette IS a business fact. The values in _TOKENS below are Ejentic's locked
palette (cyan on black / burnt sienna on cream), hardcoded on the owner's explicit
instruction on 2026-09-18: "just hardcode it for speed since Ejentic is the only
tenant today."

So this is a KNOWN exception, not an oversight. The cost is real and deferred: the
day a second tenant is onboarded, their approval page renders in Ejentic's colours
until this is fixed. The fix is contained on purpose — every colour lives in the
single _TOKENS block below and nowhere else, so making it tenant-aware later means
reading those values from clients/<client>/config.json and changing nothing else.
Do not scatter new hex values through the components; add a token here instead.

Palette source of truth: ai-agency/src/styles/tokens.css + ai-agency/AGENTS.md
("one accent only", pure-black dark ground, cream light ground, Outfit + Inter,
weight cap 600, 8px radius / no pills, glass panels rather than grey fills).
"""

# --- 1. tokens ---------------------------------------------------------------
# Dark is the default (:root). Light applies when the visitor's OS prefers light
# and they have not forced dark, or when they explicitly pick light. Kept as a
# plain string (not an f-string) so CSS braces need no escaping.
_TOKENS = """
:root {
  --ground:    #000000;
  --panel:     rgba(255,255,255,0.03);
  --panel-2:   rgba(255,255,255,0.05);
  --hairline:        rgba(255,255,255,0.09);
  --hairline-strong: rgba(255,255,255,0.18);
  --ink:       #ECEDEF;
  --ink-dim:   #9AA0A9;
  --ink-faint: #6C727B;
  --accent:       #00f0ff;
  --accent-hover: #7df6ff;
  --accent-press: #00c2d1;
  --accent-quiet: rgba(0,240,255,0.10);
  --accent-text:  #00f0ff;
  --on-accent:    #00191c;
  --danger:  #E5484D;
  --danger-quiet: rgba(229,72,77,0.12);
  --success: #30A46C;
  --warning: #F5A524;
  --glow:   0 0 34px color-mix(in srgb, var(--accent) 18%, transparent);
  --lift:   0 1px 0 rgba(255,255,255,0.06) inset, 0 8px 24px rgba(0,0,0,0.5);
  --sheen:  rgba(255,255,255,0.15);
}

/* System prefers light and the visitor has not pinned dark. */
@media (prefers-color-scheme: light) {
  :root:not([data-theme='dark']) {
    --ground:    #F4F0E6;
    --panel:     #FCFAF3;
    --panel-2:   #EEE7D7;
    --hairline:        #E3DBCA;
    --hairline-strong: #D3C9B4;
    --ink:       #24211C;
    --ink-dim:   #6E675A;
    --ink-faint: #9A9182;
    --accent:       #B4512C;
    --accent-hover: #C25C34;
    --accent-press: #9C4525;
    --accent-quiet: rgba(180,81,44,0.10);
    --accent-text:  #A94A26;
    --on-accent:    #FFFFFF;
    --danger:  #C4323A;
    --danger-quiet: rgba(196,50,58,0.10);
    --success: #2A7F55;
    --warning: #A9680F;
    --glow:   0 1px 2px rgba(36,33,28,0.06);
    --lift:   0 1px 2px rgba(36,33,28,0.05), 0 6px 16px rgba(36,33,28,0.07);
    --sheen:  rgba(255,255,255,0.65);
  }
}

/* Explicit choice always wins, in both directions. */
:root[data-theme='light'] {
  --ground:    #F4F0E6;
  --panel:     #FCFAF3;
  --panel-2:   #EEE7D7;
  --hairline:        #E3DBCA;
  --hairline-strong: #D3C9B4;
  --ink:       #24211C;
  --ink-dim:   #6E675A;
  --ink-faint: #9A9182;
  --accent:       #B4512C;
  --accent-hover: #C25C34;
  --accent-press: #9C4525;
  --accent-quiet: rgba(180,81,44,0.10);
  --accent-text:  #A94A26;
  --on-accent:    #FFFFFF;
  --danger:  #C4323A;
  --danger-quiet: rgba(196,50,58,0.10);
  --success: #2A7F55;
  --warning: #A9680F;
  --glow:   0 1px 2px rgba(36,33,28,0.06);
  --lift:   0 1px 2px rgba(36,33,28,0.05), 0 6px 16px rgba(36,33,28,0.07);
  --sheen:  rgba(255,255,255,0.65);
}
"""

# --- 2. components -----------------------------------------------------------
# Radius ladder 4/8/12, no pills. Panels are a ring + lift + faint sheen rather
# than a grey fill, per the locked system.
_COMPONENTS = """
*, *::before, *::after { box-sizing: border-box; }

body {
  margin: 0;
  padding: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
  font-size: 15px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

h1, h2, h3 {
  font-family: 'Outfit', 'Inter', ui-sans-serif, system-ui, sans-serif;
  font-weight: 600;
  letter-spacing: -0.02em;
  line-height: 1.2;
  margin: 0;
}
h1 { font-size: clamp(24px, 4vw, 32px); }
h2 { font-size: 19px; }
h3 { font-size: 16px; }

a { color: var(--accent-text); text-decoration: none; }
a:hover { text-decoration: underline; }

.wrap { max-width: 780px; margin: 0 auto; padding: 48px 20px 72px; }
.muted { color: var(--ink-dim); }
.meta  { color: var(--ink-faint); font-size: 12px; }
.rule  { border: none; border-top: 1px solid var(--hairline); margin: 24px 0; }

/* Glass panel: ring + lift + 1px top sheen, near-zero fill. */
.panel {
  background: var(--panel);
  border: 1px solid var(--hairline);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 20px;
  box-shadow: var(--lift);
  position: relative;
}
.panel::before {
  content: '';
  position: absolute; inset: 0 0 auto 0; height: 1px;
  background: linear-gradient(90deg, transparent, var(--sheen), transparent);
  border-radius: 12px 12px 0 0;
  pointer-events: none;
}
.panel:hover { border-color: var(--hairline-strong); }

.head {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 12px; flex-wrap: wrap; margin-bottom: 6px;
}

/* Badges: small radius, never pills. */
.badge {
  display: inline-block;
  padding: 3px 9px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  border: 1px solid var(--hairline-strong);
  color: var(--ink-dim);
  white-space: nowrap;
}
.badge-accent  { color: var(--accent-text); border-color: var(--accent); background: var(--accent-quiet); }
.badge-success { color: var(--success); border-color: var(--success); }
.badge-warning { color: var(--warning); border-color: var(--warning); }
.badge-danger  { color: var(--danger);  border-color: var(--danger); background: var(--danger-quiet); }

/* The drafted email itself — quoted, monospaced-ish, clearly "their words". */
.draft {
  background: var(--panel-2);
  border: 1px solid var(--hairline);
  border-left: 3px solid var(--accent);
  border-radius: 8px;
  padding: 16px 18px;
  margin: 14px 0 18px;
  font-size: 14px;
  line-height: 1.65;
  white-space: normal;
  overflow-wrap: anywhere;
}
.draft .subject {
  display: block;
  font-weight: 600;
  color: var(--ink);
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--hairline);
}

/* Buttons: 8px radius, no pills, press-darken + 0.97 scale. */
.btn {
  display: inline-block;
  padding: 11px 20px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
  text-decoration: none;
  border: 1px solid var(--hairline-strong);
  color: var(--ink);
  background: transparent;
  cursor: pointer;
  transition: background 140ms cubic-bezier(0.23,1,0.32,1),
              border-color 140ms cubic-bezier(0.23,1,0.32,1),
              transform 90ms cubic-bezier(0.23,1,0.32,1);
}
.btn:hover { border-color: var(--ink-dim); text-decoration: none; }
.btn:active { transform: scale(0.97); }

.btn-primary {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--on-accent);
  box-shadow: var(--glow);
}
.btn-primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
.btn-primary:active { background: var(--accent-press); }

.btn-danger { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, transparent); }
.btn-danger:hover { background: var(--danger-quiet); border-color: var(--danger); }

.actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 4px; }

:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

/* Theme toggle, top-right. */
.theme-toggle {
  position: fixed; top: 14px; right: 14px; z-index: 50;
  width: 34px; height: 34px; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 8px;
  border: 1px solid var(--hairline);
  background: var(--panel);
  color: var(--ink-dim);
  font-size: 15px; line-height: 1; cursor: pointer;
}
.theme-toggle:hover { border-color: var(--hairline-strong); color: var(--ink); }

/* Centred one-line outcome pages (approved / declined / unsubscribed). */
.center { text-align: center; padding-top: 12vh; }
.center .panel { display: inline-block; text-align: left; min-width: min(420px, 92vw); }

table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: 11px 10px; text-align: left; border-bottom: 1px solid var(--hairline); }
th { color: var(--ink-dim); font-weight: 600; font-size: 12px;
     text-transform: uppercase; letter-spacing: 0.04em; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.92em; padding: 1px 5px; border-radius: 4px;
  background: var(--panel-2); border: 1px solid var(--hairline);
}
.empty { text-align: center; padding: 56px 24px; color: var(--ink-dim); }

@media (max-width: 560px) {
  .wrap { padding: 28px 14px 56px; }
  .actions .btn { flex: 1 1 auto; text-align: center; }
}
"""

# Stamp the stored theme BEFORE first paint, otherwise a light-mode visitor gets a
# black flash. Wrapped in try/catch: a blocked localStorage must not break the page.
_THEME_SCRIPT = """
<script>
(function(){try{var t=localStorage.getItem('ejx-theme');
if(t){document.documentElement.setAttribute('data-theme',t);}}catch(e){}})();
</script>
"""

_TOGGLE = """
<button class="theme-toggle" type="button" aria-label="Switch light or dark theme"
        onclick="(function(){var r=document.documentElement;
        var cur=r.getAttribute('data-theme');
        if(!cur){cur=window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';}
        var next=cur==='light'?'dark':'light';
        r.setAttribute('data-theme',next);
        try{localStorage.setItem('ejx-theme',next);}catch(e){}})();">◐</button>
"""

_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2'
    '?family=Inter:wght@400;500;600&family=Outfit:wght@500;600&display=swap">'
)


def shell(title, inner, body_class="", toggle=True):
    """Wrap page content in the shared head + theme machinery.

    `title` must already be escaped by the caller (it is interpolated raw).
    """
    # Built outside the f-string on purpose: a backslash inside an f-string
    # EXPRESSION is a SyntaxError before Python 3.12, and this repo still runs on
    # 3.9 locally (the box is 3.12). Keep string-building like this out of the
    # interpolation so the module imports on both.
    body_attr = ' class="%s"' % body_class if body_class else ""
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f"{_FONTS}\n"
        f"<style>{_TOKENS}{_COMPONENTS}</style>\n"
        f"{_THEME_SCRIPT}"
        "</head>\n"
        f"<body{body_attr}>\n"
        f"{_TOGGLE if toggle else ''}"
        f"{inner}\n"
        "</body>\n</html>"
    )
