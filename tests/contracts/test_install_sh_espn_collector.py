"""D1.11.a — ``ops/install.sh`` extends N=4 → N=5 parallel-array installer.

Ticket `86ba0ppy0` (2026-05-19). The post-D1.8 install.sh validates +
installs FOUR units atomically (kalshi-bot + kalshi-collector +
kalshi-coinbase-collector + kalshi-weather-collector). D1.11.a adds a
FIFTH: kalshi-espn-collector.

Pins:
  - All 6 parallel arrays (UNIT_NAMES / UNIT_WRAPPERS / UNIT_ENV_FILES
    / UNIT_EXPECTED_EXECSTART / UNIT_EXPECTED_WORKINGDIR /
    UNIT_EXPECTED_ENVFILE) include the kalshi-espn-collector entry.
  - The length-mismatch guard (`N=${#UNIT_NAMES[@]}` + loop) catches
    any future array-update drift between the 6 arrays.
  - Per-unit FAIL hint for ESPN mentions the D1.11.a operator runbook.
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


def test_unit_names_array_includes_kalshi_espn_collector():
    """``UNIT_NAMES`` parallel-array entry for the new unit."""
    text = _read_install_sh()
    assert '"kalshi-espn-collector"' in text, (
        "ops/install.sh UNIT_NAMES array missing 'kalshi-espn-collector' "
        "entry. The N=5 parallel-array installer needs this string in "
        "the UNIT_NAMES block (alongside kalshi-bot + kalshi-collector + "
        "kalshi-coinbase-collector + kalshi-weather-collector)."
    )


def test_unit_wrappers_array_includes_espn_start_sh():
    """``UNIT_WRAPPERS`` parallel-array entry for the new wrapper script."""
    text = _read_install_sh()
    assert "$REPO_ROOT/espn-collector-start.sh" in text, (
        "ops/install.sh UNIT_WRAPPERS array missing "
        "'$REPO_ROOT/espn-collector-start.sh' — the wrapper invoked "
        "by ExecStart of the ESPN unit."
    )


def test_unit_env_files_array_includes_espn_env():
    """``UNIT_ENV_FILES`` parallel-array entry."""
    text = _read_install_sh()
    assert "/home/botuser/.env.espn-collector" in text, (
        "ops/install.sh UNIT_ENV_FILES array missing "
        "'/home/botuser/.env.espn-collector' — the dedicated env "
        "file for the ESPN collector unit."
    )


def test_unit_expected_directives_include_espn_paths():
    """All 3 expected-directive parallel arrays (EXECSTART / WORKINGDIR
    / ENVFILE) must include the ESPN collector entries.
    """
    text = _read_install_sh()
    assert "ExecStart=$REPO_ROOT/espn-collector-start.sh" in text, (
        "UNIT_EXPECTED_EXECSTART array missing ESPN ExecStart entry."
    )
    assert (
        "EnvironmentFile=/home/botuser/.env.espn-collector" in text
    ), (
        "UNIT_EXPECTED_ENVFILE array missing ESPN EnvironmentFile entry."
    )


def test_fail_hint_for_espn_env_provisioning():
    """The per-unit FAIL hint for missing .env.espn-collector must
    mention the D1.11.a operator runbook (provision guidance).
    Mirrors the D1.5 + D2.5 + D1.8 FAIL hints.
    """
    text = _read_install_sh()
    assert "kalshi-espn-collector" in text or "espn-collector" in text, (
        "install.sh must mention 'kalshi-espn-collector' (or "
        "'espn-collector') in its FAIL hint for "
        "/home/botuser/.env.espn-collector missing."
    )
    # The D1.11 plan-doc references should appear (operator-pointer).
    assert "D1.11" in text or "d1-11" in text, (
        "install.sh missing 'D1.11' pointer in the ESPN collector "
        "FAIL hint — operator needs to know which runbook to follow."
    )


def test_length_mismatch_guard_handles_n_5():
    """The length-mismatch guard counts UNIT_NAMES and checks the 5
    sibling arrays match. With N=5 now, the guard's logic is unchanged
    (it's data-driven via `${#UNIT_NAMES[@]}`).

    Pin both: (a) the guard mechanism exists, AND (b) UNIT_NAMES has
    AT LEAST 5 entries. The AT-LEAST-5 assertion ties the test to
    D1.11.a specifically — at N=4 (pre-D1.11.a) this fails RED.
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
    # Pin N>=5: all 5 unit names must appear as quoted strings
    # inside the UNIT_NAMES array literal.
    for unit_name_literal in (
        '"kalshi-bot"',
        '"kalshi-collector"',
        '"kalshi-coinbase-collector"',
        '"kalshi-weather-collector"',
        '"kalshi-espn-collector"',
    ):
        assert unit_name_literal in text, (
            f"install.sh UNIT_NAMES array missing entry "
            f"{unit_name_literal} — D1.11.a requires N>=5 entries."
        )
