"""Regression tests for scripts/setup_h4_cron.sh.

H-4 Option C (per kb/decisions/phase-h-forward-going-capture-required-may02.md):
daily cron entry on VPS that runs each H-4 backfill script (GDELT,
Glassnode, CryptoCompare) on the last 24h of new rows. The scripts
themselves are idempotent (WHERE col IS NULL filter). This setup
script installs 3 systemd timer+service pairs to invoke them.

These tests pin the structural contract of the install script — paths,
cadence, env-file inclusion, and the existing-pattern conventions —
without requiring sudo or actual systemd installation.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'setup_h4_cron.sh'


@pytest.fixture(scope='module')
def script_content() -> str:
    assert SCRIPT_PATH.exists(), (
        f"{SCRIPT_PATH} missing — install script not yet created"
    )
    return SCRIPT_PATH.read_text()


def test_setup_script_exists_and_parses():
    """Bash syntax check on the install script."""
    assert SCRIPT_PATH.exists()
    result = subprocess.run(
        ['bash', '-n', str(SCRIPT_PATH)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"bash -n failed: stderr={result.stderr!r}"
    )


def test_setup_script_uses_strict_mode(script_content: str):
    """Match setup_audit_cron.sh discipline: set -euo pipefail."""
    assert 'set -euo pipefail' in script_content, (
        "install script must use strict mode (set -euo pipefail)"
    )


def test_installs_three_services_one_per_h4_source(script_content: str):
    """Each H-4 sub-phase gets its own systemd service unit."""
    expected_services = [
        'kalshi-h4-gdelt.service',
        'kalshi-h4-glassnode.service',
        'kalshi-h4-cryptocompare.service',
    ]
    for svc in expected_services:
        assert svc in script_content, (
            f"missing systemd service unit: {svc}"
        )


def test_installs_three_timers_one_per_h4_source(script_content: str):
    """Matching timer for each service."""
    expected_timers = [
        'kalshi-h4-gdelt.timer',
        'kalshi-h4-glassnode.timer',
        'kalshi-h4-cryptocompare.timer',
    ]
    for tmr in expected_timers:
        assert tmr in script_content, (
            f"missing systemd timer unit: {tmr}"
        )


def test_services_invoke_existing_backfill_scripts(script_content: str):
    """ExecStart references the right scripts under scripts/."""
    expected_scripts = [
        'scripts/gdelt_backfill.py',
        'scripts/glassnode_backfill.py',
        'scripts/cryptocompare_news_backfill.py',
    ]
    for sc in expected_scripts:
        assert sc in script_content, (
            f"install script must invoke {sc} via ExecStart"
        )


def test_timers_use_daily_oncalendar(script_content: str):
    """Per design (Option C, daily cron). Each timer must have an
    OnCalendar entry that fires once per day. Staggered times are
    expected (avoid DB / API contention)."""
    oncal_lines = re.findall(r'OnCalendar=([^\n]+)', script_content)
    assert len(oncal_lines) >= 3, (
        f"expected ≥3 OnCalendar entries (one per timer); got: {oncal_lines}"
    )
    # All three should be daily cadence.
    for line in oncal_lines:
        line_lower = line.lower()
        assert 'daily' in line_lower or '*-*-*' in line or 'hourly' in line_lower, (
            f"OnCalendar entry must indicate daily cadence; got: {line!r}"
        )


def test_timers_are_staggered(script_content: str):
    """The 3 OnCalendar entries should not all fire at the same time
    (avoid simultaneous DB writes + API calls). Pin distinct values."""
    oncal_lines = re.findall(r'OnCalendar=([^\n]+)', script_content)
    distinct = set(line.strip() for line in oncal_lines)
    assert len(distinct) >= 3, (
        f"OnCalendar entries must be staggered (distinct values); "
        f"got: {oncal_lines}"
    )


def test_services_run_as_botuser(script_content: str):
    """Match setup_audit_cron.sh — User=botuser owns the bot files."""
    assert re.search(r'User=botuser\b', script_content), (
        "services must run as User=botuser (matches existing audit cron)"
    )


def test_services_use_environment_file(script_content: str):
    """GLASSNODE_API_KEY (optional) lives in .env; without
    EnvironmentFile, paid Glassnode metrics silently skip. The
    setup_audit_cron.sh doesn't need this (no env vars), but H-4 does."""
    assert 'EnvironmentFile=' in script_content, (
        "services must source .env via EnvironmentFile so "
        "GLASSNODE_API_KEY is picked up if set"
    )
    # Specifically the bot's .env path.
    assert '/home/botuser/kalshi-bot-repo/.env' in script_content


def test_services_pass_db_argument(script_content: str):
    """Each backfill script requires --db state.db."""
    db_count = script_content.count('--db')
    assert db_count >= 3, (
        f"expected ≥3 --db arguments (one per service); got: {db_count}"
    )


def test_uses_oneshot_service_type(script_content: str):
    """Backfill scripts exit when done — Type=oneshot is the right
    systemd service type."""
    assert 'Type=oneshot' in script_content


def test_persistent_timer_for_missed_runs(script_content: str):
    """Persistent=true means systemd will run the timer on boot if
    the last scheduled run was missed (e.g., VPS rebooted overnight).
    Important for daily backfill so we don't lose a day's coverage."""
    assert 'Persistent=true' in script_content, (
        "timers must be Persistent=true so missed daily runs catch up"
    )


def test_install_script_no_append_redirect_to_unit_files(script_content: str):
    """Necessary-not-sufficient idempotency check: `sudo tee` overwrites
    by default; `>>` would append + corrupt unit files on re-run.
    Full idempotency also depends on tee-overwrite mode (default) and
    daemon-reload ordering — see test_unit_writes_precede_daemon_reload.
    """
    bad_redirects = re.findall(r'>>\s*"?\$?\w*\.(?:service|timer)', script_content)
    assert not bad_redirects, (
        f"install script uses append (>>) on systemd units — corrupts "
        f"on re-run; got: {bad_redirects}"
    )


def test_unit_writes_precede_daemon_reload_precede_enable(script_content: str):
    """Round-1 critique #5 strengthening: ordering matters for systemd.
    All `tee <unit-file>` writes MUST appear BEFORE `daemon-reload`,
    which MUST appear BEFORE the first `systemctl enable`. Otherwise
    enable can fire on units systemd hasn't yet learned about.
    """
    lines = script_content.splitlines()
    tee_unit_lines = [
        i for i, line in enumerate(lines)
        if re.search(r'tee\s+/etc/systemd/system/\S+\.(service|timer)', line)
    ]
    daemon_reload_lines = [
        i for i, line in enumerate(lines)
        if re.search(r'\bsystemctl\s+daemon-reload\b', line)
    ]
    enable_lines = [
        i for i, line in enumerate(lines)
        if re.search(r'systemctl\s+enable\s+kalshi-h4-', line)
    ]
    assert tee_unit_lines, "expected tee writes to unit files"
    assert daemon_reload_lines, "expected daemon-reload"
    assert enable_lines, "expected systemctl enable"
    last_tee = max(tee_unit_lines)
    first_reload = min(daemon_reload_lines)
    first_enable = min(enable_lines)
    assert last_tee < first_reload, (
        f"all unit-file tee writes (last={last_tee}) must precede "
        f"daemon-reload (first={first_reload})"
    )
    assert first_reload < first_enable, (
        f"daemon-reload (first={first_reload}) must precede first "
        f"systemctl enable (first={first_enable})"
    )


def test_script_runs_daemon_reload_after_writes(script_content: str):
    """systemctl daemon-reload must be invoked after writing unit files
    so systemd picks up the new units."""
    assert 'daemon-reload' in script_content


def test_timers_are_enabled_and_started(script_content: str):
    """Timers must be both enabled (start on boot) AND started (start
    now)."""
    enable_count = len(re.findall(
        r'systemctl\s+enable\s+kalshi-h4-\w+\.timer', script_content,
    ))
    start_count = len(re.findall(
        r'systemctl\s+start\s+kalshi-h4-\w+\.timer', script_content,
    ))
    assert enable_count >= 3, (
        f"expected ≥3 'systemctl enable' invocations; got {enable_count}"
    )
    assert start_count >= 3, (
        f"expected ≥3 'systemctl start' invocations; got {start_count}"
    )


def test_run_from_repo_root_guard(script_content: str):
    """Match setup_audit_cron.sh — script should be runnable directly
    without changing CWD assumptions. The hardcoded BOT_DIR=/home/botuser/
    kalshi-bot-repo guards against ambient CWD issues."""
    assert '/home/botuser/kalshi-bot-repo' in script_content, (
        "BOT_DIR must be hardcoded to the VPS path"
    )


def test_execstart_routes_through_alert_wrapper(script_content: str):
    """Round-1 critique #1 fix: each backfill must run via
    h4_run_with_alert.py so non-zero exits trigger Telegram alerts.
    Without the wrapper, multi-day H-4 outages stay invisible until
    the v2 acceptance gate fires weeks later.
    """
    # WRAPPER variable must point at h4_run_with_alert.py.
    assert re.search(
        r'^WRAPPER=.*h4_run_with_alert\.py', script_content, re.MULTILINE,
    ), "WRAPPER variable must reference h4_run_with_alert.py"
    # Each ExecStart must invoke the wrapper with --label <name> -- <real cmd>.
    wrapper_uses = re.findall(
        r'ExecStart=\S+\s+\$\{WRAPPER\}\s+--label\s+(\w+)\s+--\s+',
        script_content,
    )
    expected_labels = {'gdelt', 'glassnode', 'cryptocompare'}
    assert set(wrapper_uses) == expected_labels, (
        f"each ExecStart must invoke ${{WRAPPER}} with the right --label; "
        f"got labels: {wrapper_uses}"
    )
    # Each label's wrapped command points at the matching backfill script.
    for label, script in [
        ('gdelt', 'gdelt_backfill.py'),
        ('glassnode', 'glassnode_backfill.py'),
        ('cryptocompare', 'cryptocompare_news_backfill.py'),
    ]:
        pattern = (
            r'ExecStart=\S+\s+\$\{WRAPPER\}\s+--label\s+'
            + re.escape(label) + r'\s+--\s+\S+\s+\S*'
            + re.escape(script)
        )
        assert re.search(pattern, script_content), (
            f"ExecStart for label {label!r} must wrap {script!r}"
        )


def test_timeoutstartsec_covers_first_run_catchup(script_content: str):
    """Round-1 critique #2 fix: TimeoutStartSec must cover the first
    daily run's catch-up (drains historical NULL backlog). Anything
    below 1h risks killing mid-pass and the operator sees the failure
    as a recurring timeout. 3600 (1 hour) is enough for steady-state
    + first-week catch-up.
    """
    timeouts = re.findall(r'TimeoutStartSec=(\d+)', script_content)
    assert timeouts, "expected TimeoutStartSec on each service"
    for t in timeouts:
        assert int(t) >= 3600, (
            f"TimeoutStartSec={t}s too short for first-run catch-up "
            f"(need ≥3600s); see kb/decisions/h4-daily-cron-shipped.md"
        )


def test_preflight_warns_on_missing_glassnode_key(script_content: str):
    """Round-1 critique #4 fix: install script must warn (not fail) if
    GLASSNODE_API_KEY isn't set in the live .env. Otherwise H-4b paid
    metrics silently stay NULL and operator finds out at v2 acceptance
    gate time."""
    assert 'GLASSNODE_API_KEY' in script_content, (
        "install script must surface the GLASSNODE_API_KEY requirement"
    )
    assert 'WARNING' in script_content, (
        "preflight must use a clearly-labeled warning message"
    )


def test_preflight_grep_pattern_rejects_empty_value(script_content: str, tmp_path):
    """Round-3 critique #1 fix: `^GLASSNODE_API_KEY=` matches both a
    real value AND `GLASSNODE_API_KEY=` (empty), but glassnode_backfill.py
    silently skips paid metrics on empty value too. The preflight must
    warn for both cases. Regression: lock the regex requires `.+`.
    """
    # The script must use a regex that requires at least one char after =.
    assert re.search(
        r"grep\s+-qE?\s+'\^GLASSNODE_API_KEY=\.\+'", script_content,
    ), (
        "preflight must use 'grep -qE ^GLASSNODE_API_KEY=.+' so empty "
        "values also trigger the warning"
    )
    # And behavioral check: simulate the grep against an empty-value .env.
    fake_env = tmp_path / 'fake.env'
    fake_env.write_text("GLASSNODE_API_KEY=\nOTHER_KEY=foo\n")
    result = subprocess.run(
        ['grep', '-qE', '^GLASSNODE_API_KEY=.+', str(fake_env)],
        capture_output=True,
    )
    assert result.returncode != 0, (
        "expected grep to FAIL on empty value (so preflight warns); "
        "if grep succeeds, the regex is too loose"
    )
    fake_env.write_text("GLASSNODE_API_KEY=actualvalue123\n")
    result = subprocess.run(
        ['grep', '-qE', '^GLASSNODE_API_KEY=.+', str(fake_env)],
        capture_output=True,
    )
    assert result.returncode == 0, (
        "expected grep to PASS on real value (no warning needed)"
    )
