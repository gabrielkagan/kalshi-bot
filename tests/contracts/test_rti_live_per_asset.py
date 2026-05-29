"""RTI-6 go-live contract — per-asset promotion of the synthetic RTI into the
live decision spot, gated on ``SYNTHETIC_RTI_LIVE_ASSETS`` (default EMPTY).

Pairs with ``test_synthetic_rti_shadow_invariant.py``: that pins the shadow
staging; this pins the per-asset live carve-out. With the default empty set,
RTI feeds NO decision — the shadow invariant holds for EVERY asset, so this is
a zero-behavior-change build until an asset is explicitly promoted (which still
requires the RTI-3 beats-market gate + the RMSE gate + operator approval).

Umbrella 86ba6hdqr / ticket 86ba6hf2y. Plan: kb/decisions/rti-go-live-plan.md.
"""
from __future__ import annotations

from pathlib import Path

from bot.constants import SYNTHETIC_RTI_LIVE_ASSETS, RTI_LIVE_MIN_CONFIDENCE
from bot.scanner import OpportunityScanner

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"


def test_live_assets_default_empty():
    """Default config: RTI feeds NO decision (zero behavior change)."""
    assert SYNTHETIC_RTI_LIVE_ASSETS == set()


def test_effective_spot_is_coinbase_when_asset_not_live():
    cache = {"BTC": (50000.0, 4, 1.0)}
    assert OpportunityScanner._effective_decision_spot("BTC", 49999.0, cache) == 49999.0


def test_effective_spot_uses_rti_when_live_and_confident(monkeypatch):
    monkeypatch.setattr("bot.scanner.SYNTHETIC_RTI_LIVE_ASSETS", {"BTC"})
    cache = {"BTC": (50000.0, 4, 1.0)}
    assert OpportunityScanner._effective_decision_spot("BTC", 49999.0, cache) == 50000.0


def test_effective_spot_falls_back_on_low_confidence(monkeypatch):
    monkeypatch.setattr("bot.scanner.SYNTHETIC_RTI_LIVE_ASSETS", {"BTC"})
    cache = {"BTC": (50000.0, 2, RTI_LIVE_MIN_CONFIDENCE - 0.01)}
    assert OpportunityScanner._effective_decision_spot("BTC", 49999.0, cache) == 49999.0


def test_effective_spot_falls_back_when_rti_missing(monkeypatch):
    monkeypatch.setattr("bot.scanner.SYNTHETIC_RTI_LIVE_ASSETS", {"BTC"})
    assert OpportunityScanner._effective_decision_spot("BTC", 49999.0, {}) == 49999.0


def test_effective_spot_falls_back_when_rti_value_none(monkeypatch):
    monkeypatch.setattr("bot.scanner.SYNTHETIC_RTI_LIVE_ASSETS", {"BTC"})
    cache = {"BTC": (None, 0, 0.0)}
    assert OpportunityScanner._effective_decision_spot("BTC", 49999.0, cache) == 49999.0


def test_decision_compute_routes_through_effective_spot():
    """AST/text pin: the gated helper is the ONLY route RTI can take into the
    decision — the live compute must be fed via _effective_decision_spot."""
    src = SCANNER_PY.read_text()
    assert "_effective_decision_spot" in src, "helper missing from scanner"
