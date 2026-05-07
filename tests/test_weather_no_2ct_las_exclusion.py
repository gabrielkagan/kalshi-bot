"""TDD for weather NO 2-contract sizing + LAS exclusion (R-wx-no-2ct, 2026-05-04).

Data backing:
  39c+ all-time, ex-LAS: n=56, 57.1% WR, Wilson 95% LB 44.1% (vs 40c BE),
  PF 2.02, +$9.74, max DD $1.57. LAS within band: -$1.19 on 8 trades, 25% WR.

Mirrors the bleed-cell pattern at tests/test_bleed_cell_blocks.py: module-level
predicate function tested directly, plus AST/regex checks that bot/_impl.py wires the
predicate and the new sizing constant into the live-candidate gate.

Per CLAUDE.md: data collection unaffected. Excluded LAS still flows through the
shadow logging paths (this change only short-circuits the LIVE candidate
creation branch at bot/_impl.py:17633).
"""
from pathlib import Path
import re
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]


def _import_bot():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    # Mock heavy deps (mirrors tests/test_weather_no_side.py prelude)
    from unittest.mock import MagicMock
    for _mod in ["websockets", "websocket", "requests",
                 "cryptography", "cryptography.hazmat",
                 "cryptography.hazmat.primitives",
                 "cryptography.hazmat.primitives.serialization",
                 "cryptography.hazmat.primitives.hashes",
                 "cryptography.hazmat.primitives.asymmetric",
                 "cryptography.hazmat.primitives.asymmetric.padding"]:
        if _mod not in sys.modules:
            sys.modules[_mod] = MagicMock()
    import bot  # type: ignore
    return bot


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_weather_no_contract_count_constant_exists_and_is_2():
    bot = _import_bot()
    assert hasattr(bot, "WEATHER_NO_CONTRACT_COUNT"), \
        "WEATHER_NO_CONTRACT_COUNT must be a module-level constant"
    assert bot.WEATHER_NO_CONTRACT_COUNT == 2, \
        f"Expected 2 contracts, got {bot.WEATHER_NO_CONTRACT_COUNT}"
    assert isinstance(bot.WEATHER_NO_CONTRACT_COUNT, int)


def test_weather_no_excluded_prefixes_constant_exists_with_las():
    bot = _import_bot()
    assert hasattr(bot, "WEATHER_NO_EXCLUDED_CITY_PREFIXES"), \
        "WEATHER_NO_EXCLUDED_CITY_PREFIXES must be a module-level constant"
    excluded = bot.WEATHER_NO_EXCLUDED_CITY_PREFIXES
    assert "KXHIGHTLV" in excluded, \
        "Las Vegas (KXHIGHTLV) must be in the exclusion set"
    # frozenset is the standard for immutable membership constants in this repo
    assert isinstance(excluded, frozenset), \
        f"Expected frozenset, got {type(excluded).__name__}"


# ---------------------------------------------------------------------------
# Predicate behavior
# ---------------------------------------------------------------------------

def test_predicate_function_exists():
    bot = _import_bot()
    assert hasattr(bot, "should_exclude_weather_no_ticker"), \
        "should_exclude_weather_no_ticker(ticker) predicate must exist"


def test_predicate_excludes_las_ticker():
    bot = _import_bot()
    # Real ticker patterns from production data
    for ticker in (
        "KXHIGHTLV-26APR14-B74.5",
        "KXHIGHTLV-26MAY041800-A82",
        "KXHIGHTLV-26DEC25-T100",
    ):
        assert bot.should_exclude_weather_no_ticker(ticker) is True, \
            f"Expected exclusion for {ticker}"


def test_predicate_passes_other_weather_cities():
    bot = _import_bot()
    for ticker in (
        "KXHIGHNY-26MAR151200-A55",       # New York
        "KXHIGHMIA-26APR12-B85",           # Miami
        "KXHIGHLAX-26APR12-B82",           # LAX (different city; do NOT confuse with LAS)
        "KXHIGHTPHX-26APR12-B95",          # Phoenix
        "KXHIGHTSEA-26APR12-B70",          # Seattle
        "KXHIGHTBOS-26APR12-B65",          # Boston
        "KXHIGHCHI-26APR12-B72",           # Chicago
        "KXHIGHTDAL-26APR12-B88",          # Dallas
    ):
        assert bot.should_exclude_weather_no_ticker(ticker) is False, \
            f"Expected pass-through for {ticker}"


def test_predicate_no_partial_prefix_collision():
    """KXHIGHTLVXXX must NOT match — only the exact city prefix counts.

    Defensive against hypothetical future Kalshi ticker schemes that share a
    prefix substring with KXHIGHTLV.
    """
    bot = _import_bot()
    # Exact prefix without the dash separator should still be safe
    # because real tickers always have 'KXHIGHTLV-' followed by date.
    # But we want defense against e.g. "KXHIGHTLVX-..." or "KXHIGHTLVENICE-..."
    assert bot.should_exclude_weather_no_ticker("KXHIGHTLVX-26APR12") is False
    assert bot.should_exclude_weather_no_ticker("KXHIGHTLVENICE-26APR12") is False


def test_predicate_handles_empty_and_none_ticker():
    bot = _import_bot()
    # Defensive: empty string returns False (don't crash, don't match)
    assert bot.should_exclude_weather_no_ticker("") is False
    # None returns False (defensive)
    assert bot.should_exclude_weather_no_ticker(None) is False


def test_predicate_accepts_custom_exclusion_set():
    """Predicate must accept an override set for testability."""
    bot = _import_bot()
    custom = frozenset({"KXHIGHNY"})
    assert bot.should_exclude_weather_no_ticker(
        "KXHIGHNY-26APR12", excluded_prefixes=custom) is True
    assert bot.should_exclude_weather_no_ticker(
        "KXHIGHTLV-26APR12", excluded_prefixes=custom) is False
    # Empty set excludes nothing
    assert bot.should_exclude_weather_no_ticker(
        "KXHIGHTLV-26APR12", excluded_prefixes=frozenset()) is False


# ---------------------------------------------------------------------------
# Source-level wiring (AST/regex guards)
# ---------------------------------------------------------------------------

def _read_bot_source():
    return (REPO / "bot/_impl.py").read_text()


def test_source_replaces_position_size_1_with_constant_in_weather_no_live():
    """The two `position_size=1` literals at the weather_no_live candidate
    creation site MUST be replaced with WEATHER_NO_CONTRACT_COUNT."""
    src = _read_bot_source()
    # Find the weather_no_live block: starts at the comment, ends at the next
    # major comment block. Check no `position_size=1,` and no `"position_size": 1,`
    # within ~120 lines of the WEATHER_NO_CANDIDATE log line.
    log_idx = src.find('"WEATHER_NO_CANDIDATE: %s')
    assert log_idx != -1, "WEATHER_NO_CANDIDATE log line not found"
    # Walk backwards ~150 lines from the log to find the block start
    start = src.rfind("# ── Weather NO-side LIVE candidate", 0, log_idx)
    assert start != -1, "weather_no_live block start comment not found"
    block = src[start:log_idx + 200]
    # The block must NOT contain `position_size=1,` (kwarg) or `"position_size": 1,`
    assert "position_size=1," not in block, \
        "weather_no_live block still contains position_size=1 — must use WEATHER_NO_CONTRACT_COUNT"
    assert '"position_size": 1,' not in block, \
        'weather_no_live block still contains "position_size": 1 — must use WEATHER_NO_CONTRACT_COUNT'
    # The constant must appear at least twice (insert kwargs + candidate dict)
    assert block.count("WEATHER_NO_CONTRACT_COUNT") >= 2, \
        "WEATHER_NO_CONTRACT_COUNT must be referenced ≥ 2x in the live block"


def test_source_calls_exclusion_predicate_in_weather_no_live_gate():
    """The gate (weather_no_live conjunction) MUST short-circuit on
    should_exclude_weather_no_ticker(ticker). Anchored on the unique block
    comment so that only the gate region is searched, not the constant
    declaration block at the top of the module."""
    src = _read_bot_source()
    gate_start = src.find("# ── Weather NO-side LIVE candidate")
    assert gate_start != -1, "weather_no_live block start comment not found"
    # The conjunction ends at the first `):` after the block start
    gate_end = src.find("):", gate_start)
    assert gate_end != -1
    region = src[gate_start:gate_end + 2]
    assert "should_exclude_weather_no_ticker" in region, \
        "Gate conjunction must call should_exclude_weather_no_ticker(ticker)"


# ---------------------------------------------------------------------------
# Execution path: 2-contract candidate → place_order count=2
# ---------------------------------------------------------------------------

def _make_executor(bot):
    from unittest.mock import MagicMock
    client = MagicMock()
    state = MagicMock()
    logger = MagicMock()
    main_loop = MagicMock()
    kalshi_feed = MagicMock()
    kalshi_feed.is_connected = True
    kalshi_feed.pop_fills.return_value = []
    return bot.OrderExecutor(
        client=client, state=state, logger=logger,
        main_loop=main_loop, kalshi_feed=kalshi_feed,
    )


def _make_weather_no_2ct_candidate():
    return {
        "ticker": "KXHIGHNY-26MAR151200-A55",
        "event_ticker": "KXHIGHNY-26MAR151200",
        "asset": "NYC_TEMP",
        "best_yes_ask": 39,
        "position_size": 2,
        "calibrated_prob": 0.70,
        "edge": 0.31,
        "seconds_to_close": 36000,
        "strategy": "weather_no_live",
        "balance_at_scan": 50000,
        "spot": 55.0,
        "threshold": 50.0,
        "blended_rv": 0.01,
        "z_score": 0.0,
        "vol_regime": "normal",
        "kelly_f": 0.0,
        "product_type": "weather",
        "side": "no",
        "ofa_adjustment": 0.0,
        "ob_snapshot": {},
        "calibrated_prob_raw": 0.70,
        "drawdown_scaler": 1.0,
        "fee_adjusted_edge": 0.30,
    }


def test_execute_2ct_weather_no_routes_count_2_to_place_order():
    """An incoming candidate with position_size=2 must place an order with count=2."""
    bot = _import_bot()
    from unittest.mock import patch
    with patch.object(bot, "WEATHER_NO_SIDE_LIVE", True), \
         patch.object(bot, "OBSERVATION_MODE", False):
        ex = _make_executor(bot)
        candidate = _make_weather_no_2ct_candidate()
        ex._client.place_order.return_value = {
            "order": {"order_id": "test_2ct", "status": "executed",
                      "count_fp": "200", "yes_price_dollars": "0.61"}
        }

        ex.execute(candidate)

        ex._client.place_order.assert_called_once()
        call_kwargs = ex._client.place_order.call_args.kwargs
        assert call_kwargs["count"] == 2, \
            f"Expected count=2, got count={call_kwargs.get('count')}"
        assert call_kwargs["side"] == "no"
        assert "no_price" in call_kwargs


if __name__ == "__main__":
    import pytest as _pt
    _pt.main([__file__, "-v"])
