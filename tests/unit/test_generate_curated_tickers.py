"""Unit tests for scripts/ops/generate_curated_tickers.py (RCA-G).

Pins the structural shape of the curated-tickers generator so a future
edit that drifts the universe-prefix list from the bot's actual analyzed
series fails RED before reaching production.

Three drift surfaces guarded:

  1. Every crypto SERIES_TICKERS entry in bot/constants.py has a
     matching prefix in BOT_SERIES_PREFIXES (15M and hourly).
  2. Every weather city in bot/engines/weather_engine.py CITIES has a
     matching prefix in BOT_SERIES_PREFIXES.
  3. KXINXU- prefix is present (mirrors bot/engines/spx_engine.py
     SPX_SERIES_TICKER="KXINXU"; legacy KXSPX patterns in bot/state.py
     are historical backfill classifiers that capture zero current rows).

Plus behavioral tests of the filter function + main CLI.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ops" / "generate_curated_tickers.py"


def _load_script_module():
    """Import the script as a module so we can call its functions directly."""
    spec = importlib.util.spec_from_file_location(
        "generate_curated_tickers", SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Drift pins ───────────────────────────────────────────────────────────────


def test_crypto_15m_prefixes_match_bot_constants():
    """Every 15M series in bot.constants.SERIES_TICKERS has a curated prefix.

    If the bot promotes a new crypto asset (e.g. ADA), SERIES_TICKERS
    grows. This test fails RED until the operator adds the matching
    ``KXADA15M-`` prefix here.
    """
    from bot.constants import SERIES_TICKERS

    mod = _load_script_module()
    for asset, series in SERIES_TICKERS.items():
        expected_prefix = series + "-"
        assert expected_prefix in mod.BOT_SERIES_PREFIXES, (
            f"BOT_SERIES_PREFIXES missing {expected_prefix!r} for "
            f"asset={asset!r}. bot.constants.SERIES_TICKERS added a new "
            f"15M series; update the script's prefix list in lockstep."
        )


def test_crypto_hourly_prefixes_match_bot_constants():
    """Every hourly series in bot.constants.HOURLY_SERIES_TICKERS has a curated prefix."""
    from bot.constants import HOURLY_SERIES_TICKERS

    mod = _load_script_module()
    for asset, series in HOURLY_SERIES_TICKERS.items():
        expected_prefix = series + "-"
        assert expected_prefix in mod.BOT_SERIES_PREFIXES, (
            f"BOT_SERIES_PREFIXES missing {expected_prefix!r} for "
            f"asset={asset!r}. bot.constants.HOURLY_SERIES_TICKERS added "
            f"a new hourly series; update the script's prefix list in "
            f"lockstep."
        )


def test_weather_city_prefixes_match_engine_config():
    """Every weather city in weather_engine has a curated prefix.

    Reads ``bot.engines.weather_engine`` city config and verifies each
    series_ticker has a matching ``<series>-`` prefix in
    BOT_SERIES_PREFIXES.
    """
    from bot.engines import weather_engine

    # The config dict lives at module level; find it via attribute scan.
    city_series = set()
    for attr_name in dir(weather_engine):
        value = getattr(weather_engine, attr_name, None)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict) and "series_ticker" in entry:
                    city_series.add(entry["series_ticker"])
        elif isinstance(value, dict):
            # nested-dict shape: dict-of-dicts where inner has series_ticker
            for inner in value.values():
                if isinstance(inner, dict) and "series_ticker" in inner:
                    city_series.add(inner["series_ticker"])

    assert city_series, (
        "Could not extract weather city series from "
        "bot.engines.weather_engine. The data shape may have changed; "
        "update this test's scan logic in lockstep."
    )

    mod = _load_script_module()
    for series in city_series:
        expected_prefix = series + "-"
        assert expected_prefix in mod.BOT_SERIES_PREFIXES, (
            f"BOT_SERIES_PREFIXES missing {expected_prefix!r}. "
            f"weather_engine added a new city {series!r}; update the "
            f"script's prefix list in lockstep."
        )


def test_spx_prefix_matches_spx_engine_series_ticker():
    """KXINXU- prefix matches the LIVE Kalshi SPX series.

    bot/engines/spx_engine.py:27 declares ``SPX_SERIES_TICKER = "KXINXU"``;
    the engine queries Kalshi via ``series_ticker=SPX_SERIES_TICKER``
    (see :1107-1123). The legacy ``KXSPX*`` patterns at
    bot/state.py:988/:1014/:1704 are historical backfill classifiers
    that capture zero current rows since Kalshi renamed SPX → KXINXU.

    This test pins the script's SPX prefix against the live source
    (spx_engine.SPX_SERIES_TICKER) — if Kalshi renames again, both must
    update in lockstep.
    """
    from bot.engines.spx_engine import SPX_SERIES_TICKER

    mod = _load_script_module()
    expected_prefix = SPX_SERIES_TICKER + "-"
    assert expected_prefix in mod.BOT_SERIES_PREFIXES, (
        f"BOT_SERIES_PREFIXES missing {expected_prefix!r} (derived from "
        f"bot.engines.spx_engine.SPX_SERIES_TICKER={SPX_SERIES_TICKER!r}). "
        f"If Kalshi renamed the SPX series, update both sides in lockstep."
    )


# ── Filter logic ─────────────────────────────────────────────────────────────


def test_filter_curated_includes_matching_prefix():
    mod = _load_script_module()
    tickers = [
        "KXBTC15M-26MAY200030-50000",
        "KXMVESPORTSMULTIGAMEEXTENDED-X",
        "KXBTCD-26MAY1920-T70000",
        "KXMVECROSSCATEGORY-Y",
    ]
    result = mod.filter_curated(tickers, mod.BOT_SERIES_PREFIXES)
    assert "KXBTC15M-26MAY200030-50000" in result
    assert "KXBTCD-26MAY1920-T70000" in result


def test_filter_curated_excludes_non_matching_prefix():
    mod = _load_script_module()
    tickers = [
        "KXBTC15M-26MAY200030-50000",
        "KXMVESPORTSMULTIGAMEEXTENDED-X",
        "KXMVECROSSCATEGORY-Y",
    ]
    result = mod.filter_curated(tickers, mod.BOT_SERIES_PREFIXES)
    # esports + cross-category must be excluded
    assert "KXMVESPORTSMULTIGAMEEXTENDED-X" not in result
    assert "KXMVECROSSCATEGORY-Y" not in result


def test_filter_curated_returns_sorted_unique():
    mod = _load_script_module()
    tickers = [
        "KXBTC15M-26MAY200030-50000",
        "KXBTC15M-26MAY200030-50000",  # duplicate
        "KXBTC15M-26MAY200000-50000",
    ]
    result = mod.filter_curated(tickers, ["KXBTC15M-"])
    assert result == [
        "KXBTC15M-26MAY200000-50000",
        "KXBTC15M-26MAY200030-50000",
    ], "filter_curated must return sorted + deduped output"


def test_filter_curated_prefix_does_not_match_lookalike_series():
    """``KXBTC15M-`` must NOT match a hypothetical ``KXBTC15MFOO-`` series.

    Trailing hyphen is load-bearing — it locks the prefix to exact
    series matching. Without the hyphen, ``KXBTC15M`` would partially
    match a sibling series like ``KXBTC15MFOO``.
    """
    mod = _load_script_module()
    tickers = ["KXBTC15MFOO-26MAY200030-50000"]
    result = mod.filter_curated(tickers, ["KXBTC15M-"])
    assert result == [], (
        "KXBTC15M- (with hyphen) MUST NOT match KXBTC15MFOO-; the "
        "trailing hyphen is the series-boundary delimiter."
    )


def test_kxinxu_prefix_matches_live_spx_tickers():
    """``KXINXU-`` matches Kalshi's live SPX ticker shape.

    Real KXINXU tickers look like ``KXINXU-26MAY192100-T5750`` (event-day
    + threshold). The script's hyphen-suffixed prefix locks the match to
    the KXINXU series and would NOT capture a hypothetical lookalike like
    ``KXINXUFOO-``.
    """
    mod = _load_script_module()
    tickers = [
        "KXINXU-26MAY192100-T5750",
        "KXINXU-26MAY192100-T5800",
        "KXINXUFOO-X",  # lookalike — must NOT match
        "KXSPX-LEGACY-X",  # legacy historical — must NOT match
    ]
    result = mod.filter_curated(tickers, ["KXINXU-"])
    assert "KXINXU-26MAY192100-T5750" in result
    assert "KXINXU-26MAY192100-T5800" in result
    assert "KXINXUFOO-X" not in result
    assert "KXSPX-LEGACY-X" not in result


# ── CLI entry point ──────────────────────────────────────────────────────────


def test_main_errors_on_missing_credentials(monkeypatch, capsys):
    """Without API key or key path, main exits 2 with a clear error."""
    monkeypatch.delenv("KALSHI_COLLECTOR_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_COLLECTOR_KEY_PATH", raising=False)

    mod = _load_script_module()
    rc = mod.main([])
    assert rc == 2, "Missing credentials must exit code 2."
    err = capsys.readouterr().err
    assert "KALSHI_COLLECTOR_KEY_ID" in err and "KALSHI_COLLECTOR_KEY_PATH" in err


def test_main_writes_output_atomically(tmp_path, monkeypatch):
    """``main`` writes to ``<path>.tmp`` then os.replace's to final path.

    The atomic-rename is load-bearing: the cron-driven refresh must
    never expose a partial JSON file to the collector reader (which
    parses on every boot via ``COLLECTOR_TICKERS_FILE``).
    """
    output_path = tmp_path / "curated.json"
    mod = _load_script_module()

    fake_result = {mod.TIER_ALL: [
        "KXBTC15M-26MAY200030-50000",
        "KXMVESPORTSMULTIGAMEEXTENDED-Y",
    ]}

    # Stub the network + auth dependencies via patch.dict on sys.modules
    # to inject lightweight fakes (the lazy import in main() resolves
    # against sys.modules).
    fake_auth = type(sys)("fake_auth")
    fake_auth.load_private_key = lambda _: object()
    fake_rest = type(sys)("fake_rest")
    fake_rest.fetch_tickers_by_tier = lambda **_: fake_result

    monkeypatch.setitem(sys.modules, "kalshi_wire.auth", fake_auth)
    monkeypatch.setitem(sys.modules, "collector.rest_snapshot", fake_rest)

    rc = mod.main([
        "--output", str(output_path),
        "--api-key", "K",
        "--key-path", "/nonexistent.pem",
    ])
    assert rc == 0, "Successful write must return 0"
    assert output_path.exists(), "Output file must exist post-write"

    data = json.loads(output_path.read_text())
    assert mod.TIER_ALL in data
    assert "KXBTC15M-26MAY200030-50000" in data[mod.TIER_ALL]
    # Esports excluded
    assert "KXMVESPORTSMULTIGAMEEXTENDED-Y" not in data[mod.TIER_ALL]


def test_main_fetch_failure_does_not_overwrite_existing(tmp_path, monkeypatch):
    """If fetch_tickers_by_tier returns None, the prior curated file is preserved.

    Load-bearing: a transient Kalshi REST failure must NOT clobber the
    operator's working curated file with an empty/partial one. The
    collector reads the file on next boot; corrupting it would cause
    silent under-subscription.
    """
    output_path = tmp_path / "curated.json"
    prior_content = json.dumps({"1": ["KXBTC15M-PRIOR"]})
    output_path.write_text(prior_content)

    mod = _load_script_module()

    fake_auth = type(sys)("fake_auth")
    fake_auth.load_private_key = lambda _: object()
    fake_rest = type(sys)("fake_rest")
    fake_rest.fetch_tickers_by_tier = lambda **_: None  # transport failure

    monkeypatch.setitem(sys.modules, "kalshi_wire.auth", fake_auth)
    monkeypatch.setitem(sys.modules, "collector.rest_snapshot", fake_rest)

    rc = mod.main([
        "--output", str(output_path),
        "--api-key", "K",
        "--key-path", "/nonexistent.pem",
    ])
    assert rc == 3, "Fetch failure must return exit code 3"
    # Prior file content must be untouched
    assert output_path.read_text() == prior_content, (
        "Fetch failure must NOT overwrite the prior curated file; "
        "the operator's working file is the load-bearing artifact for "
        "the collector's next boot."
    )


def test_main_dry_run_does_not_write_file(tmp_path, monkeypatch, capsys):
    """``--dry-run`` prints sample to stdout WITHOUT writing the output file."""
    output_path = tmp_path / "curated.json"
    mod = _load_script_module()

    fake_auth = type(sys)("fake_auth")
    fake_auth.load_private_key = lambda _: object()
    fake_rest = type(sys)("fake_rest")
    fake_rest.fetch_tickers_by_tier = lambda **_: {
        mod.TIER_ALL: ["KXBTC15M-X"],
    }

    monkeypatch.setitem(sys.modules, "kalshi_wire.auth", fake_auth)
    monkeypatch.setitem(sys.modules, "collector.rest_snapshot", fake_rest)

    rc = mod.main([
        "--output", str(output_path),
        "--api-key", "K",
        "--key-path", "/nonexistent.pem",
        "--dry-run",
    ])
    assert rc == 0
    assert not output_path.exists(), (
        "--dry-run must NOT write the output file."
    )
    out = capsys.readouterr().out
    assert "KXBTC15M-X" in out, "--dry-run must print the sample to stdout"


# ── Schema sanity ────────────────────────────────────────────────────────────


def test_tier_all_constant_matches_collector():
    """The script's TIER_ALL must match collector.rest_snapshot.TIER_ALL.

    Load-bearing: the collector reads the file with ``json.load`` and
    looks for the ``TIER_ALL`` key — a mismatch silently maps the file
    to the wrong tier and the collector subscribes to zero tickers.
    """
    from collector.rest_snapshot import TIER_ALL as COLLECTOR_TIER_ALL

    mod = _load_script_module()
    assert mod.TIER_ALL == COLLECTOR_TIER_ALL, (
        f"Script TIER_ALL={mod.TIER_ALL!r} drifted from collector "
        f"TIER_ALL={COLLECTOR_TIER_ALL!r}. The two MUST stay in lockstep."
    )
