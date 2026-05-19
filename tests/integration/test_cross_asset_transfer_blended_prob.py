"""Bit A (86ba0jmzu) of HYPE/DOGE cal_mlp v1.1 retrain umbrella 86ba0jmyq.

Pins the contract for `scripts/backfill/cross_asset_transfer_blended_prob.py`:

- Backfills `blended_prob` on `historical_replay_calmlp` HYPE/DOGE rows by
  feeding them through `CalMLPPredictor("BTC")` (cross-asset transfer eval
  scoped as fu1 of 86b9wy7v3 in the existing replay backfill docstring).
- Per-row try/except with categorized skip reasons. No fabricated feature values.
- Idempotent — re-running only touches `WHERE blended_prob IS NULL` rows.
- Mac-only: refuses production VPS paths.
- Lock-step preserved: routes derived features through
  `bot.helpers.derived_features` canonical helpers.

Plan doc: `kb/decisions/v1-1-A-cross-asset-transfer-plan.md`.
"""
from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT / "scripts" / "backfill" / "cross_asset_transfer_blended_prob.py"
)

sys.path.insert(0, str(REPO_ROOT))

REPLAY_TABLE = "historical_replay_calmlp"


# ── Fixture helpers ───────────────────────────────────────────────────


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _seed_replay_table(conn: sqlite3.Connection) -> None:
    """Mirror the schema shipped by hype_doge_replay_backfill.ensure_schema()."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REPLAY_TABLE} (
            ticker TEXT NOT NULL,
            evaluation_time TEXT NOT NULL,
            asset TEXT NOT NULL CHECK (asset IN ('HYPE','DOGE')),
            strike_cents INTEGER,
            threshold REAL,
            close_time TEXT,
            open_time TEXT,
            raw_prob REAL,
            calibrated_prob REAL,
            blended_prob REAL,
            spot_at_evaluation REAL,
            sigma_at_evaluation REAL,
            hour_sin REAL,
            hour_cos REAL,
            prob_breakeven_gap REAL,
            sigma_winsorize REAL,
            result TEXT NOT NULL CHECK (result IN ('yes','no')),
            settlement_value INTEGER,
            data_provenance TEXT NOT NULL,
            replay_run_ts INTEGER NOT NULL,
            PRIMARY KEY (ticker, evaluation_time)
        )
        """
    )
    conn.commit()


def _insert_row(
    conn: sqlite3.Connection,
    *,
    ticker: str = "KXHYPE15M-26APR011200-15",
    eval_time: str = "2026-04-01T11:45:00Z",
    close_time: str = "2026-04-01T12:00:00Z",
    open_time: str = "2026-04-01T11:45:00Z",
    asset: str = "HYPE",
    raw_prob: float | None = 0.82,
    calibrated_prob: float | None = 0.78,
    blended_prob: float | None = None,
    spot_at_eval: float | None = 45.5,
    sigma_at_eval: float | None = 0.002,
    hour_sin: float | None = 0.5,
    hour_cos: float | None = 0.5,
    prob_breakeven_gap: float | None = 0.08,
    sigma_winsorize: float | None = 0.85,
    result: str = "yes",
    settlement_value: int = 100,
    threshold: float | None = 45.0,
    strike_cents: int | None = 4500,
) -> None:
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {REPLAY_TABLE} (
            ticker, evaluation_time, asset, strike_cents, threshold,
            close_time, open_time,
            raw_prob, calibrated_prob, blended_prob,
            spot_at_evaluation, sigma_at_evaluation,
            hour_sin, hour_cos, prob_breakeven_gap, sigma_winsorize,
            result, settlement_value, data_provenance, replay_run_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticker, eval_time, asset, strike_cents, threshold,
            close_time, open_time,
            raw_prob, calibrated_prob, blended_prob,
            spot_at_eval, sigma_at_eval,
            hour_sin, hour_cos, prob_breakeven_gap, sigma_winsorize,
            result, settlement_value, "replay_phase2_v1", 1700000000,
        ),
    )
    conn.commit()


# ── Pillar 4 (TDD): script must exist ─────────────────────────────────


def test_script_exists():
    assert SCRIPT_PATH.is_file(), (
        f"Bit A script not found at {SCRIPT_PATH}. "
        "Umbrella 86ba0jmyq Bit A must ship "
        "scripts/backfill/cross_asset_transfer_blended_prob.py."
    )


def test_module_imports_canonical_helpers():
    """Lock-step guard: any feature transform must route through
    `bot.helpers.derived_features` (or `scripts.cal_mlp.features` for
    apply_sigma_winsor/compute_hour_features). Inline drift re-opens the
    train/serve-skew surface."""
    src = SCRIPT_PATH.read_text()
    tree = ast.parse(src)
    imports_seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports_seen.add(f"{node.module}.{alias.name}")
    expected = {
        "bot.helpers.derived_features.compute_hour_sin_cos",
        "bot.helpers.derived_features.compute_derived_features",
    }
    missing = expected - imports_seen
    assert not missing, (
        f"Lock-step contract violated — script must import {missing} from "
        f"bot.helpers.derived_features (per bot/CLAUDE.md 'Helper-call sites'). "
        f"Got: {sorted(imports_seen)[:10]}..."
    )


def test_no_inline_hour_sin_cos_math():
    """Lock-step AST guard: no inline math.sin/math.cos on hour-of-day.

    Pattern: math.sin(2*math.pi*hour/24) or numpy.sin(...) on a name
    containing 'hour'. The canonical helper compute_hour_sin_cos() is
    the only allowed surface (mirrors HOUR_SINCOS_DRIFT_SITES pin in
    tests/contracts/test_calmlp_lockstep.py)."""
    src = SCRIPT_PATH.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("sin", "cos"):
                # OK if it's on a non-hour arg AND not from the file body
                # (we forbid ANY math.sin/cos call in this script to keep
                # the lock-step pin maximally simple).
                pytest.fail(
                    f"Inline {node.func.attr}() call found at line {node.lineno}. "
                    "Use bot.helpers.derived_features.compute_hour_sin_cos() instead."
                )


# ── API surface ───────────────────────────────────────────────────────


def test_module_exposes_required_api():
    """Script must expose a `backfill_db()` callable and a `main()` CLI entrypoint."""
    from scripts.backfill import cross_asset_transfer_blended_prob as m
    assert callable(getattr(m, "backfill_db", None)), "missing backfill_db()"
    assert callable(getattr(m, "main", None)), "missing main()"


# ── Skip categories — predict() raises pass-through ───────────────────


def test_skips_rows_with_null_raw_prob(tmp_path: Path, monkeypatch):
    """A row with NULL raw_prob is skipped — predictor never even invoked
    (saves the import-overhead amortized cost). blended_prob stays NULL."""
    from scripts.backfill import cross_asset_transfer_blended_prob as m

    db = tmp_path / "state.db"
    conn = _open(db)
    _seed_replay_table(conn)
    _insert_row(conn, raw_prob=None, calibrated_prob=None, blended_prob=None)
    conn.close()

    # Stub the predictor so the test doesn't depend on a real BTC bundle.
    fake_predictor = MagicMock()
    monkeypatch.setattr(m, "_build_btc_predictor", lambda: fake_predictor)

    result = m.backfill_db(str(db), assets=("HYPE",), dry_run=False)
    assert result["updated"] == 0
    assert result["skipped"]["null_raw_prob"] >= 1
    fake_predictor.predict.assert_not_called()

    conn = _open(db)
    val = conn.execute(
        f"SELECT blended_prob FROM {REPLAY_TABLE}"
    ).fetchone()[0]
    conn.close()
    assert val is None


def test_skips_rows_where_predictor_raises(tmp_path: Path, monkeypatch):
    """When CalMLPPredictor.predict raises (e.g. CalMLPError missing_features
    for NULL prob_breakeven_gap), the row is skipped and blended_prob stays NULL.

    This is the data-honest path on the current replay corpus (all 9,794
    rows have NULL prob_breakeven_gap). No fabricated values."""
    from scripts.backfill import cross_asset_transfer_blended_prob as m

    db = tmp_path / "state.db"
    conn = _open(db)
    _seed_replay_table(conn)
    _insert_row(conn, prob_breakeven_gap=None, blended_prob=None)
    conn.close()

    fake_predictor = MagicMock()
    fake_predictor.predict.side_effect = RuntimeError(
        "CalMLPError missing_features"
    )
    monkeypatch.setattr(m, "_build_btc_predictor", lambda: fake_predictor)

    result = m.backfill_db(str(db), assets=("HYPE",), dry_run=False)
    assert result["updated"] == 0
    assert result["skipped"]["predict_raised"] >= 1

    conn = _open(db)
    val = conn.execute(
        f"SELECT blended_prob FROM {REPLAY_TABLE}"
    ).fetchone()[0]
    conn.close()
    assert val is None


# ── Happy path ────────────────────────────────────────────────────────


def test_updates_blended_prob_on_full_feature_row(tmp_path: Path, monkeypatch):
    """Row with EVERY feature populated → predictor invoked → blended_prob written."""
    from scripts.backfill import cross_asset_transfer_blended_prob as m

    db = tmp_path / "state.db"
    conn = _open(db)
    _seed_replay_table(conn)
    _insert_row(conn, blended_prob=None)  # all features present (defaults)
    conn.close()

    fake_predictor = MagicMock()
    fake_predictor.predict.return_value = (0.7654, 0.01, 0.74, 0.79)
    monkeypatch.setattr(m, "_build_btc_predictor", lambda: fake_predictor)

    result = m.backfill_db(str(db), assets=("HYPE",), dry_run=False)
    assert result["updated"] == 1
    fake_predictor.predict.assert_called_once()

    conn = _open(db)
    val = conn.execute(
        f"SELECT blended_prob FROM {REPLAY_TABLE}"
    ).fetchone()[0]
    conn.close()
    assert val is not None
    assert abs(val - 0.7654) < 1e-9


# ── Idempotency ───────────────────────────────────────────────────────


def test_idempotent_rerun_skips_already_filled_rows(
    tmp_path: Path, monkeypatch
):
    """Re-running the backfill touches zero rows the second time because
    `WHERE blended_prob IS NULL` filters out rows the first run filled."""
    from scripts.backfill import cross_asset_transfer_blended_prob as m

    db = tmp_path / "state.db"
    conn = _open(db)
    _seed_replay_table(conn)
    _insert_row(conn, blended_prob=None)
    conn.close()

    fake_predictor = MagicMock()
    fake_predictor.predict.return_value = (0.55, 0.01, 0.50, 0.60)
    monkeypatch.setattr(m, "_build_btc_predictor", lambda: fake_predictor)

    r1 = m.backfill_db(str(db), assets=("HYPE",), dry_run=False)
    r2 = m.backfill_db(str(db), assets=("HYPE",), dry_run=False)
    assert r1["updated"] == 1
    assert r2["updated"] == 0
    assert r2["candidates"] == 0  # no NULL blended_prob rows remain
    assert fake_predictor.predict.call_count == 1


# ── Dry-run ───────────────────────────────────────────────────────────


def test_dry_run_reports_without_writing(tmp_path: Path, monkeypatch):
    """--dry-run reports counts but doesn't UPDATE."""
    from scripts.backfill import cross_asset_transfer_blended_prob as m

    db = tmp_path / "state.db"
    conn = _open(db)
    _seed_replay_table(conn)
    _insert_row(conn, blended_prob=None)
    conn.close()

    fake_predictor = MagicMock()
    fake_predictor.predict.return_value = (0.55, 0.01, 0.50, 0.60)
    monkeypatch.setattr(m, "_build_btc_predictor", lambda: fake_predictor)

    result = m.backfill_db(
        str(db), assets=("HYPE",), dry_run=True
    )
    assert result["candidates"] >= 1
    assert result["updated"] == 0  # dry-run never writes

    conn = _open(db)
    val = conn.execute(
        f"SELECT blended_prob FROM {REPLAY_TABLE}"
    ).fetchone()[0]
    conn.close()
    assert val is None


# ── Mac-only defensive guard ─────────────────────────────────────────


def test_refuses_vps_path(tmp_path: Path):
    """Must refuse paths under /home/botuser/ — Mac-only invariant per
    feedback_vps_compute_isolation."""
    from scripts.backfill import cross_asset_transfer_blended_prob as m
    with pytest.raises((ValueError, SystemExit)):
        m.backfill_db(
            "/home/botuser/kalshi-bot-repo/state.db",
            assets=("HYPE",),
            dry_run=True,
        )


# ── Predictor-build failure surfaces loudly ──────────────────────────


def test_predictor_build_failure_aborts_before_writes(
    tmp_path: Path, monkeypatch
):
    """If `_build_btc_predictor()` raises (bad bundle, torch import error,
    flock failure), the script must exit non-zero BEFORE touching any rows —
    don't pretend to backfill against a broken predictor."""
    from scripts.backfill import cross_asset_transfer_blended_prob as m

    db = tmp_path / "state.db"
    conn = _open(db)
    _seed_replay_table(conn)
    _insert_row(conn, blended_prob=None)
    conn.close()

    def boom():
        raise RuntimeError("simulated BTC predictor load failure")

    monkeypatch.setattr(m, "_build_btc_predictor", boom)

    with pytest.raises(RuntimeError, match="simulated BTC predictor"):
        m.backfill_db(str(db), assets=("HYPE",), dry_run=False)

    conn = _open(db)
    val = conn.execute(
        f"SELECT blended_prob FROM {REPLAY_TABLE}"
    ).fetchone()[0]
    conn.close()
    assert val is None  # row untouched
