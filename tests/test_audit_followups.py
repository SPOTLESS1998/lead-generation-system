"""Audit follow-ups: fail-closed money, tenant config isolation, CSV parity.

Three unrelated fixes with one thing in common — each was a failure that wore the
costume of a deliberate choice:

  1. core/budget: a malformed `premium_daily_usd_cap` ("$5") raised inside
     float(), returned None, and None is the sentinel for UNLIMITED. So a typo in
     the only config value denominated in real dollars silently removed the
     ceiling, and /health rendered "uncapped · Remaining today: ∞" — identical to
     having meant it. Now it fails CLOSED.

  2. core/config: _deep_merge shared nested DEFAULTS dicts by reference, so
     cfg["yellowpages"] IS DEFAULTS["yellowpages"]. One tenant mutating its own
     config rewrote the defaults for every tenant loaded afterwards in the same
     process — and the approval server is long-running and multi-tenant.

  3. core/leads: the CSV path validated emails with "@" plus a dotted domain, so
     contact@example.com passed — while both scraped paths refuse it. Same guard
     on every entry route now.

Run:  venv/bin/python tests/test_audit_followups.py
"""

import os
import sys
import copy
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import budget, config, leads as leads_mod        # noqa: E402
from core.budget import BudgetCapInvalid                   # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _cfg(cap, premium=True):
    c = {"client": "t", "copy": {"premium": premium}}
    if cap is not _ABSENT:
        c["copy"]["premium_daily_usd_cap"] = cap
    return c


_ABSENT = object()


class _NoSpend:
    """Stands in for a DB connection; spend is stubbed to 0 so only the cap matters."""


# --------------------------------------------------------------------------
def test_valid_caps():
    print("\n[a usable cap is read as a cap]")
    budget.spent_today_usd = lambda conn, cfg: 0.0
    check("5.0 => 5.0", budget.daily_cap_usd(_cfg(5.0)) == 5.0)
    check("int 5 => 5.0", budget.daily_cap_usd(_cfg(5)) == 5.0)
    check("absent => None (uncapped, deliberate)",
          budget.daily_cap_usd(_cfg(_ABSENT)) is None)
    check("explicit null => None (uncapped, deliberate)",
          budget.daily_cap_usd(_cfg(None)) is None)
    check("0 => 0.0 (spend nothing, NOT unlimited)",
          budget.daily_cap_usd(_cfg(0)) == 0.0)


def test_malformed_cap_fails_closed():
    print("\n[a MALFORMED cap must never read as unlimited]  <-- the fix")
    for bad in ["$5", "5 USD", "", "abc", "5,00", [], {}, float("nan"), float("inf"), -1, True]:
        try:
            got = budget.daily_cap_usd(_cfg(bad))
            raised = False
        except BudgetCapInvalid:
            raised, got = True, "raised"
        check(f"{bad!r} => refused (not None/unlimited)", raised)

    budget.spent_today_usd = lambda conn, cfg: 0.0
    check("premium_allowed is FALSE on a malformed cap",
          budget.premium_allowed(_NoSpend(), _cfg("$5")) is False)
    check("remaining_usd is 0.0, not None (None renders as ∞)",
          budget.remaining_usd(_NoSpend(), _cfg("$5")) == 0.0)

    st = budget.status(_NoSpend(), _cfg("$5"))
    check("status carries a cap_error for the page to show", bool(st.get("cap_error")))
    check("status cap_usd is None but allowed_now is False",
          st["cap_usd"] is None and st["allowed_now"] is False)


def test_cap_zero_blocks_spend():
    print("\n[a cap of 0 means spend nothing]")
    budget.spent_today_usd = lambda conn, cfg: 0.0
    check("premium refused at cap 0", budget.premium_allowed(_NoSpend(), _cfg(0)) is False)
    check("...and at cap 0.0", budget.premium_allowed(_NoSpend(), _cfg(0.0)) is False)
    check("but allowed at cap 5 with 0 spent",
          budget.premium_allowed(_NoSpend(), _cfg(5.0)) is True)


def test_uncapped_still_works():
    print("\n[an intentionally uncapped client is unaffected]")
    budget.spent_today_usd = lambda conn, cfg: 999.0
    check("no cap => allowed even at high spend",
          budget.premium_allowed(_NoSpend(), _cfg(None)) is True)
    check("no cap => remaining is None (renders ∞, correctly)",
          budget.remaining_usd(_NoSpend(), _cfg(None)) is None)
    check("premium off => never allowed, cap irrelevant",
          budget.premium_allowed(_NoSpend(), _cfg(None, premium=False)) is False)


# --- tenant config isolation ----------------------------------------------

def test_config_does_not_alias_defaults():
    print("\n[a tenant's config must not BE the module-global DEFAULTS]  <-- the fix")
    snapshot = copy.deepcopy(config.DEFAULTS)
    merged = config._deep_merge(config.DEFAULTS, {"client_name": "Acme"})

    for key in ("yellowpages", "notify", "heartbeat", "discovery", "sending", "copy"):
        check(f"cfg[{key!r}] is a COPY, not DEFAULTS[{key!r}]",
              merged[key] is not config.DEFAULTS[key])
    check("nested dict is also a copy",
          merged["copy"]["quality_gate"] is not config.DEFAULTS["copy"]["quality_gate"])
    check("nested LIST is a copy too",
          merged["sending"]["warmup"]["ramp"] is not
          config.DEFAULTS["sending"]["warmup"]["ramp"])

    # Mutate the tenant's config the way scripts/discover_preview.py does.
    merged["yellowpages"]["location"] = "MUTATED"
    merged["sending"]["mailboxes"].append({"address": "leak@tenant-a.test"})
    merged["copy"]["quality_gate"]["min_score"] = 1

    check("DEFAULTS['yellowpages'] untouched",
          config.DEFAULTS["yellowpages"].get("location") == snapshot["yellowpages"].get("location"))
    check("DEFAULTS['sending']['mailboxes'] untouched (no credential bleed)",
          config.DEFAULTS["sending"]["mailboxes"] == snapshot["sending"]["mailboxes"])
    check("DEFAULTS quality_gate untouched",
          config.DEFAULTS["copy"]["quality_gate"]["min_score"]
          == snapshot["copy"]["quality_gate"]["min_score"])
    check("DEFAULTS fully unchanged", config.DEFAULTS == snapshot)


def test_two_tenants_are_independent():
    print("\n[tenant A's edits cannot reach tenant B]")
    a = config._deep_merge(config.DEFAULTS, {"client_name": "A"})
    a["notify"]["mode"] = "per_draft"
    a["discovery"]["max_leads"] = 999
    b = config._deep_merge(config.DEFAULTS, {"client_name": "B"})
    check("B did not inherit A's notify.mode", b["notify"]["mode"] == "digest")
    check("B did not inherit A's discovery.max_leads", b["discovery"]["max_leads"] != 999)
    check("the two tenants share no dict", a["notify"] is not b["notify"])


# --- CSV parity ------------------------------------------------------------

def _csv(rows):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "leads.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        f.write("first_name,last_name,title,email,company_name,company_description,website_url\n")
        for r in rows:
            f.write(r + "\n")
    return p


def test_csv_refuses_placeholder_domains():
    print("\n[CSV path now refuses reserved domains, like the scraped paths]  <-- the fix")
    p = _csv([
        "Ada,Obi,CEO,contact@example.com,Fake Ltd,desc,http://x.test",
        "Ben,Eze,CTO,info@test.com,Also Fake,desc,http://y.test",
        "Cal,Udo,COO,a@mail.example.org,Sub Fake,desc,http://z.test",
        "Dee,Nwa,MD,real@actualbusiness.com,Real Ltd,desc,http://r.test",
    ])
    got, skipped = leads_mod.load_leads({"paths": {"leads_csv": p}})
    emails = [l["email"] for l in got]
    check("example.com refused", "contact@example.com" not in emails)
    check("test.com refused", "info@test.com" not in emails)
    check("subdomain of a reserved domain refused", "a@mail.example.org" not in emails)
    check("the real address is KEPT", "real@actualbusiness.com" in emails)
    check("exactly one lead loaded", len(got) == 1)
    check("the three bad rows are counted as skipped", skipped == 3)


def test_csv_still_accepts_normal_addresses():
    print("\n[the new guard does not reject legitimate small-business addresses]")
    good = ["info@adebayo-co.com.ng", "first.last+tag@sub.company.co.uk",
            "hello@shop.io", "k@x.ng"]
    p = _csv([f"A,B,T,{e},Co,desc,http://x.test" for e in good])
    got, skipped = leads_mod.load_leads({"paths": {"leads_csv": p}})
    check(f"all {len(good)} accepted", len(got) == len(good))
    check("none skipped", skipped == 0)

    # And the pre-existing rules still hold.
    p2 = _csv(["A,B,T,REPLACE-ME@example.com,Co,d,http://x.test",
               "A,B,T,,Co,d,http://x.test",
               "A,B,T,#commented@real.com,Co,d,http://x.test",
               "A,B,T,not-an-email,Co,d,http://x.test"])
    got2, skipped2 = leads_mod.load_leads({"paths": {"leads_csv": p2}})
    check("template/blank/commented/malformed rows still skipped",
          got2 == [] and skipped2 == 4)


if __name__ == "__main__":
    print("=" * 62)
    print("AUDIT FOLLOW-UPS: money, tenant isolation, CSV parity")
    print("=" * 62)
    test_valid_caps()
    test_malformed_cap_fails_closed()
    test_cap_zero_blocks_spend()
    test_uncapped_still_works()
    test_config_does_not_alias_defaults()
    test_two_tenants_are_independent()
    test_csv_refuses_placeholder_domains()
    test_csv_still_accepts_normal_addresses()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
