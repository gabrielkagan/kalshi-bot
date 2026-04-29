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
from sizing import compute_size, SIZING_TIERS, SIZING_TIER_RISK_FRACTIONS

# R-p6-impl-2#C2/C11: import fees from models.py (pure-math module — no
# bot.py side effects). models.calculate_taker_fee / calculate_maker_fee
# are the authoritative implementations bot.py itself calls.
from models import calculate_taker_fee, calculate_maker_fee  # noqa: E402


# R-p6-impl-2#C4 — bot.py STRATEGY_CLAMP_POLICY (1583-1623) + MAKER_PATIENT
# is the ONLY strategy that posts and waits for maker fill in 15M flow. All
# other strategies cross the spread. Default: taker.
MAKER_ONLY_STRATEGIES = frozenset({'MAKER_PATIENT'})


def _strategy_uses_taker(strategy: str) -> bool:
    if strategy is None:
        return True
    return strategy not in MAKER_ONLY_STRATEGIES


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
    final_lo: float,
    breakeven: float,
    min_edge_frac: float,
    is_weekend: bool,
    hour_of_day_utc: int,
    entry_price_cents: int,
    seconds_to_close: float,
    fee_frac: float,
) -> bool:
    """A31 gate with bot-faithful discount fallbacks.

    R-p6-impl-4#C1/C2: bot.py runs the regular gate FIRST (bot.py:12104).
    Only on insufficient_edge rejection does it fall through to weekend
    (bot.py:12430) then overnight (bot.py:12606) discount paths. Fee
    subtraction is applied to the edge (bot.py uses fee_adjusted_edge for
    every comparison)."""
    fee_adj_edge = (final_lo - breakeven) - fee_frac
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


# Per-asset minimum entry price floors mirrored from bot.py:219-225.
PER_ASSET_MIN_ENTRY_PRICE = {
    'BTC': 88,
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
    edge_frac: float,
) -> bool:
    """bot.py:14511-14520: in 300-600s STC zone, per-asset floor applies
    UNLESS edge_frac >= STC_EXTENDED_BUFFER_RESCUE."""
    if seconds_to_close <= STC_EXTENDED_LIVE_FLOOR:
        return True
    if seconds_to_close > 600:
        return True
    floor = STC_EXTENDED_PER_ASSET_FLOOR.get(asset, 100)
    if entry_price_cents >= floor:
        return True
    return edge_frac >= STC_EXTENDED_BUFFER_RESCUE


def reconstruct_hwm_init(
    db_path: str,
    test_start_ts: pd.Timestamp,
) -> tuple[int, str]:
    """Returns (hwm_cents, source). Source ∈ {'balance_walked',
    'forward_only_from_now'}.

    R-p6-impl-2#C5/#C6: `audit_snapshots` table doesn't exist in bot.py
    schema and `positions` has `total_cost_cents`. Reconstructable signal
    is `evaluated_opportunities.available_balance_cents` per-scan. Take
    MAX over rows BEFORE test_start_ts as the HWM init."""
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        ts_iso = _iso_z(test_start_ts)
        try:
            row = conn.execute(
                "SELECT MAX(available_balance_cents) FROM evaluated_opportunities "
                "WHERE evaluation_time < ? AND available_balance_cents IS NOT NULL",
                (ts_iso,),
            ).fetchone()
            hwm = int(row[0] or 0) if row else 0
            if hwm > 0:
                return (hwm, 'balance_walked')
        except sqlite3.OperationalError:
            pass
        try:
            row = conn.execute(
                "SELECT available_balance_cents FROM evaluated_opportunities "
                "WHERE available_balance_cents IS NOT NULL "
                "ORDER BY evaluation_time DESC LIMIT 1"
            ).fetchone()
            current = int(row[0] or 0) if row else 0
            return (current, 'forward_only_from_now')
        except sqlite3.OperationalError:
            return (0, 'forward_only_from_now')
    finally:
        conn.close()


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
                   market_result, available_balance_cents
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
    n_universe = len(candidate_df)
    unsettled_drop_rate = (n_unsettled / max(1, n_total))

    candidate_df = candidate_df.rename(columns={
        'market_price': 'entry_price_cents',
        'yes_spread_cents': 'spread_cents',
    })
    # R-p7-deploy-r4#H1: features.py:21 documents `right=True` as the
    # canonical convention. bot.py + integration.py (Edit 4) and Phase 2
    # extract_data all use right=True. sim_pnl was using right=False —
    # off-by-one binning that silently misaligned counterfactual cells
    # against the production calibrator. Now matches.
    PRICE_BIN_CUTOFFS = [80, 90, 96]
    STC_BIN_CUTOFFS = [120, 300, 600]
    candidate_df['price_tier'] = np.digitize(
        candidate_df['entry_price_cents'].astype(float).to_numpy(),
        PRICE_BIN_CUTOFFS, right=True,
    ).astype(np.int64)
    candidate_df['stc_bucket'] = np.digitize(
        candidate_df['seconds_to_close'].astype(float).to_numpy(),
        STC_BIN_CUTOFFS, right=True,
    ).astype(np.int64)
    candidate_df['vol_regime_int'] = (
        candidate_df['vol_regime'].astype(str) == 'elevated'
    ).astype(np.int64)
    # R3#C2: derive Phase 4-required columns for the model forward pass.
    candidate_df['side_int'] = (candidate_df['side'].astype(str) == 'yes').astype(np.int64)
    # R-p6-impl-2#C6: fall back to raw_prob when calibrated_prob is NULL.
    cal_series = pd.to_numeric(candidate_df['calibrated_prob'], errors='coerce')
    raw_series = pd.to_numeric(candidate_df['raw_prob'], errors='coerce')
    candidate_df['method_output'] = cal_series.fillna(raw_series).astype(np.float32)
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

    hwm_init_cents, hwm_source = reconstruct_hwm_init(
        db_path, pd.Timestamp(test_start),
    )

    results = {}
    for block_label, block_enabled in [('block_off', False), ('block_on', True)]:
        results[block_label] = _replay_one_path(
            candidate_df, conformal_artifact, market_blend_w,
            asset, block_enabled, hwm_init_cents,
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
        'excluded_null_market_price': excluded_null_market_price,
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


def _replay_one_path(
    df: pd.DataFrame,
    conformal_artifact: dict,
    market_blend_w: float,
    asset: str,
    block_enabled: bool,
    hwm_init_cents: int,
) -> dict:
    """Walk forward through candidates in evaluation_time order, replaying
    the gate. Tracks per-band/per-strategy/per-asset PnL + tier migration +
    drawdown 7d worst-case."""
    df = df.sort_values(['evaluation_time', 'ticker']).reset_index(drop=True)
    pnl_per_strategy = defaultdict(int)
    pnl_per_band = defaultdict(int)
    pnl_per_asset = defaultdict(int)
    pnl_pessimistic_total = 0
    pnl_modeled_total = 0
    tier_counts_pre = defaultdict(int)
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
        if not gate_passes(
            final_lo, breakeven, min_edge_frac,
            is_weekend_b, hour_i,
            int(row['entry_price_cents']),
            float(row['seconds_to_close']),
            fee_frac_taker,
        ):
            continue
        # STC_EXTENDED 300-600s zone per-asset floor.
        approx_edge_frac = float(p_mean) - breakeven
        if not stc_extended_floor_passes(
            asset, int(row['entry_price_cents']),
            float(row['seconds_to_close']), approx_edge_frac,
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
        edge_frac = float(p_mean) - breakeven - fee_frac_taker
        sizing = compute_size(
            edge_frac,
            int(row.get('available_balance_cents') or 100000),
            int(row['entry_price_cents']),
            current_balance_cents=cumulative + hwm_init_cents,
            hwm_cents=hwm,
            seconds_to_close=float(row['seconds_to_close']),
            asset=asset,
        )
        if sizing.contract_count <= 0:
            continue
        is_taker = _strategy_uses_taker(str(row.get('strategy', '')))
        pnl = trade_pnl_cents(
            sizing.contract_count, int(row['entry_price_cents']),
            str(row['side']), str(row['market_result']), is_taker,
        )
        pnl_pessimistic_total += pnl
        pnl_modeled_total += pnl
        cumulative += pnl
        if cumulative + hwm_init_cents > hwm:
            hwm = cumulative + hwm_init_cents
        strategy = str(row.get('strategy') or '_unknown')
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
            tier_counts_pre[pre_tier] += 1
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
    pre_total_risk = sum(
        SIZING_TIER_RISK_FRACTIONS[i] * tier_counts_pre.get(i, 0)
        for i in range(n_tiers)
    )
    post_total_risk = sum(
        sum(SIZING_TIER_RISK_FRACTIONS[post] * cnt
            for post, cnt in counts.items() if 0 <= post < n_tiers)
        for counts in tier_counts_post.values()
    )
    n_total_pre = sum(tier_counts_pre.values()) or 1
    n_total_post = sum(sum(c.values()) for c in tier_counts_post.values()) or 1
    pre_weighted = pre_total_risk / n_total_pre
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
        },
        'worst_7d_drawdown_cents': worst_7d,
        'worst_7d_drawdown_prod_cents': worst_7d,
        'daily_pnl': dict(daily_pnl),
        'daily_pnl_count': len(daily_pnl),
    }
