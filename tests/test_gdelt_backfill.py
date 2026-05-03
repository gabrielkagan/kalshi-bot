"""Phase H-4a: GDELT news event clusters → evaluated_opportunities backfill.

Tests the backfill harness in scripts/gdelt_backfill.py with the GDELT
Doc API mocked. Mirrors the test conventions established in
test_shadow_coverage_backfill_g2.py / g4.py / g5.py:

  - Real SQLite via bot.StateManager + tmp_path.
  - All HTTP via injected `request_fn` / `fetcher` — no real network.
  - Eight named tests per the Phase-H spec:
      test_api_url_format
      test_cache_hit_avoids_api_call
      test_idempotent_rerun
      test_zero_data_aborts_not_silent_null
      test_handles_api_429_with_backoff
      test_resume_from_checkpoint
      test_db_columns_only_15m_rows
      (+ test_summarize_articles_basic — sanity of the aggregation helper)
"""

import datetime
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def _make_db(tmp_path):
    import bot
    return bot.StateManager(str(tmp_path / "test.db"))


def _insert_eval_row(
    sm,
    rid,
    asset="BTC",
    eval_time="2026-04-15T10:00:00Z",
    product_type="15m",
):
    """Insert a minimal evaluated_opportunities row sufficient for H-4a."""
    sm.conn.execute(
        "INSERT INTO evaluated_opportunities "
        "(id, ticker, event_ticker, asset, evaluation_time, product_type, "
        " filter_stage, side, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, f"T{rid}", f"E{rid}", asset, eval_time, product_type,
         "candidate", "yes", "evaluated"),
    )
    sm.conn.commit()


# ── API URL + parameter shape ──────────────────────────────────────────

class TestApiUrlFormat:

    def test_api_url_format(self):
        """Verify GDELT request goes to the documented endpoint with the
        expected query / mode / format / time params."""
        from gdelt_backfill import fetch_gdelt_articles, GDELT_DOC_URL

        captured = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"articles": []}

        def _req(url, params, timeout):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

        start = datetime.datetime(2026, 4, 15, 9, 0, 0, tzinfo=datetime.timezone.utc)
        end = datetime.datetime(2026, 4, 15, 10, 0, 0, tzinfo=datetime.timezone.utc)
        fetch_gdelt_articles("BTC", start, end, request_fn=_req)

        assert captured["url"] == GDELT_DOC_URL
        assert captured["params"]["mode"] == "ArtList"
        assert captured["params"]["format"] == "json"
        # GDELT timestamp shape YYYYMMDDHHMMSS:
        assert captured["params"]["startdatetime"] == "20260415090000"
        assert captured["params"]["enddatetime"] == "20260415100000"
        # Query must contain the asset's literal phrase (not a filter on
        # currency code that would over-match).
        assert "bitcoin" in captured["params"]["query"].lower()


# ── Round 4 fix #1 — bucket window must exclude future-of-decision data ─

class TestBucketWindowExcludesFutureData:

    def test_bucket_window_excludes_future_data(self):
        """The bucket window is the strictly-prior hour [hour-1, hour].
        Round 3 set it to [hour, hour+1] which leaked up to 59 minutes of
        post-decision news. Round 4 narrows it to the prior hour — zero
        leakage at the cost of up to 60 min trailing-edge loss.

        For a bucket key whose `hour_int=14`, the window must be
        [13:00, 14:00] UTC. The eval timestamp itself (any time in HH:00
        – HH:59) is the cutoff; nothing AT or AFTER `eval_hour:00` can
        be included."""
        from gdelt_backfill import _bucket_window
        start, end = _bucket_window(("2026-04-15", 14, "BTC"))
        assert start == datetime.datetime(
            2026, 4, 15, 13, 0, 0, tzinfo=datetime.timezone.utc,
        )
        assert end == datetime.datetime(
            2026, 4, 15, 14, 0, 0, tzinfo=datetime.timezone.utc,
        )
        # No part of the eval hour itself is in the window — strictly prior.
        # The decision was made at some HH:MM with HH=14; the window
        # ends at exactly 14:00 (exclusive of any post-decision data).
        assert end <= datetime.datetime(
            2026, 4, 15, 14, 0, 0, tzinfo=datetime.timezone.utc,
        )


# ── Caching: same hour bucket → 1 API call across many rows ────────────

class TestCacheHitAvoidsApiCall:

    def test_cache_hit_avoids_api_call(self, tmp_path):
        from gdelt_backfill import backfill_gdelt
        sm = _make_db(tmp_path)
        # 5 BTC rows in the same hour bucket should trigger 1 API call.
        for i in range(1, 6):
            _insert_eval_row(
                sm, i, asset="BTC",
                eval_time=f"2026-04-15T10:{i:02d}:00Z",
            )

        n_calls = {"n": 0}

        def _fake_fetcher(asset, start_dt, end_dt):
            n_calls["n"] += 1
            return [
                {"tone": "1.5"}, {"tone": "-2.1"}, {"tone": "0.0"},
            ]

        backfill_gdelt(sm.conn, fetcher=_fake_fetcher, sleep_ms=0)
        assert n_calls["n"] == 1

        # All 5 rows should now have the same count + tone.
        rows = sm.conn.execute(
            "SELECT gdelt_event_count_1h_pre_decision, "
            "       gdelt_avg_tone_1h_pre_decision "
            "FROM evaluated_opportunities ORDER BY id"
        ).fetchall()
        assert len(rows) == 5
        for r in rows:
            assert r[0] == 3
            assert r[1] == pytest.approx((1.5 - 2.1 + 0.0) / 3)


# ── Idempotency ────────────────────────────────────────────────────────

class TestIdempotentRerun:

    def test_idempotent_rerun(self, tmp_path):
        from gdelt_backfill import backfill_gdelt
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1)

        n_calls = {"n": 0}

        def _fetcher(asset, start_dt, end_dt):
            n_calls["n"] += 1
            return [{"tone": "1.0"}, {"tone": "2.0"}]

        backfill_gdelt(sm.conn, fetcher=_fetcher, sleep_ms=0)
        first_calls = n_calls["n"]
        # Re-run: no new API calls (row already has non-NULL count → WHERE filters out).
        backfill_gdelt(sm.conn, fetcher=_fetcher, sleep_ms=0)
        assert n_calls["n"] == first_calls

        row = sm.conn.execute(
            "SELECT gdelt_event_count_1h_pre_decision FROM "
            "evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert row[0] == 2


# ── Zero-data abort ────────────────────────────────────────────────────

class TestZeroDataAbortsNotSilentNull:

    def test_zero_data_aborts_not_silent_null(self, tmp_path):
        """If the API returns 0 articles for an asset across many buckets
        (likely silent-empty failure), abort rather than silently writing
        NULLs that look "backfilled" forever."""
        from gdelt_backfill import backfill_gdelt, GdeltFetchError
        sm = _make_db(tmp_path)
        # 8 BTC rows across 8 different hour buckets, all empty responses.
        for i, hour in enumerate(range(8), start=1):
            _insert_eval_row(
                sm, i, asset="BTC",
                eval_time=f"2026-04-15T{hour:02d}:00:00Z",
            )

        def _empty(asset, start_dt, end_dt):
            return []

        with pytest.raises(GdeltFetchError, match="zero data"):
            backfill_gdelt(sm.conn, fetcher=_empty, sleep_ms=0)


# ── 429 retry with backoff ─────────────────────────────────────────────

class TestHandlesApi429WithBackoff:

    def test_handles_api_429_with_backoff(self, monkeypatch):
        """First call → 429, second call → 200. Confirms retry path."""
        from gdelt_backfill import fetch_gdelt_articles
        # Speed up backoff sleeps in the test.
        import gdelt_backfill as gd
        monkeypatch.setattr(gd._time_mod, "sleep", lambda *_: None)

        calls = {"n": 0}

        class _Resp429:
            status_code = 429

            def json(self):
                return {}

        class _Resp200:
            status_code = 200

            def json(self):
                return {"articles": [{"tone": "0.5"}]}

        def _req(url, params, timeout):
            calls["n"] += 1
            return _Resp429() if calls["n"] == 1 else _Resp200()

        out = fetch_gdelt_articles(
            "BTC",
            datetime.datetime(2026, 4, 15, 10, 0, 0,
                              tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 4, 15, 11, 0, 0,
                              tzinfo=datetime.timezone.utc),
            request_fn=_req,
            max_retries=3,
        )
        assert calls["n"] == 2
        assert len(out) == 1


# ── Resume from checkpoint ─────────────────────────────────────────────

class TestResumeFromCheckpoint:

    def test_resume_from_checkpoint(self, tmp_path):
        """After a partial run, the checkpoint is honored on the next
        invocation — already-processed ids are skipped."""
        from gdelt_backfill import backfill_gdelt
        sm = _make_db(tmp_path)
        for i in range(1, 4):
            _insert_eval_row(
                sm, i, asset="BTC",
                eval_time=f"2026-04-15T1{i}:00:00Z",
            )
        ckpt = tmp_path / "ckpt"
        ckpt.mkdir()

        # The H-4a columns are added by the backfill harness on first
        # call (idempotent ALTER TABLE). Force them to exist before we
        # seed the "already-processed" rows.
        from gdelt_backfill import _ensure_local_columns
        _ensure_local_columns(sm.conn)

        # Pretend rows 1+2 were already processed in a prior run by
        # writing them out and seeding the checkpoint.
        sm.conn.execute(
            "UPDATE evaluated_opportunities SET "
            "gdelt_event_count_1h_pre_decision = 99 "
            "WHERE id IN (1, 2)"
        )
        sm.conn.commit()
        (ckpt / "h4a_gdelt.last_id").write_text("2")

        n_calls = {"n": 0}

        def _fetcher(asset, start_dt, end_dt):
            n_calls["n"] += 1
            return [{"tone": "0.0"}]

        n = backfill_gdelt(
            sm.conn, fetcher=_fetcher, sleep_ms=0,
            checkpoint_dir=str(ckpt),
        )
        # Only id=3 should have been touched.
        assert n == 1
        # Checkpoint advanced.
        assert (ckpt / "h4a_gdelt.last_id").read_text().strip() == "3"
        # Pre-existing rows 1+2 not overwritten.
        r1 = sm.conn.execute(
            "SELECT gdelt_event_count_1h_pre_decision FROM "
            "evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert r1[0] == 99


# ── Only 15m rows ──────────────────────────────────────────────────────

class TestDbColumnsOnly15mRows:

    def test_db_columns_only_15m_rows(self, tmp_path):
        """The backfill respects product_type='15m' filter so it doesn't
        touch hourly / weather / sports rows."""
        from gdelt_backfill import backfill_gdelt
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, asset="BTC", product_type="15m")
        _insert_eval_row(sm, 2, asset="BTC", product_type="hourly")
        _insert_eval_row(sm, 3, asset="BTC", product_type="weather")

        def _fetcher(asset, start_dt, end_dt):
            return [{"tone": "1.0"}]

        backfill_gdelt(sm.conn, fetcher=_fetcher, sleep_ms=0, only_15m=True)
        rows = sm.conn.execute(
            "SELECT id, gdelt_event_count_1h_pre_decision "
            "FROM evaluated_opportunities ORDER BY id"
        ).fetchall()
        assert rows[0][1] == 1   # 15m: backfilled
        assert rows[1][1] is None  # hourly: untouched
        assert rows[2][1] is None  # weather: untouched


# ── Aggregation sanity ────────────────────────────────────────────────

class TestSummarizeArticles:

    def test_summarize_articles_basic(self):
        from gdelt_backfill import summarize_articles
        n, t = summarize_articles([
            {"tone": "1.0"}, {"tone": "-1.0"}, {"tone": "3.0"},
        ])
        assert n == 3
        assert t == pytest.approx(1.0)

    def test_summarize_articles_drops_outliers(self):
        from gdelt_backfill import summarize_articles
        # Out-of-range tone gets dropped from MEAN but row counted in N.
        n, t = summarize_articles([
            {"tone": "1.0"}, {"tone": "1e9"},  # absurd
        ])
        assert n == 2
        assert t == pytest.approx(1.0)

    # Round 3 fix #3 — tone field fallback chain.
    def test_summarize_articles_tone_fallback_chain(self):
        """`tone` → `documenttone` → `docTone` fallback. Verifies all
        three spellings are recognized so the column doesn't silently
        NULL when GDELT's response uses a non-canonical key."""
        from gdelt_backfill import summarize_articles
        n, t = summarize_articles([
            {"tone": "2.0"},
            {"documenttone": "-2.0"},
            {"docTone": "4.0"},
        ])
        assert n == 3
        assert t == pytest.approx((2.0 - 2.0 + 4.0) / 3)

    def test_summarize_articles_uses_fixture(self):
        """Smoke test against the captured GDELT response shape in
        tests/fixtures/gdelt_artlist_sample.json. The fixture has 4
        articles: one each of `tone`, `documenttone`, `docTone`, and
        no tone field. Expected: n=4, mean=mean([2.81, -1.42, 0.55])."""
        import json
        import os
        from gdelt_backfill import summarize_articles
        fixture = os.path.join(
            os.path.dirname(__file__), "fixtures",
            "gdelt_artlist_sample.json",
        )
        with open(fixture) as f:
            body = json.load(f)
        n, t = summarize_articles(body["articles"])
        assert n == 4
        # Mean of the 3 articles that DO have a tone in any spelling.
        assert t == pytest.approx((2.81 - 1.42 + 0.55) / 3, abs=0.001)


# ── Round 4 cross-cutting — H-4a stamps data_provenance ───────────────

class TestGdeltStampsDataProvenance:

    def test_gdelt_stamps_data_provenance(self, tmp_path):
        """Round 4 cross-cutting (MEDIUM): the H-4a backfill must stamp
        `data_provenance='backfill_gdelt_hour_bucket'` so the v2 trainer
        can downweight or exclude these rows. COALESCE preserves any
        pre-existing provenance value."""
        from gdelt_backfill import backfill_gdelt
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, asset="BTC")

        def _fetcher(asset, start_dt, end_dt):
            return [{"tone": "1.0"}]

        backfill_gdelt(sm.conn, fetcher=_fetcher, sleep_ms=0)
        row = sm.conn.execute(
            "SELECT data_provenance FROM evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert row[0] == "backfill_gdelt_hour_bucket"

    def test_gdelt_preserves_existing_provenance(self, tmp_path):
        """COALESCE: if a row already has `'live_ws'` or
        `'backfill_60s_inputs'` provenance, H-4a must NOT overwrite it."""
        from gdelt_backfill import backfill_gdelt, _ensure_local_columns
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, asset="BTC")
        _ensure_local_columns(sm.conn)
        sm.conn.execute(
            "UPDATE evaluated_opportunities SET data_provenance = 'live_ws' "
            "WHERE id = 1"
        )
        sm.conn.commit()

        def _fetcher(asset, start_dt, end_dt):
            return [{"tone": "1.0"}]

        backfill_gdelt(sm.conn, fetcher=_fetcher, sleep_ms=0)
        row = sm.conn.execute(
            "SELECT data_provenance FROM evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert row[0] == "live_ws"


# ── Round 4 fix #2 — restored "ripple" XRP query ───────────────────────

class TestXrpQueryIncludesRipple:

    def test_xrp_query_includes_ripple(self):
        """Round 4 fix #2 (MEDIUM): restore "ripple" — it is the
        dominant news-headline term for XRP. Round 3 dropped it on
        symmetry grounds; recall regression was bigger than the small
        false-positive cost."""
        from gdelt_backfill import GDELT_ASSET_QUERIES
        q = GDELT_ASSET_QUERIES["XRP"].lower()
        assert "ripple" in q
        assert "xrp" in q
        assert "$xrp" in q
