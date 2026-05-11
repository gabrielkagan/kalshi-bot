"""Phase G-5: OKX + Deribit perp funding-rate backfill.

Both exchanges expose public funding-rate-history endpoints (no auth):
  - OKX: /api/v5/public/funding-rate-history?instId={ASSET}-USDT-SWAP
    Returns reverse-chronological with `fundingTime` (epoch_ms str) +
    `fundingRate` (str). Funding cadence: 8h.
  - Deribit: /api/v2/public/get_funding_rate_history?instrument_name={ASSET}-PERPETUAL
    Returns chronological with `timestamp` (epoch_ms int) + `interest_8h`
    (float). Funding cadence: 8h.

Backfill writes `okx_funding_rate_at_decision` + `deribit_funding_rate_at_decision`
per row using the most-recent funding rate <= row.evaluation_time.
"""

import os
import sys

import pytest
import bot.state  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def _make_db(tmp_path):
    import bot
    return bot.state.StateManager(str(tmp_path / "test.db"))


class TestG5OkxParser:
    """Parse raw OKX funding response → sorted (epoch_ms, rate) list."""

    def test_parses_okx_response(self):
        from shadow_coverage_backfill import parse_okx_funding_response
        resp = {
            "code": "0",
            "data": [
                {"fundingTime": "1777737600000", "fundingRate": "-0.0000064207"},
                {"fundingTime": "1777708800000", "fundingRate": "0.0000007653"},
            ],
        }
        out = parse_okx_funding_response(resp)
        # Should be sorted ascending by fundingTime.
        assert out == [
            (1777708800000, pytest.approx(0.0000007653)),
            (1777737600000, pytest.approx(-0.0000064207)),
        ]

    def test_handles_missing_data(self):
        from shadow_coverage_backfill import parse_okx_funding_response
        assert parse_okx_funding_response({"code": "1", "msg": "err"}) == []
        assert parse_okx_funding_response({"data": []}) == []

    def test_skips_malformed_entries(self):
        from shadow_coverage_backfill import parse_okx_funding_response
        resp = {
            "data": [
                {"fundingTime": "1000", "fundingRate": "0.001"},
                {"fundingTime": "bad", "fundingRate": "0.002"},
                {"fundingTime": "2000"},  # missing rate
                {"fundingTime": "3000", "fundingRate": "0.003"},
            ],
        }
        out = parse_okx_funding_response(resp)
        assert out == [(1000, 0.001), (3000, 0.003)]


class TestG5DeribitParser:
    """Parse raw Deribit funding response → sorted (epoch_ms, rate) list."""

    def test_parses_deribit_response(self):
        from shadow_coverage_backfill import parse_deribit_funding_response
        resp = {
            "jsonrpc": "2.0",
            "result": [
                {"timestamp": 1745002800000, "interest_8h": -1.28e-6},
                {"timestamp": 1745006400000, "interest_8h": 2.5e-6},
            ],
        }
        out = parse_deribit_funding_response(resp)
        assert out == [
            (1745002800000, pytest.approx(-1.28e-6)),
            (1745006400000, pytest.approx(2.5e-6)),
        ]

    def test_handles_missing_result(self):
        from shadow_coverage_backfill import parse_deribit_funding_response
        assert parse_deribit_funding_response({}) == []
        assert parse_deribit_funding_response({"result": []}) == []


class TestG5LookupFundingRate:
    """Given a sorted (timestamp_ms, rate) list + eval_ms, return the
    most-recent rate at or before eval. None if no entry ≤ eval."""

    def test_returns_most_recent_at_or_before(self):
        from shadow_coverage_backfill import lookup_funding_rate_at_or_before
        rates = [(1000, 0.001), (2000, 0.002), (3000, 0.003)]
        assert lookup_funding_rate_at_or_before(rates, 2500) == 0.002
        assert lookup_funding_rate_at_or_before(rates, 3000) == 0.003
        assert lookup_funding_rate_at_or_before(rates, 1000) == 0.001

    def test_returns_none_when_eval_before_all(self):
        from shadow_coverage_backfill import lookup_funding_rate_at_or_before
        rates = [(1000, 0.001), (2000, 0.002)]
        assert lookup_funding_rate_at_or_before(rates, 500) is None

    def test_returns_last_when_eval_after_all(self):
        from shadow_coverage_backfill import lookup_funding_rate_at_or_before
        rates = [(1000, 0.001), (2000, 0.002)]
        assert lookup_funding_rate_at_or_before(rates, 5000) == 0.002

    def test_empty_rates_returns_none(self):
        from shadow_coverage_backfill import lookup_funding_rate_at_or_before
        assert lookup_funding_rate_at_or_before([], 1000) is None


class TestG5OkxPagination:
    """Phase G-5 round 2 M2 regression: exercise the multi-page fetch
    loop in fetch_okx_funding. Round 1 added pagination but no test
    covered it — a "simplification" PR could remove the loop and pass
    the original 13 tests."""

    def test_paginates_until_start_reached(self):
        from shadow_coverage_backfill import fetch_okx_funding
        # Simulate 2 pages: first 100 entries (newest), then 50 entries.
        # Third call (after exhaustion) returns empty.
        calls = []

        def _fake_request_fn(url, params, timeout):
            calls.append(int(params["after"]))
            n_calls = len(calls)

            class _Resp:
                status_code = 200
                def json(self):
                    if n_calls == 1:
                        # Newest 100 entries — fundingTime descending from end_ms.
                        return {"code": "0", "data": [
                            {"fundingTime": str(2_000_000 - i * 1000),
                             "fundingRate": "0.0001"}
                            for i in range(100)
                        ]}
                    elif n_calls == 2:
                        # Older 50 entries; oldest in page <= start_ms triggers exit.
                        return {"code": "0", "data": [
                            {"fundingTime": str(1_900_000 - i * 1000),
                             "fundingRate": "0.0002"}
                            for i in range(50)
                        ]}
                    return {"code": "0", "data": []}
            return _Resp()

        page = fetch_okx_funding(
            "BTC", start_ms=1_000_000, end_ms=2_000_000,
            request_fn=_fake_request_fn, page_sleep_ms=0,
        )
        # Pagination aggregates 150 entries; loop exits on the 3rd call
        # which returns empty data.
        assert len(page["data"]) == 150
        assert len(calls) == 3

    def test_pagination_stall_guard(self):
        """Phase G-5 round 2 M1 regression: if API repeats the same page
        without cursor advance, break with warning instead of looping
        forever."""
        from shadow_coverage_backfill import fetch_okx_funding
        call_count = {"n": 0}

        def _stuck_request_fn(url, params, timeout):
            call_count["n"] += 1

            class _Resp:
                status_code = 200
                def json(self):
                    # Always return the SAME entry; oldest_ts never advances
                    # past `after` because we set fundingTime = after - 1.
                    return {"code": "0", "data": [
                        {"fundingTime": str(int(params["after"])),
                         "fundingRate": "0.001"}
                    ]}
            return _Resp()

        # Should bail after 1 page (no advance).
        page = fetch_okx_funding(
            "BTC", start_ms=1, end_ms=2_000_000,
            request_fn=_stuck_request_fn, page_sleep_ms=0,
        )
        # 1 page worth of data.
        assert len(page["data"]) == 1
        assert call_count["n"] == 1

    def test_bails_on_app_layer_code(self):
        from shadow_coverage_backfill import fetch_okx_funding
        call_count = {"n": 0}

        def _err_request_fn(url, params, timeout):
            call_count["n"] += 1

            class _Resp:
                status_code = 200
                def json(self):
                    return {"code": "50011", "msg": "rate limited", "data": []}
            return _Resp()

        page = fetch_okx_funding(
            "BTC", start_ms=1_000_000, end_ms=2_000_000,
            request_fn=_err_request_fn, page_sleep_ms=0,
        )
        assert page == {"data": []}
        # Only one call — bailed on app-layer code.
        assert call_count["n"] == 1


class TestG5BackfillFundingRatesIntegration:
    """End-to-end: backfill_funding_rates fetches via injected fetchers,
    then UPDATEs rows."""

    def test_backfill_writes_both_funding_columns(self, tmp_path):
        from shadow_coverage_backfill import backfill_funding_rates
        sm = _make_db(tmp_path)
        # Eval row with evaluation_time = 2026-04-15T10:00:00Z.
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, status) "
            "VALUES ('TEST', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 'pending')"
        )
        sm.conn.commit()

        import datetime as _dt
        eval_ms = int(_dt.datetime(2026, 4, 15, 10, 0, 0,
                                       tzinfo=_dt.timezone.utc).timestamp() * 1000)

        def _mock_okx(asset, start_ms, end_ms):
            return {
                "data": [
                    {"fundingTime": str(eval_ms - 60_000), "fundingRate": "0.001"},
                ],
            }

        def _mock_deribit(asset, start_ms, end_ms):
            return {
                "result": [
                    {"timestamp": eval_ms - 60_000, "interest_8h": 0.0005},
                ],
            }

        n = backfill_funding_rates(
            sm.conn, okx_fetcher=_mock_okx, deribit_fetcher=_mock_deribit,
            batch_size=10, sleep_ms=0,
        )
        assert n == 1
        row = sm.conn.execute(
            "SELECT okx_funding_rate_at_decision, "
            "deribit_funding_rate_at_decision FROM evaluated_opportunities "
            "WHERE ticker='TEST'"
        ).fetchone()
        assert row["okx_funding_rate_at_decision"] == pytest.approx(0.001)
        assert row["deribit_funding_rate_at_decision"] == pytest.approx(0.0005)

    def test_backfill_skips_already_populated(self, tmp_path):
        from shadow_coverage_backfill import backfill_funding_rates
        sm = _make_db(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, status, okx_funding_rate_at_decision, "
            "deribit_funding_rate_at_decision) "
            "VALUES ('LIVE', 'E', 'BTC', 'candidate', "
            "'2026-05-02T10:00:00Z', '15m', 'pending', 999.0, 999.0)"
        )
        sm.conn.commit()
        called = {"okx": 0, "deribit": 0}
        def _mock_okx(*a, **k):
            called["okx"] += 1
            return {"data": []}
        def _mock_deribit(*a, **k):
            called["deribit"] += 1
            return {"result": []}
        n = backfill_funding_rates(
            sm.conn, okx_fetcher=_mock_okx, deribit_fetcher=_mock_deribit,
            batch_size=10, sleep_ms=0,
        )
        assert n == 0
        # Row untouched.
        row = sm.conn.execute(
            "SELECT okx_funding_rate_at_decision FROM evaluated_opportunities "
            "WHERE ticker='LIVE'"
        ).fetchone()
        assert row["okx_funding_rate_at_decision"] == 999.0

    def test_backfill_fills_in_missing_only_column(self, tmp_path):
        """Phase G-5 round 1 C2 regression: row where only OKX is NULL
        but Deribit is populated must still get OKX filled. Pre-fix
        WHERE okx IS NULL AND deribit IS NULL excluded such rows
        permanently from re-runs after a partial backfill."""
        from shadow_coverage_backfill import backfill_funding_rates
        sm = _make_db(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, status, deribit_funding_rate_at_decision) "
            "VALUES ('PARTIAL', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 'pending', 0.0007)"
        )
        sm.conn.commit()
        import datetime as _dt
        eval_ms = int(_dt.datetime(2026, 4, 15, 10, 0, 0,
                                       tzinfo=_dt.timezone.utc).timestamp() * 1000)
        n = backfill_funding_rates(
            sm.conn,
            okx_fetcher=lambda *a, **k: {
                "data": [{"fundingTime": str(eval_ms - 60_000),
                          "fundingRate": "0.001"}]
            },
            deribit_fetcher=lambda *a, **k: {"result": []},
            batch_size=10, sleep_ms=0,
        )
        assert n == 1
        row = sm.conn.execute(
            "SELECT okx_funding_rate_at_decision, "
            "deribit_funding_rate_at_decision FROM evaluated_opportunities "
            "WHERE ticker='PARTIAL'"
        ).fetchone()
        # OKX got filled; Deribit preserved (NOT clobbered to NULL).
        assert row["okx_funding_rate_at_decision"] == pytest.approx(0.001)
        assert row["deribit_funding_rate_at_decision"] == pytest.approx(0.0007)

    def test_backfill_writes_null_when_no_funding_before_eval(self, tmp_path):
        """If both fetchers return empty (no funding in range), backfill
        writes NULL for both — does NOT abort (unlike G-2/G-4 which need
        candle data; funding can legitimately be unavailable for an
        exchange/asset pair)."""
        from shadow_coverage_backfill import backfill_funding_rates
        sm = _make_db(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, status) "
            "VALUES ('NO-FUND', 'E', 'SOL', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 'pending')"
        )
        sm.conn.commit()
        n = backfill_funding_rates(
            sm.conn,
            okx_fetcher=lambda *a, **k: {"data": []},
            deribit_fetcher=lambda *a, **k: {"result": []},
            batch_size=10, sleep_ms=0,
        )
        assert n == 1
        row = sm.conn.execute(
            "SELECT okx_funding_rate_at_decision, "
            "deribit_funding_rate_at_decision FROM evaluated_opportunities "
            "WHERE ticker='NO-FUND'"
        ).fetchone()
        assert row["okx_funding_rate_at_decision"] is None
        assert row["deribit_funding_rate_at_decision"] is None
