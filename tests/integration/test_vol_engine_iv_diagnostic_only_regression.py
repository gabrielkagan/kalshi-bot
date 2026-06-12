"""Regression tests — Bit V.4: DVOL is diagnostic-only, never blended (2026-06-12).

Pins the fix for the soak-night BTC/ETH vol OVERSTATEMENT breach
(postmortem ``kb/failures/vol-engine-beta-dvol-deflation-jun12.md``
addendum, local-only KB; tickets 86bae6hwz RCA + 86badv7xj redesign).

RCA summary (verified against bot/engines/volatility.py + 9 days of
scan-journal counterfactuals at fix time):

1. The Step-5 ``stress_override`` branch (``blended = 0.3·rv + 0.7·iv``
   whenever ``(iv − rv)/rv > IV_RV_SPREAD_THRESHOLD=0.50``) fired on
   60-91% of BTC/ETH ticks through the 2026-06-12 quiet US evening:
   tape RV collapsed (~22% annualized ETH) while 30-day DVOL held ~55%
   (live feed, not stale — variance risk premium + zero intraday
   seasonality in a 30-day tenor). The trigger is a level-spread and
   cannot distinguish "IV rose" (its design scenario) from "RV fell"
   (every quiet evening) — it fires nightly by construction. Observed:
   0.3×0.9e-4 + 0.7×2.21e-4 = 1.82e-4 = the journaled blended median;
   honesty ratio vs tape 1.8-2.7; day-1 soak medians BTC 1.262 /
   ETH 1.386 — both outside the pre-registered [0.8, 1.25] band.
2. The Step-4 ``inverse_variance`` branch is dimensionally incoherent
   (``var_rv`` is a squared RK term-structure SPREAD, ``var_iv`` a
   squared 10%-of-level) — ``w_iv`` rises exactly when realized vol is
   moving. Measured on 9 days of journal counterfactuals it is
   QLIKE-negative on BTC and ETH (it dragged blended to ~0.55× truth
   on the Jun-5 spike day while w_iv≈0.9), and the one segment where
   the IV blend helped (stress windows) is matched by the EGARCH term
   alone.

Fix shape pinned here (Bit V.4):

* ``_get_implied_vol`` / ``_get_implied_vol_hourly`` are UNCHANGED
  (BTC/ETH direct DVOL, alts None — the Bit V.2 contract).
* The Step-4/5 blend branches are GONE: DVOL never mutates ``blended``.
  ``iv_rv_blend_method`` is always ``"rv_only"``.
* Diagnostics stay alive: ``dvol_5s``, ``iv_rv_spread``,
  ``dvol_sq_hourly``, ``vrp`` keep flowing (the W.1/W.2 replacement
  program needs the ΔDVOL shadow history — ticket 86bae6t6r).
"""

import math
import random
import time

import pytest

from bot.engines.volatility import VolatilityEngine


# ── Sidecar isolation (mirrors tests/integration/test_vol_engine_beta_dvol_regression.py) ──

@pytest.fixture(autouse=True)
def _isolate_vol_sidecar_files(tmp_path, monkeypatch):
    """Redirect rk_state.json + jump_adaptive_state.json so a stray
    sidecar in the repo root can't pre-load return buffers."""
    import bot.engines.volatility as _vol_mod

    monkeypatch.setattr(
        _vol_mod.VolatilityEngine, "RK_STATE_PATH",
        str(tmp_path / "rk_state.json"), raising=True,
    )
    monkeypatch.setattr(
        _vol_mod, "JUMP_ADAPTIVE_STATE_PATH",
        str(tmp_path / "jump_adaptive_state.json"), raising=True,
    )
    yield


# ── Minimal stubs (same shapes as the Bit V.2 sibling test file) ─────

class _StubFeed:
    def __init__(self, buffers=None):
        self._buffers = dict(buffers) if buffers else {}

    def get_buffer(self, asset):
        return list(self._buffers.get(asset, []))


class _StubDVOL:
    """Deterministic DeribitDVOLFetcher stand-in (per-5s scale values)."""

    def __init__(self, dvol=None, dvol_hourly=None):
        self._dvol_map = dict(dvol) if dvol else {}
        self._dvol_hourly_map = dict(dvol_hourly) if dvol_hourly else {}
        self._hourly_dvol = {a: [v] for a, v in self._dvol_hourly_map.items()}

    def get_dvol(self, asset):
        return self._dvol_map.get(asset)

    def get_dvol_hourly_avg(self, asset):
        return self._dvol_hourly_map.get(asset)


class _StubEGARCH:
    """EGARCH stand-in returning a fixed sigma for every asset."""

    def __init__(self, sigma=None):
        self._sigma = sigma
        self._log_var = {}
        self._n_updates = {}

    def record_return(self, asset, log_return):
        pass

    def recursive_update(self, asset, log_return):
        return self._sigma

    def get_sigma(self, asset):
        return self._sigma

    def seed_variance(self, asset, var):
        self._log_var[asset] = math.log(var) if var > 0 else None

    def get_constrained_sigma(self, asset):
        return self._sigma


class _StubMZ:
    """MincerZarnowitz stand-in with a pinned blend weight."""

    def __init__(self, w_eg=0.0):
        self._w_eg = w_eg
        self._r_squared = {}
        self._qlike = {}
        self._baseline_qlike = {}
        self._shadow_sigmoid_w = {}

    def record(self, asset, forecast_var, realized_var):
        pass

    def maybe_recompute(self, asset, now):
        return self._w_eg

    def save_state(self):
        pass


# Incident-night numbers (2026-06-12 ~21:00 UTC, from scan journal):
# ETH tape rv ≈ 0.9e-4 per-5s (~22% annualized), ETH DVOL ≈ 2.21e-4
# per-5s (~55% annualized) → iv_rv_spread ≈ 1.45 ≫ 0.50 threshold.
_QUIET_TAPE_SIGMA = 0.9e-4
_QUIET_DVOL_5S = 2.21e-4


def _seed_returns(eng, asset, n, sigma, seed):
    rng = random.Random(seed)
    for _ in range(n):
        eng._returns[asset].append(rng.gauss(0.0, sigma))


def _quiet_evening_engine(asset="ETH", egarch_sigma=1.0e-4):
    """Engine in the incident-night configuration: quiet tape, flat
    30-day DVOL far above it, EGARCH promoted (w_eg=1.0) so the
    EGARCH-blend value is exactly ``egarch_sigma`` and any IV
    contamination of ``blended_rv`` is detectable to 1e-9."""
    dvol = _StubDVOL(
        dvol={asset: _QUIET_DVOL_5S},
        dvol_hourly={asset: _QUIET_DVOL_5S},
    )
    eng = VolatilityEngine(
        _StubFeed(), dvol_fetcher=dvol,
        egarch_estimator=_StubEGARCH(sigma=egarch_sigma),
        mz_tracker=_StubMZ(w_eg=1.0),
    )
    _seed_returns(eng, asset, 180, _QUIET_TAPE_SIGMA, seed=21)
    return eng


# ─────────────────────────────────────────────────────────────────────
#  1. Incident shape: quiet evening must NOT stress-override
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("asset", ["BTC", "ETH"])
def test_quiet_evening_iv_never_blended_regression(asset):
    """2026-06-12 soak-night shape, end-to-end through _compute().

    Pre-V.4 this construction took the ``stress_override`` branch:
    spread = (2.21e-4 − ~0.9e-4)/0.9e-4 ≈ 1.45 > 0.50 →
    blended = 0.3·rv + 0.7·iv ≈ 1.8e-4 ≈ 2× the tape (the journaled
    incident value). Post-V.4 the blend must be untouched by IV: it
    equals the EGARCH-promoted value exactly.
    """
    egarch_sigma = 1.0e-4
    eng = _quiet_evening_engine(asset=asset, egarch_sigma=egarch_sigma)

    result = eng._compute(asset, now=time.time(), seconds_to_close=600)
    assert result is not None

    # Guard the pin's assumptions: no jump multipliers, EGARCH promoted.
    assert result["jump_multiplier"] == 1.0
    assert result["adaptive_jump_multiplier"] == 1.0
    assert result["egarch_blend_weight"] == 1.0

    pre_v4_stress_value = 0.3 * result["rv_only_blended"] + 0.7 * _QUIET_DVOL_5S
    assert result["blended_rv"] == pytest.approx(egarch_sigma, rel=1e-9), (
        f"blended_rv={result['blended_rv']:.6e} contaminated by DVOL — "
        f"pre-V.4 stress_override would have produced "
        f"{pre_v4_stress_value:.6e} (the 2026-06-12 1.8-2.7x honesty-"
        f"breach shape); expected the EGARCH-promoted {egarch_sigma:.6e}"
    )
    assert result["iv_rv_blend_method"] == "rv_only", (
        f"iv_rv_blend_method={result['iv_rv_blend_method']!r} — the "
        f"stress_override branch must be dead (fires nightly by "
        f"construction: VRP + diurnal trough satisfy a level-spread "
        f"trigger every quiet evening)"
    )


# ─────────────────────────────────────────────────────────────────────
#  2. The inverse-variance branch is dead too (sub-threshold spread)
# ─────────────────────────────────────────────────────────────────────

def test_inverse_variance_branch_dead_regression():
    """With IV only 25% above RV (below the old 0.50 stress threshold)
    pre-V.4 code took the ``inverse_variance`` branch and pulled
    blended toward IV whenever the RK term structure sloped. Post-V.4
    no IV branch fires at any spread: blended is the EGARCH-promoted
    value and the method vocabulary is ``rv_only``.

    (Counterfactual measurement 2026-06-12: this branch was
    QLIKE-negative on BTC and ETH over 9 days, n≈3.1k/3.0k segment
    rows, and dragged blended to ~0.55× truth on the Jun-5 spike day.)
    """
    rv_sigma = 2.0e-4
    egarch_sigma = 4.0e-4   # ratio 2.0, inside the EGARCH clamp
    iv = 2.5e-4             # spread vs rv ≈ 0.25 < 0.50
    dvol = _StubDVOL(dvol={"BTC": iv}, dvol_hourly={"BTC": iv})
    eng = VolatilityEngine(
        _StubFeed(), dvol_fetcher=dvol,
        egarch_estimator=_StubEGARCH(sigma=egarch_sigma),
        mz_tracker=_StubMZ(w_eg=1.0),
    )
    _seed_returns(eng, "BTC", 180, rv_sigma, seed=11)

    result = eng._compute("BTC", now=time.time(), seconds_to_close=600)
    assert result is not None
    assert result["jump_multiplier"] == 1.0
    assert result["adaptive_jump_multiplier"] == 1.0
    assert result["egarch_blend_weight"] == 1.0

    assert result["blended_rv"] == pytest.approx(egarch_sigma, rel=1e-9), (
        f"blended_rv={result['blended_rv']:.6e} != EGARCH-promoted "
        f"{egarch_sigma:.6e} — the inverse_variance branch must not "
        f"mutate blended (Bit V.4)"
    )
    assert result["iv_rv_blend_method"] == "rv_only"


# ─────────────────────────────────────────────────────────────────────
#  3. Diagnostics stay alive (the W.1/W.2 program needs the history)
# ─────────────────────────────────────────────────────────────────────

def test_dvol_diagnostics_survive_blend_removal():
    """Bit V.4 demotes DVOL to diagnostic-only — it must NOT go dark:
    ``dvol_5s``, ``iv_rv_spread``, ``dvol_sq_hourly`` and ``vrp`` keep
    flowing so the replacement program (ticket 86bae6t6r) can fit a
    ΔDVOL event term from shadow history."""
    eng = _quiet_evening_engine(asset="ETH")

    result = eng._compute("ETH", now=time.time(), seconds_to_close=600)
    assert result is not None

    assert result["dvol_5s"] == _QUIET_DVOL_5S
    assert result["dvol_sq_hourly"] == pytest.approx(_QUIET_DVOL_5S ** 2)
    assert result["vrp"] is not None
    # Spread is still computed (diagnostic), against rv_blended as before.
    # The bar is the OLD stress threshold (0.50): this construction would
    # have fired the deleted override, so a live spread diagnostic must
    # show it while blended stays untouched.
    assert result["iv_rv_spread"] is not None
    assert result["iv_rv_spread"] > 0.50, (
        "incident-night construction should show an IV-RV spread above "
        "the old stress threshold in the diagnostic field"
    )


def test_alt_diagnostics_unchanged_none():
    """Alts (no DVOL) keep the Bit V.2 contract: every IV diagnostic is
    None and the method is rv_only — V.4 must not disturb that."""
    eng = VolatilityEngine(
        _StubFeed(), dvol_fetcher=_StubDVOL(),
        egarch_estimator=_StubEGARCH(sigma=2.0e-4),
        mz_tracker=_StubMZ(w_eg=1.0),
    )
    _seed_returns(eng, "SOL", 180, 2.0e-4, seed=5)

    result = eng._compute("SOL", now=time.time(), seconds_to_close=600)
    assert result is not None
    assert result["dvol_5s"] is None
    assert result["iv_rv_spread"] is None
    assert result["iv_rv_blend_method"] == "rv_only"
