"""D1.8 — ``scripts/ops/collector_health_monitor.py`` extends to a 4th
tier: ``kalshi-weather-collector``.

Ticket `86ba0duck` (2026-05-18). The post-B3-fu3 monitor has 3 tiers:
  - kalshi-collector (dedup prefix `d1_6`)
  - kalshi-coinbase-collector (dedup prefix `d2_5`)
  - kalshi-bot (dedup prefix `b3_fu3`)

D1.8 adds a 4th tier: kalshi-weather-collector with dedup prefix `d1_8`.

Weather collector subset of checks (vs Kalshi/Coinbase):
  - check_disk: YES (filesystem may fill if rclone stalls)
  - check_collector_active: YES (systemctl is-active)
  - check_dropped_frames: YES (BronzeArchiver-style worker queue)
  - check_ws_reconnects: NO (HTTP polling has no persistent WS conn;
    Open-Meteo 429 storms surface as captured bronze records, not
    as `ws_disconnected` log lines)
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_FILE = REPO_ROOT / "scripts" / "ops" / "collector_health_monitor.py"


def _module_source() -> str:
    return MONITOR_FILE.read_text()


def test_weather_collector_unit_constant_defined():
    """A module-level constant for the weather collector unit name
    must exist, mirroring DEFAULT_COLLECTOR_UNIT / COINBASE_COLLECTOR_UNIT.
    """
    src = _module_source()
    assert (
        'WEATHER_COLLECTOR_UNIT' in src
        and '"kalshi-weather-collector"' in src
    ), (
        "scripts/ops/collector_health_monitor.py missing a "
        "WEATHER_COLLECTOR_UNIT constant referencing 'kalshi-weather-collector'. "
        "Mirrors the D1.6 DEFAULT_COLLECTOR_UNIT / D2.5 "
        "COINBASE_COLLECTOR_UNIT pattern."
    )


def test_weather_tier_added_to_main_dispatch():
    """The main() dispatch must include a 'kalshi-weather-collector' tier
    with dedup prefix 'd1_8'. String-search the source for the literal
    tier-tuple shape.
    """
    src = _module_source()
    # The tiers list shape (per current code, line ~599):
    #     tiers = [
    #         ("kalshi-collector", "d1_6", kalshi_checks),
    #         ("kalshi-coinbase-collector", "d2_5", coinbase_checks),
    #         ("kalshi-bot", "b3_fu3", bot_checks),
    #     ]
    # We expect a fourth entry referencing the weather collector.
    assert '"kalshi-weather-collector"' in src, (
        "main() dispatch missing 'kalshi-weather-collector' tier — "
        "without this, the cron-driven monitor never polls the "
        "weather collector's health surfaces."
    )
    assert '"d1_8"' in src, (
        "main() dispatch missing 'd1_8' dedup prefix — without a "
        "per-tier prefix the weather alerts would collide with "
        "Kalshi/Coinbase/bot tiers and dedup-suppress signal."
    )


def test_weather_checks_subset_excludes_ws_reconnects():
    """The weather check list must NOT include check_ws_reconnects.

    HTTP polling has no persistent WS connection, so a `ws_disconnected`
    log-line filter would never match. Including it would produce an
    always-OK signal that masks real reconnect-storm classes on the
    OTHER tiers (the dedup prefix would prevent that, but the principle
    is to exclude irrelevant checks rather than rely on dedup hygiene).
    """
    src = _module_source()
    # We can't easily AST-parse the lambda checks list; the simplest
    # robust pin is to check that the weather-tier definition block
    # doesn't include "ws_reconnects" within a reasonable window.
    assert '"kalshi-weather-collector"' in src, (
        "Module missing 'kalshi-weather-collector' tier reference — "
        "see test_weather_tier_added_to_main_dispatch for the primary pin."
    )
    weather_checks_idx = src.find("weather_checks = [")
    if weather_checks_idx == -1:
        weather_checks_idx = src.find("weather_checks=[")
    assert weather_checks_idx != -1, (
        "Module missing `weather_checks = [...]` block — the weather "
        "tier needs its own per-tier check list (subset of the Kalshi/"
        "Coinbase per-tier blocks)."
    )
    # Find the end of the weather_checks list (closing ]).
    bracket_depth = 0
    end_idx = weather_checks_idx
    for i in range(weather_checks_idx, min(len(src), weather_checks_idx + 2000)):
        if src[i] == "[":
            bracket_depth += 1
        elif src[i] == "]":
            bracket_depth -= 1
            if bracket_depth == 0:
                end_idx = i + 1
                break
    weather_block = src[weather_checks_idx:end_idx]
    assert '"ws_reconnects"' not in weather_block, (
        "weather_checks block must NOT include ws_reconnects — HTTP "
        "polling has no persistent WS connection; the filter would "
        "never match log lines (always-OK false negative)."
    )


def test_weather_checks_include_disk_active_dropped_frames():
    """The 3 weather checks: disk + collector_active + dropped_frames."""
    src = _module_source()
    weather_checks_idx = src.find("weather_checks = [")
    if weather_checks_idx == -1:
        weather_checks_idx = src.find("weather_checks=[")
    assert weather_checks_idx != -1, "weather_checks list missing"
    bracket_depth = 0
    end_idx = weather_checks_idx
    for i in range(weather_checks_idx, min(len(src), weather_checks_idx + 2000)):
        if src[i] == "[":
            bracket_depth += 1
        elif src[i] == "]":
            bracket_depth -= 1
            if bracket_depth == 0:
                end_idx = i + 1
                break
    weather_block = src[weather_checks_idx:end_idx]
    for check_name in ('"disk"', '"collector_active"', '"dropped_frames"'):
        assert check_name in weather_block, (
            f"weather_checks block missing {check_name} entry. The "
            f"weather tier needs disk + collector_active + dropped_frames "
            f"(per D1.8 plan-doc decision #10)."
        )


def test_weather_paths_bronze_and_sidecar_constants():
    """Constants for the weather bronze root + sidecar paths must be
    defined at module level, mirroring the Kalshi DEFAULT_* + Coinbase
    COINBASE_* prefix conventions.
    """
    src = _module_source()
    # The weather bronze root + sidecar path. Either WEATHER_BRONZE_ROOT
    # constant OR explicit /var/lib/kalshi-weather-collector path string.
    assert (
        "WEATHER_BRONZE_ROOT" in src
        or "/var/lib/kalshi-weather-collector" in src
    ), (
        "Module must define WEATHER_BRONZE_ROOT or reference "
        "'/var/lib/kalshi-weather-collector' bronze root — the disk "
        "check needs a path target."
    )
