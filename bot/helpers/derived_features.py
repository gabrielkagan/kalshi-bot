"""Bit 3.2: Derived feature helpers (Tier 5), extracted from bot/_impl.py.

Sprint B Bit B.1a (2026-05-12) added `apply_sigma_winsor`,
`SIGMA_WINSOR_ABS_CAP`, and `compute_hour_sin_cos`. Sprint A Bit A.1b
(2026-05-12, ticket `86b9veppa`) routed all tracked cal_mlp sites through
canonical helpers (closing the inline-drift surface). Lock-step surface
(mirrors bot/CLAUDE.md "cal_mlp feature transforms (lock-step)"):

  Drift sites (4 — pinned by HOUR_SINCOS_DRIFT_SITES in test_calmlp_lockstep.py):
    scripts/cal_mlp/extract_data.py        (calls features.compute_hour_features)
    scripts/cal_mlp/post_hoc_processor.py  (calls features.compute_hour_features)
    scripts/cal_mlp/integration.py         (calls features.compute_hour_features + compute_derived_features + apply_sigma_winsor)
    scripts/cal_mlp/sim_pnl.py             (calls features.compute_hour_features; sister site)
  Helper home (not a drift site):
    scripts/cal_mlp/features.py            (SIGMA_WINSOR_ABS_CAP + apply_sigma_winsor + compute_hour_features homes)

Any change to a constant or formula MUST update all surfaces in
lock-step in ONE commit (bot/CLAUDE.md "cal_mlp feature transforms
(lock-step)"). The mirrored bot.helpers copy keeps the bot package off
the scripts/cal_mlp import path (which transitively loads torch/numpy
heavyweight deps); numeric equivalence is pinned by
tests/contracts/test_calmlp_lockstep.py (AST + helper-call seal),
tests/integration/test_sprint_b_bit_1a_rejection_enrichment.py
(numeric parity), and tests/integration/test_calmlp_sigma_winsorize.py.
"""
import math
from typing import Dict, Optional, Tuple


# ── Lock-step constants (mirror scripts/cal_mlp/features.py) ──
# Cap chosen at 25 — above the empirical max benign value (~19) but well
# below the 30+ outlier tail. See features.py for full rationale.
SIGMA_WINSOR_ABS_CAP: float = 25.0


def apply_sigma_winsor(sd: Optional[float]) -> Optional[float]:
    """Clip a single spot_distance_to_strike_sigma to ±SIGMA_WINSOR_ABS_CAP.

    Mirrors scripts/cal_mlp/features.apply_sigma_winsor. Use this on
    every serve/extract-time read of `spot_distance_to_strike_sigma`
    so the train (clipped at ±25) and serve (DB read) distributions
    stay identical. Train/serve skew here translates directly into
    miscalibrated predictions.

    Returns:
      - None if input is None (NULL passthrough for missing-indicator path)
      - clipped value otherwise

    NaN-safe: NaN compared with `>` returns False, so NaN passes through
    unchanged (downstream NULL-imputation handles it).
    """
    if sd is None:
        return None
    cap = SIGMA_WINSOR_ABS_CAP
    if sd > cap:
        return cap
    if sd < -cap:
        return -cap
    return sd


def compute_hour_sin_cos(
    hour_of_day_utc: Optional[int],
) -> Tuple[Optional[float], Optional[float]]:
    """Cyclic 24h embedding of `hour_of_day_utc`. Scalar canonical helper.

    Lock-step partner of `scripts/cal_mlp/features.compute_hour_features`
    (A.1b 2026-05-12) which wraps the same formula and `None` passthrough,
    and additionally accepts a numpy/pandas Series for DataFrame-side
    extract paths. Both must produce byte-identical outputs for the same
    scalar (including `None`) input.

    Returns (None, None) on None input — NULL passthrough so consumers
    see honest missing values rather than a synthesized 0/1 point.

    The float cast is load-bearing to match cal_mlp's call sites which
    convert hour to `float(now_dt.hour)` / `float(dt.hour)` before the
    trig multiply.
    """
    if hour_of_day_utc is None:
        return (None, None)
    h = float(hour_of_day_utc)
    angle = 2.0 * math.pi * h / 24.0
    return (math.sin(angle), math.cos(angle))


def compute_derived_features(
    spot_price: Optional[float] = None,
    threshold: Optional[float] = None,
    volatility: Optional[float] = None,
    seconds_to_close: Optional[float] = None,
    calibrated_prob: Optional[float] = None,
    market_price_cents: Optional[int] = None,
    kelly_contracts: Optional[int] = None,
    sol_rescue_cap: int = 25,
    n_recent_cal_trades: Optional[int] = None,
) -> Dict[str, Optional[float]]:
    """Compute Tier 5 (derived) features from existing columns.

    - spot_distance_to_strike_sigma: buf_pct / (vol × sqrt(STC/5) × 100).
      How many σ of remaining-time vol the buffer covers. `volatility` is
      `blended_rv` — per-5-second stdev of log returns — so STC scales by
      sqrt(STC/5) not sqrt(STC), matching the sigma_move convention at
      certainty_score (spot × blended_rv × sqrt(remaining/5)).
    - prob_breakeven_gap: calibrated_prob − market_price/100. Model's
      conviction above breakeven.
    - kelly_vs_cap_ratio: kelly_contracts / SOL_RESCUE_CONTRACT_CAP.
      Proxy for "how aggressive Kelly wanted to be" on SOL in rescue zone.
    - calibration_confidence: n_recent_cal_trades / 100 (capped at 1.0).
      How trained the active CalEngine is.

    All features return None on missing/invalid inputs.
    """
    sigma = None
    if (spot_price is not None and threshold is not None and threshold > 0
            and volatility is not None and volatility > 0
            and seconds_to_close is not None and seconds_to_close > 0):
        buf_pct = (spot_price - threshold) / threshold * 100
        try:
            sigma_denom = volatility * math.sqrt(seconds_to_close / 5.0) * 100
            if sigma_denom > 0:
                sigma = buf_pct / sigma_denom
        except (ValueError, ZeroDivisionError):
            pass

    gap = None
    if calibrated_prob is not None and market_price_cents is not None:
        gap = calibrated_prob - (market_price_cents / 100.0)

    ratio = None
    if kelly_contracts is not None and sol_rescue_cap > 0:
        ratio = kelly_contracts / sol_rescue_cap

    conf = None
    if n_recent_cal_trades is not None and n_recent_cal_trades >= 0:
        conf = min(n_recent_cal_trades / 100.0, 1.0)

    return {
        "spot_distance_to_strike_sigma": sigma,
        "prob_breakeven_gap": gap,
        "kelly_vs_cap_ratio": ratio,
        "calibration_confidence": conf,
    }
