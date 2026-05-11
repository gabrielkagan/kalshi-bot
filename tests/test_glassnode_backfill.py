"""Phase H-4b: Glassnode on-chain → evaluated_opportunities backfill tests.

Eight named tests required by the Phase-H spec, plus a couple of helper
tests for the z-score path (since z-score is the central computation
that distinguishes H-4b from H-4a/c)."""

import datetime
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def _make_db(tmp_path):
    import bot
    return bot.state.StateManager(str(tmp_path / "test.db"))


def _insert_eval_row(
    sm,
    rid,
    asset="BTC",
    eval_time="2026-04-15T10:00:00Z",
    product_type="15m",
):
    sm.conn.execute(
        "INSERT INTO evaluated_opportunities "
        "(id, ticker, event_ticker, asset, evaluation_time, product_type, "
        " filter_stage, side, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, f"T{rid}", f"E{rid}", asset, eval_time, product_type,
         "candidate", "yes", "evaluated"),
    )
    sm.conn.commit()


def _build_daily_series(start_date, n_days, base=100.0, step=1.0):
    """Helper: return Glassnode-style raw series — list of {t, v} dicts.

    Each day increments value by `step` (deterministic, monotonic so
    z-score has finite stddev)."""
    out = []
    for i in range(n_days):
        d = start_date + datetime.timedelta(days=i)
        ts = int(datetime.datetime(d.year, d.month, d.day,
                                   tzinfo=datetime.timezone.utc).timestamp())
        out.append({"t": ts, "v": base + step * i})
    return out


# ── API URL + parameter shape ──────────────────────────────────────────

class TestApiUrlFormat:

    def test_api_url_format(self):
        from glassnode_backfill import (
            fetch_glassnode_metric, GLASSNODE_BASE_URL,
        )
        captured = {}

        class _Resp:
            status_code = 200

            def json(self):
                return []

        def _req(url, params, timeout):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

        start = datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc)
        end = datetime.datetime(2026, 4, 30, tzinfo=datetime.timezone.utc)
        fetch_glassnode_metric(
            "addresses/active_count", "BTC", start, end, request_fn=_req,
        )
        assert captured["url"] == f"{GLASSNODE_BASE_URL}/addresses/active_count"
        assert captured["params"]["a"] == "BTC"
        assert captured["params"]["i"] == "24h"
        assert captured["params"]["s"] == int(start.timestamp())
        assert captured["params"]["u"] == int(end.timestamp())


# ── Cache: one fetch per metric, not per row ───────────────────────────

class TestCacheHitAvoidsApiCall:

    def test_cache_hit_avoids_api_call(self, tmp_path):
        """Glassnode fetches ONCE per metric for the whole window —
        N rows → still 1 fetch per metric series. Verifies that
        scaling by row count does NOT inflate the API cost."""
        from glassnode_backfill import backfill_glassnode
        sm = _make_db(tmp_path)
        for i in range(1, 11):
            _insert_eval_row(
                sm, i, asset="BTC",
                eval_time=f"2026-04-15T{i:02d}:00:00Z",
            )

        n_calls = {"n": 0}

        def _fetcher(path, asset, s, e, *, api_key=None):
            n_calls["n"] += 1
            return _build_daily_series(
                datetime.date(2026, 3, 1), n_days=60,
            )

        backfill_glassnode(sm.conn, fetcher=_fetcher, sleep_ms=0)
        # 3 metrics defined → expect ≤3 fetches regardless of row count.
        # (Premium metric may be skipped if no API key — that's fine; we
        # assert the strict upper bound.)
        assert n_calls["n"] <= 3
        assert n_calls["n"] >= 2  # At least the 2 free-tier metrics.


# ── Idempotency ────────────────────────────────────────────────────────

class TestIdempotentRerun:

    def test_idempotent_rerun(self, tmp_path):
        from glassnode_backfill import backfill_glassnode
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, eval_time="2026-04-15T10:00:00Z")

        series = _build_daily_series(datetime.date(2026, 3, 1), n_days=60)

        def _fetcher(path, asset, s, e, *, api_key=None):
            return series

        backfill_glassnode(sm.conn, fetcher=_fetcher, sleep_ms=0)
        n_calls = {"n": 0}

        def _fetcher2(path, asset, s, e, *, api_key=None):
            n_calls["n"] += 1
            return series

        # Re-run: row already has non-NULL → range query returns empty,
        # function returns 0 without fetching anything.
        backfill_glassnode(sm.conn, fetcher=_fetcher2, sleep_ms=0)
        assert n_calls["n"] == 0


# ── Zero-data abort: ALL free metrics empty → raise ────────────────────

class TestZeroDataAbortsNotSilentNull:

    def test_zero_data_aborts_not_silent_null(self, tmp_path):
        from glassnode_backfill import backfill_glassnode, GlassnodeFetchError
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1)

        def _empty(path, asset, s, e, *, api_key=None):
            return []

        with pytest.raises(GlassnodeFetchError, match="zero data"):
            backfill_glassnode(sm.conn, fetcher=_empty, sleep_ms=0)


# ── Z-score correctness ───────────────────────────────────────────────

class TestZscoreComputationCorrect:

    def test_zscore_computation_correct(self):
        """Hand-calc: value=110, history=[100, 100, 100, 100, 100, 100,
        100, 100, 100, 100] → mean=100, std=0 → None.
        With history [100, 101, 102, ..., 109]: mean=104.5, std≈3.03,
        value=110 → z ≈ (110 - 104.5) / 3.03 ≈ 1.815."""
        from glassnode_backfill import compute_zscore
        # Constant history → undefined std → None.
        assert compute_zscore(110.0, [100.0] * 10) is None
        # Linearly varying history.
        history = [100.0 + i for i in range(10)]
        z = compute_zscore(110.0, history)
        assert z is not None
        assert z == pytest.approx(1.815, abs=0.01)

    def test_zscore_returns_none_for_short_history(self):
        from glassnode_backfill import compute_zscore
        assert compute_zscore(110.0, [100.0, 101.0]) is None  # n=2

    def test_lookup_zscore_excludes_target_from_history(self):
        """No-leakage check: the trailing window excludes the target
        date itself (else the target value contaminates its own mean
        and the z-score is biased toward 0)."""
        from glassnode_backfill import lookup_zscore_for_date
        # Build series with target=100, history all=100.
        # If target were INCLUDED in mean, std would still be 0 → None.
        # If target is EXCLUDED, std is also 0 → None.
        # To make the test sensitive to the exclusion: set target to
        # 200 and history to a varying series. Compare z computed
        # excluding-target against z computed including-target.
        series = []
        target_date = datetime.date(2026, 4, 15)
        # 30 days BEFORE target with values 100..129.
        for i in range(30):
            d = target_date - datetime.timedelta(days=30 - i)
            series.append((d, 100.0 + i))
        # Target date itself with anomalous value 200.
        series.append((target_date, 200.0))
        z = lookup_zscore_for_date(series, target_date, window_days=30)
        # Hand-calc: history mean=114.5, std≈8.80, z=(200-114.5)/8.80 ≈ 9.71.
        assert z is not None
        assert z == pytest.approx(9.71, abs=0.1)


# ── 429 retry ──────────────────────────────────────────────────────────

class TestHandlesApi429WithBackoff:

    def test_handles_api_429_with_backoff(self, monkeypatch):
        from glassnode_backfill import fetch_glassnode_metric
        import glassnode_backfill as gn
        monkeypatch.setattr(gn._time_mod, "sleep", lambda *_: None)

        calls = {"n": 0}

        class _Resp429:
            status_code = 429

            def json(self):
                return {}

        class _Resp200:
            status_code = 200

            def json(self):
                return [{"t": 1700000000, "v": 1.0}]

        def _req(url, params, timeout):
            calls["n"] += 1
            return _Resp429() if calls["n"] == 1 else _Resp200()

        out = fetch_glassnode_metric(
            "addresses/active_count", "BTC",
            datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 4, 2, tzinfo=datetime.timezone.utc),
            request_fn=_req, max_retries=3,
        )
        assert calls["n"] == 2
        assert len(out) == 1


# ── Resume from checkpoint ─────────────────────────────────────────────

class TestResumeFromCheckpoint:

    def test_resume_from_checkpoint(self, tmp_path):
        from glassnode_backfill import backfill_glassnode
        sm = _make_db(tmp_path)
        for i in range(1, 4):
            _insert_eval_row(
                sm, i, asset="BTC",
                eval_time=f"2026-04-15T{10 + i:02d}:00:00Z",
            )
        ckpt = tmp_path / "ckpt"
        ckpt.mkdir()
        from glassnode_backfill import _ensure_local_columns
        _ensure_local_columns(sm.conn)
        sm.conn.execute(
            "UPDATE evaluated_opportunities SET "
            "btc_active_addresses_24h_zscore = 7.0, "
            "eth_active_addresses_24h_zscore = 7.0 "
            "WHERE id IN (1, 2)"
        )
        sm.conn.commit()
        (ckpt / "h4b_glassnode.last_id").write_text("2")

        series = _build_daily_series(datetime.date(2026, 3, 1), n_days=60)

        def _fetcher(path, asset, s, e, *, api_key=None):
            return series

        n = backfill_glassnode(
            sm.conn, fetcher=_fetcher, sleep_ms=0,
            checkpoint_dir=str(ckpt),
        )
        assert n == 1
        # Pre-existing rows untouched.
        r1 = sm.conn.execute(
            "SELECT btc_active_addresses_24h_zscore "
            "FROM evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert r1[0] == 7.0


# ── Round 3 fix #7 — 401/403 on a FREE-tier metric must abort ─────────

class TestAbortsOnGlassnode401:

    def test_aborts_on_glassnode_401_for_free_metric(self, tmp_path):
        """If a metric we expected to be free-tier returns 401/403, the
        backfill must abort rather than silently NULL the column. (A
        Glassnode policy change that moves a metric from free → paid
        without our knowing it would otherwise produce a quiet
        backfill of all-NULLs.)"""
        from glassnode_backfill import (
            backfill_glassnode, GlassnodeAuthError, GlassnodeFetchError,
        )
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1)

        def _auth_err(path, asset, s, e, *, api_key=None):
            # Simulate Glassnode flipping the metric to paid-only.
            raise GlassnodeAuthError(
                f"metric={path} asset={asset}: HTTP 401 (auth required)"
            )

        with pytest.raises(GlassnodeFetchError, match="free-tier"):
            backfill_glassnode(sm.conn, fetcher=_auth_err, sleep_ms=0)

    def test_fetch_glassnode_metric_raises_authError_on_401(self):
        """Network-level: a 401 response must raise GlassnodeAuthError,
        NOT silently return [] (the previous behavior)."""
        from glassnode_backfill import (
            fetch_glassnode_metric, GlassnodeAuthError,
        )

        class _Resp401:
            status_code = 401

            def json(self):
                return {}

        def _req(url, params, timeout):
            return _Resp401()

        with pytest.raises(GlassnodeAuthError):
            fetch_glassnode_metric(
                "addresses/active_count", "BTC",
                datetime.datetime(2026, 4, 1,
                                  tzinfo=datetime.timezone.utc),
                datetime.datetime(2026, 4, 2,
                                  tzinfo=datetime.timezone.utc),
                request_fn=_req, max_retries=3,
            )


# ── Only 15m rows ──────────────────────────────────────────────────────

class TestDbColumnsOnly15mRows:

    def test_db_columns_only_15m_rows(self, tmp_path):
        from glassnode_backfill import backfill_glassnode
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, product_type="15m")
        _insert_eval_row(sm, 2, product_type="hourly")
        _insert_eval_row(sm, 3, product_type="sports")

        series = _build_daily_series(datetime.date(2026, 3, 1), n_days=60)

        def _fetcher(path, asset, s, e, *, api_key=None):
            return series

        backfill_glassnode(
            sm.conn, fetcher=_fetcher, sleep_ms=0, only_15m=True,
        )
        rows = sm.conn.execute(
            "SELECT id, btc_active_addresses_24h_zscore "
            "FROM evaluated_opportunities ORDER BY id"
        ).fetchall()
        assert rows[0][1] is not None
        assert rows[1][1] is None
        assert rows[2][1] is None


# ── Round 4 fix #1 (CRITICAL) — H-4b stamps data_provenance ───────────

class TestGlassnodeStampsDataProvenance:

    def test_glassnode_stamps_data_provenance(self, tmp_path):
        """Round 4 fix #1 (CRITICAL): the H-4b backfill must stamp
        `data_provenance='backfill_glassnode_daily'` so the v2 trainer
        can downweight or exclude these rows. The docstring + design
        doc + v2-skew doc all promise this stamp; the UPDATE was
        previously missing it."""
        from glassnode_backfill import backfill_glassnode
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, asset="BTC")

        series = _build_daily_series(datetime.date(2026, 3, 1), n_days=60)

        def _fetcher(path, asset, s, e, *, api_key=None):
            return series

        backfill_glassnode(sm.conn, fetcher=_fetcher, sleep_ms=0)
        row = sm.conn.execute(
            "SELECT data_provenance FROM evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert row[0] == "backfill_glassnode_daily"

    def test_glassnode_preserves_existing_provenance(self, tmp_path):
        """COALESCE: if a row already has e.g. `'backfill_60s_inputs'`
        (from G-2/G-4) or `'live_ws'`, H-4b must NOT overwrite it."""
        from glassnode_backfill import backfill_glassnode, _ensure_local_columns
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, asset="BTC")
        _ensure_local_columns(sm.conn)
        sm.conn.execute(
            "UPDATE evaluated_opportunities SET "
            "data_provenance = 'backfill_60s_inputs' WHERE id = 1"
        )
        sm.conn.commit()

        series = _build_daily_series(datetime.date(2026, 3, 1), n_days=60)

        def _fetcher(path, asset, s, e, *, api_key=None):
            return series

        backfill_glassnode(sm.conn, fetcher=_fetcher, sleep_ms=0)
        row = sm.conn.execute(
            "SELECT data_provenance FROM evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert row[0] == "backfill_60s_inputs"


# ── Schema-add regression: lock contention must NOT silently swallow ───

class TestEnsureLocalColumnsUnderLockContention:
    """Regression for the May 4 smoke-test failure on VPS, where
    `_ensure_local_columns` silently passed on a `database is locked`
    error (a transient lock-contention `OperationalError`, not the
    intended `duplicate column` `OperationalError`), then the next
    SELECT crashed with `no such column`. The original `try: ALTER ...
    except sqlite3.OperationalError: pass` was too broad."""

    def test_lock_contention_raises_does_not_silently_pass(self, tmp_path):
        """Hold an exclusive write lock on a separate connection while
        calling `_ensure_local_columns` with a short busy_timeout. A
        transient lock failure must propagate (so callers see it) — NOT
        be silently swallowed and leave columns missing.

        Repros the May 4 VPS failure: bot's snapshotter thread held the
        write lock long enough that the script's ALTER TABLE timed out;
        the broad except hid the failure; the next SELECT crashed."""
        from glassnode_backfill import _ensure_local_columns
        # Use a real file so a second connection on the same DB is
        # meaningful (unlike :memory:).
        db_path = tmp_path / "lock_test.db"
        # Ensure the table exists first.
        bootstrap = sqlite3.connect(str(db_path))
        bootstrap.execute("PRAGMA journal_mode=WAL")
        bootstrap.execute(
            "CREATE TABLE evaluated_opportunities (id INTEGER PRIMARY KEY)"
        )
        bootstrap.commit()
        bootstrap.close()

        # Connection A: hold a write lock.
        holder = sqlite3.connect(str(db_path), timeout=30.0)
        holder.execute("PRAGMA journal_mode=WAL")
        # BEGIN IMMEDIATE acquires RESERVED — blocks DDL exclusive.
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO evaluated_opportunities (id) VALUES (1)"
        )
        try:
            # Connection B: short busy_timeout so ALTER fails fast.
            target = sqlite3.connect(str(db_path), timeout=0.5)
            target.execute("PRAGMA journal_mode=WAL")
            target.execute("PRAGMA busy_timeout=200")
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                _ensure_local_columns(target)
            # The failure must be lock-related, not a duplicate-column
            # bystander. Both phrases SQLite uses for lock failures.
            msg = str(excinfo.value).lower()
            assert "lock" in msg or "busy" in msg, (
                f"expected lock/busy error, got: {excinfo.value!r}"
            )
            target.close()
        finally:
            holder.rollback()
            holder.close()

    def test_duplicate_column_still_swallowed(self, tmp_path):
        """The narrow exception filter must STILL swallow
        `duplicate column` errors — that's the intended idempotency
        path. Calling `_ensure_local_columns` twice on the same DB
        must succeed both times without raising."""
        from glassnode_backfill import _ensure_local_columns
        sm = _make_db(tmp_path)
        # First call adds the columns.
        _ensure_local_columns(sm.conn)
        # Second call: ALTER would emit "duplicate column" — must still
        # be swallowed. Function returns cleanly.
        _ensure_local_columns(sm.conn)
        # Verify columns are in fact present.
        existing = {
            row[1] for row in sm.conn.execute(
                "PRAGMA table_info(evaluated_opportunities)"
            ).fetchall()
        }
        for col in (
            "btc_active_addresses_24h_zscore",
            "eth_active_addresses_24h_zscore",
            "btc_exchange_inflow_24h_zscore",
            "data_provenance",
        ):
            assert col in existing, f"column {col} missing after _ensure_local_columns"

    def test_columns_actually_present_after_ensure(self, tmp_path):
        """Belt-and-braces: after `_ensure_local_columns` returns
        successfully (no exception), all four expected columns MUST
        exist. Regression against any future refactor that decouples
        the ALTER from the column appearing in PRAGMA table_info."""
        from glassnode_backfill import _ensure_local_columns
        sm = _make_db(tmp_path)
        _ensure_local_columns(sm.conn)
        existing = {
            row[1] for row in sm.conn.execute(
                "PRAGMA table_info(evaluated_opportunities)"
            ).fetchall()
        }
        required = {
            "btc_active_addresses_24h_zscore",
            "eth_active_addresses_24h_zscore",
            "btc_exchange_inflow_24h_zscore",
            "data_provenance",
        }
        missing = required - existing
        assert not missing, f"_ensure_local_columns left missing: {missing}"
