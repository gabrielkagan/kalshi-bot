"""D1.11.a — ``scripts/ops/collector_health_monitor.py`` extends to a
5th tier: ``kalshi-espn-collector``.

Ticket `86ba0ppy0` (2026-05-19). The post-D1.8 monitor has 4 tiers:
  - kalshi-collector (dedup prefix `d1_6`)
  - kalshi-coinbase-collector (dedup prefix `d2_5`)
  - kalshi-bot (dedup prefix `b3_fu3`)
  - kalshi-weather-collector (dedup prefix `d1_8`)

D1.11.a adds a 5th tier: kalshi-espn-collector with dedup prefix `d1_11`.

ESPN collector subset of checks (mirrors weather — both are HTTP-poll):
  - check_disk: YES (filesystem may fill if rclone stalls)
  - check_collector_active: YES (systemctl is-active)
  - check_dropped_frames: YES (BronzeArchiver-style worker queue)
  - check_ws_reconnects: NO (HTTP polling has no persistent WS conn;
    ESPN rate-limit responses surface as captured bronze records, not
    as `ws_disconnected` log lines)
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_FILE = REPO_ROOT / "scripts" / "ops" / "collector_health_monitor.py"


def _module_source() -> str:
    return MONITOR_FILE.read_text()


def test_espn_collector_unit_constant_defined():
    """A module-level constant for the ESPN collector unit name
    must exist, mirroring DEFAULT_COLLECTOR_UNIT / COINBASE_COLLECTOR_UNIT
    / WEATHER_COLLECTOR_UNIT.
    """
    src = _module_source()
    assert (
        'ESPN_COLLECTOR_UNIT' in src
        and '"kalshi-espn-collector"' in src
    ), (
        "scripts/ops/collector_health_monitor.py missing an "
        "ESPN_COLLECTOR_UNIT constant referencing 'kalshi-espn-collector'. "
        "Mirrors the D1.6 DEFAULT_COLLECTOR_UNIT / D2.5 "
        "COINBASE_COLLECTOR_UNIT / D1.8 WEATHER_COLLECTOR_UNIT pattern."
    )


def test_espn_tier_added_to_main_dispatch():
    """The main() dispatch must include a 'kalshi-espn-collector' tier
    with dedup prefix 'd1_11'. String-search the source for the literal
    tier-tuple shape.
    """
    src = _module_source()
    assert '"kalshi-espn-collector"' in src, (
        "main() dispatch missing 'kalshi-espn-collector' tier — "
        "without this, the cron-driven monitor never polls the "
        "ESPN collector's health surfaces."
    )
    assert '"d1_11"' in src, (
        "main() dispatch missing 'd1_11' dedup prefix — without a "
        "per-tier prefix the ESPN alerts would collide with "
        "Kalshi/Coinbase/bot/weather tiers and dedup-suppress signal."
    )


def test_espn_checks_subset_excludes_ws_reconnects():
    """The ESPN check list must NOT include check_ws_reconnects.

    HTTP polling has no persistent WS connection, so a `ws_disconnected`
    log-line filter would never match. Mirrors the D1.8 weather subset
    rationale.
    """
    src = _module_source()
    assert '"kalshi-espn-collector"' in src, (
        "Module missing 'kalshi-espn-collector' tier reference."
    )
    espn_checks_idx = src.find("espn_checks = [")
    if espn_checks_idx == -1:
        espn_checks_idx = src.find("espn_checks=[")
    assert espn_checks_idx != -1, (
        "Module missing `espn_checks = [...]` block — the ESPN "
        "tier needs its own per-tier check list (subset of the Kalshi/"
        "Coinbase per-tier blocks, matching D1.8 weather subset)."
    )
    bracket_depth = 0
    end_idx = espn_checks_idx
    for i in range(espn_checks_idx, min(len(src), espn_checks_idx + 2000)):
        if src[i] == "[":
            bracket_depth += 1
        elif src[i] == "]":
            bracket_depth -= 1
            if bracket_depth == 0:
                end_idx = i + 1
                break
    espn_block = src[espn_checks_idx:end_idx]
    assert '"ws_reconnects"' not in espn_block, (
        "espn_checks block must NOT include ws_reconnects — HTTP "
        "polling has no persistent WS connection; the filter would "
        "never match log lines (always-OK false negative)."
    )


def test_espn_checks_include_disk_active_dropped_frames():
    """The 3 original ESPN checks (disk + collector_active + dropped_frames)
    must remain; 86bbvqhyr added a 4th (http_errors), pinned by
    test_collector_health_monitor_espn_http_errors.py."""
    src = _module_source()
    espn_checks_idx = src.find("espn_checks = [")
    if espn_checks_idx == -1:
        espn_checks_idx = src.find("espn_checks=[")
    assert espn_checks_idx != -1, "espn_checks list missing"
    bracket_depth = 0
    end_idx = espn_checks_idx
    for i in range(espn_checks_idx, min(len(src), espn_checks_idx + 2000)):
        if src[i] == "[":
            bracket_depth += 1
        elif src[i] == "]":
            bracket_depth -= 1
            if bracket_depth == 0:
                end_idx = i + 1
                break
    espn_block = src[espn_checks_idx:end_idx]
    for check_name in ('"disk"', '"collector_active"', '"dropped_frames"'):
        assert check_name in espn_block, (
            f"espn_checks block missing {check_name} entry. The "
            f"ESPN tier needs disk + collector_active + dropped_frames "
            f"(matching D1.8 weather subset per D1.11.a plan-doc "
            f"decision #10)."
        )


def test_espn_paths_bronze_and_sidecar_constants():
    """Constants for the ESPN bronze root + sidecar paths must be
    defined at module level, mirroring the Kalshi DEFAULT_* + Coinbase
    COINBASE_* + Weather WEATHER_* prefix conventions.
    """
    src = _module_source()
    assert (
        "ESPN_BRONZE_ROOT" in src
        or "/var/lib/kalshi-espn-collector" in src
    ), (
        "Module must define ESPN_BRONZE_ROOT or reference "
        "'/var/lib/kalshi-espn-collector' bronze root — the disk "
        "check needs a path target."
    )
