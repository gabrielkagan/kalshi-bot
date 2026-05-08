"""Bit 3.2: Derived feature helpers (Tier 5), extracted from bot/_impl.py."""
import math
from typing import Dict, Optional

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
