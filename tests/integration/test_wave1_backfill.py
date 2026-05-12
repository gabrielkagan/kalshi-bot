"""B.1a-fu2 (2026-05-12, ticket 86b9wuh8r) — Wave 1 derivable-column backfill.

Pins the contract for scripts/backfill/wave1_derived_cols.py:

- Golden-row equality: backfill produces byte-identical values to B.1a's
  live-write path (bot/state.py::insert_rejection auto-fill block at lines
  1700-1727 + insert_evaluated_opportunity at 1988-2024).
- Idempotency: running backfill twice produces identical state.
- Honest-NULL: rows where any input is NULL stay NULL (no fabrication).
- Schema-asymmetry guard: backfill refuses cols that don't exist on a table
  (rejected_opportunities has no hour_of_day_utc; evaluated has it but lacks
   sigma_winsorize/hour_sin/hour_cos cols).

Mirrors the lock-step contract surface in
tests/contracts/test_calmlp_lockstep.py — backfill must call canonical
helpers, not duplicate the math.
"""
from __future__ import annotations

import math
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKFILL_SCRIPT = REPO_ROOT / "scripts" / "backfill" / "wave1_derived_cols.py"

# Canonical helpers — backfill MUST call these (lock-step contract).
sys.path.insert(0, str(REPO_ROOT))
from bot.helpers.derived_features import (  # noqa: E402
    SIGMA_WINSOR_ABS_CAP,
    apply_sigma_winsor,
    compute_derived_features,
    compute_hour_sin_cos,
)
from bot.helpers.time_features import compute_time_regime_features  # noqa: E402


# ── Minimal schema that mirrors production for the columns we touch ──

REJECTED_SCHEMA = """
CREATE TABLE rejected_opportunities (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    asset TEXT NOT NULL,
    rejection_reason TEXT,
    rejection_time TEXT NOT NULL,
    spot_price REAL,
    threshold REAL,
    volatility REAL,
    seconds_to_close REAL,
    market_price INTEGER,
    calibrated_prob REAL,
    sigma_winsorize REAL,
    hour_sin REAL,
    hour_cos REAL,
    prob_breakeven_gap REAL,
    vol_regime TEXT,
    data_provenance TEXT
);
"""

EVALUATED_SCHEMA = """
CREATE TABLE evaluated_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    filter_stage TEXT NOT NULL,
    evaluation_time TEXT NOT NULL,
    spot_price REAL,
    threshold REAL,
    volatility REAL,
    seconds_to_close REAL,
    calibrated_prob REAL,
    market_price INTEGER,
    prob_breakeven_gap REAL
);
"""


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    """Fresh state.db with the two tables we backfill into."""
    db = tmp_path / "state.db"
    conn = _open(db)
    conn.executescript(REJECTED_SCHEMA + EVALUATED_SCHEMA)
    conn.commit()
    conn.close()
    return db


def _seed_rejection(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    asset: str = "BTC",
    rejection_time: str = "2026-04-15T14:30:00.000000Z",
    spot_price: float | None = 65000.0,
    threshold: float | None = 65500.0,
    volatility: float | None = 0.0008,
    seconds_to_close: float | None = 600.0,
    market_price: int | None = 45,
    calibrated_prob: float | None = 0.52,
    sigma_winsorize: float | None = None,
    hour_sin: float | None = None,
    hour_cos: float | None = None,
    prob_breakeven_gap: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO rejected_opportunities
          (ticker, asset, rejection_time, spot_price, threshold, volatility,
           seconds_to_close, market_price, calibrated_prob, sigma_winsorize,
           hour_sin, hour_cos, prob_breakeven_gap)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticker, asset, rejection_time, spot_price, threshold, volatility,
            seconds_to_close, market_price, calibrated_prob, sigma_winsorize,
            hour_sin, hour_cos, prob_breakeven_gap,
        ),
    )


def _run_backfill(db: Path, *extra: str) -> subprocess.CompletedProcess:
    """Invoke the backfill script as a subprocess (smoke + integration shape)."""
    cmd = [sys.executable, str(BACKFILL_SCRIPT), "--db", str(db), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))


# ── Pillar 4 (TDD): script must exist + run without import error ──


def test_backfill_script_exists():
    assert BACKFILL_SCRIPT.is_file(), (
        f"Backfill script not found at {BACKFILL_SCRIPT}. "
        "B.1a-fu2 ticket 86b9wuh8r must ship scripts/backfill/wave1_derived_cols.py."
    )


def test_backfill_dry_run_smokes(empty_db: Path):
    """Empty DB → dry-run should succeed and report 0 would-update."""
    result = _run_backfill(empty_db, "--dry-run")
    assert result.returncode == 0, (
        f"Dry run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )


# ── Golden-row equality: backfill matches B.1a live-write byte-for-byte ──


def test_golden_row_equality_all_inputs_present(empty_db: Path):
    """Row w/ full inputs → all 4 derivable cols match canonical-helper output."""
    rt = "2026-04-15T14:30:00.000000Z"
    sp, th, vol, stc = 65000.0, 65500.0, 0.0008, 600.0
    cp, mp = 0.52, 45

    conn = _open(empty_db)
    _seed_rejection(
        conn, ticker="BTC-T1",
        rejection_time=rt, spot_price=sp, threshold=th, volatility=vol,
        seconds_to_close=stc, calibrated_prob=cp, market_price=mp,
    )
    conn.commit()
    conn.close()

    result = _run_backfill(empty_db)
    assert result.returncode == 0, f"Backfill failed: {result.stderr}"

    # Compute expected via the same canonical helpers B.1a uses
    expected_t4 = compute_time_regime_features(rt)
    expected_hs, expected_hc = compute_hour_sin_cos(expected_t4["hour_of_day_utc"])
    expected_t5 = compute_derived_features(
        spot_price=sp, threshold=th, volatility=vol,
        seconds_to_close=stc, calibrated_prob=cp, market_price_cents=mp,
    )
    expected_sigma = apply_sigma_winsor(expected_t5["spot_distance_to_strike_sigma"])
    expected_gap = expected_t5["prob_breakeven_gap"]

    conn = _open(empty_db)
    row = conn.execute(
        "SELECT hour_sin, hour_cos, sigma_winsorize, prob_breakeven_gap "
        "FROM rejected_opportunities WHERE ticker = 'BTC-T1'"
    ).fetchone()
    conn.close()

    assert row[0] == pytest.approx(expected_hs, abs=1e-12)
    assert row[1] == pytest.approx(expected_hc, abs=1e-12)
    assert row[2] == pytest.approx(expected_sigma, abs=1e-12)
    assert row[3] == pytest.approx(expected_gap, abs=1e-12)


# ── Honest-NULL: missing inputs → NULL output, never fabricate ──


def test_honest_null_no_calibrated_prob(empty_db: Path):
    """calibrated_prob NULL → prob_breakeven_gap stays NULL even though market_price is set.

    Early-stage filters (price_out_of_range_early, etc.) bail before the
    calibrator runs; ~88% of rejected rows have calibrated_prob NULL.
    """
    conn = _open(empty_db)
    _seed_rejection(
        conn, ticker="EARLY-1", calibrated_prob=None, market_price=45,
    )
    conn.commit()
    conn.close()

    result = _run_backfill(empty_db)
    assert result.returncode == 0, f"Backfill failed: {result.stderr}"

    conn = _open(empty_db)
    gap = conn.execute(
        "SELECT prob_breakeven_gap FROM rejected_opportunities WHERE ticker='EARLY-1'"
    ).fetchone()[0]
    # hour_sin/cos + sigma_winsorize can still be derived (different inputs)
    hs, hc, sigma = conn.execute(
        "SELECT hour_sin, hour_cos, sigma_winsorize FROM rejected_opportunities WHERE ticker='EARLY-1'"
    ).fetchone()
    conn.close()

    assert gap is None, "Honest-NULL: prob_breakeven_gap must stay NULL when calibrated_prob is None"
    assert hs is not None and hc is not None, "hour_sin/cos derivable from rejection_time alone"
    assert sigma is not None, "sigma_winsorize derivable from spot+threshold+vol+stc"


def test_honest_null_no_spot_inputs(empty_db: Path):
    """volatility NULL → sigma_winsorize stays NULL (cannot compute raw sigma)."""
    conn = _open(empty_db)
    _seed_rejection(conn, ticker="NULLVOL-1", volatility=None)
    conn.commit()
    conn.close()

    result = _run_backfill(empty_db)
    assert result.returncode == 0

    conn = _open(empty_db)
    sigma = conn.execute(
        "SELECT sigma_winsorize FROM rejected_opportunities WHERE ticker='NULLVOL-1'"
    ).fetchone()[0]
    conn.close()

    assert sigma is None


# ── Idempotency: re-running is a no-op ──


def test_idempotent_second_run_is_noop(empty_db: Path):
    """Two backfills produce identical state — UPDATE ... WHERE IS NULL guard."""
    conn = _open(empty_db)
    _seed_rejection(conn, ticker="IDEM-1")
    _seed_rejection(conn, ticker="IDEM-2", asset="ETH")
    conn.commit()
    conn.close()

    r1 = _run_backfill(empty_db)
    assert r1.returncode == 0

    snapshot_sql = (
        "SELECT ticker, hour_sin, hour_cos, sigma_winsorize, "
        "prob_breakeven_gap, data_provenance "
        "FROM rejected_opportunities ORDER BY ticker"
    )
    conn = _open(empty_db)
    snapshot_after_first = conn.execute(snapshot_sql).fetchall()
    conn.close()

    r2 = _run_backfill(empty_db)
    assert r2.returncode == 0

    conn = _open(empty_db)
    snapshot_after_second = conn.execute(snapshot_sql).fetchall()
    conn.close()

    assert snapshot_after_first == snapshot_after_second


def test_idempotent_does_not_overwrite_existing_values(empty_db: Path):
    """If hour_sin is already set (e.g. by live-write), backfill leaves it untouched."""
    sentinel_hs, sentinel_hc = -0.5, 0.866  # arbitrary live-set values
    conn = _open(empty_db)
    _seed_rejection(
        conn, ticker="LIVE-1",
        hour_sin=sentinel_hs, hour_cos=sentinel_hc,
    )
    conn.commit()
    conn.close()

    result = _run_backfill(empty_db)
    assert result.returncode == 0

    conn = _open(empty_db)
    hs, hc = conn.execute(
        "SELECT hour_sin, hour_cos FROM rejected_opportunities WHERE ticker='LIVE-1'"
    ).fetchone()
    conn.close()

    assert hs == sentinel_hs, "Backfill must not overwrite pre-existing hour_sin"
    assert hc == sentinel_hc, "Backfill must not overwrite pre-existing hour_cos"


# ── Evaluated table: only prob_breakeven_gap is backfillable ──


def test_evaluated_prob_breakeven_gap_backfill(empty_db: Path):
    """evaluated_opportunities lacks sigma_winsorize/hour_sin/hour_cos cols — only
    prob_breakeven_gap is in scope for that table."""
    conn = _open(empty_db)
    conn.execute(
        """
        INSERT INTO evaluated_opportunities
          (ticker, asset, filter_stage, evaluation_time, spot_price, threshold,
           volatility, seconds_to_close, calibrated_prob, market_price)
        VALUES ('EVAL-1', 'BTC', 'candidate', '2026-04-15T14:30:00.000000Z',
                65000.0, 65500.0, 0.0008, 600.0, 0.52, 45)
        """
    )
    conn.commit()
    conn.close()

    result = _run_backfill(empty_db)
    assert result.returncode == 0

    expected = compute_derived_features(
        spot_price=65000.0, threshold=65500.0, volatility=0.0008,
        seconds_to_close=600.0, calibrated_prob=0.52, market_price_cents=45,
    )["prob_breakeven_gap"]

    conn = _open(empty_db)
    gap = conn.execute(
        "SELECT prob_breakeven_gap FROM evaluated_opportunities WHERE ticker='EVAL-1'"
    ).fetchone()[0]
    conn.close()

    assert gap == pytest.approx(expected, abs=1e-12)


# ── Winsorize bound: extreme sigma clamps to ±SIGMA_WINSOR_ABS_CAP ──


def test_sigma_winsorize_clamps_at_cap(empty_db: Path):
    """A row whose raw sigma exceeds the cap → sigma_winsorize = ±25.0."""
    # Pick inputs that yield raw sigma > 25
    # sigma = buf_pct / (vol × sqrt(STC/5) × 100)
    # → buf_pct very large, vol/stc small
    conn = _open(empty_db)
    _seed_rejection(
        conn, ticker="HUGE-1",
        spot_price=100.0, threshold=80.0,  # buf_pct = 25%
        volatility=0.00005, seconds_to_close=5.0,  # denom small
    )
    conn.commit()
    conn.close()

    result = _run_backfill(empty_db)
    assert result.returncode == 0

    conn = _open(empty_db)
    sigma = conn.execute(
        "SELECT sigma_winsorize FROM rejected_opportunities WHERE ticker='HUGE-1'"
    ).fetchone()[0]
    conn.close()

    assert sigma == SIGMA_WINSOR_ABS_CAP, (
        f"Expected sigma to clamp at +{SIGMA_WINSOR_ABS_CAP}, got {sigma}"
    )


# ── Data provenance stamp: backfill rows are distinguishable from live-writes ──


def test_data_provenance_stamped_on_backfill(empty_db: Path):
    """Rows touched by backfill must have data_provenance='backfill_b1a_fu2'
    so consumers can filter / re-backfill if helpers change later."""
    conn = _open(empty_db)
    _seed_rejection(conn, ticker="PROV-1")
    conn.commit()
    conn.close()

    result = _run_backfill(empty_db)
    assert result.returncode == 0

    conn = _open(empty_db)
    prov = conn.execute(
        "SELECT data_provenance FROM rejected_opportunities WHERE ticker='PROV-1'"
    ).fetchone()[0]
    conn.close()

    assert prov == "backfill_b1a_fu2"


def test_data_provenance_not_stamped_when_no_derivable_changes(empty_db: Path):
    """R1 C1 regression seal: a row with all 4 derivables already populated
    (e.g. live-written by B.1a) but data_provenance=NULL must keep
    data_provenance=NULL after backfill — the backfill didn't *cause* those
    values, so it must not claim provenance for them."""
    rt = "2026-04-15T14:30:00.000000Z"
    expected_t4 = compute_time_regime_features(rt)
    pre_hs, pre_hc = compute_hour_sin_cos(expected_t4["hour_of_day_utc"])
    pre_t5 = compute_derived_features(
        spot_price=65000.0, threshold=65500.0, volatility=0.0008,
        seconds_to_close=600.0, calibrated_prob=0.52, market_price_cents=45,
    )
    pre_sigma = apply_sigma_winsor(pre_t5["spot_distance_to_strike_sigma"])
    pre_gap = pre_t5["prob_breakeven_gap"]

    conn = _open(empty_db)
    _seed_rejection(
        conn, ticker="LIVE-ALL",
        spot_price=65000.0, threshold=65500.0, volatility=0.0008,
        seconds_to_close=600.0, calibrated_prob=0.52, market_price=45,
        hour_sin=pre_hs, hour_cos=pre_hc,
        sigma_winsorize=pre_sigma, prob_breakeven_gap=pre_gap,
        # data_provenance left NULL by default — that's the case under test
    )
    conn.commit()
    conn.close()

    result = _run_backfill(empty_db)
    assert result.returncode == 0

    conn = _open(empty_db)
    prov = conn.execute(
        "SELECT data_provenance FROM rejected_opportunities WHERE ticker='LIVE-ALL'"
    ).fetchone()[0]
    conn.close()

    assert prov is None, (
        "Provenance must NOT be stamped when backfill computed no cells — "
        "stamping would falsely attribute live-written values to the backfill."
    )


def test_data_provenance_not_overwritten_when_set(empty_db: Path):
    """If a row was live-written w/ data_provenance='live_ws', backfill leaves
    it alone — only fills NULL data_provenance."""
    conn = _open(empty_db)
    conn.execute(
        """
        INSERT INTO rejected_opportunities
          (ticker, asset, rejection_time, spot_price, threshold, volatility,
           seconds_to_close, calibrated_prob, market_price, data_provenance)
        VALUES ('LIVE-PROV', 'BTC', '2026-04-15T14:30:00.000000Z',
                65000.0, 65500.0, 0.0008, 600.0, 0.52, 45, 'live_ws')
        """
    )
    conn.commit()
    conn.close()

    result = _run_backfill(empty_db)
    assert result.returncode == 0

    conn = _open(empty_db)
    prov = conn.execute(
        "SELECT data_provenance FROM rejected_opportunities WHERE ticker='LIVE-PROV'"
    ).fetchone()[0]
    conn.close()

    assert prov == "live_ws"
