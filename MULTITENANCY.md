# Multi-tenancy: the redeployability rule

**Every system we build must be redeployable to a new client by CONFIGURATION ALONE.**

Standing directive (2026-09-05). Onboarding a client is: copy the template, fill it in, add that
client's credentials, run it. Editing Python to take on a client is a bug in the architecture — we
are not rebuilding a system per client, and we are not shipping one client's business facts inside
another client's outreach.

This is the **agency model, done properly**: one deployment per client, on their own infra. It is
*not* shared-instance SaaS — there is no tenant login, no co-mingled database, no billing plane.
What "multi-tenant" buys us here is that the same codebase serves any client unchanged.

## The dividing line

**A tenant owns** (config — never code):

| Tenant-owned | Where it lives |
|---|---|
| Identity + branding (name, sender, reply-to, website) | `clients/<tenant>/config.json` |
| **What they sell** — the service menu and its outcome figures | `offerings`, `service_outcomes` |
| Where leads come from (CSV, Maps, Yellow Pages, segments, queries) | `lead_source`, `discovery`, `yellowpages` |
| Sending identities and caps | `sending.mailboxes[]` (env-var *names* only) |
| Legal footer (CAN-SPAM postal address) | `physical_address` |
| Copy behaviour (premium on/off, model, quality-gate thresholds) | `copy` |
| All runtime state | `data/<tenant>/state.sqlite` |
| Secrets | `.env`, referenced by NAME from config, never inlined |

**Code owns** the pipeline mechanics only: how to discover, draft, score, queue, send, observe.

> **The rule, in one line: no business fact about any specific client may live in code.**
> Not a service name. Not an outcome figure. Not a brand string. Not a postal address.

## Why this is not theoretical

`scripts/lead_agent.py` used to carry `_DEFAULT_OFFERINGS` — Ejentic's own four services — as the
fallback menu when a client configured none. A client onboarded from the template inherited it, so
their cold emails would have **pitched Ejentic's services to their prospects**. It was verified by
onboarding a fake client and printing the resolved menu.

That is the exact failure this rule exists to prevent: a house default standing in for a
tenant-owned value, failing silently at draft time instead of loudly at config time.

## Checklist for any system (new or existing)

A system is redeployable when all of these hold:

1. **Per-tenant config file** — one file, no tenant values anywhere else.
2. **Per-tenant data path** — no shared mutable state between tenants.
3. **Every table tenant-scoped** — a `client`/`tenant` column, and every query filters on it.
4. **No house defaults for tenant-owned values.** A missing tenant value is an error, not a
   fallback to whoever we built the system for first.
5. **Validation fails loudly at load** — a misconfigured tenant must not reach an LLM call, a
   rendered page, or an outbound email.
6. **Secrets by reference** — config names an env var; it never contains the secret.
7. **A test that onboards a fresh tenant from the template** and asserts no other tenant's data or
   branding leaks into it. This is the regression test for the whole rule.
8. **Docs**: an "onboarding a new client" section someone else could follow.

## Status per system

| System | Redeployable | Notes |
|---|---|---|
| `lead-generation-system` | ✅ reference implementation | 10/10 tables tenant-scoped; `offerings` in config; validation raises; `scripts/new_client.py`; `tests/test_multitenancy.py` |
| `ejentic-rag-system` | ✅ compliant (2026-09-06) | Audit DB moved to `data/<client>/audit.db` + a `client` column every read filters on; clearance tags come from the tenant's own `clearance_levels`, not a hardcoded set; `auth` block names env vars and refuses to boot if `required:true` with no keys; `backend/tests/` (5 suites, no keys) |
| `ejentic-agents` | ⬜ not yet audited | |
| `ai-agency` (website) | ⬜ not yet audited | |
| `local-research-agent` | ⬜ not yet audited | |
| `freellmapi` | ⬜ n/a? | Shared gateway, arguably single-tenant infrastructure |

## Onboarding a client (lead-gen)

```bash
venv/bin/python scripts/new_client.py acme      # copies the template, validates
# fill in clients/acme/config.json  (offerings, service_outcomes, address, sending)
# add ACME_SMTP_USER / ACME_SMTP_PASS to .env
CLIENT=acme venv/bin/python scripts/lead_agent.py --preview
```

The preview runs the real pipeline against a throwaway DB and sends nothing — the fastest way to
confirm a new tenant drafts **its own** services before anything goes out.
