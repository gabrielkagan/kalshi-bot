---
status: current
updated: 2026-04-18
tags: [process, agents, verification, lessons-learned]
---
# Agent-Audit Verification Protocol

## Lesson from the 2026-04-18 dashboard audit
A six-agent swarm was dispatched to map the dashboard's current state. Three of those agents (Chesterton fence, frontend map, reverse-index) produced findings with **substantial false positives** on the specific "safe to delete" list:

| Agent claim | Reality | How the claim failed |
|---|---|---|
| `eth_filter_shadow` is dead (no bot.py writer) | LIVE — it's a query-time counterfactual analytics panel over real trades, not a filter_stage-based shadow | Agent searched for `filter_stage='eth_filter_shadow'` writers; missed that the key is a pure analytics reducer over existing data |
| `nig_distribution` never read by frontend | LIVE — frontend renders at `dashboard/index.html:6145` | Agent's reverse-index grep missed the destructuring `const nig = s.nig_distribution` pattern |
| `ask_distribution` never read by frontend | LIVE — frontend renders at `dashboard/index.html:5732` | Same as above |
| `hourly_config_c..m` are ghost (backend never writes) | LIVE — backend produces all 8 via loop over `_HOURLY_VARIANT_DEFS` at `dashboard_snapshot.py:1826` | Agent grep'd for explicit `snap["hourly_config_c"] = ...` and missed the generic builder |
| `dip_addon_shadow` safe to remove as top-level key | NOT a top-level key — only appears nested under `exec_eng["dip_addon_shadow"]` counter | Agent conflated "referenced" with "written as top-level snap key" |

Of the 5 Chesterton-flagged deletes, **zero** were actually safe to delete after verification.

## The protocol

Before acting on any agent's "safe to remove / delete / kill" recommendation, verify each item *individually*:

### 1. Confirm the target exists as the agent claims
- If the agent says "top-level snap key" — grep for `^\s*snap\["<key>"\]\s*=` (anchored to start of line, not nested access)
- If "filter_stage writer" — grep bot.py + engines for `filter_stage="<stage>"`
- If "never read by frontend" — grep the frontend for **all** access patterns: `s.<key>`, `d.<key>`, `snapshot.<key>`, destructuring (`const { <key> }`), dynamic access (`s[name]`)

### 2. Check the live data
- Is the backend writer gated by a flag? What's the flag value today?
- Is there data in the DB? `SELECT COUNT(*), MAX(<ts>) FROM <table>` for the stage
- Is the feature referenced in `CLAUDE.md` current state? KB decision docs?

### 3. Check adjacent machinery
- Tests asserting the key exists (`tests/integration/test_contracts.py`, `test_dashboard_contract.py`)
- Settlement routing that references the product_type
- Analyst / auditor / researcher scripts consuming the key
- Other KB articles describing why the key was added

### 4. Ask when in doubt
If two or more verification checks are ambiguous, ask the user. Cheaper than restoring deleted analytics.

## Why this matters
The swarm pattern is correct — parallel investigation surfaces more than a serial thread can see. But agent outputs are *hypotheses*, not commits. The subagent's context is narrower than the main thread's; it hasn't seen the full CLAUDE.md "Current State" table or the full KB history. Its grep patterns aren't exhaustive. Treat its findings like a PR from a smart stranger: valuable, not trusted.

## Related
- [[decisions/dashboard-overhaul-plan.md]] — where this lesson was learned
- [[failures/dashboard-drift.md]] — the incident that spawned the audit
- CLAUDE.md "Critical Rules" — "Never present analysis without checking actual data first — no assumptions about column values, schema, enum strings, or data shape"
