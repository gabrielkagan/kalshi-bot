"""Bit V.1 — estimator-parity tape-vol input for the live-small strategy
engines (re-arm gate; postmortem kb/failures/vol-engine-beta-dvol-deflation-
jun12.md, lesson L-VOL-1 "estimator parity is a contract").

Two test surfaces:

1. Formula parity for ``bot.helpers.tape_rv.trailing_rv300`` against an
   INDEPENDENT in-test reimplementation of the research estimator
   (``scripts/research/genhunt/02_longshot_tick_floor.py::_rv`` — the
   validated entry rule's rv_5s; same /(n-1) sample-stdev construction as
   ``scripts/research/fairvalue_extract.py::_realized_vol``), to 1e-12.
2. Scanner seam behavior via the full ``OpportunityScanner.scan()`` harness
   (mirrors tests/integration/test_longshot_r2_regressions.py): the
   longshot + twaplock overlays must receive ``max(blended_rv, rv300)``
   — never price risk off the SMALLER estimate — with a clean
   blended_rv fallback (no crash) when rv300 is None, and the per-asset
   ``StateManager._scan_tape_rv_cache`` stash must be written (honest-NULL
   pop on None, mirroring ``_scan_spot_staleness_cache``).
"""
from __future__ import annotations

import bisect
import math
import random
import time
from unittest.mock import MagicMock

import pytest

import bot.constants as C
from bot.state import StateManager


# ─────────────────────────────────────────────────────────────────────────────
# Independent reference implementation of the research formula.
#
# Transcribed from scripts/research/genhunt/02_longshot_tick_floor.py::_rv
# (function-name anchor; constants VOL_WIN_S=300.0, VOL_STEP_S=5.0,
# STALE_S=30.0 in that script). This is the formula the validated
# backtests conditioned on. Deliberately NOT imported from the helper
# under test — parity means two independent code paths agree.
# ─────────────────────────────────────────────────────────────────────────────

def _research_rv(secs, px, t, *, window_s=300.0, step_s=5.0, stale_s=30.0):
    k = int(window_s // step_s)
    samples = []
    for j in range(k + 1):
        tt = t - j * step_s
        i = bisect.bisect_right(secs, tt) - 1
        if i < 0 or (tt - secs[i]) > stale_s:
            return None
        samples.append(px[i])
    samples.reverse()
    rets = [math.log(samples[i] / samples[i - 1])
            for i in range(1, len(samples))
            if samples[i - 1] > 0]
    if len(rets) < 10:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def _make_walk_buffer(now, *, span_s=360, per5s_vol=3e-4, seed=7,
                      start_price=100.0):
    """1s-sampled random-walk buffer shaped like CoinbaseFeed.get_buffer():
    a list of (ts, price) tuples, ts at integer offsets ending at `now`."""
    rng = random.Random(seed)
    per1s = per5s_vol / math.sqrt(5.0)
    logp = math.log(start_price)
    out = []
    for i in range(span_s + 1):
        ts = now - span_s + i
        logp += rng.gauss(0.0, per1s)
        out.append((ts, math.exp(logp)))
    return out


# ── 1. Formula parity (1e-12) ────────────────────────────────────────────────

class TestFormulaParity:
    def test_synthetic_buffer_matches_research_formula_to_1e12(self):
        from bot.helpers.tape_rv import trailing_rv300
        now = 1_000_000.0
        buf = _make_walk_buffer(now, per5s_vol=1.2e-4, seed=11)
        secs = [s[0] for s in buf]
        px = [s[1] for s in buf]
        expected = _research_rv(secs, px, now)
        got = trailing_rv300(buf, now)
        assert expected is not None and got is not None
        assert abs(got - expected) < 1e-12, (
            f"trailing_rv300={got!r} vs research formula={expected!r} — "
            "estimator parity is a contract (L-VOL-1)")

    def test_hype_like_buffer_where_blended_style_underestimates(self):
        """Recorded-shape HYPE case from the postmortem: tape rv ~3e-4
        per-5s while blended_rv collapsed to 8.97e-5 (clamp(beta,0.5,3.0)
        × BTC_DVOL). The helper must (a) match the research formula to
        1e-12 and (b) come out far above the deflated blended-style value
        so the max() seam actually re-prices."""
        from bot.helpers.tape_rv import trailing_rv300
        now = 1_765_432_100.0
        buf = _make_walk_buffer(now, per5s_vol=3e-4, seed=42)
        secs = [s[0] for s in buf]
        px = [s[1] for s in buf]
        expected = _research_rv(secs, px, now)
        got = trailing_rv300(buf, now)
        assert expected is not None and got is not None
        assert abs(got - expected) < 1e-12
        deflated_blended = 8.97e-5  # postmortem HYPE live-window value
        assert got > 1.4 * deflated_blended, (
            f"rv300={got} should dwarf the deflated blended {deflated_blended}"
            " on a 3e-4 tape — otherwise this fixture doesn't exercise the "
            "underestimate case")

    def test_sample_stdev_uses_n_minus_1(self):
        """Pin /(n-1) (SAMPLE stdev — fairvalue_extract._realized_vol and
        02's _rv both divide by len(rets)-1, not len(rets))."""
        from bot.helpers.tape_rv import trailing_rv300
        now = 2_000_000.0
        # Log-price alternates ±b each 5s block → per-5s grid returns
        # alternate ±2b: mean 0, sample var = sum(4b²·60)/59.
        b = 1e-4
        buf = []
        for i in range(361):
            ts = now - 360 + i
            sgn = 1.0 if (int(ts) // 5) % 2 == 0 else -1.0
            buf.append((ts, math.exp(sgn * b)))
        got = trailing_rv300(buf, now)
        assert got is not None
        rets_expected = 60
        closed_form_sample = math.sqrt(
            (2 * b) ** 2 * rets_expected / (rets_expected - 1))
        closed_form_population = math.sqrt((2 * b) ** 2)
        assert got == pytest.approx(closed_form_sample, rel=1e-9)
        assert got != pytest.approx(closed_form_population, rel=1e-3)


# ── 2. None conditions ───────────────────────────────────────────────────────

class TestNoneConditions:
    def test_none_on_empty_buffer(self):
        from bot.helpers.tape_rv import trailing_rv300
        assert trailing_rv300([], 1_000_000.0) is None

    def test_none_when_newest_sample_staler_than_max_staleness(self):
        from bot.helpers.tape_rv import trailing_rv300
        now = 1_000_000.0
        buf = _make_walk_buffer(now - 31.0, span_s=360)  # ends 31s ago
        assert trailing_rv300(buf, now) is None

    def test_fresh_when_newest_sample_within_max_staleness(self):
        from bot.helpers.tape_rv import trailing_rv300
        now = 1_000_000.0
        buf = _make_walk_buffer(now - 29.0, span_s=400)  # ends 29s ago
        assert trailing_rv300(buf, now) is not None

    def test_none_when_buffer_spans_less_than_window(self):
        from bot.helpers.tape_rv import trailing_rv300
        now = 1_000_000.0
        buf = _make_walk_buffer(now, span_s=200)  # < 300s of history
        assert trailing_rv300(buf, now) is None

    def test_none_on_mid_buffer_gap_exceeding_staleness(self):
        """02's _rv guards staleness at EVERY 5s grid point, not just the
        newest sample — a >30s hole anywhere inside the trailing 300s
        kills the estimate (feed outage honesty)."""
        from bot.helpers.tape_rv import trailing_rv300
        now = 1_000_000.0
        buf = [s for s in _make_walk_buffer(now, span_s=360)
               if not (now - 150 < s[0] < now - 110)]  # 40s hole
        assert trailing_rv300(buf, now) is None

    def test_none_on_nonpositive_prices(self):
        from bot.helpers.tape_rv import trailing_rv300
        now = 1_000_000.0
        buf = [(now - 360 + i, 0.0) for i in range(361)]
        assert trailing_rv300(buf, now) is None


# ── 3. Scanner seam: max() routing + cache write ─────────────────────────────
# Harness mirrors tests/integration/test_longshot_r2_regressions.py.

TICKER = "KXBTC15M-26JUN121200-T104"
EVENT = "KXBTC15M-26JUN121200"


class _SpyLongshot:
    def __init__(self):
        self.calls = []

    def has_open_main_pipeline_position(self, ticker):
        return False

    def has_opposite_side_longshot_position(self, ticker, side):
        return False

    def has_opposite_side_resting_quote(self, ticker, side):
        return False

    def evaluate_market(self, **kw):
        self.calls.append(kw)
        return []


class _SpyTwaplock:
    def __init__(self):
        self.calls = []

    def evaluate_market(self, **kw):
        self.calls.append(kw)
        return []


class _ML:
    """Minimal MainLoop stand-in (plain object — unexpected attribute
    reads fail loudly; see test_longshot_r2_regressions._ML)."""

    def __init__(self, longshot_engine, twaplock_engine):
        self.longshot_engine = longshot_engine
        self.twaplock_engine = twaplock_engine
        self.config_snapshot_id = None
        self.cross_feed = None
        self.synthetic_rti_feed = None
        self.spx_engine = None
        self.weather_engine = None
        self.fifteenm_shadow = None
        self.hourly_alt_shadow = None
        self.spx_harrv_shadow = None
        self.capital_allocator = None
        self._scan_iter = 0
        self._scan_loop_start = 0.0
        self._open_positions_count_cache = {}


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "test_tape_rv.db"))
    yield s
    s.close()


@pytest.fixture
def client():
    cl = MagicMock()
    cl.get_fills.return_value = {"fills": []}
    cl.get_orders.return_value = {"orders": []}
    cl.get_balance.return_value = {"balance": 50000}
    cl.get_orderbook.return_value = {
        "orderbook": {"yes": [[3, 50]], "no": [[92, 50]]}}
    return cl


@pytest.fixture
def overlays_enabled(monkeypatch):
    monkeypatch.setattr(C, "LONGSHOT_ENABLED", True, raising=False)
    monkeypatch.setattr(C, "TWAPLOCK_ENABLED", True, raising=False)


def _build_scanner(state, client, *, blended_rv, buffer):
    from bot.scanner import OpportunityScanner
    feed = MagicMock()
    feed.get_price_with_ts.return_value = (100.0, time.monotonic())
    feed.get_price.return_value = 100.0
    feed.get_price_trailing_avg.return_value = 100.0
    feed.get_buffer.return_value = buffer
    vol = MagicMock()
    vol.update.return_value = {"blended_rv": blended_rv, "regime": "normal"}
    ls, tw = _SpyLongshot(), _SpyTwaplock()
    ml = _ML(ls, tw)
    scanner = OpportunityScanner(
        client, state, feed, vol, MagicMock(), MagicMock(),
        kalshi_feed=None, main_loop=ml)
    state._extended_feature_provider = None
    return scanner, ls, tw


def _window(stc=600.0):
    return {
        "asset": "BTC",
        "event_ticker": EVENT,
        "product_type": "15m",
        "seconds_to_close": stc,
        "markets": [{"ticker": TICKER, "floor_strike": 104.0,
                     "yes_ask": 8}],
    }


def _reference_candidates(buf, now0, slack_s=4):
    """Reference rv300 values for any scan-time `now` within now0+slack.

    The buffer has 1s-integer-offset timestamps, so the grid sample
    SELECTION is invariant for fractional now-jitter; whole-second
    delays shift selection by one sample — enumerate a few."""
    secs = [s[0] for s in buf]
    px = [s[1] for s in buf]
    out = []
    for k in range(slack_s):
        v = _research_rv(secs, px, now0 + k)
        if v is not None:
            out.append(v)
    return out


class TestScannerSeam:
    def test_rv300_above_blended_routes_rv300_to_both_engines(
            self, state, client, overlays_enabled):
        """Deflated blended (9e-5, the postmortem cluster) + honest 3e-4
        tape → BOTH engines must be priced off the tape estimate."""
        now0 = time.time()
        buf = _make_walk_buffer(now0, per5s_vol=3e-4, seed=42)
        deflated = 9e-5
        scanner, ls, tw = _build_scanner(
            state, client, blended_rv=deflated, buffer=buf)
        scanner.scan([_window()])
        assert ls.calls and tw.calls, "overlays must be reached"
        refs = _reference_candidates(buf, now0)
        for calls in (ls.calls, tw.calls):
            got = calls[0]["blended_rv"]
            assert got > deflated, (
                "engine still priced off the deflated blended_rv — "
                "max(blended_rv, rv300) seam missing (Bit V.1)")
            assert any(abs(got - r) < 1e-12 for r in refs), (
                f"vol passed to engine ({got}) doesn't match the research "
                f"formula on the same buffer (candidates {refs})")

    def test_blended_above_rv300_routes_blended(
            self, state, client, overlays_enabled):
        now0 = time.time()
        buf = _make_walk_buffer(now0, per5s_vol=1e-4, seed=5)
        big_blended = 0.01
        scanner, ls, tw = _build_scanner(
            state, client, blended_rv=big_blended, buffer=buf)
        scanner.scan([_window()])
        assert ls.calls and tw.calls
        assert ls.calls[0]["blended_rv"] == big_blended
        assert tw.calls[0]["blended_rv"] == big_blended

    def test_rv300_none_falls_back_to_blended_without_crash(
            self, state, client, overlays_enabled):
        """Junk buffer (the historical MagicMock list-of-floats shape) →
        rv300 None → engines get blended_rv unchanged, scan survives."""
        blended = 1.5e-4
        scanner, ls, tw = _build_scanner(
            state, client, blended_rv=blended, buffer=[100.0] * 120)
        scanner.scan([_window()])
        assert ls.calls and tw.calls
        assert ls.calls[0]["blended_rv"] == blended
        assert tw.calls[0]["blended_rv"] == blended

    def test_scanner_writes_scan_tape_rv_cache(
            self, state, client, overlays_enabled):
        now0 = time.time()
        buf = _make_walk_buffer(now0, per5s_vol=2e-4, seed=9)
        scanner, _ls, _tw = _build_scanner(
            state, client, blended_rv=1e-4, buffer=buf)
        scanner.scan([_window()])
        cached = state._scan_tape_rv_cache.get("BTC")
        assert cached is not None, (
            "_scan_tape_rv_cache must be staged per asset per tick "
            "(mirrors _scan_spot_staleness_cache)")
        refs = _reference_candidates(buf, now0)
        assert any(abs(cached - r) < 1e-12 for r in refs)

    def test_cache_slot_popped_when_rv300_goes_none(
            self, state, client, overlays_enabled):
        """Honest-NULL: a prior tick's rv300 must not linger once the
        buffer goes dead (mirror of the _scan_spot_staleness_cache pop)."""
        scanner, _ls, _tw = _build_scanner(
            state, client, blended_rv=1e-4, buffer=[100.0] * 120)
        state._scan_tape_rv_cache["BTC"] = 3e-4  # stale prior tick
        scanner.scan([_window()])
        assert state._scan_tape_rv_cache.get("BTC") is None


# ── 4. Event-time staleness gate at the seam (R1-M1 fix round) ──────────────
# The live CoinbaseFeed sampler re-stamps the LAST-KNOWN price with a fresh
# time.time() every 1s unconditionally (bot/feeds/coinbase.py::_sampler_loop
# step-hold), so a frozen WS feed presents a gapless buffer of flat,
# freshly-stamped samples — trailing_rv300's per-grid-point BUFFER-time
# staleness guard can never fire live, and rv300 reads LOW (~0) off the
# step-held flat segment. The seam must consult the Bit-S.1 EVENT-time
# staleness signal (state._scan_spot_staleness_cache, written from the WS
# tick timestamp in the same scanner block) and treat rv300 as None when
# the spot is unmeasured or >30s event-stale — restoring the validated
# backtest's abstention (02_longshot_tick_floor STALE_S=30.0 applies to
# EVENT time in the research timeline, which has no re-stamping sampler).


def _frozen_feed_buffer(now, *, span_s=400, price=100.0):
    """The live frozen-feed shape: flat price, fresh 1s stamps to `now`."""
    return [(now - span_s + i, price) for i in range(span_s + 1)]


class TestEventTimeStalenessGate:
    def test_frozen_feed_pops_cache_and_falls_back_with_info_log(
            self, state, client, overlays_enabled, caplog):
        """Frozen-feed shape: buffer full of fresh-stamped flat prices
        (rv300 would read 0.0 — LOW, not None) + event-time staleness 45s
        → the seam must treat rv300 as None: pop the cache slot (even
        over a prior tick's honest value), fire the throttled TAPE_RV_NONE
        fallback at INFO (not debug — MN1), and hand the engines plain
        blended_rv."""
        import logging as _logging
        now0 = time.time()
        buf = _frozen_feed_buffer(now0)
        blended = 1.5e-4
        scanner, ls, tw = _build_scanner(
            state, client, blended_rv=blended, buffer=buf)
        # Feed froze 45s ago: last real WS tick is 45s old even though the
        # sampler kept re-stamping the buffer.
        scanner._feed.get_price_with_ts.return_value = (
            100.0, time.monotonic() - 45.0)
        state._scan_tape_rv_cache["BTC"] = 3e-4  # prior tick's honest value
        with caplog.at_level(_logging.INFO):
            scanner.scan([_window()])
        assert state._scan_tape_rv_cache.get("BTC") is None, (
            "event-stale tick must POP the rv300 slot — a step-held flat "
            "buffer reads rv300~0.0 and silently reverts max() to the "
            "broken blended_rv (R1-M1)")
        assert ls.calls and tw.calls
        assert ls.calls[0]["blended_rv"] == blended
        assert tw.calls[0]["blended_rv"] == blended
        assert "TAPE_RV_NONE" in caplog.text, (
            "fallback log must fire (at INFO) when rv300 is unavailable")

    def test_event_fresh_feed_keeps_tape_estimate(
            self, state, client, overlays_enabled):
        """Sanity twin: a fresh WS tick (staleness ~0, the default harness
        shape) must NOT trip the gate — rv300 still computed + cached."""
        now0 = time.time()
        buf = _make_walk_buffer(now0, per5s_vol=2e-4, seed=13)
        scanner, _ls, _tw = _build_scanner(
            state, client, blended_rv=1e-4, buffer=buf)
        scanner.scan([_window()])
        assert state._scan_tape_rv_cache.get("BTC") is not None

    def test_unmeasured_staleness_pops_cache(
            self, state, client, overlays_enabled):
        """get_price_with_ts None (warmup) → staleness cache popped →
        rv300 must be treated as None too (no event-time signal = no
        honest tape estimate), independent of buffer contents."""
        now0 = time.time()
        buf = _make_walk_buffer(now0, per5s_vol=2e-4, seed=21)
        scanner, _ls, _tw = _build_scanner(
            state, client, blended_rv=1e-4, buffer=buf)
        scanner._feed.get_price_with_ts.return_value = None
        state._scan_tape_rv_cache["BTC"] = 3e-4  # prior tick's honest value
        scanner.scan([_window()])
        assert state._scan_tape_rv_cache.get("BTC") is None


# ── 5. R1-M2: raw vol pair separately recoverable (V.3 ratio source) ─────────
# The engines persist their vol kwarg into eval rows (volatility= keys), so
# post-V.1 rows carry max(blended_rv, rv300) — the HONEST INPUT THE DECISION
# USED, correct for decision provenance, but useless for the V.3 re-arm
# ratio: max(b, rv300)/rv300 >= 1 ALWAYS, so the planned deflation gate
# (per-asset median blended_rv/rv300 in [0.8, 1.25]) could never detect
# deflation off the rows alone. Both RAW values must be separately
# recoverable at eval-row write time: rv300 already lives in
# _scan_tape_rv_cache; the RAW engine blended_rv gets its own per-asset
# stash (_scan_raw_blended_rv_cache) written at the _strategy_vol seam.
# V.3 sources the ratio from these VOL-ENGINE caches, NOT from the rows'
# volatility column.


class TestRawVolPairRecoverable:
    def test_raw_blended_and_rv300_separately_recoverable(
            self, state, client, overlays_enabled):
        """Deflated blended (9e-5) + honest tape (3e-4): the engines see
        max() = rv300, but BOTH raw sources must remain readable —
        raw blended from _scan_raw_blended_rv_cache, rv300 from
        _scan_tape_rv_cache — so the V.3 ratio raw_b/rv300 (~0.3 here,
        deflation!) is computable instead of pinned at >= 1."""
        now0 = time.time()
        buf = _make_walk_buffer(now0, per5s_vol=3e-4, seed=42)
        deflated = 9e-5
        scanner, ls, tw = _build_scanner(
            state, client, blended_rv=deflated, buffer=buf)
        scanner.scan([_window()])
        assert ls.calls and tw.calls
        raw_b = state._scan_raw_blended_rv_cache.get("BTC")
        rv300 = state._scan_tape_rv_cache.get("BTC")
        assert raw_b == deflated, (
            "raw engine blended_rv must be stashed per asset — the eval "
            "rows' volatility column carries max(b, rv300) and cannot "
            "source the V.3 deflation ratio (R1-M2)")
        assert rv300 is not None
        refs = _reference_candidates(buf, now0)
        assert any(abs(rv300 - r) < 1e-12 for r in refs)
        # The decision input was the max — and the ratio detects deflation.
        assert ls.calls[0]["blended_rv"] == max(raw_b, rv300)
        assert raw_b / rv300 < 0.8

    def test_raw_blended_stashed_even_when_rv300_unavailable(
            self, state, client, overlays_enabled):
        """rv300 None (junk buffer) → tape cache popped, but the raw
        blended stash still updates (V.3's honest-NULL semantics: ratio
        is NULL because the DENOMINATOR is missing, not because the
        numerator silently went stale)."""
        scanner, _ls, _tw = _build_scanner(
            state, client, blended_rv=1.5e-4, buffer=[100.0] * 120)
        scanner.scan([_window()])
        assert state._scan_raw_blended_rv_cache.get("BTC") == 1.5e-4
        assert state._scan_tape_rv_cache.get("BTC") is None
