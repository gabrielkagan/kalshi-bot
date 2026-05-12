"""Phase H-4c: CryptoCompare news sentiment → evaluated_opportunities tests."""

import datetime
import os
import sys

import pytest
import bot.state  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def _make_db(tmp_path):
    import bot
    import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
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


def _article(label, when_iso):
    """Build a CC news article fixture. `label` ∈ POSITIVE/NEUTRAL/NEGATIVE."""
    dt = datetime.datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
    return {"sentiment": label, "published_on": int(dt.timestamp())}


# ── API URL + parameter shape ──────────────────────────────────────────

class TestApiUrlFormat:

    def test_api_url_format(self):
        from cryptocompare_news_backfill import (
            fetch_cryptocompare_news, CRYPTOCOMPARE_NEWS_URL,
        )
        captured = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"Type": 100, "Data": []}

        def _req(url, params, timeout):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

        end = datetime.datetime(2026, 4, 15, 10, 0, 0,
                                tzinfo=datetime.timezone.utc)
        fetch_cryptocompare_news("BTC", end, request_fn=_req)
        assert captured["url"] == CRYPTOCOMPARE_NEWS_URL
        assert captured["params"]["lang"] == "EN"
        assert captured["params"]["categories"] == "BTC"
        assert captured["params"]["lTs"] == int(end.timestamp())


# ── Cache: same hour bucket → 1 API call ──────────────────────────────

class TestCacheHitAvoidsApiCall:

    def test_cache_hit_avoids_api_call(self, tmp_path):
        from cryptocompare_news_backfill import backfill_cryptocompare
        sm = _make_db(tmp_path)
        for i in range(1, 6):
            _insert_eval_row(
                sm, i, asset="BTC",
                eval_time=f"2026-04-15T10:{i:02d}:00Z",
            )

        n_calls = {"n": 0}

        def _fetcher(asset, end_dt):
            n_calls["n"] += 1
            # Round 4 fix #1: bucket window is now the strictly-prior
            # hour [09:00, 10:00] for an eval at hour=10. Articles must
            # be inside that hour to count.
            return [
                _article("POSITIVE", "2026-04-15T09:15:00Z"),
                _article("NEGATIVE", "2026-04-15T09:45:00Z"),
            ]

        # Round 4 fix #2: default min_eval_age_hours is now 24h. Use
        # `now_fn` so the eval timestamps look old enough to process.
        fixed_now = datetime.datetime(
            2026, 4, 17, 0, 0, 0, tzinfo=datetime.timezone.utc,
        )
        backfill_cryptocompare(
            sm.conn, fetcher=_fetcher, sleep_ms=0, now_fn=lambda: fixed_now,
        )
        assert n_calls["n"] == 1
        rows = sm.conn.execute(
            "SELECT news_sentiment_score_1h_pre_decision "
            "FROM evaluated_opportunities ORDER BY id"
        ).fetchall()
        assert len(rows) == 5
        for r in rows:
            assert r[0] == pytest.approx(0.0)  # +1 + -1 averaged.


# ── Idempotency ────────────────────────────────────────────────────────

class TestIdempotentRerun:

    def test_idempotent_rerun(self, tmp_path):
        from cryptocompare_news_backfill import backfill_cryptocompare
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1)

        n_calls = {"n": 0}

        def _fetcher(asset, end_dt):
            n_calls["n"] += 1
            # Round 4 fix #1: bucket window is now [09:00, 10:00] for
            # an eval at 10:00:00; article must be inside that hour.
            return [_article("POSITIVE", "2026-04-15T09:30:00Z")]

        # Round 4 fix #2: default min_eval_age_hours bumped to 24.
        fixed_now = datetime.datetime(
            2026, 4, 17, 0, 0, 0, tzinfo=datetime.timezone.utc,
        )
        backfill_cryptocompare(
            sm.conn, fetcher=_fetcher, sleep_ms=0, now_fn=lambda: fixed_now,
        )
        first = n_calls["n"]
        backfill_cryptocompare(
            sm.conn, fetcher=_fetcher, sleep_ms=0, now_fn=lambda: fixed_now,
        )
        assert n_calls["n"] == first

        row = sm.conn.execute(
            "SELECT news_sentiment_score_1h_pre_decision "
            "FROM evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert row[0] == pytest.approx(1.0)


# ── Zero-data abort ────────────────────────────────────────────────────

class TestZeroDataAbortsNotSilentNull:

    def test_zero_data_aborts_not_silent_null(self, tmp_path):
        from cryptocompare_news_backfill import (
            backfill_cryptocompare, CryptoCompareFetchError,
        )
        sm = _make_db(tmp_path)
        for i, hour in enumerate(range(8), start=1):
            _insert_eval_row(
                sm, i, asset="BTC",
                eval_time=f"2026-04-15T{hour:02d}:00:00Z",
            )

        def _empty(asset, end_dt):
            return []

        # Round 4 fix #2: default min_eval_age_hours is now 24h. Use a
        # fixed `now_fn` far in the future so the relabel-window guard
        # doesn't short-circuit before we get to the zero-data check.
        fixed_now = datetime.datetime(
            2026, 4, 17, 0, 0, 0, tzinfo=datetime.timezone.utc,
        )
        with pytest.raises(CryptoCompareFetchError, match="zero data"):
            backfill_cryptocompare(
                sm.conn, fetcher=_empty, sleep_ms=0,
                now_fn=lambda: fixed_now,
            )


# ── 429 retry ──────────────────────────────────────────────────────────

class TestHandlesApi429WithBackoff:

    def test_handles_api_429_with_backoff(self, monkeypatch):
        from cryptocompare_news_backfill import fetch_cryptocompare_news
        import cryptocompare_news_backfill as cc
        monkeypatch.setattr(cc._time_mod, "sleep", lambda *_: None)

        calls = {"n": 0}

        class _Resp429:
            status_code = 429

            def json(self):
                return {}

        class _Resp200:
            status_code = 200

            def json(self):
                return {"Type": 100, "Data": [
                    {"sentiment": "POSITIVE",
                     "published_on": 1700000000},
                ]}

        def _req(url, params, timeout):
            calls["n"] += 1
            return _Resp429() if calls["n"] == 1 else _Resp200()

        out = fetch_cryptocompare_news(
            "BTC",
            datetime.datetime(2026, 4, 15, 10, 0, 0,
                              tzinfo=datetime.timezone.utc),
            request_fn=_req, max_retries=3,
        )
        assert calls["n"] == 2
        assert len(out) == 1


# ── Resume from checkpoint ─────────────────────────────────────────────

class TestResumeFromCheckpoint:

    def test_resume_from_checkpoint(self, tmp_path):
        from cryptocompare_news_backfill import backfill_cryptocompare
        sm = _make_db(tmp_path)
        for i in range(1, 4):
            _insert_eval_row(
                sm, i, asset="BTC",
                eval_time=f"2026-04-15T1{i}:00:00Z",
            )
        ckpt = tmp_path / "ckpt"
        ckpt.mkdir()
        from cryptocompare_news_backfill import _ensure_local_columns
        _ensure_local_columns(sm.conn)
        # Mark rows 1+2 as already processed.
        sm.conn.execute(
            "UPDATE evaluated_opportunities SET "
            "news_sentiment_score_1h_pre_decision = 0.5 "
            "WHERE id IN (1, 2)"
        )
        sm.conn.commit()
        (ckpt / "h4c_cc_news.last_id").write_text("2")

        def _fetcher(asset, end_dt):
            # Round 4 fix #1: id=3 has eval_time 2026-04-15T13:00:00Z →
            # bucket window now [12:00, 13:00] (strictly prior); article
            # must be inside that hour.
            return [_article("POSITIVE", "2026-04-15T12:30:00Z")]

        # Round 4 fix #2: default min_eval_age_hours bumped to 24.
        fixed_now = datetime.datetime(
            2026, 4, 17, 0, 0, 0, tzinfo=datetime.timezone.utc,
        )
        n = backfill_cryptocompare(
            sm.conn, fetcher=_fetcher, sleep_ms=0,
            checkpoint_dir=str(ckpt), now_fn=lambda: fixed_now,
        )
        assert n == 1
        r1 = sm.conn.execute(
            "SELECT news_sentiment_score_1h_pre_decision "
            "FROM evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert r1[0] == 0.5


# ── Only 15m rows ──────────────────────────────────────────────────────

class TestDbColumnsOnly15mRows:

    def test_db_columns_only_15m_rows(self, tmp_path):
        from cryptocompare_news_backfill import backfill_cryptocompare
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, product_type="15m")
        _insert_eval_row(sm, 2, product_type="hourly")
        _insert_eval_row(sm, 3, product_type="weather")

        def _fetcher(asset, end_dt):
            # Round 4 fix #1: bucket window is now [09:00, 10:00] for
            # eval at 10:00:00 (strictly prior hour, no leakage).
            return [_article("POSITIVE", "2026-04-15T09:30:00Z")]

        # Round 4 fix #2: default min_eval_age_hours bumped to 24.
        fixed_now = datetime.datetime(
            2026, 4, 17, 0, 0, 0, tzinfo=datetime.timezone.utc,
        )
        backfill_cryptocompare(
            sm.conn, fetcher=_fetcher, sleep_ms=0, only_15m=True,
            now_fn=lambda: fixed_now,
        )
        rows = sm.conn.execute(
            "SELECT id, news_sentiment_score_1h_pre_decision "
            "FROM evaluated_opportunities ORDER BY id"
        ).fetchall()
        assert rows[0][1] == pytest.approx(1.0)
        assert rows[1][1] is None
        assert rows[2][1] is None


# ── Round 3 fix #12 — skip rows inside the CC relabel window ──────────

class TestSkipsRowsWithinRelabelWindow:

    def test_skips_rows_within_relabel_window(self, tmp_path):
        """CryptoCompare relabels article sentiment for a window after
        publication. Rows whose evaluation_time is within the relabel
        window (default 2h) must be SKIPPED — backfilling them would
        cache an unstable label that never gets re-pulled.

        Verifies: row at NOW-1h is skipped (still in 2h window), row
        at NOW-3h is processed normally."""
        from cryptocompare_news_backfill import backfill_cryptocompare
        sm = _make_db(tmp_path)

        fixed_now = datetime.datetime(
            2026, 4, 15, 12, 0, 0, tzinfo=datetime.timezone.utc,
        )
        # id=1 at 09:00 (3h ago) → process. id=2 at 11:00 (1h ago) → skip.
        _insert_eval_row(sm, 1, eval_time="2026-04-15T09:00:00Z")
        _insert_eval_row(sm, 2, eval_time="2026-04-15T11:00:00Z")

        n_calls = {"n": 0}

        def _fetcher(asset, end_dt):
            n_calls["n"] += 1
            # Round 4 fix #1: id=1 eval=09:00 → bucket window
            # [08:00, 09:00] (strictly prior hour); article inside.
            return [_article("POSITIVE", "2026-04-15T08:30:00Z")]

        backfill_cryptocompare(
            sm.conn, fetcher=_fetcher, sleep_ms=0,
            min_eval_age_hours=2.0,
            now_fn=lambda: fixed_now,
        )

        rows = sm.conn.execute(
            "SELECT id, news_sentiment_score_1h_pre_decision "
            "FROM evaluated_opportunities ORDER BY id"
        ).fetchall()
        # id=1 backfilled with POSITIVE → 1.0.
        assert rows[0][1] == pytest.approx(1.0)
        # id=2 untouched (still NULL).
        assert rows[1][1] is None
        # Only one fetch happened (for id=1).
        assert n_calls["n"] == 1


# ── Round 3 fix #13 — warn on 50-article CC pages (truncation risk) ───

class TestWarnsWhen50ArticlesReturned:

    def test_warns_when_50_articles_returned(self, tmp_path, caplog):
        """When CC returns its page-limit of 50 articles AND all 50 are
        in-window, the next-older article may also be in-window and we'd
        be silently truncating. The backfill must emit a WARNING (not
        fail) so operators can decide whether to add pagination."""
        import logging as _log
        from cryptocompare_news_backfill import backfill_cryptocompare
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, eval_time="2026-04-15T10:00:00Z")

        def _fetcher(asset, end_dt):
            # Round 4 fix #1: bucket window is now [09:00, 10:00] for
            # an eval at 10:00:00 (strictly prior hour). All 50 articles
            # published inside that hour to trigger the truncation warning.
            return [
                _article("POSITIVE", "2026-04-15T09:30:00Z")
                for _ in range(50)
            ]

        # Round 4 fix #2: default min_eval_age_hours bumped to 24.
        fixed_now = datetime.datetime(
            2026, 4, 17, 0, 0, 0, tzinfo=datetime.timezone.utc,
        )
        with caplog.at_level(_log.WARNING):
            backfill_cryptocompare(
                sm.conn, fetcher=_fetcher, sleep_ms=0,
                now_fn=lambda: fixed_now,
            )
        assert any(
            "truncated" in rec.message.lower()
            and "50" in rec.message
            for rec in caplog.records
        ), [r.message for r in caplog.records]


# ── Sentiment mapping sanity ──────────────────────────────────────────

class TestSentimentMapping:

    def test_summarize_sentiment_basic(self):
        from cryptocompare_news_backfill import summarize_sentiment
        n, s = summarize_sentiment([
            {"sentiment": "POSITIVE"},
            {"sentiment": "NEGATIVE"},
            {"sentiment": "NEUTRAL"},
        ])
        assert n == 3
        assert s == pytest.approx(0.0)

    def test_summarize_sentiment_returns_none_when_no_recognized_labels(self):
        from cryptocompare_news_backfill import summarize_sentiment
        n, s = summarize_sentiment([
            {"sentiment": "UNKNOWN_LABEL"},
            {"sentiment": ""},
        ])
        assert n == 0
        assert s is None  # honest NULL, not 0.0


# ── Round 4 fix #1 — bucket window must exclude future-of-decision data ─

class TestBucketWindowExcludesFutureData:

    def test_bucket_window_excludes_future_data(self):
        """The bucket window is the strictly-prior hour [hour-1, hour].
        Round 3 set it to [hour, hour+1] which leaked up to 59 minutes of
        post-decision news. Round 4 narrows it to the prior hour — zero
        leakage at the cost of up to 60 min trailing-edge loss.

        For a bucket key whose `hour_int=14`, the window must be
        [13:00, 14:00] UTC."""
        from cryptocompare_news_backfill import _bucket_window
        start, end = _bucket_window(("2026-04-15", 14, "BTC"))
        assert start == datetime.datetime(
            2026, 4, 15, 13, 0, 0, tzinfo=datetime.timezone.utc,
        )
        assert end == datetime.datetime(
            2026, 4, 15, 14, 0, 0, tzinfo=datetime.timezone.utc,
        )
        # The window strictly precedes the eval hour.
        assert end <= datetime.datetime(
            2026, 4, 15, 14, 0, 0, tzinfo=datetime.timezone.utc,
        )


# ── Round 4 cross-cutting — H-4c stamps data_provenance ───────────────

class TestCryptoCompareStampsDataProvenance:

    def test_cryptocompare_stamps_data_provenance(self, tmp_path):
        """Round 4 cross-cutting (MEDIUM): the H-4c backfill must stamp
        `data_provenance='backfill_cc_hour_bucket'` so the v2 trainer
        can downweight or exclude these rows."""
        from cryptocompare_news_backfill import backfill_cryptocompare
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, eval_time="2026-04-15T10:00:00Z")

        def _fetcher(asset, end_dt):
            return [_article("POSITIVE", "2026-04-15T09:30:00Z")]

        fixed_now = datetime.datetime(
            2026, 4, 17, 0, 0, 0, tzinfo=datetime.timezone.utc,
        )
        backfill_cryptocompare(
            sm.conn, fetcher=_fetcher, sleep_ms=0, now_fn=lambda: fixed_now,
        )
        row = sm.conn.execute(
            "SELECT data_provenance FROM evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert row[0] == "backfill_cc_hour_bucket"

    def test_cryptocompare_preserves_existing_provenance(self, tmp_path):
        """COALESCE: if a row already has provenance, H-4c must NOT
        overwrite it."""
        from cryptocompare_news_backfill import (
            backfill_cryptocompare, _ensure_local_columns,
        )
        sm = _make_db(tmp_path)
        _insert_eval_row(sm, 1, eval_time="2026-04-15T10:00:00Z")
        _ensure_local_columns(sm.conn)
        sm.conn.execute(
            "UPDATE evaluated_opportunities SET data_provenance = "
            "'backfill_glassnode_daily' WHERE id = 1"
        )
        sm.conn.commit()

        def _fetcher(asset, end_dt):
            return [_article("POSITIVE", "2026-04-15T09:30:00Z")]

        fixed_now = datetime.datetime(
            2026, 4, 17, 0, 0, 0, tzinfo=datetime.timezone.utc,
        )
        backfill_cryptocompare(
            sm.conn, fetcher=_fetcher, sleep_ms=0, now_fn=lambda: fixed_now,
        )
        row = sm.conn.execute(
            "SELECT data_provenance FROM evaluated_opportunities WHERE id=1"
        ).fetchone()
        assert row[0] == "backfill_glassnode_daily"


# ── Round 4 fix #2 — default min_eval_age_hours bumped 2 → 24 ─────────

class TestDefaultMinEvalAgeHours:

    def test_default_min_eval_age_hours_is_24(self):
        """Round 4 fix #2 (MEDIUM): the default was 2h, but typical news
        label settle windows are several hours to a day. 24h is the
        conservative ceiling."""
        from cryptocompare_news_backfill import DEFAULT_MIN_EVAL_AGE_HOURS
        assert DEFAULT_MIN_EVAL_AGE_HOURS == 24


# ── 2026-05-04: CC API key required (CoinDesk policy change) ─────────

class TestCryptoCompareApiKey:
    """CryptoCompare (now CoinDesk Indices) requires an API key on the
    news endpoint as of late 2024. fetch_cryptocompare_news must read
    CRYPTOCOMPARE_API_KEY from env and forward it as the `api_key`
    query parameter. Live failure mode without this: 401-style "you
    need a valid auth key" surfaced via wrapper Telegram alert.
    """

    def test_api_key_forwarded_when_env_set(self, monkeypatch):
        """Env set → params include api_key=<value>."""
        from cryptocompare_news_backfill import fetch_cryptocompare_news
        monkeypatch.setenv('CRYPTOCOMPARE_API_KEY', 'sentinel-test-key-12345')
        captured: list = []

        def spy_request(url, params, timeout):
            captured.append(dict(params))
            class _R:
                status_code = 200
                def json(self): return {"Type": 100, "Data": []}
            return _R()

        end = datetime.datetime(2026, 5, 4, 12, 0, 0,
                                 tzinfo=datetime.timezone.utc)
        fetch_cryptocompare_news('BTC', end, request_fn=spy_request)
        assert captured, "request_fn was not called"
        assert captured[0].get('api_key') == 'sentinel-test-key-12345', (
            f"api_key must be forwarded; got params: {captured[0]}"
        )

    def test_api_key_omitted_when_env_unset(self, monkeypatch):
        """Env unset → params omit api_key (so the original auth-error
        surfaces clearly in the Telegram alert instead of being masked
        by an empty key value)."""
        from cryptocompare_news_backfill import fetch_cryptocompare_news
        monkeypatch.delenv('CRYPTOCOMPARE_API_KEY', raising=False)
        captured: list = []

        def spy_request(url, params, timeout):
            captured.append(dict(params))
            class _R:
                status_code = 200
                def json(self): return {"Type": 100, "Data": []}
            return _R()

        end = datetime.datetime(2026, 5, 4, 12, 0, 0,
                                 tzinfo=datetime.timezone.utc)
        fetch_cryptocompare_news('BTC', end, request_fn=spy_request)
        assert captured
        assert 'api_key' not in captured[0], (
            f"api_key must NOT be present when env is unset; got params: {captured[0]}"
        )

    def test_empty_or_whitespace_env_treated_as_unset(self, monkeypatch):
        """Whitespace-only value → omit, same as unset (mirrors the
        CALMLP_BUNDLE_DIR + CALMLP_ENABLED .strip() pattern)."""
        from cryptocompare_news_backfill import fetch_cryptocompare_news
        monkeypatch.setenv('CRYPTOCOMPARE_API_KEY', '   ')
        captured: list = []

        def spy_request(url, params, timeout):
            captured.append(dict(params))
            class _R:
                status_code = 200
                def json(self): return {"Type": 100, "Data": []}
            return _R()

        end = datetime.datetime(2026, 5, 4, 12, 0, 0,
                                 tzinfo=datetime.timezone.utc)
        fetch_cryptocompare_news('BTC', end, request_fn=spy_request)
        assert captured
        assert 'api_key' not in captured[0]
