"""Unit tests for scripts/postdeploy_verify.py (Tier 2 #4).

Verifies the script's building blocks so future edits don't silently
regress the verify logic. End-to-end test uses an in-memory DB that
mimics state.db shape.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "postdeploy_verify.py"


def _load_verify_module():
    """Import postdeploy_verify.py as a module without running main."""
    spec = importlib.util.spec_from_file_location("postdeploy_verify", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def verify():
    return _load_verify_module()


class TestLoadEnv:
    def test_missing_file_returns_empty(self, verify, tmp_path):
        missing = tmp_path / "nonexistent"
        assert verify.load_env(missing) == {}

    def test_parses_key_value(self, verify, tmp_path):
        f = tmp_path / ".env"
        f.write_text('FOO=bar\nBAZ="quoted"\nQUX=\'single\'\n# comment\n\nEMPTY=\n')
        env = verify.load_env(f)
        assert env == {"FOO": "bar", "BAZ": "quoted", "QUX": "single", "EMPTY": ""}


class TestFlagTruthy:
    @pytest.mark.parametrize("val,expected", [
        ("1", True), ("true", True), ("True", True), ("YES", True), ("on", True),
        ("0", False), ("false", False), ("", False), (None, False), ("random", False),
    ])
    def test_flag_truthy(self, verify, val, expected):
        assert verify.flag_truthy(val) is expected


class TestReadBotConstants:
    def test_parses_module_level_assignments(self, verify, tmp_path):
        f = tmp_path / "fake_bot" / "_impl.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            "import os\n"
            "WEATHER_NO_SIDE_LIVE = True  # comment\n"
            "SPORTS_OBSERVATION_ONLY = True\n"
            "HOURLY_LIVE_ENABLED = False\n"
            "MIN_ENTRY_PRICE = 75\n"
            "def fn():\n"
            "    LOCAL = True  # must not be captured\n"
        )
        flags = verify.read_bot_constants(f)
        assert flags["WEATHER_NO_SIDE_LIVE"] is True
        assert flags["SPORTS_OBSERVATION_ONLY"] is True
        assert flags["HOURLY_LIVE_ENABLED"] is False
        assert flags["MIN_ENTRY_PRICE"] == 75


class TestBuildChecks:
    def test_minimum_always_includes_scan_liveness(self, verify):
        checks = verify.build_checks(bot_flags={}, env={})
        assert any(c.name == "15m_scan_liveness" and c.strict for c in checks)

    def test_weather_flag_adds_weather_check(self, verify):
        checks_off = verify.build_checks(
            bot_flags={"WEATHER_NO_SIDE_LIVE": False}, env={})
        checks_on = verify.build_checks(
            bot_flags={"WEATHER_NO_SIDE_LIVE": True}, env={})
        names_off = {c.name for c in checks_off}
        names_on = {c.name for c in checks_on}
        assert "weather_no_side_rows" not in names_off
        assert "weather_no_side_rows" in names_on

    def test_hourly_live_env_adds_live_check(self, verify):
        checks = verify.build_checks(
            bot_flags={}, env={"HOURLY_LIVE_ENABLED": "1"})
        assert any(c.name == "hourly_live_candidates" for c in checks)

    def test_overnight_guard_applied(self, verify):
        checks = verify.build_checks(
            bot_flags={"OVERNIGHT_DISCOUNT_LIVE": True}, env={})
        c = next(c for c in checks if c.name == "overnight_discount_rows")
        # Wednesday noon UTC — outside 04-11 window
        wed_noon = datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)
        assert c.guard(wed_noon) is False
        # Wednesday 05:00 UTC — inside window
        wed_05 = datetime(2026, 4, 22, 5, 0, tzinfo=timezone.utc)
        assert c.guard(wed_05) is True
        # Saturday 05:00 UTC — weekend, outside
        sat_05 = datetime(2026, 4, 25, 5, 0, tzinfo=timezone.utc)
        assert c.guard(sat_05) is False

    def test_weekend_guard_applied(self, verify):
        checks = verify.build_checks(
            bot_flags={"WEEKEND_DISCOUNT_LIVE": True}, env={})
        c = next(c for c in checks if c.name == "weekend_discount_rows")
        sat = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
        wed = datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)
        assert c.guard(sat) is True
        assert c.guard(wed) is False


class TestResolveParams:
    def test_sentinels_substitute(self, verify):
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        got = verify._resolve_params(("__1H_AGO__",), now)
        assert got == ("2026-04-24T11:00:00+00:00",)

    def test_non_sentinel_passes_through(self, verify):
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        assert verify._resolve_params(("plain_string",), now) == ("plain_string",)


# ───────────────── End-to-end with in-memory DB ─────────────────

def _setup_stub_db(path: Path):
    """Build a minimal DB with the columns our queries hit."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_type TEXT, filter_stage TEXT, side TEXT,
            evaluation_time TEXT
        );
        CREATE TABLE sports_shadow_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluation_time TEXT
        );
    """)
    conn.commit()
    return conn


class TestEndToEnd:
    def test_strict_fail_when_scan_liveness_empty(self, verify, tmp_path):
        db = tmp_path / "state.db"
        _setup_stub_db(db).close()
        rc = verify.run_checks(
            db_path=db,
            checks=verify.build_checks(bot_flags={}, env={}),
            strict_all=False, dry_run=False,
        )
        assert rc == 1  # 15m_scan_liveness is strict and empty

    def test_strict_pass_when_scan_has_rows(self, verify, tmp_path):
        db = tmp_path / "state.db"
        conn = _setup_stub_db(db)
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=1)).isoformat()
        for _ in range(15):
            conn.execute(
                "INSERT INTO evaluated_opportunities "
                "(product_type, filter_stage, side, evaluation_time) "
                "VALUES ('15m', 'candidate', 'yes', ?)",
                (recent,),
            )
        conn.commit()
        conn.close()

        rc = verify.run_checks(
            db_path=db,
            checks=verify.build_checks(bot_flags={}, env={}),
            strict_all=False, dry_run=False,
        )
        # scan_liveness ok, hourly warning may fire (no rows). rc=0 for non-strict.
        assert rc == 0

    def test_dry_run_never_touches_db(self, verify, tmp_path):
        # Point to a path that does not exist — dry_run should not care.
        nonexistent = tmp_path / "missing.db"
        rc = verify.run_checks(
            db_path=nonexistent,
            checks=verify.build_checks(bot_flags={}, env={}),
            strict_all=False, dry_run=True,
        )
        assert rc == 0
