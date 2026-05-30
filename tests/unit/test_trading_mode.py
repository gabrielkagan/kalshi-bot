"""Trading-mode control — global + per-asset live/shadow gate (TDD-first).

The single source of truth for "should this asset place REAL orders right now?".
Replaces the scattered inline `_15M_SHADOW` checks (~6 candidate-append sites,
4 assets hardcoded) with one modular gate (north star: modular + easy). Read
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
    # DRIFT-PIN (R1-MN1): the gate keys on dict MEMBERSHIP (`asset in
    # ASSET_LIVE_TRADING` at the executor; the asset_from_ticker loop). A crypto
    # series added to SERIES_TICKERS but forgotten here would BYPASS both gates
    # and trade live (the very hardcoded-asset-drift anti-pattern this Bit kills).
    # Lock the two sets together so a new asset must be added in lock-step.
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
