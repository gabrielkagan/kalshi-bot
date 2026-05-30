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

import ast
from pathlib import Path

from bot.constants import SYNTHETIC_RTI_LIVE_ASSETS, RTI_LIVE_MIN_CONFIDENCE
from bot.scanner import OpportunityScanner

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
EXECUTOR_PY = REPO_ROOT / "bot" / "executor.py"


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


def _probability_compute_calls(node):
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "compute"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "ProbabilityEngine"
    ]


def _scan_fn(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "scan":
            return n
    return None


def _is_gated_spot(first):
    """First positional arg references the gated effective spot — either the
    inlined ``_effective_decision_spot(...)`` call or the ``_decision_spot``
    local bound from it."""
    if isinstance(first, ast.Name) and first.id == "_decision_spot":
        return True
    return (isinstance(first, ast.Call) and isinstance(first.func, ast.Attribute)
            and first.func.attr == "_effective_decision_spot")


def test_live_executing_compute_fed_by_effective_spot():
    """Within ``scan()`` (the LIVE decision path), the TRADE-DETERMINING compute
    — the ``ProbabilityEngine.compute`` with a ``market_price_cents`` kwarg,
    whose ``calibrated_prob`` becomes ``final_prob`` — must route its spot
    through ``_effective_decision_spot``, NOT bare ``spot``. Catches the R1-C1
    class (RTI wired into the pre-market screen but not the executed trade).

    Scope is ``scan()`` only: the ``_process_*_shadow`` methods have their own
    market-price computes that INTENTIONALLY stay on Coinbase spot (shadow
    strategies keep their own basis; RTI is logged separately, not fed)."""
    scan = _scan_fn(ast.parse(SCANNER_PY.read_text()))
    assert scan is not None, "scan() not found"
    executing = [c for c in _probability_compute_calls(scan)
                 if any(kw.arg == "market_price_cents" for kw in c.keywords)]
    assert executing, "no market-price (executing) compute inside scan()"
    for call in executing:
        first = call.args[0] if call.args else None
        assert not (isinstance(first, ast.Name) and first.id == "spot"), (
            "scan() executing compute fed bare `spot` — RTI gate bypassed (R1-C1)")
        assert _is_gated_spot(first), (
            "scan() executing compute must route spot through _effective_decision_spot")


def test_live_screen_compute_also_uses_effective_spot():
    """The pre-market screen compute inside ``scan()`` (no market_price_cents)
    must also use the gated spot, so screen and executed trade agree."""
    scan = _scan_fn(ast.parse(SCANNER_PY.read_text()))
    assert scan is not None, "scan() not found"
    screen = [c for c in _probability_compute_calls(scan)
              if not any(kw.arg == "market_price_cents" for kw in c.keywords)]
    assert any(_is_gated_spot(c.args[0]) for c in screen if c.args), (
        "no screen compute in scan() is fed the gated effective spot")


def test_executor_addon_computes_routed_through_gate():
    """Executor add-on / scale-in computes (LIVE position scaling) must route
    their spot through the RTI gate (``_addon_decision_spot`` →
    ``_effective_decision_spot``), so a scaled position is priced on the same
    effective spot as its entry. Catches the R2-M1 class (entry on RTI spot but
    scale-in on Coinbase spot once an asset is promoted)."""
    calls = _probability_compute_calls(ast.parse(EXECUTOR_PY.read_text()))
    assert calls, "no ProbabilityEngine.compute found in executor"
    for call in calls:
        first = call.args[0] if call.args else None
        assert not (isinstance(first, ast.Name) and first.id == "spot"), (
            "executor compute fed bare `spot` — add-on bypasses the RTI gate (R2-M1)")
        assert (isinstance(first, ast.Call) and isinstance(first.func, ast.Attribute)
                and first.func.attr == "_addon_decision_spot"), (
            "executor compute must route spot through _addon_decision_spot(...)")
