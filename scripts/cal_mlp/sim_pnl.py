"""
P2 Phase 6: counterfactual sim PnL replay (A6/A26/A28/A30).

Pulls all evaluated_opportunities candidates in the test window, replays the
full live gate (MIN_EDGE_BY_PRICE + weekend/overnight discounts +
HIGH_PRICE_STC_BLOCK_ENABLED + STC_EXTENDED per-asset floors) under MLP
path, sizes via Kelly + drawdown + STC scaler + per-asset caps, nets fees,
sums per-asset/per-band/per-strategy.

Round 1-4 fixes applied (R-p6-impl-1 through R-p6-impl-4):
- MIN_EDGE_BY_PRICE 6-tier FRACTION schedule (verbatim bot.py:1180-1187)
- Weekend/overnight discount with regular-gate-first fallback control flow
- WEEKEND_EDGE_FLOOR=0.0 cap on weekend threshold (bot.py:857)
- Overnight inclusive-end at OVERNIGHT_QUIET_END=11 (bot.py:861)
- Microsecond-precision Z-suffix ISO timestamps for SQL params
- Per-asset MIN_ENTRY_PRICE filter
- STC_EXTENDED 300-600s zone per-asset floors (BTC=93/ETH=90/SOL=95/XRP=92)
  with STC_EXTENDED_BUFFER_RESCUE=0.25 bypass
- HIGH_PRICE_STC_BLOCK side='yes' filter
- Strategy taker dispatch: only MAKER_PATIENT is maker
- Fee-adjusted edge (taker fee always for gate + tier) per bot.py / models.py
- Per-asset risk caps via sizing.compute_size(asset=...)
- O(n) deque drawdown
- NaN-safe is_weekend / hour_of_day_utc with evaluation_time fallback
- Challenger A/B: own normstats + own ticker_to_id, day_bootstrap_ci on deltas
- Pre-tier=-1 excluded from migration accounting
"""
from __future__ import annotations

import math
import sqlite3
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import torch

sys.path.insert(0, str(Path(__file__).parent))
# R-p7-claude-md#LOW1: previously inserted Path.cwd() unconditionally — could
# leak unusual CWD modules if sim_pnl was imported with a non-standard cwd.
# sim_pnl is a CLI script-time tool (never imported by bot.py at runtime)
# but tightening anyway: only add cwd if not already on path.
_cwd = str(Path.cwd())
if _cwd not in sys.path:
    sys.path.insert(0, _cwd)

from train import (  # noqa: E402
    CalibrationDataset, CONT_FEATURE_COLS, apply_norm, collate_dict,
)
from _helpers import (  # noqa: E402
    market_implied_prob_yes, predict_with_interval, lookup_cell_quantile,
)
from sizing import (
    compute_size, compute_drawdown_scaler,
    SIZING_TIERS, SIZING_TIER_RISK_FRACTIONS, SizingResult,
)

# R-p6-impl-2#C2/C11: import fees from bot.models (pure-math module — no
# bot.py side effects). bot.models.calculate_taker_fee / calculate_maker_fee
# are the authoritative implementations bot.py itself calls.
from bot.models import calculate_taker_fee, calculate_maker_fee  # noqa: E402  (Sprint 10.5b 2026-05-11)


# ── Shared method_output construction ──────────────────────────────────

def compute_method_output(df: "pd.DataFrame") -> "pd.Series":
    """Return the canonical "production output" baseline column:
    `calibrated_prob` with `raw_prob` fallback when `calibrated_prob` is
    NULL. Output is float32 to match the parquet's
    `calibrated_prob_audit` numeric encoding.

    Single source of truth for Phase 6 — both `sim_pnl.run_sim_pnl` and
    `validate.main` MUST go through this helper. Hand-rolled duplicates
    drift silently (cf. CLAUDE.md "cal_mlp feature transforms (lock-step)"
    anti-pattern). When the formula changes, this is the
    one site to update.

    Inputs: DataFrame REQUIRED to contain `calibrated_prob` and
    `raw_prob` columns (numeric or numeric-coercible). Missing columns
    raise KeyError — there is no graceful degradation. Errors-coerce
    protects against legacy rows with stringified probabilities; for
    current extract_data.py output (both columns are float64), the
    coerce is a defensive no-op."""
    cal_series = pd.to_numeric(df['calibrated_prob'], errors='coerce')
    raw_series = pd.to_numeric(df['raw_prob'], errors='coerce')
    return cal_series.fillna(raw_series).astype(np.float32)


# R-p6-impl-2#C4 — bot.py STRATEGY_CLAMP_POLICY (1583-1623) + MAKER_PATIENT
# is the ONLY strategy that posts and waits for maker fill in 15M flow. All
# other strategies cross the spread. Default: taker.
MAKER_ONLY_STRATEGIES = frozenset({'MAKER_PATIENT'})


def _strategy_uses_taker(strategy: str) -> bool:
    if strategy is None:
        return True
    return strategy not in MAKER_ONLY_STRATEGIES


# H3+H5: stages where production rejects at runtime for reasons sim_pnl
# does NOT model (cooldowns, post-gate sizing rejects, time-window
# tightenings, cell-blocks, the cal_mlp TM-96 gate). Without filtering
# these out, sim_pnl admits rows production never took and counts them
# as wins/losses — the dominant cause of the live_ws sim_pnl divergence
# documented in kb/findings/sim-pnl-live-ws-divergence-rca-may05.md.
#
# Cell-block stage names (string literals stored in DB — NOT Python
# constant names) per CLAUDE.md "Cell-block activations deflate
# `filter_stage='candidate'` rollups" and
# kb/decisions/bleed-cell-blocks-2026-04-30.md.
#
# HPSB note: sim_pnl has its own block_off / block_on toggle for HPSB,
# but the toggle only matters for ROWS THAT REACHED HPSB. Once
# production already wrote `'96C_SOL_XRP_STC_DANGER_BAND'` as the
# filter_stage, the row is in production-rejected state — sim_pnl can't
# faithfully replay block_off vs block_on on it because the upstream
# decision pipeline already short-circuited. Conservative call: exclude.
_PRODUCTION_RUNTIME_BLOCKED_STAGES = frozenset({
    # Cooldowns / time-window rejections (production wouldn't have taken
    # regardless of gate width):
    'silent_loss_cooldown',
    'dead_hour_passed',
    'usaft_short_stc',
    # Post-gate sizing rejection (production sized to 0 contracts —
    # sim_pnl would too if it replicated the same Kelly+balance state):
    'zero_sizing',
    # Cell-blocks shipped 2026-04-30 (not internally modeled by sim_pnl):
    'TM98_97_98C_2_5MIN_BLEED',
    'SOL_TAKER_85_89C_2_5MIN_BLEED',
    'SOL_BLEED_V2_88_93C_2_5MIN',  # SOL_BLEED_V2 (2026-05-10)
    '96C_SOL_XRP_STC_DANGER_BAND',  # HPSB
    # cal_mlp TM-96 gate (sim_pnl doesn't model the TM-96 cal_mlp path):
    'tm96_calmlp_gate_blocked',
    # TM NBBO gate (bot.py:13651-13683) — production rejects 96/97c TM
    # rows on NBBO source AND sub-0.10% buffer 98/99c rows. sim_pnl
    # doesn't replicate the source-aware gate. (n=11 in the May 2-6
    # window per snapshot DB.)
    'tm_nbbo_buffer_shadow',
    # H7 dedup: scan-block precursor logs for executed live trades.
    # bot.py logs each TM/DC/weekend/overnight decision twice — once
    # at the strategy-specific scan block (filter_stages below) and
    # once at the executor (filter_stage='candidate'). Sim_pnl's
    # universe pre-dedup admitted both rows, double-counting PnL for
    # the executed trade. The 'candidate' row is the canonical row
    # because it represents the EXECUTOR state (with `order_id` set
    # when fired per snapshot verification 2026-05-05), not the SCAN
    # state. Precursor row populates strategy in some cases
    # (`terminal_momentum` per bot.py:13834 = "terminal_momentum_{ask}")
    # and not others (DC/weekend/overnight per bot.py:14140 / 14227
    # / 14446 omit `strategy=`); both are dropped uniformly so the
    # 'candidate' row's strategy field drives H7 dispatch. For
    # shadow-only rows without a candidate sibling, this filter loses
    # counterfactual coverage — accepted trade-off for this round;
    # future work: derive strategy from filter_stage for shadow-only
    # rows. See kb/findings/sim-pnl-live-ws-divergence-rca-may05.md
    # adversarial round 1 CRITICAL #1+#2.
    'decided_contract_t1',
    'decided_contract_t1b',
    'decided_contract_t2',
    'decided_contract_t2_z25',
    'decided_contract_t2_z2',
    'weekend_discount',
    'weekend_discount_shadow',
    'overnight_discount',
    'overnight_discount_shadow',
    'terminal_momentum',
    # R4: floor_raise_shadow rows are sub-floor entries production
    # logs but does NOT trade (the scan-time floor rejection is what
    # this stage shadows). With the BTC min-price floor lowered to 80
    # to capture LPNE rows, sim_pnl SQL would otherwise admit these
    # 50+ rows whose `strategy=NULL` would fall through to standard
    # Kelly and contribute counterfactual PnL on trades production
    # never took.
    'floor_raise_shadow',
})


def _dedup_by_ticker_keep_canonical(df: 'pd.DataFrame') -> 'pd.DataFrame':
    """R6 MAJOR #1: drop duplicate rows on the same `ticker` so sim_pnl
    counts each ticker's outcome AT MOST ONCE.

    bot.py logs each ticker MANY times during a scan cycle: (a) one
    `filter_stage='candidate'` row when it actually trades, (b) various
    `*_shadow*` filter_stage rows for counterfactual observability
    (relaxed_edge_shadow, golden_hour_shadow, dc_shadow_t2_z2, etc.),
    (c) gate-rejection log rows (`insufficient_edge`). Each row carries
    the SAME `market_result` (the ticker resolved YES or NO once); if
    sim_pnl admits N rows for the ticker, it counts the outcome N
    times — systematic over-counting.

    Production realized AT MOST ONE trade per ticker (concurrent-
    position cap + executor-row-is-canonical). Sim_pnl should mirror.

    Dedup priority (highest first):
        1. `filter_stage='candidate'` (the executor row when trade fired)
        2. `filter_stage='terminal_momentum'` only intersects pre-block;
           after `_exclude_production_runtime_blocked` it's gone.
        3. Latest `evaluation_time` among remaining rows for the ticker
           (closest to settlement; matches the moment the bot would have
           last evaluated the gate).

    Empirical: 312 shadow rows in the May 2-6 window had `candidate`
    siblings on the same ticker. 282 `low_price_shadow` rows alone —
    each previously contributed PnL on top of the candidate sibling's.
    Aggregate over-count was the dominant component of the residual
    `_unknown` PnL bucket per the round-6 adversarial review.

    Output: dedup'd df with index reset; preserves all columns.
    """
    if 'ticker' not in df.columns:
        raise RuntimeError(
            "candidate_df missing `ticker` column — required for ticker-"
            "level dedup. Check that the SELECT-list includes ticker."
        )
    if 'filter_stage' not in df.columns:
        raise RuntimeError(
            "candidate_df missing `filter_stage` column — required for "
            "candidate-vs-shadow dedup priority."
        )
    if len(df) == 0:
        return df.reset_index(drop=True)
    # Sort: candidate=1 first (descending), then evaluation_time DESC
    # (latest first). drop_duplicates(subset='ticker', keep='first') then
    # picks the candidate when it exists, else the latest row.
    df = df.copy()
    df['_is_candidate'] = (df['filter_stage'] == 'candidate').astype('int8')
    df = df.sort_values(
        ['ticker', '_is_candidate', 'evaluation_time'],
        ascending=[True, False, False],
    ).drop_duplicates(subset='ticker', keep='first')
    return df.drop(columns=['_is_candidate']).sort_values(
        ['evaluation_time', 'ticker']
    ).reset_index(drop=True)


def _exclude_production_runtime_blocked(df: 'pd.DataFrame') -> 'pd.DataFrame':
    """Drop rows whose `filter_stage` is in
    `_PRODUCTION_RUNTIME_BLOCKED_STAGES`. Preserves all other rows + all
    columns + resets the index. Raises if `filter_stage` is missing
    (SELECT-list regression guard — without this column the H3+H5
    filter is a silent no-op).

    Wired in `run_sim_pnl` AFTER the SQL pull and BEFORE
    `_replay_one_path`. See kb/findings/sim-pnl-live-ws-divergence-rca-may05.md
    H3+H5.
    """
    if 'filter_stage' not in df.columns:
        raise RuntimeError(
            "candidate_df is missing the `filter_stage` column — the SQL "
            "SELECT-list must include it for the H3+H5 production-eligibility "
            "filter to apply. Without it, sim_pnl admits rows production "
            "rejected at runtime."
        )
    mask = ~df['filter_stage'].isin(_PRODUCTION_RUNTIME_BLOCKED_STAGES)
    return df[mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# H7 — strategy-specific sizing dispatch
# ---------------------------------------------------------------------------
#
# bot.py sizes four strategy families via paths that bypass the standard
# Kelly-tier compute_size pipeline. Without per-strategy dispatch, sim_pnl
# over-sizes TM/DC candidates by 5-25× and under-sizes weekend rows whose
# Kelly clamps to zero. Mirrors documented in
# kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H7.
#
# Drift contract: bot.py lines numbered are the source of truth. Any
# change there MUST update both this block AND the constants below in
# the same commit (same lock-step rule as cal_mlp feature transforms in
# CLAUDE.md).

# Terminal momentum — bot.py:1075-1115 + tm_compute_contracts at 1226.
TM_BASE_CONTRACTS = 100
TM_PRICE_SET = frozenset({96, 98, 99})
TM_STC_SAFE_THRESHOLD = 180
TM_STC_DANGER_HI = 240
TM_STC_SAFE_MULT = 1.5
TM_STC_DANGER_MULT = 0.5
TM_STC_NORMAL_MULT = 1.0
TM_MIN_CONTRACTS = 25
TM_MAX_CONTRACTS = 500
TM_THIN_BUFFER_PCT = 0.20
TM_THIN_BUFFER_CONTRACT_CAP = 50
TM_NEGATIVE_EV_TIERS: frozenset = frozenset()  # bot.py:1090 cleared
TM_ASSET_RISK_CAPS = {'BTC': 0.15, 'ETH': 0.20, 'SOL': 0.15, 'XRP': 0.15}
# bot.py:1153 TM_SWEEP_LIVE_ENABLED defaults to "1"; bot.py:13763 passes
# MAX_ENTRY_PRICE(=99) as the worst-case sweep-tier denom for risk caps.
# Pin the worst-case so sim_pnl doesn't accidentally over-size TM at low
# prices (96c) when production would clamp at the 99c sweep tier.
TM_SWEEP_LIVE_RISK_DENOM_PRICE = 99

TM_LIVE_STRATEGIES = frozenset(f'terminal_momentum_{p}' for p in TM_PRICE_SET)


def _tm_size(
    price_cents: int,
    stc: float,
    balance_cents: int,
    asset: str,
    buf_pct: Optional[float] = None,
) -> int:
    """Mirror bot.py:1226 tm_compute_contracts. Returns contract count.

    Formula: TM_BASE × margin × stc_mult, capped by per-asset risk frac
    against the sweep-live worst-case price (=99c), then by thin-buffer
    cap when buf_pct < 0.20%, then floored at TM_MIN_CONTRACTS and
    capped at TM_MAX_CONTRACTS.
    """
    margin = 100 - price_cents
    if margin <= 0:
        return TM_MIN_CONTRACTS
    if price_cents in TM_NEGATIVE_EV_TIERS:
        return TM_MIN_CONTRACTS
    if stc < TM_STC_SAFE_THRESHOLD:
        stc_mult = TM_STC_SAFE_MULT
    elif stc < TM_STC_DANGER_HI:
        stc_mult = TM_STC_DANGER_MULT
    else:
        stc_mult = TM_STC_NORMAL_MULT
    ct = int(TM_BASE_CONTRACTS * margin * stc_mult)
    if balance_cents > 0:
        risk_frac = TM_ASSET_RISK_CAPS.get(asset, 0.15)
        max_by_risk = int(balance_cents * risk_frac / TM_SWEEP_LIVE_RISK_DENOM_PRICE)
        ct = min(ct, max_by_risk)
    if buf_pct is not None and buf_pct < TM_THIN_BUFFER_PCT:
        ct = min(ct, TM_THIN_BUFFER_CONTRACT_CAP)
    return max(TM_MIN_CONTRACTS, min(TM_MAX_CONTRACTS, ct))


# Decided contract — bot.py:1042-1048 + sizing block at bot.py:14401-14425.
DECIDED_CONTRACT_T2_Z25_RISK = 0.10
DECIDED_CONTRACT_T2_Z2_RISK = 0.20
DECIDED_CONTRACT_RISK = 0.20
# bot.py:1048 — ordered (price_floor, risk) pairs; first match wins.
SOL_DC_RISK_TIERS = ((97, 0.05), (95, 0.10))
# bot.py:14418-14424 — only BTC/SOL/XRP are capped (ETH skipped).
DC_PER_ASSET_RISK_CAP = {'BTC': 0.15, 'SOL': 0.15, 'XRP': 0.15}

# bot.py:14586-14590 strategy → tier mapping.
_DC_STRATEGY_TO_RISK = {
    'decided_t1':     DECIDED_CONTRACT_RISK,
    'decided_t1b':    DECIDED_CONTRACT_RISK,
    'decided_t2':     DECIDED_CONTRACT_RISK,
    'decided_t2_z25': DECIDED_CONTRACT_T2_Z25_RISK,
    'decided_t2_z2':  DECIDED_CONTRACT_T2_Z2_RISK,
}
DC_LIVE_STRATEGIES = frozenset(_DC_STRATEGY_TO_RISK.keys())


def _dc_size(
    strategy: str,
    price_cents: int,
    balance_cents: int,
    asset: str,
) -> int:
    """Mirror bot.py:14401-14425 fixed-% per tier sizing.

    Out-of-scope vs production: window risk cap (`DECIDED_CONTRACT_MAX_WINDOW_RISK`)
    requires multi-row state (`_dc_window_risk` per event_ticker) sim_pnl
    doesn't track. Same-ticker existing-exposure cap (bot.py:14560-14579)
    is also stateful and out of scope. Both effects are minor in
    aggregate per the H3+H5 structural insight in the RCA — the dominant
    sizing divergence is the per-trade 20% vs 25% Kelly mismatch which
    THIS function fixes.
    """
    if balance_cents <= 0:
        return 0
    # Round-1 adversarial MINOR #2: guard against price_cents <= 0.
    # SQL filters market_price > 0 so this should never fire from the
    # production path, but defends against direct-helper callers (e.g.
    # tests) and keeps the contract symmetric with bot.py's max(1, ...)
    # floor below.
    if price_cents <= 0:
        return 0
    risk = _DC_STRATEGY_TO_RISK.get(strategy, DECIDED_CONTRACT_RISK)
    if asset == 'SOL':
        for floor, sol_risk in SOL_DC_RISK_TIERS:
            if price_cents >= floor:
                risk = sol_risk
                break
    pos = max(1, int(balance_cents * risk / price_cents))
    cap_frac = DC_PER_ASSET_RISK_CAP.get(asset)
    if cap_frac is not None:
        cap = int(balance_cents * cap_frac / price_cents)
        if pos > cap >= 1:
            pos = cap
    return pos


# Weekend discount fixed fallback — bot.py:968 + 14076-14079.
WEEKEND_FIXED_RISK = 0.07


# Low-Price Near-Expiry — bot.py:1327-1334 + 13013/13057.
# BTC-only intercept at 80-87c, STC 10-120s, sized at FLAT
# LPNE_FIXED_CONTRACTS=50. Bypasses Kelly entirely; production stores
# kelly_f=0.0 and drawdown_scaler=1.0 for every LPNE row.
LPNE_FIXED_CONTRACTS = 50
LPNE_LIVE_STRATEGIES = frozenset({'low_price_near_expiry'})


def _lpne_size(balance_cents: int) -> int:
    """Mirror bot.py:13013 + 13057. Flat 50 contracts when balance > 0;
    0 otherwise (defensive — bot.py only fires LPNE when balance is
    nonzero implicitly via scan-time eligibility)."""
    if balance_cents <= 0:
        return 0
    return LPNE_FIXED_CONTRACTS


def _strategy_size(
    strategy: Optional[str],
    fee_adjusted_edge_frac: float,
    available_balance_cents: int,
    entry_price_cents: int,
    current_balance_cents: int,
    hwm_cents: int,
    seconds_to_close: float,
    asset: str,
    spot_price: Optional[float] = None,
    threshold: Optional[float] = None,
) -> SizingResult:
    """Per-strategy dispatcher. Routes terminal_momentum_* and
    decided_t* via their bot.py-specific sizing formulas; weekend_discount
    falls back to WEEKEND_FIXED_RISK when Kelly produces 0; everything
    else (including overnight_discount, TAKER_NOW, MAKER_PATIENT, NULL)
    uses the standard compute_size path.

    Bankroll input semantics match the standard sim_pnl path:
        * available_balance_cents — per-row stored snapshot (production's
          balance at decision time). Used as the bankroll for ALL
          per-strategy formulas (parity with bot.py's
          `self._get_balance_cached()`).
        * current_balance_cents — sim_pnl's running cumulative used for
          drawdown_scaler input. Only relevant to the standard
          compute_size + WEEKEND_FIXED_RISK fallback paths; TM/DC are
          drawdown-agnostic in production.

    Returns a SizingResult. For TM/DC, tier_idx is set to -1 (these
    strategies bypass the SIZING_TIERS Kelly ladder); risk_fraction is
    set to the formula's effective per-trade risk where applicable, or
    0.0 for TM (which is margin-based, not edge-based).
    """
    if strategy in TM_LIVE_STRATEGIES:
        # R3 MINOR #3: bot.py:13649 defaults `_tm_buf_pct = 0` when
        # spot/threshold missing — 0 < TM_THIN_BUFFER_PCT(0.20) so the
        # thin-buffer cap fires (50ct cap). Sim_pnl pre-fix used None
        # which skipped the cap entirely, over-sizing TM rows with
        # missing-feature rows by 6×. May 2-6 window has 0 such rows
        # so latent only; align with bot.py default for wider backtests.
        if spot_price is not None and threshold is not None and threshold > 0:
            buf_pct = (spot_price - threshold) / threshold * 100.0
        else:
            buf_pct = 0.0
        ct = _tm_size(
            price_cents=entry_price_cents,
            stc=seconds_to_close,
            balance_cents=available_balance_cents,
            asset=asset,
            buf_pct=buf_pct,
        )
        return SizingResult(
            contract_count=int(ct),
            risk_fraction=0.0,
            tier_idx=-1,
            drawdown_scaler=1.0,
            stc_scaler=1.0,
            notional_cents=int(ct * entry_price_cents),
        )
    if strategy in DC_LIVE_STRATEGIES:
        ct = _dc_size(
            strategy=strategy,
            price_cents=entry_price_cents,
            balance_cents=available_balance_cents,
            asset=asset,
        )
        risk = _DC_STRATEGY_TO_RISK.get(strategy, DECIDED_CONTRACT_RISK)
        return SizingResult(
            contract_count=int(ct),
            risk_fraction=float(risk),
            tier_idx=-1,
            drawdown_scaler=1.0,
            stc_scaler=1.0,
            notional_cents=int(ct * entry_price_cents),
        )
    if strategy in LPNE_LIVE_STRATEGIES:
        # R4 MAJOR #1: bot.py:13013 sizes LPNE flat 50ct regardless of
        # balance/edge/STC. Without this dispatch, sim_pnl Kelly-sizes
        # LPNE rows ~350× over-sized at production-typical $100k bankroll.
        ct = _lpne_size(balance_cents=available_balance_cents)
        return SizingResult(
            contract_count=int(ct),
            risk_fraction=0.0,
            tier_idx=-1,
            drawdown_scaler=1.0,
            stc_scaler=1.0,
            notional_cents=int(ct * entry_price_cents),
        )
    sizing = compute_size(
        fee_adjusted_edge_frac, available_balance_cents, entry_price_cents,
        current_balance_cents=current_balance_cents, hwm_cents=hwm_cents,
        seconds_to_close=seconds_to_close, asset=asset,
    )
    if (strategy == 'weekend_discount'
            and sizing.contract_count == 0
            and available_balance_cents > 0
            and entry_price_cents > 0):
        # bot.py:14074-14085 — Kelly=0 fallback to WEEKEND_FIXED_RISK.
        # Drawdown scaler applied to the fixed sizing too (bot.py:14077-79).
        drawdown = compute_drawdown_scaler(current_balance_cents, hwm_cents)
        fixed_raw = max(1, int(available_balance_cents * WEEKEND_FIXED_RISK / entry_price_cents))
        if drawdown < 1.0:
            fixed_raw = max(1, int(fixed_raw * drawdown))
        return SizingResult(
            contract_count=int(fixed_raw),
            risk_fraction=float(WEEKEND_FIXED_RISK),
            tier_idx=-1,
            drawdown_scaler=float(drawdown),
            stc_scaler=1.0,
            notional_cents=int(fixed_raw * entry_price_cents),
        )
    return sizing


# ---------------------------------------------------------------------------
# Live gate replay (A26)
# ---------------------------------------------------------------------------

# R-p7-deploy-r3: MIN_EDGE_BY_PRICE_SCHEDULE moved to sizing.py for
# import-decoupling (integration.parity_assert no longer needs to load
# torch/pandas via sim_pnl). Re-exported here for back-compat.
from sizing import MIN_EDGE_BY_PRICE_SCHEDULE  # noqa: E402,F401


def min_edge_for_price(entry_price_cents: int) -> float:
    """Returns minimum edge as FRACTION (e.g., 0.01 = 1%)."""
    for floor, edge_frac in MIN_EDGE_BY_PRICE_SCHEDULE:
        if entry_price_cents >= floor:
            return edge_frac
    return 0.0025


# R-p6-impl-3#C2: weekend/overnight discount constants mirrored from bot.py.
# R-p7-deploy-r3: WEEKEND_EDGE_DISCOUNT/FLOOR + OVERNIGHT_EDGE_DISCOUNT moved
# to sizing.py (re-exported here). Live-eligibility filters stay local.
from sizing import (  # noqa: E402,F401
    WEEKEND_EDGE_DISCOUNT, WEEKEND_EDGE_FLOOR, OVERNIGHT_EDGE_DISCOUNT,
)
WEEKEND_DISCOUNT_MIN_PRICE = 90
WEEKEND_DISCOUNT_MAX_STC = 600
OVERNIGHT_DISCOUNT_MIN_PRICE = 89
OVERNIGHT_DISCOUNT_MAX_STC = 600
OVERNIGHT_HOUR_LO = 4
OVERNIGHT_HOUR_HI = 11             # INCLUSIVE upper bound (bot.py:861)
GLOBAL_MIN_ENTRY_PRICE = 75        # bot.py:219 floor for any 15M discount path


def gate_passes(
    prob_yes_calibrated: float,
    breakeven: float,
    min_edge_frac: float,
    is_weekend: bool,
    hour_of_day_utc: int,
    entry_price_cents: int,
    seconds_to_close: float,
    fee_frac: float,
) -> bool:
    """A31 gate with bot-faithful discount fallbacks.

    First parameter is the post-calibration YES probability (production's
    `final_prob`, stored in DB as `calibrated_prob`). Pre-H4 this was
    `final_lo` (conformal lower bound), which was strictly more
    conservative than production — see
    kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H4.

    R-p6-impl-4#C1/C2: bot.py runs the regular gate FIRST (bot.py:12104).
    Only on insufficient_edge rejection does it fall through to weekend
    (bot.py:12430) then overnight (bot.py:12606) discount paths. Fee
    subtraction is applied to the edge (bot.py uses fee_adjusted_edge for
    every comparison)."""
    fee_adj_edge = (prob_yes_calibrated - breakeven) - fee_frac
    # 1) Regular gate first.
    if fee_adj_edge >= min_edge_frac:
        return True
    # 2) Weekend discount fallback (bot.py:12430-12442 + 12480 live filter).
    if is_weekend and entry_price_cents >= GLOBAL_MIN_ENTRY_PRICE:
        wknd_threshold = min(min_edge_frac * WEEKEND_EDGE_DISCOUNT, WEEKEND_EDGE_FLOOR)
        wknd_live = (
            entry_price_cents >= WEEKEND_DISCOUNT_MIN_PRICE
            and seconds_to_close <= WEEKEND_DISCOUNT_MAX_STC
        )
        if wknd_live and fee_adj_edge >= wknd_threshold:
            return True
    # 3) Overnight discount fallback (bot.py:12606-12642 + live filter).
    if (not is_weekend
            and OVERNIGHT_HOUR_LO <= hour_of_day_utc <= OVERNIGHT_HOUR_HI
            and entry_price_cents >= GLOBAL_MIN_ENTRY_PRICE):
        ovn_threshold = min_edge_frac * OVERNIGHT_EDGE_DISCOUNT
        ovn_live = (
            entry_price_cents >= OVERNIGHT_DISCOUNT_MIN_PRICE
            and seconds_to_close <= OVERNIGHT_DISCOUNT_MAX_STC
        )
        if ovn_live and fee_adj_edge >= ovn_threshold:
            return True
    return False


# R-p7-deploy-r3: HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES moved to sizing.py.
from sizing import HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES  # noqa: E402,F401


def high_price_stc_block_passes(
    entry_price_cents: int,
    seconds_to_close: float,
    asset: str,
    strategy: str,
    side: str,
) -> bool:
    """A27 + finding_96c_sol_xrp_bleed_apr26 (commit 82f24b4). Returns True
    if the candidate passes (not blocked). R-p6-impl-3#C1: side='no' is
    NEVER blocked (bot.py:1218 `if side != 'yes': return False` from the
    "should-block" predicate)."""
    if side != 'yes':
        return True
    if asset not in ('SOL', 'XRP'):
        return True
    if entry_price_cents != 96:
        return True
    if seconds_to_close < 121 or seconds_to_close > 300:
        return True
    return strategy not in HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES


# ---------------------------------------------------------------------------
# Outcome PnL math
# ---------------------------------------------------------------------------

def trade_pnl_cents(
    contract_count: int,
    entry_price_cents: int,
    side: str,
    market_result: str,
    is_taker: bool,
) -> int:
    """Net PnL in cents. Side wins → contract_count × (100 - entry); side
    loses → -contract_count × entry. Net of fees."""
    if contract_count <= 0:
        return 0
    side_won = (side == market_result)
    if side_won:
        gross = contract_count * (100 - entry_price_cents)
    else:
        gross = -contract_count * entry_price_cents
    if is_taker:
        fee = calculate_taker_fee(contract_count, entry_price_cents)
    else:
        fee = calculate_maker_fee(contract_count, entry_price_cents)
    return int(gross - fee)


# ---------------------------------------------------------------------------
# HWM init reconstruction (R-p6-2#C3 + R-p6-3#C1)
# ---------------------------------------------------------------------------

# R-p6-impl-2#C5/C12 + R-p6-impl-3#C3: bot.py writes evaluation_time via
# strftime('%Y-%m-%dT%H:%M:%S.%fZ') at bot.py:3890 — MICROSECOND precision.
def _iso_z(ts: pd.Timestamp) -> str:
    """Convert pd.Timestamp → ISO8601 with µs + Z suffix (UTC), matching
    bot.py's evaluated_opportunities.evaluation_time write format."""
    if ts is None or pd.isna(ts):
        raise ValueError("test window timestamp is NaT/None")
    if not isinstance(ts, pd.Timestamp):
        ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    else:
        ts = ts.tz_convert('UTC')
    return ts.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


# Per-asset minimum entry price floors mirrored from bot.py:246-250.
# R4: BTC's effective scan-time floor is LPNE_MIN_PRICE(=80), not
# BTC_MIN_ENTRY_PRICE(=88) — bot.py:12978-12997 LPNE intercepts BTC
# rows at 80-87c BEFORE the main-pipeline floor rejection. Sub-88c
# BTC rows that aren't LPNE-eligible reach `filter_stage='floor_raise_shadow'`
# (added to _PRODUCTION_RUNTIME_BLOCKED_STAGES so they're dropped from
# the audit universe). Without this expansion, the H7 LPNE dispatcher
# is dead code for the main audit window — the SQL filter excluded
# the 10 LPNE candidate rows in the May 2-6 snapshot before they
# could be sized.
PER_ASSET_MIN_ENTRY_PRICE = {
    'BTC': 80,    # LPNE_MIN_PRICE per bot.py:1329
    'ETH': 90,
    'SOL': 86,
    'XRP': 92,
}

# R-p6-impl-3#C5 — bot.py:238-244: STC_EXTENDED zone per-asset floors.
# R-p7-deploy-r3: BUFFER_RESCUE + PER_ASSET_FLOOR moved to sizing.py.
STC_EXTENDED_LIVE_FLOOR = 300
from sizing import (  # noqa: E402,F401
    STC_EXTENDED_BUFFER_RESCUE, STC_EXTENDED_PER_ASSET_FLOOR,
)


def min_entry_price_for_asset(asset: str) -> int:
    return PER_ASSET_MIN_ENTRY_PRICE.get(asset, 75)


def stc_extended_floor_passes(
    asset: str,
    entry_price_cents: int,
    seconds_to_close: float,
    buf_pct: Optional[float],
) -> bool:
    """bot.py:16125-16151: in 300-600s STC zone, per-asset floor applies
    UNLESS buf_pct >= STC_EXTENDED_BUFFER_RESCUE.

    R5 MAJOR #1: the rescue threshold is a BUFFER PERCENT, not an edge
    fraction. bot.py:16134 computes `_ext_buf = (spot - threshold) /
    threshold * 100` (e.g. 0.260% = 0.260) and compares
    `_ext_buf >= STC_EXTENDED_BUFFER_RESCUE` (= 0.25, meaning 0.25%).
    Pre-fix sim_pnl compared `edge_frac` (probability edge fraction)
    against 0.25 — an implausibly high threshold that always rejected
    realistic rows. Empirical impact on May 2-6 window: 18 BTC/SOL
    candidate rows (most wins) were silently rejected by sim_pnl that
    bot.py admitted via buffer rescue.

    `buf_pct` is the (spot-threshold)/threshold*100 percentage. None
    indicates spot/threshold unavailable — defensive: treat as 0
    buffer (no rescue).
    """
    if seconds_to_close <= STC_EXTENDED_LIVE_FLOOR:
        return True
    if seconds_to_close > 600:
        return True
    floor = STC_EXTENDED_PER_ASSET_FLOOR.get(asset, 100)
    if entry_price_cents >= floor:
        return True
    if buf_pct is None:
        return False
    return buf_pct >= STC_EXTENDED_BUFFER_RESCUE


def reconstruct_hwm_init(
    db_path: str,
    test_start_ts: pd.Timestamp,
) -> tuple[int, int, str]:
    """Returns (hwm_cents, start_balance_cents, source).

    Source ∈ {'balance_walked', 'forward_only_from_now'}.

    `hwm_cents` — MAX(available_balance_cents) in the 7-day rolling
        window BEFORE test_start_ts. Matches bot.py PositionSizer's
        7-day rolling deque (`models.py:1011` —
        `deque(maxlen=60480)` = 7 days at 10s intervals).
    `start_balance_cents` — LATEST available_balance_cents BEFORE
        test_start_ts. Used as the initial running balance for
        drawdown-scaler input. Pre-fix sim_pnl used hwm as the
        initial balance, so drawdown ratio = 1.0 → no drawdown
        scaler → top-tier sizing even when bot was in 50%+ drawdown.
        See kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H1b.

    Pre-fix returned (hwm, source) and assumed start_balance == hwm,
    silently inflating sizing during drawdown. Combined H1a+H1b fix
    matches bot.py's PositionSizer: 7-day rolling HWM AND current
    balance separately tracked.
    """
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        ts_iso = _iso_z(test_start_ts)
        # 7-day rolling window — matches PositionSizer.balance_history maxlen.
        ts_window_start_iso = _iso_z(test_start_ts - pd.Timedelta(days=7))
        try:
            row = conn.execute(
                "SELECT MAX(available_balance_cents) FROM evaluated_opportunities "
                "WHERE evaluation_time < ? AND evaluation_time >= ? "
                "AND available_balance_cents IS NOT NULL",
                (ts_iso, ts_window_start_iso),
            ).fetchone()
            hwm = int(row[0] or 0) if row else 0
            # Separately fetch the LATEST pre-window balance — this is the
            # start-of-window state, NOT the historical peak.
            start_row = conn.execute(
                "SELECT available_balance_cents FROM evaluated_opportunities "
                "WHERE evaluation_time < ? AND available_balance_cents IS NOT NULL "
                "ORDER BY evaluation_time DESC LIMIT 1",
                (ts_iso,),
            ).fetchone()
            start_balance = int(start_row[0] or 0) if start_row else 0
            if hwm > 0:
                # Defensive: if no recent pre-window balance available
                # (gap in evaluations), fall back to hwm so sim_pnl
                # doesn't size against zero.
                if start_balance <= 0:
                    start_balance = hwm
                return (hwm, start_balance, 'balance_walked')
        except sqlite3.OperationalError:
            pass
        try:
            row = conn.execute(
                "SELECT available_balance_cents FROM evaluated_opportunities "
                "WHERE available_balance_cents IS NOT NULL "
                "ORDER BY evaluation_time DESC LIMIT 1"
            ).fetchone()
            current = int(row[0] or 0) if row else 0
            return (current, current, 'forward_only_from_now')
        except sqlite3.OperationalError:
            return (0, 0, 'forward_only_from_now')
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Candidate feature preparation (post-SQL → pre-apply_norm)
# ---------------------------------------------------------------------------

def _prepare_candidate_features(candidate_df: 'pd.DataFrame') -> 'pd.DataFrame':
    """Augment the SQL-pulled candidate_df with the columns apply_norm
    + downstream replay both need. Mirrors the canonical Phase 2 train-
    time derivation at extract_data.py:396-406.

    Operates IN-PLACE on the passed df (matches the inline pre-helper
    code path) and ALSO returns it. **Callers MUST reassign the return
    value** — see test_run_sim_pnl_assigns_helper_return_to_candidate_df
    for the AST-level lock-step rationale. The reassignment shape is
    the contract; treating the helper as side-effect-only would silently
    drift if the helper later switched to returning a new df.

    Output columns added to whatever the SQL pulled in:
      - entry_price_cents := market_price       (alias; downstream
        sizing/replay reads this label, but apply_norm reads
        market_price via CONT_FEATURE_COLS — duplicate, do not rename)
      - spread_cents      := yes_spread_cents   (alias)
      - price_tier        := digitize over PRICE_BIN_CUTOFFS [80,90,96]
                              with right=True (matches features.py:21
                              canonical convention; bot.py + integration.py
                              + extract_data all use right=True)
      - stc_bucket        := digitize over STC_BIN_CUTOFFS [120,300,600]
                              with right=True
      - vol_regime_int    := 1 if vol_regime == 'elevated' else 0
      - spot_distance_to_strike_sigma := WINSORIZED to ±SIGMA_WINSOR_ABS_CAP
                              (overwrites column with clipped value;
                              train/serve invariant per CLAUDE.md
                              "cal_mlp feature transforms ship in ONE
                              commit"; mirror of extract_data.py:396-399)
      - abs_spot_distance_to_strike_sigma := abs(winsorized sd)
      - time_decayed_proximity := winsorized_sd * (1 - stc/900)
      - hour_sin / hour_cos := analytical from hour_of_day_utc % 24

    The helper exists so apply_norm's `out['market_price']` lookup +
    downstream sizing's `entry_price_cents` lookup BOTH succeed. The
    original code renamed market_price → entry_price_cents pre-norm,
    silently breaking apply_norm.
    """
    import features  # module-level lookup so monkey-patches in tests
                     # propagate (per features.SIGMA_WINSOR_ABS_CAP doc)

    # Aliases (duplicate, NOT rename — apply_norm reads market_price).
    candidate_df['entry_price_cents'] = candidate_df['market_price']
    candidate_df['spread_cents'] = candidate_df['yes_spread_cents']

    # Mondrian/conformal cell axes. Import the cutoffs from features
    # (NOT inlined) so any future change there propagates here in
    # lock-step — matches extract_data.py:60-65 import pattern. int8
    # dtype matches extract_data.py:365,376 — train/serve parity.
    candidate_df['price_tier'] = np.digitize(
        candidate_df['entry_price_cents'].astype(float).to_numpy(),
        features.PRICE_BIN_CUTOFFS, right=True,
    ).astype(np.int8)
    candidate_df['stc_bucket'] = np.digitize(
        candidate_df['seconds_to_close'].astype(float).to_numpy(),
        features.STC_BIN_CUTOFFS, right=True,
    ).astype(np.int8)
    candidate_df['vol_regime_int'] = (
        candidate_df['vol_regime'].astype(str) == 'elevated'
    ).astype(np.int8)

    # Sigma winsorization MUST happen before deriving abs() and
    # time_decayed_proximity. Cap=25.0 (features.SIGMA_WINSOR_ABS_CAP) —
    # raw sigma can hit ±3,000+ at terminal STC; without the clip,
    # train (extract) and serve (here) diverge silently.
    cap = features.SIGMA_WINSOR_ABS_CAP
    sd_raw = candidate_df['spot_distance_to_strike_sigma'].astype(np.float32)
    sd = sd_raw.clip(lower=-cap, upper=cap)
    candidate_df['spot_distance_to_strike_sigma'] = sd
    candidate_df['abs_spot_distance_to_strike_sigma'] = sd.abs()
    stc = candidate_df['seconds_to_close'].astype(np.float32)
    candidate_df['time_decayed_proximity'] = sd * (1.0 - stc / 900.0)

    # Cyclic hour. Routes through canonical features.compute_hour_features
    # so train (this path) and serve (integration.py) stay in lock-step.
    from features import compute_hour_features
    h = candidate_df['hour_of_day_utc'].astype(np.float32) % 24.0
    candidate_df['hour_sin'], candidate_df['hour_cos'] = compute_hour_features(h)

    return candidate_df


# ---------------------------------------------------------------------------
# Main sim PnL entrypoint
# ---------------------------------------------------------------------------

def run_sim_pnl(
    asset: str,
    bundle: dict,
    conformal_artifact: dict,
    predictor,
    market_blend_w: float,
    test_window: tuple,
    normstats: dict,
    db_path: str,
    device: torch.device,
    challenger_bundle: Optional[dict] = None,
    challenger_artifact: Optional[dict] = None,
) -> dict:
    """Returns the sim_pnl dict for the audit JSON. Runs DUAL replay
    (block_off and block_on)."""
    test_start, test_end = test_window
    ts_start_iso = _iso_z(test_start)
    ts_end_iso = _iso_z(test_end)
    asset_min_price = min_entry_price_for_asset(asset)
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        excluded_null_market_price = int(conn.execute(
            """SELECT COUNT(*) FROM evaluated_opportunities
               WHERE asset=? AND product_type='15m'
                 AND market_price IS NULL
                 AND ticker NOT LIKE 'SPORTS-%'
                 AND evaluation_time >= ? AND evaluation_time < ?""",
            (asset, ts_start_iso, ts_end_iso),
        ).fetchone()[0] or 0)
        candidate_df = pd.read_sql(
            """
            SELECT ticker, evaluation_time, asset, side, strategy,
                   market_price, seconds_to_close, vol_regime, z_score,
                   yes_spread_cents, calibrated_prob, calibration_method,
                   raw_prob, breakeven_wr, fee_adjusted_edge, kelly_f,
                   is_weekend, hour_of_day_utc,
                   spot_distance_to_strike_sigma, prob_breakeven_gap,
                   market_result, available_balance_cents,
                   filter_stage,
                   -- H7: spot_price + threshold derive TM thin-buffer
                   -- buf_pct = (spot - threshold) / threshold * 100. NULL
                   -- on legacy rows is fine — _strategy_size passes
                   -- buf_pct=None, no thin-buffer cap applied.
                   spot_price, threshold
            FROM evaluated_opportunities
            WHERE asset = ?
              AND product_type = '15m'
              AND market_price IS NOT NULL
              AND market_price > 0
              AND market_price >= ?
              AND ticker NOT LIKE 'SPORTS-%'
              AND evaluation_time IS NOT NULL
              AND evaluation_time >= ? AND evaluation_time < ?
              AND raw_prob IS NOT NULL
              -- R6 CRITICAL #1: side IS NULL rows (e.g. the
              -- dc_shadow_t1b_93c / dc_shadow_t2_90c family at
              -- bot.py:13543) were admitted by the gate, then
              -- mis-bucketed at trade_pnl_cents because
              -- `str(None) == 'yes'` is False → all 46 May 2-6
              -- rows (100% YES outcomes) counted as systematic
              -- losses. The side filter drops them universally
              -- and locks against future shadow stages that
              -- forget to populate the column.
              AND side IN ('yes', 'no')
              -- spot_distance_to_strike_sigma + prob_breakeven_gap are
              -- NOT filtered: extract_data.py also does not filter NULLs
              -- on these columns (build_feature_frame relies on apply_norm
              -- mean-imputation per features.MISSING_INDICATOR_COLS doc
              -- and the cfg_fp-locked null_imputation_policy =
              -- 'fold_train_mean_with_missing_indicator'). Adding NULL
              -- filters here would shrink the candidate universe vs
              -- train, biasing v1-vs-v2 comparison silently.
            ORDER BY evaluation_time, ticker
            """,
            conn, params=(asset, asset_min_price, ts_start_iso, ts_end_iso),
        )
    finally:
        conn.close()

    n_total = len(candidate_df)
    unsettled = candidate_df[
        candidate_df['market_result'].isna()
        | (~candidate_df['market_result'].isin(['yes', 'no']))
    ]
    n_unsettled = len(unsettled)
    candidate_df = candidate_df[
        candidate_df['market_result'].isin(['yes', 'no'])
    ].reset_index(drop=True)
    # H3+H5: drop rows production rejected at runtime (cooldowns,
    # cell-blocks, cal_mlp TM-96 gate, zero-sizing). sim_pnl can't model
    # these, so admitting them = counterfactual PnL on trades production
    # never could have taken. See
    # kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H3+H5.
    n_pre_eligibility = len(candidate_df)
    candidate_df = _exclude_production_runtime_blocked(candidate_df)
    n_excluded_runtime_blocked = n_pre_eligibility - len(candidate_df)
    # R6 MAJOR #1: dedup multiple rows per ticker so sim_pnl counts each
    # ticker's outcome AT MOST ONCE. Pre-fix the same ticker's outcome
    # was counted by every shadow + candidate row that passed the gate
    # (282 low_price_shadow + 115 relaxed_edge_shadow + ... = ~700 over-
    # counted contributions in May 2-6 window). Helper prefers
    # 'candidate' rows, falls back to latest evaluation_time per ticker.
    n_pre_dedup = len(candidate_df)
    candidate_df = _dedup_by_ticker_keep_canonical(candidate_df)
    n_excluded_ticker_dedup = n_pre_dedup - len(candidate_df)
    n_universe = len(candidate_df)
    unsettled_drop_rate = (n_unsettled / max(1, n_total))

    # Imputation observability — count rows that reach apply_norm with
    # NULL in the unfiltered columns (apply_norm fillna-imputes them to
    # fold-train mean per train parity with extract_data.py). A sudden
    # spike in null rate would silently shift the imputed-mean
    # distribution. Computed POST-settlement-filter so the count
    # reflects what apply_norm actually sees (not the pre-filter pull).
    n_null_imputed_spot_distance = int(
        candidate_df['spot_distance_to_strike_sigma'].isna().sum()
    )
    n_null_imputed_prob_breakeven_gap = int(
        candidate_df['prob_breakeven_gap'].isna().sum()
    )

    # Aliases + cell axes + winsorize + derive (mirror of
    # extract_data.py:396-406). Helper is testable in isolation; pre-helper
    # code did `rename(market_price → entry_price_cents)` which stripped
    # the `market_price` column apply_norm needs.
    candidate_df = _prepare_candidate_features(candidate_df)
    # R3#C2: derive Phase 4-required columns for the model forward pass.
    # int8 dtype matches extract_data.py:377 — train/serve parity.
    candidate_df['side_int'] = (candidate_df['side'].astype(str) == 'yes').astype(np.int8)
    # R-p6-impl-2#C6: fall back to raw_prob when calibrated_prob is NULL.
    # Single source of truth: compute_method_output (top of this module).
    candidate_df['method_output'] = compute_method_output(candidate_df)
    candidate_df['outcome'] = (
        candidate_df['market_result'].str.lower() == candidate_df['side'].str.lower()
    ).astype(np.int8)
    # R3#C2: clipped logit of raw_prob for the skip-term forward pass.
    from features import RAW_PROB_CLIP_EPS, MISSING_INDICATOR_COLS
    rp = pd.to_numeric(candidate_df['raw_prob'], errors='coerce').astype(np.float64).to_numpy()
    rp_c = np.clip(rp, RAW_PROB_CLIP_EPS, 1.0 - RAW_PROB_CLIP_EPS)
    candidate_df['logit_raw_prob_clipped'] = np.log(rp_c / (1.0 - rp_c)).astype(np.float32)
    # MISSING_INDICATOR_COLS — Phase4Dataset requires these. Phase 6 doesn't
    # have the source NULLs, so default to zero (no missing).
    for col in MISSING_INDICATOR_COLS:
        if col not in candidate_df.columns:
            candidate_df[col] = np.int8(0)

    # R-p6-impl-r5#CRIT: _load_normstats returns the full payload {'stats':...,
    # 'transforms':...}; apply_norm needs the inner per-column dict.
    cand_normed = apply_norm(
        candidate_df, normstats['stats'], CONT_FEATURE_COLS,
        transforms=normstats.get('transforms', {}),
    )
    ticker_to_id = {t: i for i, t in enumerate(sorted(cand_normed['ticker'].unique()))}
    # R3#C2: ticker_id column needed by Phase4Dataset.
    cand_normed['ticker_id'] = cand_normed['ticker'].astype(str).map(ticker_to_id).fillna(0).astype(np.int64)
    ds = CalibrationDataset(cand_normed, CONT_FEATURE_COLS, ticker_to_id)
    from torch.utils.data import DataLoader
    loader = DataLoader(ds, batch_size=2048, shuffle=False, collate_fn=collate_dict)
    p_means, p_stds = [], []
    with torch.no_grad():
        for batch in loader:
            p, p_std = predictor.predict(batch)
            p_means.extend(p.detach().cpu().tolist())
            p_stds.extend(p_std.detach().cpu().tolist())
    assert len(p_means) == len(candidate_df), \
        f"base predictor count drift: {len(p_means)} vs {len(candidate_df)}"
    candidate_df['p_pred'] = np.asarray(p_means, dtype=np.float64)
    candidate_df['p_std'] = np.asarray(p_stds, dtype=np.float64)

    hwm_init_cents, start_balance_cents, hwm_source = reconstruct_hwm_init(
        db_path, pd.Timestamp(test_start),
    )

    results = {}
    for block_label, block_enabled in [('block_off', False), ('block_on', True)]:
        results[block_label] = _replay_one_path(
            candidate_df, conformal_artifact, market_blend_w,
            asset, block_enabled, hwm_init_cents,
            start_balance_cents=start_balance_cents,
            # H4: BASE replay against the production-matching bundle uses
            # the exact stored calibrated_prob so sim_pnl decisions track
            # production's. Challenger replay below stays on the
            # 'p_mean' default (different bundle → must re-compute).
            gate_prob_source='stored_calibrated_prob',
        )
    tier_migration = results['block_off'].get('tier_migration', {})
    weighted_drop = tier_migration.get('drop_pct', 0.0)
    worst_7d_mlp = results['block_off'].get('worst_7d_drawdown_cents', 0)
    worst_7d_prod = results['block_off'].get('worst_7d_drawdown_prod_cents', 0)
    if worst_7d_prod and abs(worst_7d_prod) > 0:
        worst_ratio = abs(worst_7d_mlp) / abs(worst_7d_prod)
    else:
        worst_ratio = 1.0

    out = {
        'block_off': results['block_off'],
        'block_on': results['block_on'],
        'tier_migration': tier_migration,
        'weighted_avg_risk_drop': weighted_drop,
        'worst_7d_drawdown_ratio': worst_ratio,
        'hwm_init_cents': hwm_init_cents,
        'hwm_init_source': hwm_source,
        'unsettled_drop_rate': unsettled_drop_rate,
        'n_candidate_universe': n_universe,
        'n_total_pre_filter': n_total,
        'n_unsettled_in_window': n_unsettled,
        # H3+H5: count of rows dropped because production rejected them
        # at runtime (cooldowns, cell-blocks, cal_mlp gate, zero-sizing).
        # Surface so the audit JSON shows the filter is active.
        'n_excluded_production_runtime_blocked': n_excluded_runtime_blocked,
        # R6 MAJOR #1: count of duplicate rows per ticker dropped post-
        # blocked-stage filter. High value indicates many shadow rows
        # had candidate siblings (= would have double-counted PnL pre-fix).
        'n_excluded_ticker_dedup': n_excluded_ticker_dedup,
        # Audit-field denominator note: `excluded_null_market_price` is
        # counted on the FULL test-window query (asset + product_type +
        # !sports), pre any other SQL filter — historical convention,
        # downstream consumers depend on the name. The two `n_null_imputed_*`
        # counts are computed on the POST-settlement-filter candidate_df
        # (the universe that actually reaches apply_norm). The two
        # denominators are NOT comparable; do not sum them.
        'excluded_null_market_price': excluded_null_market_price,
        'n_null_imputed_spot_distance_to_strike_sigma': n_null_imputed_spot_distance,
        'n_null_imputed_prob_breakeven_gap': n_null_imputed_prob_breakeven_gap,
        'block_deprecation_confound_note': (
            "MIN_EDGE_BY_PRICE was tuned with HIGH_PRICE_STC_BLOCK ON; "
            "block_off marginal PnL is confounded — manual MIN_EDGE_BY_PRICE "
            "re-validation required before deprecating."
        ),
        'known_limitations': [
            'pnl_modeled == pnl_pessimistic (per-cell fill-rate model deferred — Phase 6 limitation, ship-blocker #9 dead)',
            'worst_7d_drawdown_prod == worst_7d_drawdown_mlp (production-path replay deferred — Phase 6 limitation)',
            'HWM uses all-time monotonic peak; bot.py uses 7-day rolling HWM (R-p6-impl-4#C5 — deferred to follow-up)',
            'HWM init via balance_walked uses available_balance_cents max; falls back to forward_only_from_now if no pre-window balance signal',
        ],
    }
    # A/B challenger replay — own normstats + own ticker_to_id, day_bootstrap.
    if challenger_bundle is not None and challenger_artifact is not None:
        try:
            from conformal import load_predictor as _load_pred, _load_normstats
            # R-p6-impl-r5#M3: defensive check at the sim_pnl boundary —
            # challenger_bundle MUST be loaded via load_bundle_with_dir.
            if '_bundle_dir' not in challenger_bundle:
                raise RuntimeError(
                    "challenger_bundle missing '_bundle_dir'; load via "
                    "_helpers.load_bundle_with_dir, not json.load"
                )
            ch_predictor = _load_pred(challenger_bundle, device)
            # Phase 4 bundle stores per-fold normstats under eval_fold_artifacts;
            # use deploy_fold_idx to find the right one.
            ch_deploy_idx = challenger_bundle.get(
                'deploy_fold_idx',
                max(r['fold'] for r in challenger_bundle['eval_fold_artifacts']),
            )
            ch_deploy_fold = next(
                r for r in challenger_bundle['eval_fold_artifacts']
                if r['fold'] == ch_deploy_idx
            )
            # R2#C3 + R3#C4: normstats in extract dir; fail fast if missing.
            ch_extract_rel = challenger_bundle.get('extract_bundle_path', '')
            if not ch_extract_rel:
                raise RuntimeError("challenger bundle missing extract_bundle_path")
            ch_project_root = Path(__file__).resolve().parents[2]
            ch_ext_bp = Path(ch_extract_rel)
            if not ch_ext_bp.is_absolute():
                ch_ext_bp = ch_project_root / ch_ext_bp
            ch_extract_dir = ch_ext_bp.parent
            ch_normstats_path = Path(ch_deploy_fold['normstats_path'])
            if not ch_normstats_path.is_absolute():
                ch_normstats_path = ch_extract_dir / ch_normstats_path
            ch_normstats = _load_normstats(
                ch_normstats_path,
                expected_sha=ch_deploy_fold.get('normstats_sha256'),
            )
            # R-p6-impl-r5#CRIT: unwrap normstats payload {'stats':..., 'transforms':...}
            ch_normed = apply_norm(
                candidate_df, ch_normstats['stats'], CONT_FEATURE_COLS,
                transforms=ch_normstats.get('transforms', {}),
            )
            ch_ticker_to_id = {t: i for i, t in enumerate(sorted(ch_normed['ticker'].unique()))}
            # R3#C2: ticker_id column for Phase4Dataset.
            ch_normed['ticker_id'] = ch_normed['ticker'].astype(str).map(ch_ticker_to_id).fillna(0).astype(np.int64)
            ch_ds = CalibrationDataset(ch_normed, CONT_FEATURE_COLS, ch_ticker_to_id)
            ch_loader = DataLoader(ch_ds, batch_size=2048, shuffle=False, collate_fn=collate_dict)
            ch_p_means, ch_p_stds = [], []
            with torch.no_grad():
                for batch in ch_loader:
                    p, p_std = ch_predictor.predict(batch)
                    ch_p_means.extend(p.detach().cpu().tolist())
                    ch_p_stds.extend(p_std.detach().cpu().tolist())
            assert len(ch_p_means) == len(candidate_df), \
                f"challenger pred count drift: {len(ch_p_means)} vs {len(candidate_df)}"
            ch_df = candidate_df.copy()
            ch_df['p_pred'] = np.asarray(ch_p_means, dtype=np.float64)
            ch_df['p_std'] = np.asarray(ch_p_stds, dtype=np.float64)
            ch_results = {}
            for block_label, block_enabled in [('block_off', False), ('block_on', True)]:
                ch_results[block_label] = _replay_one_path(
                    ch_df, challenger_artifact, market_blend_w,
                    asset, block_enabled, hwm_init_cents,
                    start_balance_cents=start_balance_cents,
                )
            out['challenger'] = {
                'block_off': ch_results['block_off'],
                'block_on': ch_results['block_on'],
            }
            from stats import day_bootstrap_ci as _day_boot
            base_daily = results['block_off'].get('daily_pnl', {})
            ch_daily = ch_results['block_off'].get('daily_pnl', {})
            all_days = sorted(set(base_daily.keys()) | set(ch_daily.keys()))
            deltas = np.asarray([
                (ch_daily.get(d, 0) - base_daily.get(d, 0)) / 100.0
                for d in all_days
            ], dtype=np.float64)
            if len(deltas) > 0:
                d_point, d_lo, d_hi, d_audit = _day_boot(
                    deltas, n_bootstrap=2000, alpha=0.05, seed=0,
                )
            else:
                d_point, d_lo, d_hi, d_audit = (0.0, 0.0, 0.0, {'n_days': 0})
            out['ab_summary'] = {
                'base_total_pessimistic_30d': results['block_off'].get('total_pessimistic_30d', 0.0),
                'challenger_total_pessimistic_30d': ch_results['block_off'].get('total_pessimistic_30d', 0.0),
                'delta_pessimistic_30d': (
                    ch_results['block_off'].get('total_pessimistic_30d', 0.0)
                    - results['block_off'].get('total_pessimistic_30d', 0.0)
                ),
                'delta_per_day_mean': d_point,
                'delta_per_day_ci_lo_95': d_lo,
                'delta_per_day_ci_hi_95': d_hi,
                'delta_per_day_audit': d_audit,
                'n_days_paired': len(all_days),
            }
        except Exception as e:
            out['challenger_error'] = f"A/B replay failed: {type(e).__name__}: {e}"
    return out


_VALID_GATE_PROB_SOURCES = ('p_mean', 'stored_calibrated_prob')


def _replay_one_path(
    df: pd.DataFrame,
    conformal_artifact: dict,
    market_blend_w: float,
    asset: str,
    block_enabled: bool,
    hwm_init_cents: int,
    start_balance_cents: Optional[int] = None,
    gate_prob_source: str = 'p_mean',
) -> dict:
    """Walk forward through candidates in evaluation_time order, replaying
    the gate. Tracks per-band/per-strategy/per-asset PnL + tier migration +
    drawdown 7d worst-case.

    `start_balance_cents` defaults to `hwm_init_cents` for backwards-compat
    with old callers, but new callers should pass the actual start-of-window
    balance (from `reconstruct_hwm_init` 3-tuple). When the bot is in
    drawdown at audit-window start, start_balance < hwm_init.

    `gate_prob_source` selects what flows into `gate_passes`:
      - 'p_mean' (default) — pass the post-blend center recomputed from
        THIS bundle's prediction. Right choice for the v2 challenger
        path and for any base run where the bundle differs from
        production at decision time.
      - 'stored_calibrated_prob' — pass `row['calibrated_prob']` (the
        exact value production used at decision time per bot.py:13675/
        13702/13739/13832/13949). Right choice for the BASE replay where
        the bundle == production's bundle; this is what makes sim_pnl's
        BASE-run aggregate match production within tolerance. Falls back
        to `p_mean` when the row's calibrated_prob is NaN/None (legacy
        rows pre cal_mlp annotation).
    See kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H4.
    """
    if start_balance_cents is None:
        start_balance_cents = hwm_init_cents
    if gate_prob_source not in _VALID_GATE_PROB_SOURCES:
        raise ValueError(
            f"gate_prob_source={gate_prob_source!r} not in "
            f"{_VALID_GATE_PROB_SOURCES}"
        )
    df = df.sort_values(['evaluation_time', 'ticker']).reset_index(drop=True)
    pnl_per_strategy = defaultdict(int)
    pnl_per_band = defaultdict(int)
    pnl_per_asset = defaultdict(int)
    pnl_pessimistic_total = 0
    pnl_modeled_total = 0
    # R4 MINOR #1: removed orphaned `tier_counts_pre`. R2's pre/post
    # symmetry refactor switched all consumers to `tier_counts_post`;
    # the pre dict was retained-but-unused. Removing avoids a future
    # maintainer reintroducing the asymmetric denominator that R1+R2
    # fixed.
    tier_counts_post = defaultdict(lambda: defaultdict(int))
    daily_pnl = defaultdict(int)
    cumulative = 0
    hwm = max(hwm_init_cents, 0)
    worst_7d = 0
    rolling_window: deque = deque()

    for _, row in df.iterrows():
        if pd.isna(row['evaluation_time']):
            continue
        row_features = {
            'price_tier': int(row['price_tier']),
            'stc_bucket': int(row['stc_bucket']),
            'vol_regime': int(row['vol_regime_int']),  # R4#C1: was reading source string
        }
        result = predict_with_interval(
            float(row['p_pred']), float(row['p_std']),
            conformal_artifact, row_features,
            int(row['entry_price_cents']), str(row['side']),
            market_blend_w, mode='inference',
        )
        p_mean, p_std, final_lo, final_hi = result
        if final_lo is None:
            continue
        breakeven = market_implied_prob_yes(int(row['entry_price_cents']), str(row['side']))
        min_edge_frac = min_edge_for_price(int(row['entry_price_cents']))
        # NaN-safe is_weekend / hour_of_day_utc.
        wknd_val = row.get('is_weekend')
        if pd.notna(wknd_val):
            is_weekend_b = bool(wknd_val)
        else:
            try:
                _ts_d = pd.Timestamp(row['evaluation_time'])
                is_weekend_b = _ts_d.weekday() >= 5
            except Exception:
                is_weekend_b = False
        hr_val = row.get('hour_of_day_utc')
        if pd.notna(hr_val):
            hour_i = int(hr_val)
        else:
            try:
                _ts_h = pd.Timestamp(row['evaluation_time'])
                hour_i = int(_ts_h.hour)
            except Exception:
                hour_i = 12
        # R-p6-impl-4#C2/#C4: fee-adjusted edge w/ taker fee unconditionally
        # at the gate (bot.py:12104 + models.py:1053 use taker for tier).
        fee_1c_taker = calculate_taker_fee(1, int(row['entry_price_cents']))
        fee_frac_taker = fee_1c_taker / 100.0
        # H4: select gate input per gate_prob_source. Defaults to p_mean
        # (this-bundle re-computed post-blend); BASE replay can opt into
        # row['calibrated_prob'] (exact production decision value) with
        # NaN-safe fallback.
        if gate_prob_source == 'stored_calibrated_prob':
            stored_cal = row.get('calibrated_prob')
            if pd.notna(stored_cal):
                gate_prob = float(stored_cal)
            else:
                gate_prob = float(p_mean)
        else:
            gate_prob = float(p_mean)
        if not gate_passes(
            gate_prob, breakeven, min_edge_frac,
            is_weekend_b, hour_i,
            int(row['entry_price_cents']),
            float(row['seconds_to_close']),
            fee_frac_taker,
        ):
            continue
        # STC_EXTENDED 300-600s zone per-asset floor. R5 MAJOR #1:
        # rescue compares buffer_pct, not edge_frac. Pre-fix sim_pnl
        # used edge_frac vs threshold=0.25 → always rejected; bot.py
        # admits when buf_pct >= 0.25%. Pull spot/threshold (added to
        # SQL in H7 for the TM thin-buffer cap) and reuse here.
        _spot_pre = row.get('spot_price')
        _thr_pre = row.get('threshold')
        if (pd.notna(_spot_pre) and pd.notna(_thr_pre)
                and float(_thr_pre) > 0):
            _buf_pct_pre = (float(_spot_pre) - float(_thr_pre)) / float(_thr_pre) * 100.0
        else:
            _buf_pct_pre = None
        if not stc_extended_floor_passes(
            asset, int(row['entry_price_cents']),
            float(row['seconds_to_close']), _buf_pct_pre,
        ):
            continue
        # HIGH_PRICE_STC_BLOCK gate (only when block_enabled).
        if block_enabled and not high_price_stc_block_passes(
            int(row['entry_price_cents']),
            float(row['seconds_to_close']),
            asset, str(row.get('strategy', '')),
            str(row['side']),
        ):
            continue
        # Sizing — taker fee always for tier (matches bot.py / models.py:1053).
        # H7: dispatch through _strategy_size so terminal_momentum_*,
        # decided_t*, and weekend_discount honor their bot.py-specific
        # paths. Default fall-through is compute_size.
        edge_frac = float(p_mean) - breakeven - fee_frac_taker
        _spot_val = row.get('spot_price')
        _thr_val = row.get('threshold')
        # R3 MAJOR #1: NaN-safe balance coercion. `np.nan or 100000`
        # evaluates to NaN (NaN is truthy in Python's bool semantics),
        # then `int(NaN)` raises ValueError. The May 2-6 window has no
        # NULL-balance rows but wider backtests do; failing-fast here
        # lets the operator notice rather than crashing mid-replay.
        _bal_raw = row.get('available_balance_cents')
        _bal_cents = (int(_bal_raw)
                      if pd.notna(_bal_raw) and _bal_raw and int(_bal_raw) > 0
                      else 100000)
        # R3 MINOR #5: NaN-safe strategy coercion. Same `or` pitfall
        # — `np.nan or '_unknown'` returns NaN, then `str(NaN)` =
        # 'nan' which would corrupt the per_strategy_pnl key. Use
        # pd.notna explicitly.
        _strat_val = row.get('strategy')
        _strat_str = (str(_strat_val) if pd.notna(_strat_val) else None)
        sizing = _strategy_size(
            strategy=_strat_str,
            fee_adjusted_edge_frac=edge_frac,
            available_balance_cents=_bal_cents,
            entry_price_cents=int(row['entry_price_cents']),
            current_balance_cents=cumulative + start_balance_cents,
            hwm_cents=hwm,
            seconds_to_close=float(row['seconds_to_close']),
            asset=asset,
            spot_price=(float(_spot_val) if pd.notna(_spot_val) else None),
            threshold=(float(_thr_val) if pd.notna(_thr_val) else None),
        )
        if sizing.contract_count <= 0:
            continue
        is_taker = _strategy_uses_taker(_strat_str if _strat_str is not None else '')
        pnl = trade_pnl_cents(
            sizing.contract_count, int(row['entry_price_cents']),
            str(row['side']), str(row['market_result']), is_taker,
        )
        pnl_pessimistic_total += pnl
        pnl_modeled_total += pnl
        cumulative += pnl
        if cumulative + start_balance_cents > hwm:
            hwm = cumulative + start_balance_cents
        strategy = _strat_str if _strat_str is not None else '_unknown'
        band = '<0.85'
        for (lo, hi), name in zip(
            [(0, 0.85), (0.85, 0.92), (0.92, 0.96), (0.96, 1.0)],
            ['<0.85', '0.85-0.92', '0.92-0.96', '0.96+'],
        ):
            if hi == 1.0 and lo <= float(row['p_pred']) <= 1.0:
                band = name; break
            if lo <= float(row['p_pred']) < hi:
                band = name; break
        pnl_per_strategy[strategy] += pnl
        pnl_per_band[band] += pnl
        pnl_per_asset[asset] += pnl
        # Tier migration tracking — pre-tier from row's stored fee_adjusted_edge
        # (already in fractions per bot.py:11971).
        post_tier = sizing.tier_idx
        pre_edge_frac = float(row.get('fee_adjusted_edge') or 0)
        pre_tier = -1
        for i, (floor, _r) in enumerate(SIZING_TIERS):
            if pre_edge_frac >= floor:
                pre_tier = i
                break
        if pre_tier >= 0:
            tier_counts_post[pre_tier][post_tier] += 1
        # Daily PnL — explicit UTC bucketing.
        eval_ts = pd.Timestamp(row['evaluation_time'])
        if eval_ts.tzinfo is None:
            eval_ts_utc = eval_ts.tz_localize('UTC')
        else:
            eval_ts_utc = eval_ts.tz_convert('UTC')
        day = eval_ts_utc.date().isoformat()
        daily_pnl[day] += pnl
        # 7-day rolling drawdown — deque popleft for O(n) total.
        rolling_window.append((eval_ts_utc, cumulative))
        cutoff = eval_ts_utc - pd.Timedelta(days=7)
        while rolling_window and rolling_window[0][0] < cutoff:
            rolling_window.popleft()
        if rolling_window:
            window_max = max(c for _, c in rolling_window)
            window_curr = rolling_window[-1][1]
            drawdown = window_curr - window_max
            if drawdown < worst_7d:
                worst_7d = drawdown

    n_tiers = len(SIZING_TIERS)
    matrix = [[0] * n_tiers for _ in range(n_tiers)]
    for pre, post_counts in tier_counts_post.items():
        if 0 <= pre < n_tiers:
            for post, c in post_counts.items():
                if 0 <= post < n_tiers:
                    matrix[pre][post] += c
    # Adversarial round-1+2: pre and post cohorts must be symmetric
    # (apples-to-apples). H7 dispatch sets tier_idx=-1 for TM/DC
    # strategies that bypass the SIZING_TIERS Kelly ladder. Round 1
    # filtered post=-1 from numerator only → biased denominator;
    # Round 2 (this fix): filter from BOTH AND restrict pre cohort to
    # rows where post also stayed on the Kelly ladder so
    # `pre_weighted_avg_risk` and `post_weighted_avg_risk` measure the
    # same set of trades. TM/DC bypass count surfaced as
    # `n_tm_dc_bypass` separately so the audit consumer sees the
    # bypassed share.
    pre_kelly_ladder_risk = sum(
        sum(SIZING_TIER_RISK_FRACTIONS[pre] * cnt
            for post, cnt in counts.items() if 0 <= post < n_tiers)
        for pre, counts in tier_counts_post.items()
        if 0 <= pre < n_tiers
    )
    # R5 MINOR #1: belt-and-suspenders against future regression — the
    # populate loop only writes pre>=0 keys, but explicitly filtering
    # the outer loop too means a future maintainer adding pre=-1 keys
    # (e.g. to track TM/DC pre-bypass routing) won't silently
    # reintroduce the asymmetric-denominator bug R1+R2 fixed.
    post_total_risk = sum(
        sum(SIZING_TIER_RISK_FRACTIONS[post] * cnt
            for post, cnt in counts.items() if 0 <= post < n_tiers)
        for pre, counts in tier_counts_post.items()
        if 0 <= pre < n_tiers
    )
    n_kelly_ladder = sum(
        sum(cnt for post, cnt in counts.items() if 0 <= post < n_tiers)
        for pre, counts in tier_counts_post.items()
        if 0 <= pre < n_tiers
    )
    n_tm_dc_bypass = sum(
        sum(cnt for post, cnt in counts.items() if post == -1)
        for pre, counts in tier_counts_post.items()
        if 0 <= pre < n_tiers
    )
    n_total_pre = n_kelly_ladder or 1
    n_total_post = n_kelly_ladder or 1
    pre_weighted = pre_kelly_ladder_risk / n_total_pre
    post_weighted = post_total_risk / n_total_post
    drop_pct = (pre_weighted - post_weighted) / pre_weighted if pre_weighted > 0 else 0.0

    return {
        'total_pessimistic_30d': pnl_pessimistic_total / 100.0,
        'total_modeled_30d': pnl_modeled_total / 100.0,
        'per_asset_pnl_30d': {k: v / 100.0 for k, v in pnl_per_asset.items()},
        'per_band_pnl_30d': {k: v / 100.0 for k, v in pnl_per_band.items()},
        'per_strategy_pnl_30d': {k: v / 100.0 for k, v in pnl_per_strategy.items()},
        'tier_migration': {
            'tiers': [list(t) for t in SIZING_TIERS],
            'risk_fractions': SIZING_TIER_RISK_FRACTIONS,
            'counts': matrix,
            'pre_weighted_avg_risk': pre_weighted,
            'post_weighted_avg_risk': post_weighted,
            'drop_pct': drop_pct,
            # Adversarial round 2 MAJOR #2: pre/post avgs are restricted
            # to the Kelly-ladder cohort (rows where pre>=0 AND
            # 0<=post<n_tiers). TM/DC dispatch rows (post=-1) bypass the
            # ladder entirely; surface their count separately so audit
            # consumers see how much of the universe routed through H7.
            # n_kelly_ladder + n_tm_dc_bypass equals the count of sized
            # rows whose pre-tier was on the Kelly ladder. R3 MINOR #2:
            # weekend_discount fallback rows where pre_tier=-1 (edge
            # below all SIZING_TIERS, fixed-7%-fallback fires) are NOT
            # in either count — they sit in `per_strategy_pnl_30d`'s
            # 'weekend_discount' bucket but aren't tracked by
            # `tier_counts_post` (which only writes pre>=0 keys). If you
            # need full sized-row reconciliation, sum
            # `per_strategy_pnl_30d.values()` against PnL totals; the
            # tier_migration counters are Kelly-ladder-cohort only.
            'n_kelly_ladder': int(n_kelly_ladder),
            'n_tm_dc_bypass': int(n_tm_dc_bypass),
        },
        'worst_7d_drawdown_cents': worst_7d,
        'worst_7d_drawdown_prod_cents': worst_7d,
        'daily_pnl': dict(daily_pnl),
        'daily_pnl_count': len(daily_pnl),
    }
