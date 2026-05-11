"""ProbabilityEngine — Student-t / NIG win-probability CDF with adaptive calibration cascade.

Extracted from bot/_impl.py in Sprint 6 Bit 6.2 (2026-05-09). Second leaf
in the Sprint 6 ``bot/engines/`` subpackage; sibling of
``bot.engines.volatility.VolatilityEngine`` (Bit 6.1) and
``bot.engines.calibration.CalibrationEngine`` (Bit 6.3, 2026-05-10 —
class + path-B singleton/helper relocation). The class is a pure
@staticmethod surface — there is no instance state and no constructor —
so the move is a byte-for-byte transplant of the 5 static methods
(``_cdf_complement``, ``compute``, ``counterfactual_prob``,
``_dynamic_cap``, ``_calibrate``).

Computes ``P(price stays above threshold)`` for a Kalshi YES bet given
spot, threshold, ``seconds_remaining``, and a blended realized vol
(per-5-second log-return scale). Supports per-asset distribution
selection via ``dist_config.json`` (loaded once at import in
``config.py``): Student-t with configurable ``df`` (default 4) or
Normal-Inverse-Gaussian with fitted ``(a, b, loc, scale)``. Falls back
to ``Student-t(df=4)`` when no per-asset config is present.

The calibration cascade in ``compute()`` is the load-bearing piece —
four branches in priority order:

1. Per-product CalibrationEngine from registry
   (``_resolve_cal_engine(product_type, asset, require_enabled=True)``):
   trained learned method (Platt / Beta / BLR / temperature) → uses
   ``calibrate(raw_prob, cap=dynamic_cap, seconds_to_close=...)``. Also
   emits a "shadow" passthrough+temperature counterfactual for the
   ``shadow_cal_prob`` / ``shadow_cal_temperature`` result keys.
2. Legacy 15M ``_CALIBRATION_ENGINE`` singleton when the product is
   ``cal_eligible`` AND ``FIFTEEN_M_CALIBRATION_ENABLED`` is True AND a
   learned method is active. Otherwise emits a ``"BLR_BYPASS"``
   diagnostic line so we can audit divergence post-deployment.
3. ``cal_eligible=False``: passthrough with the ``dynamic_cap``.
4. Fallback to ``ProbabilityEngine._calibrate`` (fixed β=0.85 logistic
   compression then hard cap).

Followed by the BLR clamp post-hoc: when ``raw_prob < 0.70`` and the
calibrator added more than 5pp, the calibrated value is clamped to
``raw_prob + 0.05`` (with a linear blend over the 0.70-0.85 band).
This guards the low raw-prob extrapolation regime where training data
was sparse — does NOT affect the shadow CalEngine, CalEngine
retraining, or high-raw signals. Then the model-vs-market sanity
check: ``calibrated_prob > DISCREPANCY_PROB`` AND
``market_price_cents < DISCREPANCY_PRICE`` → flag with ``tradeable=False``.

Imports are deliberate: stdlib (``math``, ``logging``,
``typing.Optional``, ``typing.Dict``) + ``scipy.stats`` (``student_t``,
``norminvgauss`` — the only engine module that imports scipy; the
``test_engines_extraction.py`` ``test_no_forbidden_numerical_imports``
gate is parametrized per-module to allow scipy here while still
banning numpy/torch/sklearn/pandas) + ``bot.constants`` (5 names —
``DISCREPANCY_PROB``, ``DISCREPANCY_PRICE``, ``DYNAMIC_CAP_SCHEDULE``,
``HOURLY_DYNAMIC_CAP_SCHEDULE``, ``FIFTEEN_M_CALIBRATION_ENABLED``) +
``config`` (4 names — ``DIST_CONFIG``, ``STUDENT_T_DF``, ``BETA_SLOPE``,
``MAX_EFFECTIVE_PROB``; the latter is the default-value expression on
``_calibrate(raw_prob, cap=MAX_EFFECTIVE_PROB)`` which is evaluated at
class-body load time and so MUST resolve at module import) +
``market_config`` (``get_market_config``, the
``MarketTypeConfig`` accessor used to read
``cal_eligible`` / ``cal_engine_enabled`` / ``temperature_*`` /
``cal_subtypes`` per product type).

Reaches the mutable ``_CALIBRATION_ENGINE`` singleton and
``_resolve_cal_engine`` resolver via top-level
``from bot.engines import calibration as _cal_state`` plus
``_cal_state.X`` attribute access. This is the **Bit 6.3 path-B
refactor (2026-05-10)** — Bit 6.2 originally used a late-binding
``from bot import _impl as _bot_impl`` pattern inside ``compute()``
and ``counterfactual_prob()`` because both names lived in
``bot/_impl.py`` BELOW the line-109 engines re-export, so a top-level
``from bot._impl import ...`` would have ImportErrored at load time
or captured a stale ``None``. Bit 6.3 relocated the singleton +
resolver to ``bot/engines/calibration.py`` (a leaf module that does
NOT import ``bot._impl``), eliminating the circularity and lifting
the late-binding. Module-attribute access on ``_cal_state`` preserves
the mutable-singleton freshness guarantee — every read sees the
current value because we go through the module reference. The
``.importlinter`` ``bot.engines.probability -> bot._impl``
``ignore_imports`` carve-out shipped in Pillar 2 was removed in the
same Bit 6.3 commit.

Construction site: none. ``ProbabilityEngine`` is a class with only
``@staticmethod`` methods; there is no instance and no
``MainLoop``-wired wiring. Downstream consumers call ``compute()``,
``counterfactual_prob()``, ``_cdf_complement()``, ``_dynamic_cap()``,
and ``_calibrate()`` as bare-class attribute access — in ``bot/_impl.py``
via the ``from bot.engines import ProbabilityEngine`` re-export (line ~109,
residual shim until Bit 9.3-iii.c), and in ``tests/test_probability_engine.py``
via ``from bot.engines.probability import ProbabilityEngine`` directly
(post-Bit-9.3-iii.b, 2026-05-11; the ``_BotProxy`` chain that previously
routed ``from bot import ProbabilityEngine`` is retired).
"""

import logging
import math
from typing import Dict, Optional

from scipy.stats import norminvgauss
from scipy.stats import t as student_t

from bot.constants import (
    DISCREPANCY_PRICE,
    DISCREPANCY_PROB,
    DYNAMIC_CAP_SCHEDULE,
    FIFTEEN_M_CALIBRATION_ENABLED,
    HOURLY_DYNAMIC_CAP_SCHEDULE,
)
from config import (
    BETA_SLOPE,
    DIST_CONFIG,
    MAX_EFFECTIVE_PROB,
    STUDENT_T_DF,
)
from market_config import get_market_config

from bot.engines import calibration as _cal_state  # Bit 6.3 path-B: alias for _CALIBRATION_ENGINE / _CAL_REGISTRY / _resolve_cal_engine which all live in bot/engines/calibration.py post-Bit-6.3. Module-attribute access via this alias preserves mutable-singleton freshness — see module docstring.


class ProbabilityEngine:
    """Compute win probability from spot price, strike, time, and volatility.

    Supports per-asset distribution selection via dist_config.json:
    - Student-t CDF with configurable df per asset (default df=4)
    - NIG (Normal Inverse Gaussian) CDF with fitted parameters
    Falls back to Student-t(df=4) if no config file is present.
    """

    @staticmethod
    def _cdf_complement(z_score: float, asset: Optional[str] = None) -> float:
        """Compute 1 - CDF(z_score) using per-asset distribution config."""
        cfg = DIST_CONFIG.get(asset) if asset else None
        if cfg is None:
            return 1.0 - student_t.cdf(z_score, df=STUDENT_T_DF)

        if cfg.get("distribution") == "nig" and "nig_a" in cfg:
            val = 1.0 - norminvgauss.cdf(
                z_score, cfg["nig_a"], cfg["nig_b"],
                loc=cfg.get("nig_loc", 0.0),
                scale=cfg.get("nig_scale", 1.0),
            )
            return max(0.0, min(1.0, val))  # clamp float rounding
        return 1.0 - student_t.cdf(z_score, df=cfg.get("student_t_df", STUDENT_T_DF))

    @staticmethod
    def compute(spot: float, threshold: float, seconds_remaining: float,
                blended_rv: float,
                market_price_cents: Optional[int] = None,
                asset: Optional[str] = None,
                product_type: Optional[str] = None) -> Dict:
        """
        Compute calibrated win probability for a "price stays above threshold" bet.

        Args:
            spot: current price (e.g. 68500.0 for BTC)
            threshold: strike/threshold price the market resolves against
            seconds_remaining: seconds until market close
            blended_rv: blended realized vol (per-5-second log return scale)
            market_price_cents: current Kalshi YES price in cents (for sanity check)

        Returns dict with: z_score, raw_prob, calibrated_prob, tradeable, reason
        """
        result: Dict = {
            "z_score": None,
            "raw_prob": None,
            "calibrated_prob": None,
            "calibration_method": None,
            "tradeable": False,
            "reason": "",
        }

        # ── Guard: need valid inputs ─────────────────────────────────────
        if spot <= 0 or seconds_remaining <= 0 or blended_rv <= 0:
            result["reason"] = "invalid inputs (spot/time/vol <= 0)"
            return result

        # ── Annualize vol and compute z-score ────────────────────────────
        # blended_rv is std dev of 5-second log returns.
        # σ_annual = blended_rv × sqrt(seconds_per_year / 5)
        # σ_annual × sqrt(t_years) = blended_rv × sqrt(t_seconds / 5)
        # Denominator for z: spot × blended_rv × sqrt(t_seconds / 5)
        sigma_move = spot * blended_rv * math.sqrt(seconds_remaining / 5.0)

        if sigma_move <= 0:
            result["reason"] = "sigma_move is zero"
            return result

        z_score = (threshold - spot) / sigma_move
        result["z_score"] = round(z_score, 4)

        # ── Raw probability via configurable distribution CDF ────────────
        # P(price stays above threshold) = P(move > threshold - spot)
        # = P(Z > z_score) = 1 - CDF(z_score)
        raw_prob = ProbabilityEngine._cdf_complement(z_score, asset)
        result["raw_prob"] = round(raw_prob, 6)

        # ── Calibration: adaptive (if trained) or fixed β=0.85 ──────────
        dynamic_cap = ProbabilityEngine._dynamic_cap(seconds_remaining, product_type=product_type)
        _cal_cfg2 = get_market_config(product_type)
        _reg_engine = _cal_state._resolve_cal_engine(product_type, asset, require_enabled=True)
        if _reg_engine is not None and _reg_engine.is_learned_method_active():
            calibrated_prob = _reg_engine.calibrate(raw_prob, cap=dynamic_cap,
                                                     seconds_to_close=seconds_remaining)
            result["calibration_method"] = f"{product_type}_{_reg_engine.active_method}"
            # Shadow: what passthrough + temperature would have produced
            _pt_shadow = min(raw_prob, dynamic_cap)
            _temp_cfg = _cal_cfg2.temperature_t if _cal_cfg2.temperature_enabled else None
            if _temp_cfg and _temp_cfg != 1.0:
                _sp = max(0.001, min(0.999, _pt_shadow))
                _sz = math.log(_sp / (1.0 - _sp))
                _pt_shadow = 1.0 / (1.0 + math.exp(-_sz / _temp_cfg))
            result["shadow_cal_prob"] = round(_pt_shadow, 6)
            result["shadow_cal_temperature"] = _temp_cfg
        elif _cal_cfg2.cal_eligible and _cal_state._CALIBRATION_ENGINE is not None:
            if FIFTEEN_M_CALIBRATION_ENABLED and _cal_state._CALIBRATION_ENGINE.is_learned_method_active():
                calibrated_prob = _cal_state._CALIBRATION_ENGINE.calibrate(raw_prob, cap=dynamic_cap,
                                                               seconds_to_close=seconds_remaining)
                result["calibration_method"] = _cal_state._CALIBRATION_ENGINE.active_method
            else:
                calibrated_prob = min(raw_prob, dynamic_cap)
                result["calibration_method"] = "passthrough"
                # Diagnostic: log what BLR would have produced (remove after validation)
                _blr_would = _cal_state._CALIBRATION_ENGINE.calibrate(raw_prob, cap=dynamic_cap,
                                                           seconds_to_close=seconds_remaining)
                if abs(_blr_would - calibrated_prob) > 0.02:
                    logging.info(
                        "BLR_BYPASS: raw=%.4f passthrough=%.4f blr_would=%.4f delta=%.3f",
                        raw_prob, calibrated_prob, _blr_would, _blr_would - calibrated_prob)
        elif not _cal_cfg2.cal_eligible:
            calibrated_prob = min(raw_prob, dynamic_cap)
            result["calibration_method"] = "passthrough"
        else:
            calibrated_prob = ProbabilityEngine._calibrate(raw_prob, cap=dynamic_cap)
            result["calibration_method"] = "fixed_beta"
        # ── Clamped BLR: prevent extreme inflation in low raw_prob zone ──
        # BLR extrapolates badly when raw_prob < 0.70 (training data is 85c+).
        # Clamp cal_prob to max raw_prob + 5pp, with linear transition 70-85%.
        # Does NOT affect shadow CalEngine, CalEngine retraining, or high-raw signals.
        _cal_clamp_delta = calibrated_prob - raw_prob
        if raw_prob < 0.70 and _cal_clamp_delta > 0.05:
            _original_cal = calibrated_prob
            calibrated_prob = min(calibrated_prob, raw_prob + 0.05)
            logging.info(
                "CAL_CLAMP: raw=%.3f blr=%.3f clamped=%.3f delta=%.3f",
                raw_prob, _original_cal, calibrated_prob,
                _original_cal - calibrated_prob)
        elif raw_prob < 0.85 and _cal_clamp_delta > 0.05:
            _alpha = (raw_prob - 0.70) / 0.15
            _clamped = min(calibrated_prob, raw_prob + 0.05)
            _original_cal = calibrated_prob
            calibrated_prob = _alpha * calibrated_prob + (1.0 - _alpha) * _clamped
            if abs(calibrated_prob - _original_cal) > 0.005:
                logging.info(
                    "CAL_CLAMP_BLEND: raw=%.3f blr=%.3f clamped=%.3f alpha=%.2f delta=%.3f",
                    raw_prob, _original_cal, calibrated_prob, _alpha,
                    _original_cal - calibrated_prob)

        result["calibrated_prob"] = round(calibrated_prob, 6)

        # ── Sanity: model vs market discrepancy ──────────────────────────
        if market_price_cents is not None:
            if calibrated_prob > DISCREPANCY_PROB and market_price_cents < DISCREPANCY_PRICE:
                result["reason"] = (
                    f"model says {calibrated_prob:.1%} but market is "
                    f"{market_price_cents}¢ (< {DISCREPANCY_PRICE}¢) — refusing"
                )
                logging.warning(f"ProbabilityEngine: {result['reason']}")
                return result

        # ── All checks passed ────────────────────────────────────────────
        result["tradeable"] = True
        result["reason"] = "ok"
        return result

    @staticmethod
    def counterfactual_prob(spot: float, threshold: float, seconds_remaining: float,
                            alt_blended_rv: float, asset: Optional[str] = None,
                            product_type: Optional[str] = None) -> Optional[float]:
        """Compute calibrated_prob for a counterfactual blended_rv. Lightweight — no logging."""
        if spot <= 0 or seconds_remaining <= 0 or alt_blended_rv <= 0:
            return None
        sigma_move = spot * alt_blended_rv * math.sqrt(seconds_remaining / 5.0)
        if sigma_move <= 0:
            return None
        z = (threshold - spot) / sigma_move
        raw = ProbabilityEngine._cdf_complement(z, asset)
        cap = ProbabilityEngine._dynamic_cap(seconds_remaining, product_type=product_type)
        if _cal_state._CALIBRATION_ENGINE is not None:
            return round(_cal_state._CALIBRATION_ENGINE.calibrate(raw, cap=cap,
                                                       seconds_to_close=seconds_remaining), 6)
        return round(ProbabilityEngine._calibrate(raw, cap=cap), 6)

    @staticmethod
    def _dynamic_cap(seconds_remaining: float, product_type: str = None) -> float:
        """Return probability cap based on time to close."""
        schedule = (HOURLY_DYNAMIC_CAP_SCHEDULE
                    if product_type in ("hourly", "spx_hourly", "weather")
                    else DYNAMIC_CAP_SCHEDULE)
        for threshold_secs, cap in schedule:
            if seconds_remaining > threshold_secs:
                return cap
        return schedule[-1][1]  # smallest TTC bracket

    @staticmethod
    def _calibrate(raw_prob: float, cap: float = MAX_EFFECTIVE_PROB) -> float:
        """Apply logistic compression then hard cap.

        Maps raw_prob through: logit → scale by BETA_SLOPE → inverse logit → cap.
        This pulls extreme probabilities toward 0.5 and caps at 93%.
        """
        # Clamp to avoid log(0) in logit
        p = max(0.001, min(0.999, raw_prob))
        logit = math.log(p / (1.0 - p))
        scaled_logit = BETA_SLOPE * logit
        compressed = 1.0 / (1.0 + math.exp(-scaled_logit))
        return min(compressed, cap)
