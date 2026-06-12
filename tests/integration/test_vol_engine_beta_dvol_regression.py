"""Regression tests — Bit V.2: kill the beta-scaled DVOL path (2026-06-12).

Pins the fix for the vol-engine beta×DVOL deflation incident (postmortem
``kb/failures/vol-engine-beta-dvol-deflation-jun12.md``, local-only KB;
severity CRITICAL — chronic since the DVOL integration commit e7e73222,
2026-02-23).

RCA summary (verified against bot/engines/volatility.py at fix time):

1. ``_get_implied_vol`` / ``_get_implied_vol_hourly`` returned
   ``btc_dvol × _estimate_beta(asset)`` for every asset outside
   ``DERIBIT_DVOL_CURRENCIES`` ({"BTC", "ETH"}). ``_estimate_beta`` is
   unsalvageable for this purpose: regression beta = corr×(σa/σb)
   understates the VOL RATIO at 5s horizons (Epps effect), the live
   deques are appended at per-asset scan cadence with jitter so
   index-alignment ≠ time-alignment (covariance → 0), and the 0.5
   clamp floor produced the cross-asset-identical 8.2-9.0e-5 cluster
   observed live on 2026-06-12 (back-solves to BTC DVOL ~42% annualized
   × 0.5 within ~1%).
2. The IV-RV inverse-variance blend gives a DEFLATED iv quadratically
   more weight (``var_iv = (0.1·iv)²``) — lock-in feedback.
3. The IV blend line blended IV against ``rv_blended``, silently
   DISCARDING the promoted EGARCH variance-space blend (set just above
   it) whenever IV fired — dead code on the EGARCH layer.

Fix shape pinned here:

* Alts (anything not in ``DERIBIT_DVOL_CURRENCIES``) get ``None`` from
  both implied-vol getters → they take the rv/EGARCH fallback that
  already exists for ``iv is None``.
* BTC/ETH keep direct DVOL.
* The IV blend now blends against ``blended`` (the EGARCH-promoted
  value), preserving the EGARCH layer for BTC/ETH.
"""

import math
import random
import time

import pytest

from bot.engines.volatility import VolatilityEngine
from bot.constants import DERIBIT_DVOL_CURRENCIES
from bot.config import VOL_RETURN_INTERVAL


# ── Sidecar isolation (mirrors tests/equivalence/conftest.py) ─────────

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


# ── Minimal stubs (pattern from tests/integration/test_vol_engine.py) ─

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
        # _compute()'s DVOL-health logging branch reads _hourly_dvol
        # for sample count + min/max.
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


# Day-one failure shape: BTC DVOL ~42% annualized ≈ 1.7e-4 per-5s scale
# (42% / sqrt(SECONDS_PER_YEAR / 5) ≈ 1.67e-4). Pre-fix, alts received
# btc_dvol × clamp(beta, 0.5, 3.0) — the 0.5 floor gave the ~8.5e-5
# cross-asset cluster against a ~3e-4 alt tape.
_BTC_DVOL_5S = 1.7e-4
_ALT_TAPE_SIGMA = 3e-4


def _seed_returns(eng, asset, n, sigma, seed):
    rng = random.Random(seed)
    for _ in range(n):
        eng._returns[asset].append(rng.gauss(0.0, sigma))


# ─────────────────────────────────────────────────────────────────────
#  1. Implied-vol getters: alts must return None
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("asset", ["HYPE", "SOL", "BNB", "XRP", "DOGE"])
def test_get_implied_vol_returns_none_for_alts(asset):
    """Alts outside DERIBIT_DVOL_CURRENCIES have NO implied vol — the
    beta×BTC_DVOL fabrication is dead (Bit V.2)."""
    assert asset not in DERIBIT_DVOL_CURRENCIES, "test premise violated"
    dvol = _StubDVOL(dvol={"BTC": _BTC_DVOL_5S, "ETH": 2.0e-4},
                     dvol_hourly={"BTC": _BTC_DVOL_5S, "ETH": 2.0e-4})
    eng = VolatilityEngine(_StubFeed(), dvol_fetcher=dvol)
    # Seed buffers so the legacy beta path would have data (pre-fix it
    # returned a value even WITHOUT data, via the beta=1.0 default).
    _seed_returns(eng, "BTC", 60, 1e-4, seed=1)
    _seed_returns(eng, asset, 60, _ALT_TAPE_SIGMA, seed=2)
    assert eng._get_implied_vol(asset) is None, (
        f"_get_implied_vol({asset!r}) must be None — the beta-scaled "
        f"BTC-DVOL path deflated alt vol up to ~6x (8.5e-5 cluster vs "
        f"3e-4 tape, 2026-06-12 incident)"
    )


@pytest.mark.parametrize("asset", ["HYPE", "SOL", "BNB", "XRP", "DOGE"])
def test_get_implied_vol_hourly_returns_none_for_alts(asset):
    """Hourly-averaged sibling of the getter above — same kill."""
    dvol = _StubDVOL(dvol={"BTC": _BTC_DVOL_5S},
                     dvol_hourly={"BTC": _BTC_DVOL_5S})
    eng = VolatilityEngine(_StubFeed(), dvol_fetcher=dvol)
    _seed_returns(eng, "BTC", 60, 1e-4, seed=3)
    _seed_returns(eng, asset, 60, _ALT_TAPE_SIGMA, seed=4)
    assert eng._get_implied_vol_hourly(asset) is None


def test_btc_eth_still_get_direct_dvol():
    """BTC/ETH (the DERIBIT_DVOL_CURRENCIES set) keep their DIRECT
    Deribit DVOL feed — Bit V.2 only kills the cross-asset fabrication."""
    dvol = _StubDVOL(dvol={"BTC": _BTC_DVOL_5S, "ETH": 2.0e-4},
                     dvol_hourly={"BTC": 1.6e-4, "ETH": 1.9e-4})
    eng = VolatilityEngine(_StubFeed(), dvol_fetcher=dvol)
    assert eng._get_implied_vol("BTC") == _BTC_DVOL_5S
    assert eng._get_implied_vol("ETH") == 2.0e-4
    assert eng._get_implied_vol_hourly("BTC") == 1.6e-4
    assert eng._get_implied_vol_hourly("ETH") == 1.9e-4


def test_get_implied_vol_none_when_no_fetcher():
    """No fetcher → None for everyone (pre-existing behavior, pinned)."""
    eng = VolatilityEngine(_StubFeed(), dvol_fetcher=None)
    assert eng._get_implied_vol("BTC") is None
    assert eng._get_implied_vol("HYPE") is None
    assert eng._get_implied_vol_hourly("BTC") is None
    assert eng._get_implied_vol_hourly("HYPE") is None


# ─────────────────────────────────────────────────────────────────────
#  2. End-to-end: alt blended_rv tracks the realized estimator
# ─────────────────────────────────────────────────────────────────────

def test_alt_blended_rv_tracks_tape_not_dvol_anchor_regression():
    """Day-one failure shape, end-to-end through update().

    HYPE tape vol ~3e-4 per-5s; BTC DVOL implies ~8.5e-5 at the beta=0.5
    floor. Pre-fix the IV-RV inverse-variance blend pinned blended_rv
    toward the deflated DVOL anchor (~0.85-1.2e-4 — longshot sold
    deep-OTM insurance priced at 1.4-4x understated vol; 10/11 live
    fills failed the validated entry rule under honest vol). Post-fix,
    iv is None for HYPE so blended_rv must track the realized/EGARCH
    estimator: ≥ ~2e-4 for a 3e-4 tape.
    """
    # Synthetic HYPE price tape at 1s cadence, per-5s sigma ~3e-4.
    rng = random.Random(42)
    t0 = time.time() - 300
    prices = []
    p = 30.0
    for i in range(250):
        # per-1s sigma so that 5s-aggregated sigma ≈ _ALT_TAPE_SIGMA
        p *= math.exp(rng.gauss(0.0, _ALT_TAPE_SIGMA / math.sqrt(VOL_RETURN_INTERVAL)))
        prices.append((t0 + i, p))

    dvol = _StubDVOL(dvol={"BTC": _BTC_DVOL_5S},
                     dvol_hourly={"BTC": _BTC_DVOL_5S})
    eng = VolatilityEngine(_StubFeed({"HYPE": prices}), dvol_fetcher=dvol)

    # Seed return buffers: HYPE at tape vol; BTC independent (the live
    # misalignment drove cov→0 → beta pinned at the 0.5 clamp floor).
    _seed_returns(eng, "HYPE", 180, _ALT_TAPE_SIGMA, seed=7)
    _seed_returns(eng, "BTC", 180, 1e-4, seed=99)

    eng._last_return_time["HYPE"] = 0  # force a fresh return + compute
    result = eng.update("HYPE", seconds_to_close=600)

    assert result is not None
    # Honest realized vol for a 3e-4 tape — must NOT pin at the
    # DVOL-anchored ~8.5e-5..1.2e-4 cluster.
    assert result["blended_rv"] >= 2e-4, (
        f"blended_rv={result['blended_rv']:.3e} pinned toward the "
        f"deflated BTC-DVOL anchor instead of the ~3e-4 realized tape "
        f"(2026-06-12 incident shape)"
    )
    # The IV path must be fully dark for alts.
    assert result["dvol_5s"] is None
    assert result["iv_rv_blend_method"] == "rv_only"
    assert result["dvol_sq_hourly"] is None
    assert result["vrp"] is None


# ─────────────────────────────────────────────────────────────────────
#  3. EGARCH blend survives into the IV blend (BTC)
# ─────────────────────────────────────────────────────────────────────

def test_egarch_blend_survives_iv_blend_for_btc_regression():
    """Latent bug #3 in the RCA: the IV blend line computed
    ``w_rv * rv_blended + w_iv * iv``, discarding the promoted EGARCH
    variance-space blend (``blended = egarch_blend_sigma``) whenever IV
    fired. Post-fix the IV blend must use ``blended`` (the
    EGARCH-promoted value) as the RV-side input.

    Construction: rv ≈ 2e-4 tape, EGARCH sigma pinned at 4e-4 (ratio
    2.0, inside the [1/3, 3] clamp) with MZ weight w_eg=1.0 so
    ``egarch_blend_sigma == egarch_sigma``; BTC DVOL at 2.5e-4 so the
    IV branch fires without tripping the stress override
    (iv_rv_spread vs rv_blended = 0.25 < IV_RV_SPREAD_THRESHOLD=0.50).
    """
    egarch_sigma = 4e-4
    iv = 2.5e-4
    dvol = _StubDVOL(dvol={"BTC": iv}, dvol_hourly={"BTC": iv})
    eng = VolatilityEngine(
        _StubFeed(), dvol_fetcher=dvol,
        egarch_estimator=_StubEGARCH(sigma=egarch_sigma),
        mz_tracker=_StubMZ(w_eg=1.0),
    )
    _seed_returns(eng, "BTC", 180, 2e-4, seed=11)

    result = eng._compute("BTC", now=time.time(), seconds_to_close=600)
    assert result is not None

    # Guard the pin's assumptions: no jump multipliers, EGARCH promoted.
    assert result["jump_multiplier"] == 1.0
    assert result["adaptive_jump_multiplier"] == 1.0
    assert result["egarch_blend_weight"] == 1.0
    assert result["egarch_blend_var"] is not None
    egarch_blend_sigma = math.sqrt(result["egarch_blend_var"])
    assert egarch_blend_sigma == pytest.approx(egarch_sigma, rel=1e-9)
    assert result["iv_rv_blend_method"] == "inverse_variance"

    # Recompute the inverse-variance weights from the result's own
    # fields (same formula as the engine).
    var_rv = (result["rv_1min"] - result["rv_15min"]) ** 2
    var_iv = (iv * 0.10) ** 2
    w_rv = var_iv / (var_rv + var_iv)
    w_iv = var_rv / (var_rv + var_iv)
    assert w_rv > 0, "degenerate weights — test construction broken"

    expected_post_fix = w_rv * egarch_blend_sigma + w_iv * iv
    pre_fix_value = w_rv * result["rv_only_blended"] + w_iv * iv
    # The two targets must be distinguishable for the pin to mean anything.
    assert abs(expected_post_fix - pre_fix_value) > 1e-6, (
        "test construction broken — EGARCH and RV blends indistinguishable"
    )
    assert result["blended_rv"] == pytest.approx(expected_post_fix, rel=1e-9), (
        f"IV blend discarded the promoted EGARCH layer: blended_rv="
        f"{result['blended_rv']:.6e}, expected w_rv*egarch_blend + w_iv*iv="
        f"{expected_post_fix:.6e} (pre-fix dead-EGARCH value: "
        f"{pre_fix_value:.6e})"
    )
