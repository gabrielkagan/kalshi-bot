"""B2a-1 — ``ops/install.sh`` extends N=5 → N=6 parallel-array installer.

Ticket `86ba1zf5j`. The post-D1.11.a install.sh validates + installs
FIVE units atomically (kalshi-bot + kalshi-collector +
kalshi-coinbase-collector + kalshi-weather-collector +
kalshi-espn-collector). B2a-1 adds a SIXTH: kalshi-venue-l2-collector.

Pins:
  - All 6 parallel arrays include the kalshi-venue-l2-collector entry.
  - The length-mismatch guard catches array-update drift between the 6
    arrays (N>=6 now).
  - The per-unit FAIL hint for venue-l2 mentions the B2a operator runbook.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "ops" / "install.sh"


def _read() -> str:
    assert INSTALL_SH.exists()
    return INSTALL_SH.read_text()


def test_unit_names_includes_venue_l2_collector():
    assert '"kalshi-venue-l2-collector"' in _read()


def test_unit_wrappers_includes_venue_l2_start_sh():
    assert "$REPO_ROOT/venue-l2-collector-start.sh" in _read()


def test_unit_env_files_includes_venue_l2_env():
    assert "/home/botuser/.env.venue-l2-collector" in _read()


def test_expected_directives_include_venue_l2_paths():
    text = _read()
    assert "ExecStart=$REPO_ROOT/venue-l2-collector-start.sh" in text
    assert "EnvironmentFile=/home/botuser/.env.venue-l2-collector" in text


def test_fail_hint_mentions_b2a_runbook():
    text = _read()
    assert "venue-l2-collector" in text
    assert "B2a" in text or "86ba1zf5j" in text, (
        "install.sh missing a B2a / ticket pointer in the venue-l2 "
        "collector FAIL hint — operator needs to know which runbook to "
        "follow to provision /home/botuser/.env.venue-l2-collector."
    )


def test_length_guard_handles_n_6():
    text = _read()
    assert "N=${#UNIT_NAMES[@]}" in text
    for sibling in (
        "UNIT_WRAPPERS",
        "UNIT_ENV_FILES",
        "UNIT_EXPECTED_EXECSTART",
        "UNIT_EXPECTED_WORKINGDIR",
        "UNIT_EXPECTED_ENVFILE",
    ):
        assert sibling in text
    for unit_name_literal in (
        '"kalshi-bot"',
        '"kalshi-collector"',
        '"kalshi-coinbase-collector"',
        '"kalshi-weather-collector"',
        '"kalshi-espn-collector"',
        '"kalshi-venue-l2-collector"',
    ):
        assert unit_name_literal in text, (
            f"install.sh UNIT_NAMES array missing {unit_name_literal} — "
            "B2a-1 requires N>=6 entries."
        )
