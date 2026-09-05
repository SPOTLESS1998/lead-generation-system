"""Offline tests for the redeployability rule (MULTITENANCY.md).

THE REGRESSION THIS EXISTS FOR: `scripts/lead_agent._DEFAULT_OFFERINGS` used to hold
Ejentic's own four services as the fallback menu, so a client onboarded from the
template inherited it — their cold emails would have pitched EJENTIC's catalogue to
their own prospects. These tests assert that no house business fact can reach another
tenant, and that an incomplete tenant fails loudly at config load rather than
silently at draft time.

No network, no LLM, no email. Temp tenants are created under clients/ with a
`zz_test_` prefix and removed in a finally block.

Run:  venv/bin/python tests/test_multitenancy.py
"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

for _k in ("GEMINI_API_KEY", "NVIDIA_API_KEY", "SMTP_USER", "SMTP_PASS", "UNSUB_SECRET"):
    os.environ.setdefault(_k, "test")
os.environ["ANNOUNCE_TENANT"] = "0"        # keep the test output clean

from core import config, magnet, state    # noqa: E402
import lead_agent                          # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


# Ejentic's own services — the exact strings that must never leak into another tenant.
HOUSE_SERVICES = ("Localized Multilingual Support Agents", "AI Lead Generation System",
                  "Air-Gapped Internal Knowledge Base (RAG)", "Enterprise Workflow Automation")

_made = []


def make_tenant(name, **overrides):
    """Onboard a tenant from the REAL template, so the template itself is tested."""
    dest = config.CLIENTS_DIR / name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(config.CLIENTS_DIR / "_template", dest)
    _made.append(dest)
    p = dest / "config.json"
    with open(p) as f:
        cfg = json.load(f)
    cfg.update(overrides)
    with open(p, "w") as f:
        json.dump(cfg, f, indent=2)
    return dest


try:
    # ----------------------------------------------------------------------
    # The core rule: a fresh tenant pitches ITS OWN services, never ours
    # ----------------------------------------------------------------------
    print("\n[a tenant onboarded from the template sells its OWN services]")
    make_tenant("zz_test_acme", client_name="Acme Ltd", from_name="Ada Obi",
                offerings=["Bookkeeping Automation", "Invoice Chasing"],
                service_outcomes={"Bookkeeping Automation": "cut close time 40% in 60 days",
                                  "Invoice Chasing": "recover 15% of overdue invoices in 90 days"})
    acme = config.load_client("zz_test_acme")
    menu = lead_agent._client_offerings(acme)
    check("the tenant's own offerings are the menu",
          menu[:2] == ["Bookkeeping Automation", "Invoice Chasing"])
    joined = " | ".join(menu)
    check("NO house service leaks into the tenant's menu",
          not any(h in joined for h in HOUSE_SERVICES))
    check("config.tenant_offerings agrees with the drafter",
          config.tenant_offerings(acme) == menu)

    # ----------------------------------------------------------------------
    # An incomplete tenant fails LOUDLY at load — not silently at draft time
    # ----------------------------------------------------------------------
    print("\n[an incomplete tenant is refused at config load]")
    make_tenant("zz_test_empty", client_name="Empty Co", offerings=[], service_outcomes={})
    try:
        config.load_client("zz_test_empty")
        check("a tenant with no offerings raises ConfigError", False)
    except config.ConfigError as e:
        check("a tenant with no offerings raises ConfigError", True)
        check("the error names the fix (offerings / MULTITENANCY)",
              "offerings" in str(e) and "MULTITENANCY" in str(e))

    make_tenant("zz_test_noname", offerings=["Something Real"])   # client_name left REPLACE-ME
    try:
        config.load_client("zz_test_noname")
        check("a tenant with an unfilled client_name raises", False)
    except config.ConfigError as e:
        check("a tenant with an unfilled client_name raises", "client_name" in str(e))

    # ----------------------------------------------------------------------
    # Branding: a tenant's audit page carries THEIR name, never ours
    # ----------------------------------------------------------------------
    print("\n[rendered pages carry the tenant's brand]")
    content = {"company_name": "Prospect Ltd", "prepared_for": "Ada",
               "headline": "Audit", "bottleneck": "manual work",
               "steps": [{"title": "Map", "detail": "d"}], "cta": "reply"}
    html_out = magnet.render_html(acme, content)
    check("the audit page shows the tenant's name", "Acme Ltd" in html_out)
    check("the audit page never shows OUR brand", "Ejentic" not in html_out)
    # And with no client_name at all it must degrade to nothing, not to our brand.
    bare = magnet.render_html({"client": "x"}, content)
    check("a nameless cfg does not fall back to our brand", "Ejentic" not in bare)

    # ----------------------------------------------------------------------
    # Isolation: two tenants never share config, data, or rows
    # ----------------------------------------------------------------------
    print("\n[two tenants are isolated]")
    make_tenant("zz_test_beta", client_name="Beta Co", offerings=["Something Else"])
    beta = config.load_client("zz_test_beta")
    check("each tenant gets its own DB path", acme["paths"]["db"] != beta["paths"]["db"])
    check("each tenant gets its own leads file",
          acme["paths"]["leads_csv"] != beta["paths"]["leads_csv"])
    check("tenants do not share a menu",
          set(config.tenant_offerings(acme)) != set(config.tenant_offerings(beta)))

    # Row-level: a lead written for one tenant is invisible to the other.
    tmpdb = os.path.join(tempfile.mkdtemp(), "state.sqlite")
    conn = state.connect(tmpdb)
    state.upsert_lead(conn, "zz_test_acme", {"email": "a@a.ng", "company_name": "A"},
                      niche=None, status="queued")
    conn.commit()
    mine = conn.execute("SELECT COUNT(*) FROM leads WHERE client=?", ("zz_test_acme",)).fetchone()[0]
    theirs = conn.execute("SELECT COUNT(*) FROM leads WHERE client=?", ("zz_test_beta",)).fetchone()[0]
    check("a lead belongs to exactly one tenant", mine == 1 and theirs == 0)
    conn.close()

    # ----------------------------------------------------------------------
    # No house business facts remain in code
    # ----------------------------------------------------------------------
    print("\n[no tenant-owned business fact is hardcoded in code]")
    check("_DEFAULT_OFFERINGS no longer exists",
          not hasattr(lead_agent, "_DEFAULT_OFFERINGS"))

    def _code_only(path):
        """Source with comments stripped: the rule is about VALUES the code EMITS,
        so a comment naming the regression it prevents is fine."""
        with open(path) as f:
            return "\n".join(l for l in f.read().splitlines()
                             if not l.lstrip().startswith("#"))

    # Every module that can put words in front of a prospect — an email, a served
    # page, a tagged lead. A house SERVICE name in any of them is the original bug.
    for mod in ("scripts/lead_agent.py", "scripts/draft_queued.py", "scripts/discover_preview.py",
                "scripts/copy_ab.py", "core/magnet.py", "core/sender.py", "core/review.py",
                "core/quality.py", "core/compliance.py"):
        p = os.path.join(ROOT, mod)
        if not os.path.exists(p):
            continue
        offenders = [h for h in HOUSE_SERVICES if h in _code_only(p)]
        check(f"{mod} hardcodes no house SERVICE name", not offenders)

    # A BRAND string is just as leaky as a service name: `get_demo_pitch` used to sign
    # every demo pitch "Best,\nEjentic AI Team", so a client recording their own demo
    # would have shown OUR name. The sign-off now comes from cfg["from_name"].
    for mod in ("scripts/lead_agent.py", "scripts/draft_queued.py", "core/magnet.py",
                "core/sender.py", "core/review.py", "core/compliance.py"):
        p = os.path.join(ROOT, mod)
        if not os.path.exists(p):
            continue
        code = _code_only(p)
        # "Ejentic" may appear in an identifier (ejentic_service) or a docstring, but
        # never inside a string literal the code would EMIT. Quoted is the tell.
        emitted = ('"Ejentic' in code or "'Ejentic" in code
                   or "Ejentic AI Team" in code or "Ejentic AI\\n" in code)
        check(f"{mod} emits no house BRAND string", not emitted)

    check("outbound headers carry no house brand",
          "X-Ejentic" not in _code_only(os.path.join(ROOT, "core", "sender.py")))

    # The demo pitch signs off with the TENANT's name, whoever runs it.
    _s, _b = lead_agent.get_demo_pitch("Adebayo & Co", "http://x", sign_off="Ada Obi")
    check("demo pitch signs off with the tenant's own name",
          _b.strip().endswith("Ada Obi") and "Ejentic" not in _b)

    # ----------------------------------------------------------------------
    # The template itself is a complete starting point
    # ----------------------------------------------------------------------
    print("\n[the template is complete]")
    with open(config.CLIENTS_DIR / "_template" / "config.json") as f:
        tpl = json.load(f)
    with open(config.CLIENTS_DIR / "ejentic" / "config.json") as f:
        ej = json.load(f)
    missing = sorted(set(ej) - set(tpl))
    check(f"template carries every key a real client has (missing: {missing or 'none'})",
          not missing)
    for key in ("offerings", "service_outcomes", "lead_source"):
        check(f"template declares {key!r}", key in tpl)
    check("template ships no real credentials",
          "sk-" not in json.dumps(tpl) and "@gmail" not in json.dumps(tpl))

finally:
    for d in _made:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(os.path.join(ROOT, "data", os.path.basename(str(d))), ignore_errors=True)

print(f"\n{'='*50}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
