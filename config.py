"""Shared constants for kalshi-bot.

Extracted from bot/_impl.py so that models.py and tests can import constants
without pulling in bot/_impl.py's full dependency tree (requests, websockets,
cryptography, etc.).

bot/_impl.py does `from config import *` so runtime behavior is unchanged.
"""

import json
import logging
import math
import os
from typing import Dict

# ─── Assets ──────────────────────────────────────────────────────────────────
ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# Module-scoped logger. Using `logging.info(...)` directly at module-load time
# auto-triggers `logging.basicConfig()` when no handler is configured yet,
# which clobbers pytest's caplog fixture handlers — silent test-suite
# regression. A NullHandler-backed module logger leaves caplog handlers
# intact (Bit 2.1a follow-up to the gate (e) caplog regression).
_log = logging.getLogger(__name__)
if not _log.handlers:
    _log.addHandler(logging.NullHandler())

# ─── Probability Engine ──────────────────────────────────────────────────────
VOL_RETURN_INTERVAL = 5           # seconds between log returns
SECONDS_PER_YEAR = 365.25 * 24 * 3600  # crypto trades 24/7
DVOL_ANNUALIZED_TO_5S = 1.0 / math.sqrt(SECONDS_PER_YEAR / VOL_RETURN_INTERVAL)
STUDENT_T_DF = 4                  # degrees of freedom for t-distribution
BETA_SLOPE = 0.85                 # logistic calibration (<1 compresses extremes)
MAX_EFFECTIVE_PROB = 0.93         # hard cap on calibrated probability (default / fallback)
NUMERICAL_SAFETY_CEILING = 0.999  # ceiling for learned calibration methods (replaces hard cap)

# ─── Per-Asset Distribution Config ──────────────────────────────────────────
DIST_CONFIG_PATH = "dist_config.json"


def _load_dist_config() -> Dict:
    """Load per-asset distribution config from dist_config.json.

    Returns dict keyed by asset name. Falls back to Student-t(df=4) if missing.
    """
    defaults = {"distribution": "student_t", "student_t_df": STUDENT_T_DF}
    config: Dict[str, Dict] = {}

    try:
        with open(DIST_CONFIG_PATH, "r") as f:
            raw = json.load(f)

        for asset_name, acfg in raw.get("assets", {}).items():
            entry = dict(defaults)
            if "distribution" in acfg:
                entry["distribution"] = acfg["distribution"]
            if "student_t_df" in acfg:
                entry["student_t_df"] = float(acfg["student_t_df"])
            if "nig_params" in acfg:
                p = acfg["nig_params"]
                entry["nig_a"] = float(p["a"])
                entry["nig_b"] = float(p["b"])
                entry["nig_loc"] = float(p.get("loc", 0.0))
                entry["nig_scale"] = float(p.get("scale", 1.0))
            config[asset_name] = entry

        _log.info(
            "Loaded dist config: %s",
            {a: f"{c['distribution']}(df={c.get('student_t_df')})" if c['distribution'] == 'student_t'
             else "nig" for a, c in config.items()}
        )
    except FileNotFoundError:
        _log.info("No %s found, using defaults (Student-t df=%d)", DIST_CONFIG_PATH, STUDENT_T_DF)
    except Exception as e:
        _log.warning("Error loading %s, using defaults: %s", DIST_CONFIG_PATH, e)

    return config


DIST_CONFIG = _load_dist_config()

# ─── EGARCH(1,1) Estimation ──────────────────────────────────────────────
EGARCH_SHADOW_MODE = False              # PROMOTED: EGARCH conditional vol feeds live EGARCH-RV blend
EGARCH_STATE_PATH = "egarch_state.json"
EGARCH_BUFFER_SAVE_INTERVAL = 300.0     # save return buffer to disk every 5 min
EGARCH_REFIT_INTERVAL = 7200            # 2h between MLE refits
EGARCH_MIN_RETURNS = 360                # 30 min of 5s returns before first MLE fit
EGARCH_RETURN_MAXLEN = 10800            # 15h of 5s returns for MLE window
EGARCH_MLE_EWL_LAMBDA = 0.99984        # Exponential weighting in MLE: ~6-hour half-life (smooths ghost features)
EGARCH_WARMUP_RETURNS = 12              # 1 min before recursive update starts
EGARCH_E_ABS_Z = 0.7978845608           # E[|z|] for z ~ N(0,1) = sqrt(2/π)
EGARCH_LOG_VAR_FLOOR = -40.0            # exp(-40) ~ 4.25e-18 (prevents underflow)
EGARCH_LOG_VAR_CEILING = -10.0          # exp(-10) ~ 4.5e-5 (prevents explosive vol)
EGARCH_MLE_MAXITER = 200                # scipy L-BFGS-B iterations
EGARCH_OMEGA_BOUNDS = (-5.0, 0.0)
EGARCH_ALPHA_BOUNDS = (0.01, 0.5)
EGARCH_GAMMA_BOUNDS = (-0.3, 0.3)       # both leverage directions
EGARCH_BETA_BOUNDS = (0.80, 0.999)      # high persistence typical for crypto
EGARCH_DF_BOUNDS = (3.0, 30.0)          # Student-t df bounds (3.0 keeps finite variance, allows heavier tails)
EGARCH_DF_DEFAULT = 5.0                 # Typical for crypto (heavy tails, Caporale & Zekokh 2019)
EGARCH_REFIT_INTERVALS = {              # Per-asset refit intervals (seconds)
    "BTC": 7200, "ETH": 7200,          # 2h for high-persistence assets
    "SOL": 3600, "XRP": 3600,          # 1h for low-persistence (faster regime changes)
}
EGARCH_GAMMA_CONSTRAINTS = {           # Per-asset gamma bounds (leverage effect)
    "BTC": (0.0, 0.0),                    # gamma insignificant (t~0.75-1.2), fix at zero
    "ETH": (-0.3, 0.3),                   # gamma significant (t~1.6-2.6)
    "SOL": (-0.3, 0.3),                   # gamma significant (t~1.9-3.0)
    "XRP": (0.0, 0.0),                    # gamma insignificant (t~1.0-1.6), fix at zero
}

# ─── EGARCH-RV Blend ─────────────────────────────────────────────────────
EGARCH_BLEND_SHADOW_MODE = False       # PROMOTED: EGARCH-RV blend drives live blended_rv
EGARCH_BLEND_STATE_PATH = "egarch_blend_state.json"

# Mincer-Zarnowitz R² tracker
MZ_WINDOW = 720                        # Rolling window: 720 ticks × 10s = 2 hours
MZ_MIN_OBS = 240                       # Need 40 min of data before R² is valid (1:3 ratio)
MZ_RECOMPUTE_INTERVAL = 30.0           # Recompute R² every 30s (not every tick)
MZ_EMA_LAMBDA = 0.97                   # EMA decay for weight smoothing (Stock & Watson 2004)
MZ_EQUAL_WEIGHT_R2_THRESHOLD = 0.10    # Below this R², use equal-weight midpoint

# Shadow sigmoid QLIKE-ratio weight mapping
MZ_SIGMOID_SHADOW_MODE = True          # Shadow mode: log only, don't affect production weight
MZ_SIGMOID_KAPPA = 15.0                # Sigmoid steepness parameter
MZ_SIGMOID_Q_MID = 0.15               # QLIKE improvement ratio midpoint (50% weight at this improvement)
MZ_SIGMOID_W_MAX = 0.15               # Maximum sigmoid weight (cap)

# Weight bounds per asset (from persistence analysis)
EGARCH_WEIGHT_BOUNDS = {
    "BTC": (0.15, 0.45),   # High persistence → more EGARCH weight
    "ETH": (0.10, 0.35),
    "SOL": (0.05, 0.20),   # Low persistence → less EGARCH weight
    "XRP": (0.05, 0.25),
}
EGARCH_WEIGHT_DEFAULT = 0.0            # Before MZ warmup: pure RV (safe default)

# Safety clamps
EGARCH_RV_RATIO_CLAMP = 3.0           # Reject EGARCH if σ_eg/σ_rv > 3 or < 1/3
EGARCH_BLEND_LOG_INTERVAL = 300.0     # Log blend diagnostics every 5 min

# ─── Position Sizing ───────────────────────────────────────────────────────
SIZING_TIERS = [                  # (min_fee_adj_edge, risk_fraction) — aligned with MIN_EDGE_BY_PRICE
    (0.04,  0.25),                # edge ≥ 4.0% → 25% risk
    (0.025, 0.20),                # edge ≥ 2.5% → 20% risk
    (0.018, 0.15),                # edge ≥ 1.8% → 15% risk
    (0.012, 0.10),                # edge ≥ 1.2% → 10% risk
    (0.009, 0.07),                # edge ≥ 0.9% → 7% risk
    (0.007, 0.05),                # edge ≥ 0.7% → 5% risk
    (0.005, 0.03),                # edge ≥ 0.5% → 3% risk
    (0.0025, 0.02),               # edge ≥ 0.25% → 2% risk (thin-edge trades from halved schedule)
]
DRAWDOWN_HALF_THRESHOLD = 0.85    # below 85% of starting balance → halve size (was 90%)
DRAWDOWN_QUARTER_THRESHOLD = 0.75 # below 75% → quarter size (was 80%)
DRAWDOWN_HALT_THRESHOLD = 0.65    # below 65% → stop trading entirely (NEW)
HWM_LOOKBACK_SECONDS = 7 * 86400  # rolling 7-day peak for HWM (prevents stale HWM after withdrawals)
MAX_RISK_PER_TRADE = 0.25         # max 25% of bankroll at risk per trade (was 50%; reduced after loss analysis)
