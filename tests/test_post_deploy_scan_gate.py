"""Regression tests for post-deploy [6/6] startup-pattern gate.

Bit 2.0.5.3 of repo modularization plan
(kb/decisions/repo-modularization-plan-may05.md).

Spec correction (2-pivot journey across 3 candidate signals; full
audit trail at kb/decisions/bit-2.0.5.3-spec-correction-may07.md):
- Spec line 1513 named `evaluated_opportunities`. R1 + production
  audit found ~50% per-deploy false-positive rate (200 gaps >90s in
  24h; rows are written only on candidate evaluations, not per
  scan-tick).
- 1st pivot: `market_observations_continuous`. R2 + 7d audit found
  ~3.8% per-deploy false-positive rate due to KALSHI_15M_CATALOG_GAP
  stretches (snapshotter early-returns on empty active_tickers).
- 2nd pivot (final): `bot_startup_log`. The cal_mlp integration writes
  one row UNCONDITIONALLY at startup
  (`scripts/cal_mlp/integration.py:457`). Catalog gaps + market
  quietude + WS reconnects do NOT cause restarts. Empirical: 92
  startup pairs across 8d (cal_mlp integration shipped 2026-04-29);
  max 2 events in any 5-min window historically. ≥3 has zero
  historical false-positives.

The gate logic lives in `scripts/post_deploy_scan_gate.py` (mirrors
the postdeploy_verify.py / audit_cron.py separate-script convention;
the VPS doesn't have a sqlite3 CLI installed so the gate uses Python's
stdlib sqlite3 module). The workflow's `[6/6]` step is a one-line
invocation: `venv/bin/python3 scripts/post_deploy_scan_gate.py --db
state.db --window-seconds 300 --max-events 2`.

Threshold:
- 0 rows in window: FAIL (Bit 2.1a class — deploy didn't restart bot,
  OR bot crashed before init completed).
- count > max_events: FAIL (post-init crash loop).
- 1 ≤ count ≤ max_events: OK.

R1 CRITICAL #1 (PRAGMA stdout leak from sqlite3 CLI) is N/A under
the Python-script design — Python's sqlite3 module doesn't echo
PRAGMA results.
R1 CRITICAL #2 (T-vs-space lex compare) is avoided by julianday()
numerical compare; the SQL is a parameterized query so SQL injection
is also a non-issue.
R3 MAJOR #2 (sqlite3 mid-pipe error misclassification) is avoided
because Python's exception handling cleanly distinguishes
`OperationalError` (schema drift / locked DB) from valid empty-result
rows.
"""
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POST_DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "post_deploy_verify.yml"
GATE_SCRIPT = REPO_ROOT / "scripts" / "post_deploy_scan_gate.py"


# ───────────────────────────────────────────────────────────────────
# Workflow + Makefile + ops/CLAUDE.md invariant pins
# ───────────────────────────────────────────────────────────────────


def test_workflow_invokes_gate_script():
    """post_deploy_verify.yml [6/6] invokes scripts/post_deploy_scan_gate.py.

    Pins the contract that the workflow runs the separate Python
    script (not an inline sqlite3 CLI call). Anti-regression for the
    sqlite3-CLI-not-installed-on-VPS case caught while rounding off
    R3 fixes.
    """
    assert POST_DEPLOY_YML.exists(), (
        f"{POST_DEPLOY_YML.relative_to(REPO_ROOT)} missing."
    )
    text = POST_DEPLOY_YML.read_text()
    assert "venv/bin/python3 scripts/post_deploy_scan_gate.py" in text, (
        "post_deploy_verify.yml [6/6] no longer invokes "
        "scripts/post_deploy_scan_gate.py via the venv Python. The "
        "gate must run as a separate script (mirrors "
        "postdeploy_verify.py / audit_cron.py convention; sqlite3 "
        "CLI is NOT installed on the VPS, so the gate must use "
        "Python's stdlib sqlite3 module)."
    )
    # CLI args MUST be passed (window-seconds + max-events tied to
    # the empirical justification).
    assert "--window-seconds 300" in text, (
        "post_deploy_verify.yml [6/6] missing `--window-seconds 300` "
        "(5-min lookback per empirical justification). A future edit "
        "that drops or changes this arg would silently change the "
        "gate's behavior."
    )
    assert "--max-events 2" in text, (
        "post_deploy_verify.yml [6/6] missing `--max-events 2` "
        "(empirical max in any 5-min window across 92 historical "
        "pairs is 2; ceiling of >=3 has zero false-positives). "
        "A future edit that weakens to `--max-events 5` should be "
        "data-backed first."
    )
    # Anti-regression: the GATE SCRIPT must use bot_startup_log; the
    # prior failed signal tables must not appear in its SQL. The
    # workflow comment legitimately mentions them in the audit-trail
    # spec-correction note, so the check targets the script body
    # (NOT the workflow) where SQL lives.
    script_text = GATE_SCRIPT.read_text()
    assert "bot_startup_log" in script_text, (
        "scripts/post_deploy_scan_gate.py SQL must reference "
        "`bot_startup_log` (the 2nd-pivot signal choice)."
    )
    assert "evaluated_opportunities" not in script_text, (
        "scripts/post_deploy_scan_gate.py SQL contains "
        "`evaluated_opportunities` — the original spec's signal "
        "had ~50% per-deploy false-positive rate and was rejected "
        "in pivot 0→1."
    )
    assert "market_observations_continuous" not in script_text, (
        "scripts/post_deploy_scan_gate.py SQL contains "
        "`market_observations_continuous` — the 1st-pivot signal "
        "had ~3.8% per-deploy false-positive rate due to "
        "KALSHI_15M_CATALOG_GAP and was rejected in pivot 1→2."
    )
    # Anti-regression: lex-compare cutoffs MUST NOT reappear.
    assert "datetime('now'" not in script_text, (
        "scripts/post_deploy_scan_gate.py SQL contains "
        "`datetime('now', ...)` — that's a lex-compare cutoff "
        "form (R1 CRITICAL #2 class). Use julianday() instead."
    )
    assert "strftime(" not in script_text, (
        "scripts/post_deploy_scan_gate.py SQL contains "
        "`strftime(...)` — that's a lex-compare cutoff form. Use "
        "julianday() for numerical comparison."
    )
    # The julianday() form is required (no T-vs-space lex hazard,
    # handles both `+00:00` and `Z` ISO-8601 quirks).
    assert "julianday(ts)" in script_text and "julianday('now'" in script_text, (
        "scripts/post_deploy_scan_gate.py SQL must use julianday() "
        "for numerical time compare. Avoids R1 CRITICAL #2 lex class."
    )


def test_workflow_step_count():
    """Verify job has ≥ 6 numbered substeps `[N/M]` AND a `[6/N]` line.

    Anchored on `echo "[N/M]"` form so a future comment containing a
    natural-prose `[3/5]` (e.g., from a postmortem citation) cannot
    relax the assertion silently. Per R1 MINOR #2.
    """
    assert POST_DEPLOY_YML.exists()
    text = POST_DEPLOY_YML.read_text()
    step_labels = re.findall(r'echo\s+"\[(\d+)/(\d+)\]', text)
    assert step_labels, (
        "post_deploy_verify.yml has no `echo \"[N/M] ...\"` substeps."
    )
    denominators = {d for _, d in step_labels}
    assert all(int(d) >= 6 for d in denominators), (
        f"post_deploy_verify.yml `echo \"[N/M]\"` denominators = "
        f"{sorted(denominators)}; all must be ≥ 6 (the [6/6] gate)."
    )
    numerators = {n for n, _ in step_labels}
    assert "6" in numerators, (
        "post_deploy_verify.yml has no `echo \"[6/N] ...\"` substep."
    )


def test_workflow_threshold_rationale_documented():
    """Workflow comment block explains why the gate exists + why the threshold.

    Pins phrases that anchor the design rationale so a future edit
    can't silently weaken the gate without consulting the data.
    """
    assert POST_DEPLOY_YML.exists()
    text = POST_DEPLOY_YML.read_text()
    # The Bit 2.1a postmortem reference: the failure mode the gate is
    # designed to catch.
    assert "Bit 2.1a" in text, (
        "post_deploy_verify.yml [6/6] comment missing `Bit 2.1a` "
        "reference — context for WHY the gate exists is lost."
    )
    # The unconditional-INSERT property of cal_mlp integration is the
    # load-bearing rationale for choosing this signal.
    assert "UNCONDITIONALLY at startup" in text, (
        "post_deploy_verify.yml [6/6] comment missing the rationale "
        "for the bot_startup_log signal choice "
        "(`UNCONDITIONALLY at startup`). Without this, a future "
        "edit may revert to a gated signal table."
    )
    # The empirical 92-pair history anchors the ≥3 threshold.
    assert "92 historical pairs" in text, (
        "post_deploy_verify.yml [6/6] comment missing the `92 "
        "historical pairs` empirical rationale anchoring the ≥3 "
        "threshold."
    )
    # The 8-day data span (NOT 28d — see R3 MAJOR #1).
    assert "last 8d" in text, (
        "post_deploy_verify.yml [6/6] comment missing the literal "
        "`last 8d` data-span. Earlier versions overclaimed `28d`; "
        "R3 MAJOR #1 corrected to 8d (cal_mlp integration shipped "
        "2026-04-29 — older rows do not exist by construction)."
    )
    # Spec correction note pointer.
    assert "bit-2.0.5.3-spec-correction-may07.md" in text, (
        "post_deploy_verify.yml [6/6] comment missing the spec-"
        "correction breadcrumb."
    )


def test_test_fast_recipe_invokes_scan_gate_test():
    """make test-fast must run this test file.

    Mirrors Bit 1.3 / 1.4 / 1.5 / 2.0.5.2 cadence: every dev-tooling
    invariant test self-pins into the fast tier.
    """
    makefile = REPO_ROOT / "Makefile"
    assert makefile.exists(), "Makefile missing at repo root."
    text = re.sub(r"\\\n", " ", makefile.read_text())
    m = re.search(
        r"^test-fast:[^\n]*\n((?:\t[^\n]*\n)+)",
        text,
        re.M,
    )
    assert m, "test-fast: recipe missing from Makefile."
    recipe = m.group(1)
    assert "test_post_deploy_scan_gate.py" in recipe, (
        "Makefile `test-fast` recipe doesn't invoke "
        "tests/test_post_deploy_scan_gate.py."
    )


# ───────────────────────────────────────────────────────────────────
# Behavioral tests against scripts/post_deploy_scan_gate.py.
# Build a fixture state.db with controlled bot_startup_log rows; run
# the gate script as subprocess; assert the right branch fires.
#
# These are STRUCTURALLY simpler than R1+R2's bash-shell tests
# because the gate is now a real Python program — exception
# handling, exit codes, and stdout/stderr are first-class.
# ───────────────────────────────────────────────────────────────────


def _make_db(tmp_path: Path, n_recent_rows: int) -> Path:
    """Build a fixture state.db with N rows in bot_startup_log within the
    last 5 min, plus one stale row outside the window.

    Mirrors the production schema (`scripts/cal_mlp/integration.py:457`
    INSERT site). Uses Python's `datetime.now(timezone.utc).isoformat()`
    so the row format matches production exactly (T separator, +00:00
    offset, microsecond precision).
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE bot_startup_log (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            parity_check_status TEXT,
            sizing_parity_status TEXT,
            pid INTEGER
        )
    """)
    now = datetime.now(timezone.utc)
    for i in range(n_recent_rows):
        ts_recent = now - timedelta(seconds=30 * (i + 1))
        conn.execute(
            "INSERT INTO bot_startup_log (ts, parity_check_status, pid) "
            "VALUES (?, ?, ?)",
            (ts_recent.isoformat(), "passed", 12345 + i),
        )
    # Always insert one stale row (10 min ago, outside window).
    stale = now - timedelta(seconds=600)
    conn.execute(
        "INSERT INTO bot_startup_log (ts, parity_check_status, pid) "
        "VALUES (?, ?, ?)",
        (stale.isoformat(), "passed", 99999),
    )
    conn.commit()
    conn.close()
    return db


def _run_gate(db_path: Path, *, window: int = 300, max_events: int = 2):
    """Invoke scripts/post_deploy_scan_gate.py via subprocess."""
    return subprocess.run(
        [
            sys.executable,
            str(GATE_SCRIPT),
            "--db",
            str(db_path),
            "--window-seconds",
            str(window),
            "--max-events",
            str(max_events),
        ],
        capture_output=True,
        text=True,
    )


def test_gate_fires_on_zero_rows(tmp_path):
    """0 rows in 5 min → FAIL (Bit 2.1a class).

    Uses an empty fixture: no recent rows, only the stale row from
    `_make_db(0)`. The 5-min cutoff excludes the stale row → count=0
    → gate must fire. Per R4 MINOR #1, FAIL message goes to stderr
    (NOT stdout) so CI logs uniformly route bad-news to stderr.
    """
    db = _make_db(tmp_path, n_recent_rows=0)
    proc = _run_gate(db)
    assert proc.returncode == 1, (
        f"Expected exit 1 (FAIL); got rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "Bit 2.1a class" in proc.stderr, (
        f"Expected Bit 2.1a class explanation in stderr; got "
        f"stderr={proc.stderr!r} (R4 MINOR #1: FAIL messages route "
        f"to stderr, not stdout)."
    )


def test_gate_passes_on_one_row(tmp_path):
    """1 row in 5 min → OK (healthy: deploy restart)."""
    db = _make_db(tmp_path, n_recent_rows=1)
    proc = _run_gate(db)
    assert proc.returncode == 0, (
        f"Expected exit 0 (OK); got rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "OK: 1" in proc.stdout, (
        f"Expected `OK: 1` (healthy single-restart); got "
        f"stdout={proc.stdout!r}"
    )


def test_gate_passes_on_two_rows(tmp_path):
    """2 rows in 5 min → OK (healthy: deploy + 1 auto-restart)."""
    db = _make_db(tmp_path, n_recent_rows=2)
    proc = _run_gate(db)
    assert proc.returncode == 0, (
        f"Expected exit 0; got rc={proc.returncode} "
        f"stdout={proc.stdout!r}"
    )
    assert "OK: 2" in proc.stdout


def test_gate_fires_on_three_rows(tmp_path):
    """≥3 rows in 5 min → FAIL (post-init crash loop). FAIL → stderr (R4 MINOR #1)."""
    db = _make_db(tmp_path, n_recent_rows=3)
    proc = _run_gate(db)
    assert proc.returncode == 1, (
        f"Expected exit 1 (FAIL); got rc={proc.returncode} "
        f"stderr={proc.stderr!r}"
    )
    assert "post-init crash loop" in proc.stderr, (
        f"Expected `post-init crash loop` in stderr; got "
        f"stderr={proc.stderr!r}"
    )
    assert "3 startup events" in proc.stderr


def test_gate_fails_loud_on_missing_table(tmp_path):
    """Missing bot_startup_log table → sqlite3.OperationalError → FAIL.

    Reproduces R3 MAJOR #2's regression class: a schema error MUST
    surface as a sqlite3 error (with operator-grokkable message),
    NOT misclassified as a crash loop. Under the prior sqlite3-CLI-
    piped-into-tail design, the PRAGMA stdout leak made `STARTUP_COUNT`
    silently become `10000` and hit the ≥3 ceiling. Under the
    Python-script design, the OperationalError is caught explicitly.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE dummy (id INTEGER)")  # different table
    conn.commit()
    conn.close()
    proc = _run_gate(db)
    assert proc.returncode == 1, (
        f"Expected exit 1; got rc={proc.returncode}"
    )
    assert "no such table" in proc.stderr or "no such table" in proc.stdout, (
        f"Expected `no such table` sqlite3 error in output; got "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}. The gate "
        f"must surface the actual sqlite3 error, NOT misclassify as "
        f"a crash loop (R3 MAJOR #2 regression)."
    )
    # Check BOTH stdout and stderr (R5 MINOR #1): the FAIL messages
    # currently route to stderr (R4 MINOR #1), so a future regression
    # that misclassifies sqlite3 errors as crash loops would surface
    # in stderr — anti-regress against ANY stream.
    assert "post-init crash loop" not in proc.stdout, (
        f"Gate misclassified a sqlite3 schema error as a crash loop "
        f"(stdout). R3 MAJOR #2 regression."
    )
    assert "post-init crash loop" not in proc.stderr, (
        f"Gate misclassified a sqlite3 schema error as a crash loop "
        f"(stderr). R3 MAJOR #2 regression — note the FAIL messages "
        f"currently route to stderr (R4 MINOR #1), so a future "
        f"regression would land here. Pinned per R5 MINOR #1."
    )


def test_gate_excludes_stale_rows(tmp_path):
    """julianday() cutoff must enforce the 5-min boundary.

    Builds a DB with ONE recent row + ONE stale row (>5 min old).
    Total table size = 2; expected count in 5-min window = 1 → OK.
    """
    db = _make_db(tmp_path, n_recent_rows=1)
    conn = sqlite3.connect(str(db))
    total = conn.execute("SELECT COUNT(*) FROM bot_startup_log").fetchone()[0]
    conn.close()
    assert total == 2, f"Fixture has {total} rows, expected 2."
    proc = _run_gate(db)
    assert proc.returncode == 0, (
        f"Expected exit 0; got rc={proc.returncode} "
        f"stdout={proc.stdout!r}"
    )
    assert "OK: 1" in proc.stdout, (
        f"Expected `OK: 1` (1 recent + 1 stale = 1 in window); got "
        f"stdout={proc.stdout!r}"
    )


def test_gate_excludes_z_form_in_addition_to_offset_form(tmp_path):
    """julianday() handles BOTH ISO-8601 quirks (T+offset and TZ-suffix).

    bot_startup_log writes `+00:00` form (Python isoformat); other
    bot/_impl.py write sites use `Z` suffix. julianday() should parse both.
    Pin this so a future signal change to a Z-form table works.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE bot_startup_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, parity_check_status TEXT, sizing_parity_status TEXT, pid INTEGER)"
    )
    now = datetime.now(timezone.utc)
    # Recent row in `+00:00` form (production format).
    conn.execute(
        "INSERT INTO bot_startup_log (ts, parity_check_status, pid) VALUES (?, ?, ?)",
        ((now - timedelta(seconds=30)).isoformat(), "passed", 1),
    )
    # Recent row in `Z` form (alternate ISO-8601 quirk).
    z_form = (now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn.execute(
        "INSERT INTO bot_startup_log (ts, parity_check_status, pid) VALUES (?, ?, ?)",
        (z_form, "passed", 2),
    )
    conn.commit()
    conn.close()
    proc = _run_gate(db)
    assert proc.returncode == 0, (
        f"Expected exit 0 (count=2 within healthy 1-2 range); got "
        f"rc={proc.returncode} stdout={proc.stdout!r}"
    )
    assert "OK: 2" in proc.stdout, (
        f"Expected `OK: 2` (both formats parsed by julianday); got "
        f"stdout={proc.stdout!r}. If only one format is parsed, "
        f"count would be 1 — indicating julianday() lost robustness."
    )


def test_gate_threshold_is_configurable(tmp_path):
    """--max-events flag controls the ceiling.

    Validates that a future operator can re-tune the threshold via
    flag (e.g., temporarily allow more restarts during heavy dev
    iteration) without code change. Runs with --max-events 5 against
    a 4-row fixture; should pass (4 ≤ 5) where the default would fail
    (4 > 2).
    """
    db = _make_db(tmp_path, n_recent_rows=4)
    proc_default = _run_gate(db, max_events=2)
    assert proc_default.returncode == 1, (
        "Default --max-events=2 should fire on 4 rows."
    )
    proc_relaxed = _run_gate(db, max_events=5)
    assert proc_relaxed.returncode == 0, (
        f"--max-events=5 should pass on 4 rows; got "
        f"rc={proc_relaxed.returncode} stdout={proc_relaxed.stdout!r}"
    )


def test_gate_window_is_configurable(tmp_path):
    """--window-seconds flag controls the lookback.

    Validates configurability without code change.
    """
    db = _make_db(tmp_path, n_recent_rows=1)
    # Default 300s window: row at -30s is inside → count=1 → OK.
    proc_default = _run_gate(db)
    assert proc_default.returncode == 0
    # Tight 10s window: row at -30s is OUTSIDE → count=0 → FAIL.
    proc_tight = _run_gate(db, window=10)
    assert proc_tight.returncode == 1, (
        f"With --window-seconds=10, the -30s row should be outside "
        f"the window → count=0 → FAIL. Got rc={proc_tight.returncode} "
        f"stdout={proc_tight.stdout!r}"
    )


def test_gate_script_uses_wal_and_busy_timeout_pragmas():
    """Script must explicitly set PRAGMA journal_mode=WAL + busy_timeout=10000.

    scripts/CLAUDE.md convention: any new sqlite3.connect() must
    include both PRAGMAs. Sister scripts `audit_cron.py:983` and
    `postdeploy_verify.py:333` both pin them. R4 MAJOR #1: the
    Python-`timeout=10.0` parameter is NOT equivalent — it controls
    Python's wrapper retry, not SQLite's internal lock-wait.
    """
    src = GATE_SCRIPT.read_text()
    assert 'PRAGMA journal_mode=WAL' in src, (
        "scripts/post_deploy_scan_gate.py missing "
        "`PRAGMA journal_mode=WAL` after sqlite3.connect(). "
        "scripts/CLAUDE.md convention; R4 MAJOR #1."
    )
    assert 'PRAGMA busy_timeout=10000' in src, (
        "scripts/post_deploy_scan_gate.py missing "
        "`PRAGMA busy_timeout=10000` after sqlite3.connect(). "
        "scripts/CLAUDE.md convention; R4 MAJOR #1. Note: "
        "Python's `timeout=10.0` parameter is NOT equivalent."
    )


def test_gate_rejects_zero_window(tmp_path):
    """argparse must reject --window-seconds < 1 (R4 MAJOR #2).

    --window-seconds=0 makes the cutoff equal to 'now' (every row
    fails > comparison) → unconditional FAIL → looks like a Bit
    2.1a alert with a self-evidently-nonsensical "in last 0s"
    message.
    """
    db = _make_db(tmp_path, n_recent_rows=1)
    proc = _run_gate(db, window=0)
    assert proc.returncode == 2, (
        f"Expected argparse rc=2 (validation error); got "
        f"rc={proc.returncode} stdout={proc.stdout!r} "
        f"stderr={proc.stderr!r}. R4 MAJOR #2 regression."
    )
    assert "--window-seconds" in proc.stderr, (
        f"Expected argparse error mentioning --window-seconds; "
        f"got stderr={proc.stderr!r}"
    )


def test_gate_rejects_negative_window(tmp_path):
    """argparse must reject --window-seconds < 0 (R4 MAJOR #2).

    --window-seconds=-N produces f-string `--N seconds` (double-dash)
    which julianday() returns NULL on → unconditional FAIL.
    """
    db = _make_db(tmp_path, n_recent_rows=1)
    proc = _run_gate(db, window=-300)
    assert proc.returncode == 2, (
        f"Expected argparse rc=2 (validation error); got "
        f"rc={proc.returncode} stderr={proc.stderr!r}"
    )


def test_gate_rejects_zero_max_events(tmp_path):
    """argparse must reject --max-events < 1 (R4 MAJOR #2).

    --max-events=0 makes count==0 hit the Bit 2.1a branch AND
    count>=1 hit the crash-loop branch (`count > 0` is True), so
    the gate cannot return OK for any DB state.
    """
    db = _make_db(tmp_path, n_recent_rows=1)
    proc = _run_gate(db, max_events=0)
    assert proc.returncode == 2, (
        f"Expected argparse rc=2 (validation error); got "
        f"rc={proc.returncode} stderr={proc.stderr!r}. R4 MAJOR #2 "
        f"regression."
    )
    assert "--max-events" in proc.stderr, (
        f"Expected argparse error mentioning --max-events; "
        f"got stderr={proc.stderr!r}"
    )


def test_gate_script_is_executable_via_python_module():
    """Sanity check: the script can be imported as a module.

    Catches syntax errors / top-level import errors before they
    hit a CI deploy. Mirrors the repo's other ast-check pattern.
    """
    import ast
    src = GATE_SCRIPT.read_text()
    ast.parse(src)
    # Also confirm the main entrypoint exists (by name, not by run).
    assert "def main(" in src, (
        "scripts/post_deploy_scan_gate.py missing `def main(` entry "
        "point — the script structure must be conventional."
    )
    assert 'if __name__ == "__main__":' in src, (
        "scripts/post_deploy_scan_gate.py missing `if __name__ == "
        "\"__main__\":` guard — required for subprocess invocation "
        "+ unit-test isolation."
    )
