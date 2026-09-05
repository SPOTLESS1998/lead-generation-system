"""Onboard a new client (tenant) — the whole job, without touching code.

Copies clients/_template into clients/<name>, optionally fills the headline fields
from flags, then VALIDATES the result so you learn immediately whether the client is
complete rather than discovering it mid-run. See MULTITENANCY.md for the rule this
enforces: a new client is configuration, never a code edit.

    venv/bin/python scripts/new_client.py acme
    venv/bin/python scripts/new_client.py acme \
        --name "Acme Ltd" --sender "Ada Obi" \
        --offerings "Bookkeeping Automation,Invoice Chasing" \
        --address "Acme Ltd, 4 Broad St, Lagos, Nigeria"
    venv/bin/python scripts/new_client.py acme --check     # validate an existing client

Nothing is sent and no existing client is touched. Credentials are NEVER written
here — config references env-var NAMES, and you add the values to .env yourself.
"""

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import config   # noqa: E402


def _fill(cfg, args):
    """Apply whichever headline fields were supplied; leave the rest as REPLACE-ME."""
    if args.name:
        cfg["client_name"] = args.name
    if args.sender:
        cfg["from_name"] = args.sender
    if args.address:
        cfg["physical_address"] = args.address
    if args.base_url:
        cfg["unsubscribe_base_url"] = args.base_url
    if args.website:
        cfg["website_url"] = args.website
    if args.offerings:
        names = [s.strip() for s in args.offerings.split(",") if s.strip()]
        if names:
            cfg["offerings"] = names
            # Seed one outcome per service so the strategist has a figure to quote.
            # Left as REPLACE-ME on purpose: an invented figure would ship in real mail.
            cfg["service_outcomes"] = {
                n: "REPLACE-ME the concrete result, with a real figure and timeframe"
                for n in names
            }
    return cfg


def _report(client):
    """Load the client the way the pipeline will, and say what still needs filling."""
    try:
        cfg = config.load_client(client)
    except config.ConfigError as e:
        print(f"\n❌ Not ready: {e}")
        return False

    todo = []
    for key in ("client_name", "from_name", "physical_address"):
        if "REPLACE-ME" in str(cfg.get(key) or "").upper():
            todo.append(key)
    for svc, outcome in (cfg.get("service_outcomes") or {}).items():
        if "REPLACE-ME" in str(outcome).upper():
            todo.append(f"service_outcomes[{svc!r}]")
    if "REPLACE-ME" in " ".join(str(o) for o in (cfg.get("offerings") or [])).upper():
        todo.append("offerings")

    print(f"\n✅ Config loads and validates for tenant '{client}'.")
    print(f"   name      : {cfg.get('client_name')}")
    print(f"   sender    : {cfg.get('from_name')}")
    print(f"   sells     : {', '.join(config.tenant_offerings(cfg))}")
    print(f"   send mode : {cfg['sending']['mode']}")
    print(f"   database  : {cfg['paths']['db']}")

    if todo:
        print("\n⚠️  Still to fill in before real sends:")
        for t in todo:
            print(f"     - {t}")
    else:
        print("\n   Nothing left as REPLACE-ME.")
    return True


def main():
    ap = argparse.ArgumentParser(description="Onboard a new client from the template.")
    ap.add_argument("client", help="short lowercase tenant id, e.g. 'acme'")
    ap.add_argument("--name", help='display name, e.g. "Acme Ltd"')
    ap.add_argument("--sender", help='From-line name, e.g. "Ada Obi"')
    ap.add_argument("--offerings", help="comma-separated services this client SELLS")
    ap.add_argument("--address", help="real postal address (CAN-SPAM)")
    ap.add_argument("--base-url", help="public base URL for audit/unsubscribe links")
    ap.add_argument("--website", help="the client's website URL")
    ap.add_argument("--check", action="store_true",
                    help="only validate an existing client; create nothing")
    args = ap.parse_args()

    client = args.client.strip().lower()
    dest = config.CLIENTS_DIR / client

    if args.check:
        if not dest.exists():
            print(f"❌ No such client: {dest}")
            return 1
        return 0 if _report(client) else 1

    if client.startswith("_"):
        print("❌ Names starting with '_' are reserved (that's the template).")
        return 1
    if dest.exists():
        print(f"❌ {dest} already exists — refusing to overwrite an existing client.\n"
              f"   To inspect it:  venv/bin/python scripts/new_client.py {client} --check")
        return 1

    template = config.CLIENTS_DIR / "_template"
    shutil.copytree(template, dest)
    cfg_path = dest / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg = _fill(cfg, args)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")

    print(f"📁 Created {dest}/ from the template.")
    print(f"   edit: {cfg_path}")
    print(f"   leads: {dest / 'leads.csv'}  (or set lead_source to maps_firecrawl/yellowpages)")

    ok = _report(client)
    print(f"\nNext:")
    print(f"  1. finish {cfg_path} (offerings + their real outcome figures, address)")
    print(f"  2. add this client's SMTP creds to .env (config references env-var NAMES only)")
    print(f"  3. dry run, sends nothing:  CLIENT={client} venv/bin/python scripts/lead_agent.py --preview")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
