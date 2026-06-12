"""Trading-mode control — global + per-asset live/shadow gate (TDD-first).

The single source of truth for "should this asset place REAL orders right now?".
Added ALONGSIDE (defense-in-depth with) the scattered inline `_15M_SHADOW` checks
(~6 candidate-append sites, 4 assets hardcoded) as one modular gate (north star:
modular + easy) — both fail toward shadow; the scanner checks remain. Read
live (module-attribute access on bot.constants) so flipping a flag is a runtime
kill-switch — no restart needed.

Semantics (fail-safe):
  is_live(asset) == GLOBAL_LIVE_TRADING AND ASSET_LIVE_TRADING.get(asset, DEFAULT)
  - GLOBAL off  -> everything shadow (one-flag master kill)
  - asset off   -> that asset shadow
  - unknown asset -> DEFAULT (False = fail-safe: never trades live until enabled)
"""
from __future__ import annotations

import bot.constants as C
from bot import trading_mode as tm


def _set(monkeypatch, *, glob, assets, default=False):
    monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", glob)
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING", assets)
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING_DEFAULT", default)


def test_live_requires_global_on_and_asset_on(monkeypatch):
    _set(monkeypatch, glob=True, assets={"BTC": True})
    assert tm.is_live("BTC") is True
    assert tm.mode_reason("BTC") == "live"


def test_global_off_shadows_everything(monkeypatch):
    _set(monkeypatch, glob=False, assets={"BTC": True, "ETH": True})
    assert tm.is_live("BTC") is False
    assert tm.is_live("ETH") is False
    assert tm.mode_reason("BTC") == "global_shadow"


def test_asset_off_shadows_only_that_asset(monkeypatch):
    _set(monkeypatch, glob=True, assets={"BTC": True, "HYPE": False})
    assert tm.is_live("BTC") is True
    assert tm.is_live("HYPE") is False
    assert tm.mode_reason("HYPE") == "asset_shadow"


def test_unknown_asset_is_fail_safe_default_off(monkeypatch):
    _set(monkeypatch, glob=True, assets={"BTC": True}, default=False)
    assert tm.is_live("DOGE") is False          # not in dict -> default
    assert tm.mode_reason("DOGE") == "asset_shadow"


def test_runtime_flip_takes_effect_without_reimport(monkeypatch):
    # kill-switch contract: mutating the constant flips behavior live.
    _set(monkeypatch, glob=True, assets={"SOL": True})
    assert tm.is_live("SOL") is True
    monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
    assert tm.is_live("SOL") is False


def test_shipped_default_is_all_shadow():
    # As shipped (operator directive 2026-05-30: revert everything to shadow),
    # the master switch is OFF so no asset trades live until deliberately enabled.
    assert C.GLOBAL_LIVE_TRADING is False
    # every known crypto asset is present in the per-asset map (explicit surface)
    for a in ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH"):
        assert a in C.ASSET_LIVE_TRADING


def test_asset_live_trading_covers_all_series_no_drift():
    # DRIFT-PIN (R1-MN1): both chokepoints resolve the governed asset via
    # asset_from_ticker, which iterates ASSET_LIVE_TRADING's keys to match the
    # KX<ASSET>15M ticker. A crypto series added to SERIES_TICKERS but forgotten
    # here would never match → BYPASS both gates and trade live (the very
    # hardcoded-asset-drift anti-pattern this Bit kills). Lock the two sets
    # together so a new asset must be added in lock-step.
    assert set(C.ASSET_LIVE_TRADING) == set(C.SERIES_TICKERS), (
        "ASSET_LIVE_TRADING drifted from SERIES_TICKERS — a crypto-15M series is "
        "ungoverned by the live/shadow gate. Add it to ASSET_LIVE_TRADING.")


def test_asset_from_ticker_matches_only_crypto_15m():
    # governed crypto-15M tickers map to their asset (place_order backstop scope)
    assert tm.asset_from_ticker("KXBTC15M-26MAY3015-T100") == "BTC"
    assert tm.asset_from_ticker("KXHYPE15M-26MAY3015-T5") == "HYPE"
    # NON-crypto / non-15M tickers are NOT governed (backstop must not touch them)
    assert tm.asset_from_ticker("KXBTCD-26MAY30-T100") is None      # daily, not 15M
    assert tm.asset_from_ticker("KXHIGHNYC-26MAY29-T75") is None    # weather
    assert tm.asset_from_ticker("") is None
    assert tm.asset_from_ticker("GARBAGE") is None


# ── per-strategy validated-universe scoping (Bit T-1 M1 fix round) ───────────
# RCA class: feedback_shadow_flag_comprehensive_may10 — the strategy
# live-overrides were asset-UNSCOPED, so at go-live they would have armed
# longshot/twaplock on ADA/BCH (T1 zero-live-orders shadow designation,
# ADA_15M_SHADOW/BCH_15M_SHADOW) and on assets with no validation evidence
# (BNB for longshot). Fix: each strategy branch in strategy_is_live is gated
# by its validated asset set — applied to the WHOLE branch (override leg AND
# the future GLOBAL+asset dual-live leg).

_TWAPLOCK_VALIDATED = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB")
# BNB included per the 2026-06-12 operator directive ("everything
# available" at go-live) — 02b evidence gap is a corpus artifact (no
# replayable spot); see the LONGSHOT_LIVE_ASSETS constants comment.
_LONGSHOT_LIVE_UNIVERSE = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE",
                           "BNB")


def test_strategy_live_assets_constants_pin():
    # twaplock: mirrors TRACKED in scripts/research/genhunt/
    # 01b_twap_lock_validation.py ("all 7 assets positive").
    assert C.TWAPLOCK_LIVE_ASSETS == frozenset(_TWAPLOCK_VALIDATED)
    # longshot: the 02b "all 6 assets positive" set + BNB per the
    # 2026-06-12 operator directive (constants comment carries the RCA).
    assert C.LONGSHOT_LIVE_ASSETS == frozenset(_LONGSHOT_LIVE_UNIVERSE)
    # ADA/BCH: T1 zero-live-orders shadow + zero validation windows — never
    # live-eligible for either strategy.
    for s in (C.TWAPLOCK_LIVE_ASSETS, C.LONGSHOT_LIVE_ASSETS):
        assert "ADA" not in s
        assert "BCH" not in s


def test_twaplock_override_scoped_to_validated_universe(monkeypatch):
    _set(monkeypatch, glob=False, assets={})
    monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", True, raising=False)
    monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False, raising=False)
    for a in _TWAPLOCK_VALIDATED:
        assert tm.strategy_is_live("twaplock", a) is True, a
    assert tm.strategy_is_live("twaplock", "ADA") is False
    assert tm.strategy_is_live("twaplock", "BCH") is False


def test_longshot_override_scoped_to_validated_universe(monkeypatch):
    _set(monkeypatch, glob=False, assets={})
    monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", True, raising=False)
    monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", False, raising=False)
    for a in _LONGSHOT_LIVE_UNIVERSE:
        assert tm.strategy_is_live("longshot", a) is True, a
    assert tm.strategy_is_live("longshot", "ADA") is False
    assert tm.strategy_is_live("longshot", "BCH") is False


def test_dual_live_still_respects_validated_universe(monkeypatch):
    # Future dual-live posture: GLOBAL on + asset live. The strategy must
    # STILL respect its validated universe (asset-set check applies to the
    # whole strategy branch, not just the override leg) while the main
    # pipeline goes live normally.
    _set(monkeypatch, glob=True, assets={"ADA": True, "BNB": True})
    monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False, raising=False)
    monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", False, raising=False)
    assert tm.is_live("ADA") is True
    assert tm.strategy_is_live(None, "ADA") is True       # main pipeline
    assert tm.strategy_is_live("above", "ADA") is True    # main pipeline
    assert tm.strategy_is_live("twaplock", "ADA") is False
    assert tm.strategy_is_live("longshot", "ADA") is False
    # BNB: inside BOTH live universes (longshot per the 2026-06-12
    # operator directive)
    assert tm.strategy_is_live("twaplock", "BNB") is True
    assert tm.strategy_is_live("longshot", "BNB") is True
