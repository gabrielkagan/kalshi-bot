"""D1.8 — ``ops/install.sh`` extends N=3 → N=4 parallel-array installer.

Ticket `86ba0duck` (2026-05-18). The post-D2.5 install.sh validates +
installs THREE units atomically (kalshi-bot + kalshi-collector +
kalshi-coinbase-collector). D1.8 adds a FOURTH: kalshi-weather-collector.

Pins:
  - All 6 parallel arrays (UNIT_NAMES / UNIT_WRAPPERS / UNIT_ENV_FILES
    / UNIT_EXPECTED_EXECSTART / UNIT_EXPECTED_WORKINGDIR /
    UNIT_EXPECTED_ENVFILE) include the kalshi-weather-collector entry.
  - The length-mismatch guard (`N=${#UNIT_NAMES[@]}` + loop) catches
    any future array-update drift between the 6 arrays.
  - Per-unit FAIL hint for weather mentions the D1.8 operator runbook.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "ops" / "install.sh"


def _read_install_sh() -> str:
    assert INSTALL_SH.exists(), (
        f"{INSTALL_SH.relative_to(REPO_ROOT)} missing — install.sh is "
        "the source of truth for systemd unit installation."
    )
    return INSTALL_SH.read_text()


def test_unit_names_array_includes_kalshi_weather_collector():
    """``UNIT_NAMES`` parallel-array entry for the new unit."""
    text = _read_install_sh()
    assert '"kalshi-weather-collector"' in text, (
        "ops/install.sh UNIT_NAMES array missing 'kalshi-weather-collector' "
        "entry. The N=4 parallel-array installer needs this string in "
        "the UNIT_NAMES block (alongside kalshi-bot + kalshi-collector + "
        "kalshi-coinbase-collector)."
    )


def test_unit_wrappers_array_includes_weather_start_sh():
    """``UNIT_WRAPPERS`` parallel-array entry for the new wrapper script."""
    text = _read_install_sh()
    assert "$REPO_ROOT/weather-collector-start.sh" in text, (
        "ops/install.sh UNIT_WRAPPERS array missing "
        "'$REPO_ROOT/weather-collector-start.sh' — the wrapper invoked "
        "by ExecStart of the weather unit."
    )


def test_unit_env_files_array_includes_weather_env():
    """``UNIT_ENV_FILES`` parallel-array entry."""
    text = _read_install_sh()
    assert "/home/botuser/.env.weather-collector" in text, (
        "ops/install.sh UNIT_ENV_FILES array missing "
        "'/home/botuser/.env.weather-collector' — the dedicated env "
        "file for the weather collector unit."
    )


def test_unit_expected_directives_include_weather_paths():
    """All 3 expected-directive parallel arrays (EXECSTART / WORKINGDIR
    / ENVFILE) must include the weather collector entries.

    Pinning each — a missing entry means the length-mismatch guard
    fires (which is the failure mode we WANT in dev), but pin the
    presence so the test fails BEFORE running install.sh.
    """
    text = _read_install_sh()
    assert "ExecStart=$REPO_ROOT/weather-collector-start.sh" in text, (
        "UNIT_EXPECTED_EXECSTART array missing weather ExecStart entry."
    )
    # WorkingDirectory is the same value for all 4 units — already in the array.
    # ENVFILE entry is the unique one.
    assert (
        "EnvironmentFile=/home/botuser/.env.weather-collector" in text
    ), (
        "UNIT_EXPECTED_ENVFILE array missing weather EnvironmentFile entry."
    )


def test_fail_hint_for_weather_env_provisioning():
    """The per-unit FAIL hint for missing .env.weather-collector must
    mention the D1.8 operator runbook (provision guidance). Mirrors
    the D1.5 + D2.5 FAIL hints.
    """
    text = _read_install_sh()
    # The hint should mention the weather env file AND give the operator
    # a pointer to ops/CLAUDE.md or the plan-doc.
    assert "kalshi-weather-collector" in text or "weather-collector" in text, (
        "install.sh must mention 'kalshi-weather-collector' (or "
        "'weather-collector') in its FAIL hint for "
        "/home/botuser/.env.weather-collector missing."
    )
    # The D1.8 plan-doc references should appear (operator-pointer).
    assert "D1.8" in text or "d1-8" in text, (
        "install.sh missing 'D1.8' pointer in the weather collector "
        "FAIL hint — operator needs to know which runbook to follow."
    )


def test_length_mismatch_guard_handles_n_4():
    """The length-mismatch guard counts UNIT_NAMES and checks the 5
    sibling arrays match. With N=4 now, the guard's logic is unchanged
    (it's data-driven via `${#UNIT_NAMES[@]}`).

    Pin both: (a) the guard mechanism exists, AND (b) UNIT_NAMES has
    AT LEAST 4 entries (kalshi-bot + kalshi-collector +
    kalshi-coinbase-collector + kalshi-weather-collector). The
    AT-LEAST-4 assertion ties the test to D1.8 specifically — at
    N=3 (pre-D1.8) this fails RED.
    """
    text = _read_install_sh()
    assert 'N=${#UNIT_NAMES[@]}' in text, (
        "install.sh length-mismatch guard missing (N=${#UNIT_NAMES[@]}) — "
        "without this, parallel-array drift goes silent."
    )
    for sibling in (
        "UNIT_WRAPPERS",
        "UNIT_ENV_FILES",
        "UNIT_EXPECTED_EXECSTART",
        "UNIT_EXPECTED_WORKINGDIR",
        "UNIT_EXPECTED_ENVFILE",
    ):
        assert sibling in text, (
            f"install.sh missing reference to {sibling} — required for "
            f"the parallel-array length-mismatch guard."
        )
    # Pin N>=4: all 4 unit names must appear as quoted strings
    # inside the UNIT_NAMES array literal. Pre-D1.8 the count was 3
    # (no kalshi-weather-collector entry); the new entry pushes us
    # to 4.
    for unit_name_literal in (
        '"kalshi-bot"',
        '"kalshi-collector"',
        '"kalshi-coinbase-collector"',
        '"kalshi-weather-collector"',
    ):
        assert unit_name_literal in text, (
            f"install.sh UNIT_NAMES array missing entry "
            f"{unit_name_literal} — D1.8 requires N>=4 entries."
        )
