<!--
Sprint 13 Bit 13.2-rest (2026-05-11) — template for adding a new
shadow strategy.

Usage:
  1. Read this entire template before scaffolding.
  2. Create the strategy module under bot/shadows/<name>_shadow.py
     (or extend an existing sibling like fifteenm_shadow.py).
  3. Wire the writer + filter_stage + DB schema atomically per the
     bot/CLAUDE.md "Add a shadow strategy" workflow.
  4. Delete this HTML comment block before committing.

Per CLAUDE.md sacred rules:
- Shadow flag DEFAULT = observation-only (writer fires; no orders).
- ALL `candidate.append` sites must be gated by the shadow flag.
  Single downstream gate is NOT sufficient — see
  `feedback_shadow_flag_comprehensive_may10.md` (T1 R1 caught
  TM/WKND/OVN/DC strategies bypassing XRP_15M_SHADOW gate).
- DB columns ship atomically with INSERT signature changes
  (`_shadow_diag` schema chain — bot/CLAUDE.md).
- New shadow row insert with the right `filter_stage` value.
- Add a dashboard metric to `bot/snapshots/dashboard_snapshot.py` per the
  decision doc `kb/decisions/dashboard-overhaul-plan.md`.
- Shadow only — don't promote without explicit instruction +
  data backing.
-->

# New Shadow Strategy: <STRATEGY_NAME>

## Decision doc

Create `kb/decisions/<strategy-name>-shadow-design-<YYYY-MM-DD>.md`
describing:
- Hypothesis: what edge does this exploit?
- Filter criteria (price band, STC zone, asset, volatility regime, etc.)
- Sizing rule (Kelly fraction, fixed contract, etc.)
- Promotion threshold (e.g., 50 settled trades + Wilson CI lower bound
  ≥ breakeven)
- Risk-of-ruin analysis if promoted

## Constants

Add to `bot/constants.py`:

```python
# <STRATEGY_NAME> shadow strategy (Bit <BIT_NUMBER>, <DATE>).
# Cross-ref: kb/decisions/<strategy-name>-shadow-design-<YYYY-MM-DD>.md.
<STRATEGY_NAME>_SHADOW = True       # Observation mode — writer fires, no orders
<STRATEGY_NAME>_MIN_EDGE = 0.02     # Filter threshold
<STRATEGY_NAME>_FILTER_STAGE = "<filter_stage_value>"  # DB filter_stage literal
```

## Writer wiring (bot/scanner/__init__.py)

```python
# Gate ALL candidate.append sites — see feedback_shadow_flag_comprehensive_may10.md
if <STRATEGY_NAME>_SHADOW and <FILTER_CRITERIA>:
    candidate = {
        ...,
        "filter_stage": <STRATEGY_NAME>_FILTER_STAGE,
        ...
    }
    candidates.append(candidate)
```

## DB schema

If adding new columns (`_shadow_diag` chain): update ALL FOUR sites
atomically (per bot/CLAUDE.md `_shadow_diag` schema chain rule):
1. `evaluated_opportunities` table CREATE/ALTER
2. `insert_rejection()` signature + SQL
3. `insert_evaluated_opportunity()` signature + SQL
4. Any new column added to `_shadow_diag` dict population

## Dashboard

Add a metric block to `bot/snapshots/dashboard_snapshot.py`:

```python
# <STRATEGY_NAME> shadow (Bit <BIT_NUMBER>)
def _<strategy_name>_metrics(conn):
    cur = conn.execute("""
        SELECT COUNT(*) AS n, AVG(...) AS ...
        FROM evaluated_opportunities
        WHERE filter_stage = ?
    """, (<STRATEGY_NAME>_FILTER_STAGE,))
    ...
```

Ship `bot/snapshots/dashboard_snapshot.py` + `dashboard/index.html` (gh-pages) in
the SAME commit per `kb/decisions/dashboard-overhaul-plan.md`.

## Tests

After shipping, write a regression test (per CLAUDE.md "Regression
tests after bug fixes only"). Pattern:

```python
def test_<strategy_name>_shadow_writes_filter_stage_atomically():
    """Pin: every <STRATEGY_NAME> candidate.append site routes through
    the SHADOW gate. Single downstream gate is NOT sufficient per
    feedback_shadow_flag_comprehensive_may10.md."""
    ...
```

## Promotion checklist (NOT auto)

Don't promote without:
- N ≥ 50 settled trades in current regime
- Wilson CI lower bound on WR ≥ breakeven
- Counterfactual PnL positive in last 7d + 14d + 30d windows
- Explicit user approval

## Cross-refs
- `bot/CLAUDE.md` § "Add a shadow strategy"
- `bot/CLAUDE.md` § "Cell-block activations deflate filter_stage='candidate' rollups"
- `feedback_shadow_flag_comprehensive_may10.md` (memory)
