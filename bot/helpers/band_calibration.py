"""P4.1: Band-calibrated probability lookup for 15M Kelly sizing.

The bot's live decision blend (`final_prob` in `bot/scanner/__init__.py`,
stored as `live_prob` / `a1_raw_prob` in `fifteenm_shadow_signals`) is
miscalibrated across price bands per the 2026-05-17 30d shadow audit:
underconfident at 94-99c (says ~0.85, realized ~0.95-1.00), overconfident
at 70-89c (says ~0.80, realized ~0.73-0.85). Plugging that probability
straight into `PositionSizer.compute()` (which uses it in the Kelly
formula) means positions get sized 5-10x too large where calibration is
worst — empirically the May-14 -$187 loss at 86-89c.

This module exposes `calibrated_prob_for_sizing(asset, market_price_cents,
raw_prob, product_type=None)` which looks up a band-stratified empirical
realized rate (hierarchical-pooled toward a band-aggregate prior with
shrinkage k=30) and returns it for use in Kelly. Trade-selection gates
(should-we-trade?) continue to use raw_prob unchanged. Only sizing
(how-big?) changes.

Scope: 15M YES-side only. Other product types (hourly, SPX, weather,
sports) and the V2 / NO-side sizing paths are out of scope; the helper
returns raw_prob unchanged for non-15M product types.

Baseline snapshot: 2026-05-17 09:35 UTC, sourced from
state.db.fifteenm_shadow_signals on VPS HEAD=e3aecd4. Hybrid lookback
(operator decision): 30d for bands 70-93c (regime-sensitive); 60d for
bands 94-100c (thin-cell stability). Refresh recipe in
agent_docs/p4_1_calibration_baseline.md.

Sister anchors:
  - bot/scanner/__init__.py (wire-in at 10 `_sizer.compute(final_prob,...)` sites)
  - tests/contracts/test_p4_1_band_calibrated_sizing.py (contract pins)
  - kb/decisions/money-printer-roadmap-may17.md (audit + scope)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ─── Calibration metadata ────────────────────────────────────────────────────

SNAPSHOT_DATE: str = "2026-05-17"
SHRINKAGE_K: int = 30

# Inclusive integer cent bounds per band. `99` covers 99 and 100c (100c
# appears in fifteenm_shadow_signals when ask is at the cap).
BAND_BOUNDS: List[Tuple[str, int, int]] = [
    ("70-79", 70, 79),
    ("80-85", 80, 85),
    ("86-89", 86, 89),
    ("90-93", 90, 93),
    ("94-96", 94, 96),
    ("97-98", 97, 98),
    ("99",    99, 100),
]

# Per-band lookback window applied when the baseline was captured.
# (Documented for transparency; not consumed at runtime since shrunk values
# are pre-computed below.)
BAND_WINDOW_DAYS: Dict[str, int] = {
    "70-79": 30, "80-85": 30, "86-89": 30, "90-93": 30,
    "94-96": 60, "97-98": 60, "99": 60,
}

# Band-aggregate realized rates (pooled across all 6 assets within each
# band's own lookback window). Used as the shrinkage prior. Pinned at 6dp
# so the (raw -> shrunk) derivation rounds consistently to 4dp.
BAND_PRIORS: Dict[str, float] = {
    "70-79": 0.728845,
    "80-85": 0.803787,
    "86-89": 0.850467,
    "90-93": 0.889952,
    "94-96": 0.961538,
    "97-98": 0.971831,
    "99":    1.000000,
}

# Per-(asset, band) raw realized rates and sample sizes from the 2026-05-17
# baseline. `n` is the count of settled rows in the cell's lookback window
# (30d or 60d per BAND_WINDOW_DAYS); `raw_p` is mean(market_result == 'yes')
# pinned at 6dp. These are the SOURCE of truth — the shrunk lookup below
# is derived deterministically.
_RAW_ASSET_BAND_REALIZED: Dict[Tuple[str, str], Tuple[int, float]] = {
    ("BTC",  "70-79"): (293, 0.750853),
    ("BTC",  "80-85"): ( 81, 0.802469),
    ("BTC",  "86-89"): ( 28, 0.857143),
    ("BTC",  "90-93"): ( 36, 0.805556),
    ("BTC",  "94-96"): ( 48, 0.937500),
    ("BTC",  "97-98"): ( 32, 0.968750),
    ("BTC",  "99"   ): ( 64, 1.000000),
    ("ETH",  "70-79"): (513, 0.742690),
    ("ETH",  "80-85"): (239, 0.811715),
    ("ETH",  "86-89"): ( 91, 0.868132),
    ("ETH",  "90-93"): ( 98, 0.948980),
    ("ETH",  "94-96"): ( 51, 0.960784),
    ("ETH",  "97-98"): ( 43, 1.000000),
    ("ETH",  "99"   ): ( 83, 1.000000),
    ("SOL",  "70-79"): (414, 0.734300),
    ("SOL",  "80-85"): ( 69, 0.768116),
    ("SOL",  "86-89"): ( 28, 0.857143),
    ("SOL",  "90-93"): ( 20, 0.800000),
    ("SOL",  "94-96"): ( 12, 1.000000),
    ("SOL",  "97-98"): ( 21, 1.000000),
    ("SOL",  "99"   ): ( 29, 1.000000),
    ("XRP",  "70-79"): (599, 0.734558),
    ("XRP",  "80-85"): (126, 0.841270),
    ("XRP",  "86-89"): ( 33, 0.878788),
    ("XRP",  "90-93"): ( 37, 0.864865),
    ("XRP",  "94-96"): ( 32, 0.968750),
    ("XRP",  "97-98"): ( 35, 0.914286),
    ("XRP",  "99"   ): ( 29, 1.000000),
    ("HYPE", "70-79"): (178, 0.646067),
    ("HYPE", "80-85"): ( 31, 0.645161),
    ("HYPE", "86-89"): ( 10, 0.800000),
    ("HYPE", "90-93"): (  9, 0.888889),
    ("HYPE", "94-96"): (  4, 1.000000),
    ("HYPE", "97-98"): (  2, 1.000000),
    ("HYPE", "99"   ): (  6, 1.000000),
    ("DOGE", "70-79"): (142, 0.697183),
    ("DOGE", "80-85"): ( 35, 0.828571),
    ("DOGE", "86-89"): ( 24, 0.750000),
    ("DOGE", "90-93"): (  9, 0.888889),
    ("DOGE", "94-96"): (  9, 1.000000),
    ("DOGE", "97-98"): (  9, 1.000000),
    ("DOGE", "99"   ): (  7, 1.000000),
}


def _shrink(cell: Tuple[str, str]) -> float:
    """Hierarchical shrinkage: pull per-cell rate toward band prior.

    shrunk = (n_cell * raw_p_cell + k * band_prior) / (n_cell + k)

    For k=30: a cell with n=300 keeps 91% of its raw signal; n=30 splits
    50/50 with the prior; n=10 is 75% prior, 25% cell. Cells with no data
    (absent from _RAW_ASSET_BAND_REALIZED) return the prior directly.
    """
    band = cell[1]
    prior = BAND_PRIORS[band]
    raw = _RAW_ASSET_BAND_REALIZED.get(cell)
    if raw is None:
        return prior
    n, p = raw
    if n <= 0:
        return prior
    return (n * p + SHRINKAGE_K * prior) / (n + SHRINKAGE_K)


# Pre-computed shrunk realized rate per (asset, band). Pinned at 4dp by
# tests/contracts/test_p4_1_band_calibrated_sizing.py.
CALIBRATED_PROB_BY_BAND: Dict[Tuple[str, str], float] = {
    cell: round(_shrink(cell), 4) for cell in _RAW_ASSET_BAND_REALIZED
}


# ─── Helper ──────────────────────────────────────────────────────────────────

_KNOWN_15M_PRODUCT_TYPES = frozenset({None, "15m"})

# One-time-warning cache: (asset, band) tuples seen with missing cells.
_warned_missing_cells: set = set()

# Operator runbook escape hatch: any (asset, band) added here is treated as
# "calibration disabled" — the helper short-circuits to `raw_prob` for that
# cell, leaving the band-calibrated wire-in unchanged elsewhere. Use during
# the 14d soak when a cell's realized rate drifts > 5pp from baseline.
# Tracked by the contract test as a NORMALLY-EMPTY set; if a cell is added
# here, also file the per-cell rollback note in the soak-monitoring ticket.
BAND_CALIBRATION_DISABLED_CELLS: set = set()

# Wholesale kill switch (ticket 86b9znd21, flipped 2026-05-19): when True the
# helper short-circuits to `raw_prob` for ALL inputs unconditionally, which
# reverts 15M Kelly sizing to pre-P4.1 behavior without unwiring the 10 call
# sites. Flip back to False to re-engage band calibration once soak data
# supports it. Rationale: 2d post-ship corrected-PnL for wrapped strategies
# (`main` n=8 -$24, `decided` n=59 -$14) plus the 2026-05-17 rolling-backtest
# conclusion ("all approaches lose; signal noise") argues against keeping
# the wrap engaged through the original 2026-05-31 soak window. Cell-level
# escape via `BAND_CALIBRATION_DISABLED_CELLS` remains.
BAND_CALIBRATION_KILL_SWITCH: bool = True


def _reset_warning_cache_for_tests() -> None:
    """Test seam: clear the one-time-warning cache between caplog runs."""
    _warned_missing_cells.clear()


def _classify_band(price_cents: int) -> Optional[str]:
    """Return the band label for a given integer price in cents, or None
    if the price is outside the 70-100c calibrated range."""
    for label, lo, hi in BAND_BOUNDS:
        if lo <= price_cents <= hi:
            return label
    return None


def calibrated_prob_for_sizing(
    asset: str,
    market_price_cents: int,
    raw_prob: float,
    product_type: Optional[str] = None,
) -> float:
    """Return the band-calibrated probability for Kelly sizing.

    Args:
      asset: One of {BTC, ETH, SOL, XRP, HYPE, DOGE} for in-table lookup;
        any other value falls back to `raw_prob` with a one-time warning.
      market_price_cents: Integer cent price of the contract being sized
        (typically `best_ask`).
      raw_prob: The caller's existing probability (live blend `final_prob`).
        Used as a fallback for non-15M product types, out-of-range prices,
        and missing-cell asset codes.
      product_type: Sizing context. Helper returns `raw_prob` unchanged
        unless this is None or "15m" — P4.1 is 15M-only; hourly / SPX /
        weather / sports product types have separate calibration domains
        and are NOT touched by this helper. (Note: "crypto_15m" is a
        CapitalAllocator strategy-key, never a `window["product_type"]`
        value, so it is intentionally absent from the known-15M set —
        R1 M2 retraction; see `_KNOWN_15M_PRODUCT_TYPES`.)

    Returns:
      A probability in [0.0, 1.0] for use in `PositionSizer.compute()`.

    Notes:
      Trade-selection gates upstream of sizing continue to use `raw_prob`.
      This helper only changes the Kelly *magnitude*, not the
      should-we-trade decision.
    """
    if BAND_CALIBRATION_KILL_SWITCH:
        return raw_prob
    if product_type not in _KNOWN_15M_PRODUCT_TYPES:
        return raw_prob
    band = _classify_band(market_price_cents)
    if band is None:
        return raw_prob
    cell = (asset, band)
    if cell in BAND_CALIBRATION_DISABLED_CELLS:
        return raw_prob
    calibrated = CALIBRATED_PROB_BY_BAND.get(cell)
    if calibrated is None:
        if cell not in _warned_missing_cells:
            _warned_missing_cells.add(cell)
            log.warning(
                "band_calibration: missing cell (asset=%s, band=%s) — "
                "falling back to raw_prob=%.4f. Add cell to "
                "_RAW_ASSET_BAND_REALIZED in bot/helpers/band_calibration.py "
                "(re-derivation recipe in agent_docs/p4_1_calibration_baseline.md).",
                asset, band, raw_prob,
            )
        return raw_prob
    return calibrated
