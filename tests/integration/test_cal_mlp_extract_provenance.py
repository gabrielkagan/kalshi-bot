"""Phase 2 extract — data_provenance filter tests.

Per `kb/decisions/v2-cal-mlp-deploy-runbook-may03.md`, Phase 2 must support
SQL-side filtering on `data_provenance` so the v2 ablation can train two
distinct cohorts: live-only (`data_provenance='live_ws'`) and full-dataset
(`data_provenance IN ('live_ws','backfill_60s_inputs')`).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "cal_mlp"))


# Minimal CREATE TABLE matching the columns extract_data._check_schema requires
# plus data_provenance (the new requirement). Anything in REQUIRED_SOURCE_COLS
# that isn't here will trip the schema check — which is exactly what we want
# tested elsewhere.
_CREATE_TABLE_SQL = """
CREATE TABLE evaluated_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, evaluation_time TEXT, asset TEXT, side TEXT,
    strategy TEXT, product_type TEXT,
    market_price INTEGER, seconds_to_close REAL, vol_regime TEXT,
    z_score REAL, yes_spread_cents REAL, calibrated_prob REAL,
    raw_prob REAL, breakeven_wr REAL, fee_adjusted_edge REAL, kelly_f REAL,
    is_weekend INTEGER, hour_of_day_utc INTEGER, day_of_week INTEGER,
    market_result TEXT, settled_time TEXT, available_balance_cents REAL,
    spot_momentum_60s_bps REAL, spot_momentum_5m_bps REAL,
    spot_realized_range_15m_bps REAL,
    btc_spot_change_5m_bps REAL, btc_realized_vol_15m REAL,
    window_max_buf_pct REAL, window_min_buf_pct REAL, minutes_above_strike REAL,
    spot_distance_to_strike_sigma REAL, prob_breakeven_gap REAL,
    spot_coinbase_kraken_gap_bps REAL, kalshi_flow_depth_velocity REAL,
    data_provenance TEXT
)
"""

# A row that PASSES _classify_drop (so it lands in `kept`, not `drops`).
# Asset and provenance vary per insert.
_PASSING_ROW_TEMPLATE = dict(
    ticker="KXBTC15M-FAKE",
    evaluation_time="2026-04-15T10:00:00.000000Z",
    asset="BTC",
    side="yes",
    strategy="decided_contract_t1",
    product_type="15m",
    market_price=92,
    seconds_to_close=300.0,
    vol_regime="normal",
    z_score=0.5,
    yes_spread_cents=2.0,
    calibrated_prob=0.85,
    raw_prob=0.85,
    breakeven_wr=0.92,
    fee_adjusted_edge=0.05,
    kelly_f=0.10,
    is_weekend=0,
    hour_of_day_utc=10,
    day_of_week=2,
    market_result="yes",
    settled_time="2026-04-15T10:15:00.000000Z",
    available_balance_cents=10000.0,
    spot_momentum_60s_bps=1.0,
    spot_momentum_5m_bps=2.0,
    spot_realized_range_15m_bps=15.0,
    btc_spot_change_5m_bps=1.0,
    btc_realized_vol_15m=20.0,
    window_max_buf_pct=0.05,
    window_min_buf_pct=-0.02,
    minutes_above_strike=12.0,
    spot_distance_to_strike_sigma=0.3,
    prob_breakeven_gap=0.05,
    spot_coinbase_kraken_gap_bps=0.5,
    kalshi_flow_depth_velocity=0.0,
    data_provenance="live_ws",
)


def _insert_passing_row(conn: sqlite3.Connection, **overrides) -> None:
    row = dict(_PASSING_ROW_TEMPLATE)
    row.update(overrides)
    cols = list(row.keys())
    placeholders = ",".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO evaluated_opportunities ({','.join(cols)}) "
        f"VALUES ({placeholders})",
        tuple(row[c] for c in cols),
    )


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()
    return conn


# ── cfg_fp identity (bundle uniqueness) ────────────────────────────────


class TestCfgFpIncludesProvenanceFilter:
    """compute_cfg_fp must include provenance_filter so live_only and
    full_dataset bundles have distinct identities — Phase 6 A/B refuses
    to compare bundles with different cfg_fp."""

    def test_distinct_for_live_only_vs_full_dataset(self):
        from features import compute_cfg_fp

        live_only = compute_cfg_fp(
            include_sub_floor=True, provenance_filter="live_only",
        )
        full_dataset = compute_cfg_fp(
            include_sub_floor=True, provenance_filter="full_dataset",
        )
        assert live_only != full_dataset, (
            "live_only and full_dataset must produce distinct cfg_fps "
            "so bundle identity tracks training cohort"
        )

    def test_distinct_across_all_three_filter_levels(self):
        from features import compute_cfg_fp

        seen = set()
        for pf in ("live_only", "full_dataset", "all"):
            seen.add(
                compute_cfg_fp(include_sub_floor=True, provenance_filter=pf)
            )
        assert len(seen) == 3, (
            "all three provenance_filter levels must produce distinct cfg_fps"
        )

    def test_identical_when_other_args_match(self):
        from features import compute_cfg_fp

        a = compute_cfg_fp(
            include_sub_floor=True, provenance_filter="live_only",
        )
        b = compute_cfg_fp(
            include_sub_floor=True, provenance_filter="live_only",
        )
        assert a == b, "cfg_fp must be deterministic given the same inputs"

    def test_all_default_preserves_pre_change_cfg_fp(self):
        """Identity-preserving: provenance_filter='all' must produce the
        SAME cfg_fp as the pre-change function (which had no provenance_filter
        kwarg). Per adversarial review P1 — without this property, every
        existing v1-reproduction call site silently re-fingerprints."""
        from features import compute_cfg_fp

        no_kwarg = compute_cfg_fp(include_sub_floor=False)
        explicit_all = compute_cfg_fp(
            include_sub_floor=False, provenance_filter="all",
        )
        assert no_kwarg == explicit_all, (
            "compute_cfg_fp(provenance_filter='all') must equal the no-kwarg "
            "default for backwards compat with v1 reproduction"
        )

        # Same property at include_sub_floor=True (the v2-default path).
        no_kwarg_v2 = compute_cfg_fp(include_sub_floor=True)
        explicit_all_v2 = compute_cfg_fp(
            include_sub_floor=True, provenance_filter="all",
        )
        assert no_kwarg_v2 == explicit_all_v2

        # And it must STILL differ from live_only / full_dataset.
        live_only = compute_cfg_fp(
            include_sub_floor=True, provenance_filter="live_only",
        )
        assert explicit_all_v2 != live_only

    def test_all_default_pins_pre_change_hash_literal(self):
        """Structural lock: pin the cfg_fp values for both `all` modes to
        literal hashes. Per adversarial review round-2 P2 — the behavioral
        identity test alone does not catch a refactor that unconditionally
        injects the key with a constant value. If a future PR changes the
        canonical dict shape (even backwards-compatibly), this test fires
        and forces the change to be reviewed.

        If you intentionally rebase cfg_fp lineage, update both literals
        AND the comment explaining the lineage break (analogous to the
        sigma_winsor=25 break documented in run_pipeline.sh:60)."""
        from features import compute_cfg_fp

        # These hashes were captured at the time of the v2-ablation provenance
        # filter ship (2026-05-05). They reflect the current canonical dict
        # shape WITHOUT 'provenance_filter' (because value is 'all' triggers
        # the omission branch). Pinning them locks the architectural
        # invariant: 'all' produces the same hash as the no-provenance-filter
        # function would.
        EXPECTED_FALSE = "345978797274721f"
        EXPECTED_TRUE = "301ac85cef5cdc43"

        actual_false = compute_cfg_fp(include_sub_floor=False)
        actual_true = compute_cfg_fp(include_sub_floor=True)
        assert actual_false == EXPECTED_FALSE, (
            f"cfg_fp(include_sub_floor=False) drifted: expected "
            f"{EXPECTED_FALSE!r}, got {actual_false!r}. If intentional, "
            f"update this literal AND document the lineage break."
        )
        assert actual_true == EXPECTED_TRUE, (
            f"cfg_fp(include_sub_floor=True) drifted: expected "
            f"{EXPECTED_TRUE!r}, got {actual_true!r}. If intentional, "
            f"update this literal AND document the lineage break."
        )


# ── SQL filter behavior ────────────────────────────────────────────────


class TestPullAndClassifyProvenanceFilter:
    """The SQL pull must apply WHERE data_provenance IN (...) for the new
    filter modes, while preserving the existing 'all' behavior."""

    @pytest.fixture(autouse=True)
    def _require_pandas(self):
        # extract_data.py imports pandas/numpy/pyarrow at module load.
        # CI doesn't install these (they're cal_mlp-pipeline-only deps);
        # gracefully skip rather than fail on ModuleNotFoundError.
        pytest.importorskip("numpy")
        pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")

    def test_live_only_keeps_only_live_ws(self, tmp_path):
        from extract_data import pull_and_classify

        conn = _make_db(tmp_path)
        _insert_passing_row(
            conn, ticker="T1", data_provenance="live_ws",
        )
        _insert_passing_row(
            conn, ticker="T2", data_provenance="backfill_60s_inputs",
        )
        _insert_passing_row(
            conn, ticker="T3", data_provenance="other_provenance",
        )
        conn.commit()

        kept, _drops, source_total = pull_and_classify(
            conn, "BTC", "2026-04-16T00:00:00.000000Z", asset_floor=88,
            provenance_filter="live_only",
        )
        kept_tickers = {r["ticker"] for r in kept}
        assert kept_tickers == {"T1"}, (
            f"live_only must keep only live_ws rows; got {kept_tickers}"
        )
        # source_total reflects only the SQL-pulled rows (post WHERE).
        assert source_total == 1, (
            f"source_total should count only SQL-matched rows; got {source_total}"
        )

    def test_full_dataset_keeps_live_ws_and_backfill_60s(self, tmp_path):
        from extract_data import pull_and_classify

        conn = _make_db(tmp_path)
        _insert_passing_row(
            conn, ticker="T1", data_provenance="live_ws",
        )
        _insert_passing_row(
            conn, ticker="T2", data_provenance="backfill_60s_inputs",
        )
        _insert_passing_row(
            conn, ticker="T3", data_provenance="other_provenance",
        )
        _insert_passing_row(
            conn, ticker="T4", data_provenance=None,  # NULL provenance
        )
        conn.commit()

        kept, _drops, source_total = pull_and_classify(
            conn, "BTC", "2026-04-16T00:00:00.000000Z", asset_floor=88,
            provenance_filter="full_dataset",
        )
        kept_tickers = {r["ticker"] for r in kept}
        assert kept_tickers == {"T1", "T2"}, (
            f"full_dataset must keep live_ws + backfill_60s_inputs only; "
            f"got {kept_tickers}"
        )
        assert source_total == 2

    def test_all_default_preserves_pre_filter_behavior(self, tmp_path):
        from extract_data import pull_and_classify

        conn = _make_db(tmp_path)
        _insert_passing_row(
            conn, ticker="T1", data_provenance="live_ws",
        )
        _insert_passing_row(
            conn, ticker="T2", data_provenance="backfill_60s_inputs",
        )
        _insert_passing_row(
            conn, ticker="T3", data_provenance="other_provenance",
        )
        _insert_passing_row(
            conn, ticker="T4", data_provenance=None,
        )
        conn.commit()

        # 'all' = no SQL provenance filter — matches pre-change behavior.
        kept, _drops, source_total = pull_and_classify(
            conn, "BTC", "2026-04-16T00:00:00.000000Z", asset_floor=88,
            provenance_filter="all",
        )
        kept_tickers = {r["ticker"] for r in kept}
        assert kept_tickers == {"T1", "T2", "T3", "T4"}, (
            "'all' must match pre-filter behavior — every row passing "
            "_classify_drop is kept regardless of provenance"
        )
        assert source_total == 4

    def test_live_only_does_not_keep_backfill_even_if_only_provenance(self, tmp_path):
        """Adversarial: a state.db with no live_ws rows should produce 0 kept
        under live_only — NOT silently fall back to backfill rows."""
        from extract_data import pull_and_classify

        conn = _make_db(tmp_path)
        for i in range(5):
            _insert_passing_row(
                conn, ticker=f"T{i}", data_provenance="backfill_60s_inputs",
            )
        conn.commit()

        kept, _drops, _source = pull_and_classify(
            conn, "BTC", "2026-04-16T00:00:00.000000Z", asset_floor=88,
            provenance_filter="live_only",
        )
        assert kept == [], (
            "live_only must not silently keep backfill rows when no "
            "live_ws rows exist"
        )

    def test_unknown_filter_value_raises(self, tmp_path):
        """Adversarial: an invalid filter value must fail loud, not silently
        default to 'all'. Canonical exception is ValueError (matches
        compute_cfg_fp behavior); both sites use the same shared
        PROVENANCE_FILTER_CHOICES tuple."""
        from extract_data import pull_and_classify

        conn = _make_db(tmp_path)
        _insert_passing_row(conn, ticker="T1", data_provenance="live_ws")
        conn.commit()

        with pytest.raises(ValueError):
            pull_and_classify(
                conn, "BTC", "2026-04-16T00:00:00.000000Z", asset_floor=88,
                provenance_filter="not_a_real_filter",
            )


# ── data_provenance flows through to kept rows ──────────────────────────


class TestDataProvenanceInKeptRows:
    """data_provenance must appear in the kept-row dicts so Phase 6 can
    later filter the test fold to live_ws-only at evaluation time."""

    @pytest.fixture(autouse=True)
    def _require_pandas(self):
        pytest.importorskip("numpy")
        pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")

    def test_provenance_value_preserved_on_kept_rows(self, tmp_path):
        from extract_data import pull_and_classify

        conn = _make_db(tmp_path)
        _insert_passing_row(
            conn, ticker="T1", data_provenance="live_ws",
        )
        _insert_passing_row(
            conn, ticker="T2", data_provenance="backfill_60s_inputs",
        )
        conn.commit()

        kept, _drops, _source = pull_and_classify(
            conn, "BTC", "2026-04-16T00:00:00.000000Z", asset_floor=88,
            provenance_filter="full_dataset",
        )
        by_ticker = {r["ticker"]: r["data_provenance"] for r in kept}
        assert by_ticker["T1"] == "live_ws"
        assert by_ticker["T2"] == "backfill_60s_inputs"


# ── Schema check requires data_provenance column ───────────────────────


class TestSchemaCheckRequiresDataProvenance:
    """REQUIRED_SOURCE_COLS must include data_provenance so pre-G6 state.db
    snapshots fail-loud rather than silently extracting NULL provenance."""

    @pytest.fixture(autouse=True)
    def _require_pandas(self):
        # Importing extract_data triggers `import pandas` at module load,
        # even though this test only inspects a tuple constant.
        pytest.importorskip("numpy")
        pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")

    def test_data_provenance_in_required_source_cols(self):
        from extract_data import REQUIRED_SOURCE_COLS

        assert "data_provenance" in REQUIRED_SOURCE_COLS, (
            "data_provenance must be in REQUIRED_SOURCE_COLS so _check_schema "
            "rejects pre-G6 state.db (G-6 shipped 2026-05-03 added the column)"
        )


# ── CLI surface ────────────────────────────────────────────────────────


class TestCliFlagSurface:
    """parse_args must expose --provenance-filter with the three valid
    choices and a backwards-compat 'all' default."""

    @pytest.fixture(autouse=True)
    def _require_pandas(self):
        pytest.importorskip("numpy")
        pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")

    def test_provenance_filter_flag_present_with_choices(self, monkeypatch):
        import extract_data

        monkeypatch.setattr(sys, "argv", ["extract_data.py", "--asset", "BTC"])
        args = extract_data.parse_args()
        assert hasattr(args, "provenance_filter"), (
            "parse_args must expose --provenance-filter as args.provenance_filter"
        )
        # Default is 'all' for backwards compat with v1-style runs.
        assert args.provenance_filter == "all"

    def test_provenance_filter_accepts_live_only(self, monkeypatch):
        import extract_data

        monkeypatch.setattr(
            sys, "argv",
            ["extract_data.py", "--asset", "BTC", "--provenance-filter", "live_only"],
        )
        args = extract_data.parse_args()
        assert args.provenance_filter == "live_only"

    def test_provenance_filter_accepts_full_dataset(self, monkeypatch):
        import extract_data

        monkeypatch.setattr(
            sys, "argv",
            ["extract_data.py", "--asset", "BTC", "--provenance-filter", "full_dataset"],
        )
        args = extract_data.parse_args()
        assert args.provenance_filter == "full_dataset"

    def test_provenance_filter_rejects_unknown(self, monkeypatch, capsys):
        import extract_data

        monkeypatch.setattr(
            sys, "argv",
            ["extract_data.py", "--asset", "BTC", "--provenance-filter", "garbage"],
        )
        with pytest.raises(SystemExit):
            extract_data.parse_args()
