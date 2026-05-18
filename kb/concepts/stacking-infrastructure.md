---
status: active
updated: 2026-05-18
tags: [stacking, composite-pk, strategy-group]
---
# Stacking Infrastructure

## Summary
Stacking allows multiple strategies (main, DC, terminal momentum, bracket NO) to hold simultaneous positions on the same ticker. Uses a composite primary key of (ticker, strategy_group), with per-ticker and per-window caps to limit correlated exposure. Gated by `STACKING_ENABLED` env var.

## Problem Stacking Solves
Without stacking, the bot's per-ticker dedup prevents a main candidate and a DC overlay (or TM intercept) from both trading the same ticker. This leaves money on the table when multiple independent strategies identify edge on the same contract.

## Strategy Groups
`strategy_to_group()` in `models.py` maps strategy names to groups:
- **main** — standard 15M candidates, hourly, overnight/weekend discount
- **decided** — all DC tiers (T1, T1B, T2, T2_Z25, T2_Z2)
- **terminal_momentum** — TM strategy
- **bracket_no** — weather bracket NO

Different groups can coexist on the same ticker. Same group cannot stack (a ticker can only have one `main` position).

## Schema Changes
The `positions` and `settled_trades` tables gained:
- `strategy_group TEXT DEFAULT 'main'` — identifies which group owns the position
- `is_stacked BOOLEAN DEFAULT 0` — marks positions created via stacking

Primary key for positions changed from `(ticker)` to `(ticker, strategy_group)`.

## Position Checks
When evaluating a candidate:
1. Look up existing positions for that ticker
2. If `STACKING_ENABLED`, check if the candidate's strategy_group already has an open position
3. If same group exists, skip (no intra-group stacking)
4. If different group, allow (inter-group stacking)

When `STACKING_ENABLED = False`, legacy behavior: reject any ticker with an existing position regardless of group.

## Safety Caps
- Per-ticker aggregate risk limit prevents total exposure from exceeding safe levels even with stacked positions
- Per-window position limits apply to each strategy group independently
- `MAX_CONCURRENT_TAKER_PER_ASSET` still applies as a global safety cap

## Settlement
Settlement processes each (ticker, strategy_group) position independently. PnL computed per position, not aggregated at ticker level. The settlement refactor ensures each strategy group's position gets its own settlement record in `settled_trades`.

**Revenue override (Apr 5, 2026):** `_process_settlement()` passes `revenue_override=row_count*100` to `record_settlement()`. Without this, the Kalshi API's aggregate revenue (for ALL contracts on the ticker) was attributed to EACH position row, double-counting PnL. See [[failures/pnl-reporting-bugs.md]].

**Lost addon positions:** If reconciliation deletes an addon position before settlement, the surviving position's revenue may still reflect the full ticker revenue from the API. The revenue_override fix handles this for positions that exist at settlement time, but lost addons cannot be recovered.

## Kill Switch
`STACKING_ENABLED = os.environ.get("STACKING_ENABLED", "0") == "1"` — defaults OFF. When disabled, logs a warning if any tickers have multiple positions (shouldn't happen, but safety check).

## Per-Strategy Stacking Gates (overlays beyond the group-PK check)

The composite-PK check above is the BASELINE. Individual strategies may layer ADDITIONAL gates that refuse to stack on certain cross-group combinations — typically when production data shows a specific stack class is unprofitable.

### Terminal Momentum (TM) — B5 gates (2026-05-18, ticket `86b9zudg2`)

The TM intercept in `bot/scanner/__init__.py` has FOUR overlap gates (anchor: search for `# ── Terminal Momentum intercept` in `bot/scanner/__init__.py`; B5 gates land in the same block, around `_tm_dc_overlap` / `_tm_has_position`):

1. **`_tm_dc_overlap`** (pre-B5) — same-tick: skip if any `decided_*` candidate is in the current scan tick's `candidates` list for the same ticker.
2. **`_tm_dc_retry_overlap`** (B5) — cross-tick: skip if any `decided_*` IOC is in flight via `self._ml.executor._dc_retry_queue` from a prior tick. Closes the production case where TM_98 fired 22s after `decided_t1`'s first IOC entered retry on `KXHYPE15M-26MAY180530-30` (the prior-tick decided_t1 was gone from `candidates` so the `_tm_dc_overlap` check missed it).
3. **`_tm_has_position`** (pre-B5) — same-price TM-on-TM: skip if a TM position with `strategy_group == f"terminal_momentum_{best_ask}"` is already open. Different-price TM-on-TM stacking remains allowed (40/40 stackable-ticker YES-settlement data).
4. **`_tm_non_tm_position`** (B5) — per-(ticker, side='yes') non-TM entry-lock: skip if any non-TM Kelly-sized position (`decided_*`, `weekend_discount`, `overnight_discount`, `low_price_near_expiry`, etc.) holds an open YES-side position on the ticker. NO-side strategies (e.g., `bracket_no`) on the same ticker do NOT block YES-side TM (side filter preserves the NO-side carve-out).

These FOUR gates run before the TM concurrent-cap check. Note that gates 1 + 3 predate B5; gates 2 + 4 are the B5 additions. See [[strategies/terminal-momentum.md]] §"Overlap Prevention & Stacking" for the full enumeration and data justifications.

**Regression pin (B5):** `tests/integration/test_tm_stack_decided_regression.py` — AST guard on the TM intercept block source asserting the two B5 tokens (`_tm_dc_retry_overlap` + `_tm_non_tm_position`) are present, plus the `side == "yes"` side-filter and the `not (...).startswith("terminal_momentum")` non-TM filter. RED pre-fix on 3 of 4 sub-asserts; GREEN post-fix. The block boundary is anchored on the `# ── Terminal Momentum intercept` comment (start) + the `continue  # Skip insufficient_edge rejection — this is now a TM candidate` line (end).

**Behavioral pin (B5-fu1, ticket `86b9zyh4v`):** an end-to-end behavioral pin (a `decided_t1` candidate or in-flight `_dc_retry_queue` entry on window W ⇒ a same-window TM_98 evaluation hits the gate-rejection short-circuit instead of submitting a stacked order) is filed under B5-fu1. The pin may extend `test_tm_stack_decided_regression.py` or land in a sister file; status on `main` may lag this doc — check `git log -- tests/integration/test_tm_stack_decided_regression.py` (and adjacent `tests/integration/test_b5*.py` if added) for the latest state.

**Postmortem:** `kb/failures/tm-stack-decided-may18.md` (local-only) carries the full B5 RCA and L99/L104 lessons. The B5 commit hash on `main` is `76f1d54c`.

### Weekend Discount + Overnight Discount — B5-fu4 gates (2026-05-18, ticket `86ba05k5q`)

L107 sister of B5. The `weekend_discount` and `overnight_discount` live paths in `bot/scanner/__init__.py` (anchors: search for `# ── Weekend Edge Discount` and `# ── Overnight Edge Discount`) previously had only the `_wknd_dc_overlap` / `_ovn_dc_overlap` z/price/STC heuristic — that predicate does NOT scan open positions or per-tick candidates, so a Kelly-sized non-DC entry from a prior tick (TM, decided_*, main candidate, etc.) could be stacked-on by a same-ticker WKND/OVN candidate when the discount band opened up. Enumerated as UNGUARDED in B5-fu3 and tracked under ticket `86ba05k5q` until B5-fu4 closed the gap.

B5-fu4 adds the SAME pair of B5 gates to each of the two paths:

1. **`_wknd_dc_retry_overlap` / `_ovn_dc_retry_overlap`** — cross-tick: skip if any `decided_*` IOC is in flight via `self._ml.executor._dc_retry_queue` from a prior tick. Parallel of B5's `_tm_dc_retry_overlap`. Defensive `if self._ml is not None and getattr(self._ml, "executor", None) is not None:` guard preserves test-injection / boot-order safety.
2. **`_wknd_non_wknd_position` / `_ovn_non_ovn_position`** — per-(ticker, side='yes') entry-lock: skip if any open YES-side position whose `strategy` does NOT start with the protected prefix (`"weekend_discount"` / `"overnight_discount"` respectively) is open on the ticker. Mirrors B5's `_tm_non_tm_position` shape including the `(p.get("side") or "yes") == "yes"` legacy-row default and the NO-side carve-out (so `bracket_no` on the same ticker does NOT block YES-side WKND/OVN). Sourced from `self._state.get_open_positions()`.

Both predicates wire into `_wknd_live_eligible` / `_ovn_live_eligible` as `and not _wknd_dc_retry_overlap and not _wknd_non_wknd_position` (parallel for OVN), so shadow logging continues to receive the rejected signal — only the live `candidates.append` path is gated.

**Cross-strategy symmetry post-B5-fu4:** B5 closed TM-stacks-on-{decided,WKND,OVN,LPNE} from the TM side; B5-fu4 closes WKND/OVN-stacks-on-{TM,decided,LPNE} from the WKND/OVN side. TM-WKND/OVN cross-stacking is now mutually blocked.

**Regression pin (B5-fu4):** `tests/integration/test_b5_fu4_wknd_ovn_gate_regression.py` — mirrors B5-fu1's pattern: 4 structural AST guards (one per new predicate) + 24 behavioral pins (AST-extract each RHS, eval against synthetic state) covering same-ticker fire, different-ticker no-fire, non-decided/non-{wknd,ovn} carve-outs, NO-side carve-out, legacy missing-`side` default, empty-queue/empty-positions no-fire, defensive missing-`candidate` handling. RED pre-fix (4/4 structural fail, behavioral classes skip via `setUpClass`); GREEN post-fix (28/28).

**L104 promotion:** `tests/contracts/test_l104_single_decision_per_window.py` promoted both strategies from `EXEMPT_STRATEGIES` to `GUARDED_STRATEGIES` in the same atomic Bit so the L104 ratchet now catches any future regression that removes the B5-fu4 guards. `EXEMPT_STRATEGIES` is empty post-B5-fu4.

## Related
- [[concepts/dc-strategy.md]]
- [[strategies/terminal-momentum.md]]
- [[strategies/bracket-no.md]]
- [[concepts/position-reconciliation.md]]
- [[decisions/stacking-enabled.md]]
