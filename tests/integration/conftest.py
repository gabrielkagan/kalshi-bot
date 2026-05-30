"""Integration-tier fixtures.

Trading-mode default: the shipped config is SHADOW (bot.constants.GLOBAL_LIVE_TRADING
= False, per the 2026-05-30 operator directive to revert everything to shadow after a
verified account drawdown). But the integration suite exists to exercise the LIVE
execution mechanics (maker/taker/escalation/placement). So we restore live mode here
for integration tests — the gate (bot/trading_mode.py) becomes a no-op and the
pre-change placement behavior is tested as before.

The shipped-shadow CONFIG (the actual deployed default) is pinned separately in
tests/unit/test_trading_mode.py::test_shipped_default_is_all_shadow, which runs in the
unit tier (this fixture does not apply there), so both invariants are covered:
  - unit tier  → "as shipped, everything is shadow" (the deploy-safety default)
  - integ tier → "the live execution path works" (the mechanics)
"""
import pytest

import bot.constants as _C


@pytest.fixture(autouse=True)
def _live_trading_for_integration_mechanics(monkeypatch):
    monkeypatch.setattr(_C, "GLOBAL_LIVE_TRADING", True, raising=False)
    monkeypatch.setattr(
        _C, "ASSET_LIVE_TRADING",
        {a: True for a in _C.ASSET_LIVE_TRADING}, raising=False)
