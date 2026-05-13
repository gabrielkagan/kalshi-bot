"""86b9wy7v3 Phase 2 — HYPE/DOGE prediction-pipeline replay backfill.

Pulls historical Kalshi KX{HYPE,DOGE}15M settled markets + historical 1-min
spot klines (Bybit for HYPE, Binance.com for DOGE), runs the bot's
ProbabilityEngine cascade against each market's open_time evaluation moment,
pairs the model output with the realized YES/NO settlement, and writes to a
new ``historical_replay_calmlp`` table. Output is consumed by sister ticket
``86b9wy15n`` (calibration health check) which is sample-size-blocked on the
2-day T1 live shadow corpus.

Phase 1 (POSITIVE) earned the build budget: 4,911 pre-T1 settled markets per
asset (Mar 18 → May 10), 21× the live T1 corpus. See
``kb/findings/hype-doge-kalshi-market-history-may12.md``.

Architecture (load-bearing — see test ``tests/integration/test_hype_doge_replay_backfill.py``):

- NEW table ``historical_replay_calmlp`` (NOT writing to evaluated_opportunities
  — that would contaminate production audits + Wilson CIs).
- PK ``(ticker, evaluation_time)`` for idempotent INSERT OR REPLACE re-runs.
- CHECK constraints on ``result`` ∈ {'yes','no'} and ``asset`` ∈ {'HYPE','DOGE'}
  (widen in the same commit if Phase 2.5 expands scope).
- ``data_provenance='replay_phase2_v1'`` stamps every row so consumers can
  filter / re-backfill if the harness changes.

Methodology gotchas (DO NOT VIOLATE):

- **No HYPE/DOGE trained cal_mlp predictor exists.** Production
  ``_calmlp_predictors`` in ``scripts/cal_mlp/integration.py`` constructs
  predictors for ``('BTC','ETH','SOL','XRP')`` only; there are no HYPE or DOGE
  weights, neither on Mac nor VPS. ``replay_market(..., predictor=None)`` is
  therefore the production v1 shape: ``blended_prob`` stays NULL, and the
  ``calibrated_prob`` column comes from ``ProbabilityEngine.compute``'s
  in-prod calibration cascade (Student-t passthrough → BLR depending on
  CalEngine state). A future cross-asset transfer eval (file fu1 of
  ``86b9wy7v3``) would feed HYPE/DOGE rows through a ``CalMLPPredictor("BTC")``
  instance — NOT a "rerun on VPS" — because no asset-matched predictor exists.
- **Bot-state features cannot be replayed accurately** (market_price/NBBO,
  depth, OFT, queue position, recent_bot_pnl, drawdown_scaler). These are
  honest-NULL on replay rows. Calibration assessment doesn't need them;
  full admit/reject simulation would (not in scope).
- **Lock-step with canonical helpers.** ``hour_sin``/``hour_cos``,
  ``sigma_winsorize``, ``prob_breakeven_gap`` MUST route through
  ``bot.helpers.derived_features`` (per Sprint A.1b 2026-05-12 ``86b9veppa``
  cal_mlp lock-step closeout — ``tests/contracts/test_calmlp_lockstep.py``
  AST guard). Inline drift here re-opens the train/serve-skew surface.
- **Day-coverage gaps expected** (11 of 56 days are partial). Idempotent PK
  handles re-runs cleanly.
- **Vol input scale.** ``blended_rv`` semantics per
  ``bot.engines.probability.ProbabilityEngine.compute``: per-5-second stdev
  of log returns. The historical 1-min klines feed this via
  ``_per_5s_vol_from_ticks`` which scales stdev by ``sqrt(5/interval)`` —
  documented deviation from the live RK+EGARCH blend; acceptable for
  calibration assessment (vol → z_score → CDF normalizes the scale).

Mac compute only (1 vCPU on VPS — per ``feedback_vps_compute_isolation``,
2026-05-10 zstd backfill caused 9× scan regression).
"""
from __future__ import annotations

import argparse
import bisect
import datetime
import json
import logging
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Optional

# Canonical helpers — lock-step contract per bot/CLAUDE.md "cal_mlp feature
# transforms (lock-step)". Tests guard against inline-drift reintroduction.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from bot.helpers.derived_features import (  # noqa: E402
    apply_sigma_winsor,
    compute_derived_features,
    compute_hour_sin_cos,
)


REPLAY_TABLE = "historical_replay_calmlp"
REPLAY_PROVENANCE = "replay_phase2_v1"

# Phase 2 v1 scope. Widen CHECK in the same commit if expanding.
ASSETS = ("HYPE", "DOGE")

# Vol warmup window. Live convention is 15-min duration × 5-sec ticks =
# 180 samples (`bot/engines/volatility.py:129` deque(maxlen=VOL_WINDOW_15MIN)).
# Replay uses 30-min × 1-min Coinbase klines = 30 samples to clear the
# `_per_5s_vol_from_ticks` ≥10-tick threshold with margin. Window-duration
# 2× deviation is acceptable for calibration assessment (vol → z_score → CDF
# normalizes the scale; the "Vol input scale" gotcha in the module docstring
# covers the rationale).
WARMUP_SECS = 1800

# Cap the polite-sleep rate to stay under Kalshi's 30 req/s read tier and
# Coinbase's 10 req/s public read tier.
KALSHI_PAGE_SLEEP = 0.1
EXCHANGE_PAGE_SLEEP = 0.12  # 8-9 req/s, well under Coinbase's 10/s public limit

# Spot tick source: Coinbase Exchange. Bybit (CloudFront 403 from US) +
# Binance.com (HTTP 451 from US) are geo-blocked from Mac, mirroring the bot's
# `BINANCE_FEED_ENABLED=0` runtime default (bot/constants.py:233-238).
# Coinbase is also the bot's PRIMARY live feed for HYPE/DOGE
# (bot/constants.py:700-701 — "verified live + online on Coinbase Exchange"),
# so historical Coinbase candles match the live-evaluation source.
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{}/candles"
COINBASE_MAX_CANDLES_PER_REQ = 300  # documented cap; conservative chunk

KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"

T1_CUTOFF_ISO = "2026-05-10T00:00:00Z"


# ── Schema ────────────────────────────────────────────────────────────


def ensure_schema(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS for ``historical_replay_calmlp``.

    Idempotent — re-runs against an existing table are a no-op. PK
    ``(ticker, evaluation_time)`` enables idempotent INSERT OR REPLACE.

    Also migrates pre-P2.3.b-fu2 schemas by adding the ``threshold REAL``
    column if missing (sub-cent strike precision; legacy ``strike_cents``
    INTEGER is kept for HYPE back-compat). See
    ``kb/findings/replay-backfill-strike-precision-bug-may13.md``.
    """
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
    # Migration path: pre-fu2 DBs have the table without `threshold` column.
    # SQLite's `ALTER TABLE ADD COLUMN` is idempotent here only via the
    # explicit `PRAGMA table_info` probe (the `IF NOT EXISTS` clause on
    # ADD COLUMN landed in 3.35.0+; we target the older 3.x SQLite shipped
    # with system Pythons too). Race-safe via the `duplicate column` catch:
    # the contracted use case is a single Mac-side backfill process, but a
    # concurrent re-entry between probe and ALTER would otherwise raise.
    existing_cols = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({REPLAY_TABLE})").fetchall()
    }
    if "threshold" not in existing_cols:
        try:
            conn.execute(f"ALTER TABLE {REPLAY_TABLE} ADD COLUMN threshold REAL")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.commit()


# ── Volatility from historical ticks ──────────────────────────────────


def _per_5s_vol_from_ticks(
    ticks: list[tuple[int, float]],
    *,
    interval_secs: float = 60.0,
) -> Optional[float]:
    """Realized stdev of log returns, scaled to per-5-second units.

    Args:
        ticks: list of (timestamp_secs, price) tuples, sorted ascending.
        interval_secs: CANONICAL inter-tick interval (default 60.0 for the
            1-min Coinbase klines we fetch). Use the canonical interval —
            NOT a gap-derived ``span / (n-1)`` estimate — because Coinbase
            occasionally returns sparse coverage where a single 5-min gap
            inflates the span-derived average and biases vol low for the
            whole market. The canonical interval treats per-bar log returns
            as i.i.d. samples drawn at that frequency; gaps are tolerated
            but don't rescale.

    Returns:
        Per-5s stdev of log returns, or None on insufficient data.

    Conversion: stdev scales as sqrt(interval), so
    ``stdev_5s = stdev_interval × sqrt(5 / interval_secs)``.
    For 1-min klines (interval_secs=60): stdev_5s = stdev_1m / sqrt(12).

    Honest-NULL on <10 ticks or degenerate prices.
    """
    if len(ticks) < 10:
        return None
    log_rets: list[float] = []
    for i in range(1, len(ticks)):
        p_prev, p_now = ticks[i - 1][1], ticks[i][1]
        if p_prev > 0 and p_now > 0:
            log_rets.append(math.log(p_now / p_prev))
    if len(log_rets) < 5:
        return None
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean) ** 2 for r in log_rets) / max(len(log_rets) - 1, 1)
    raw_stdev = math.sqrt(var)
    if interval_secs <= 0:
        return None
    return raw_stdev * math.sqrt(5.0 / interval_secs)


# ── Replay one market ────────────────────────────────────────────────


def _parse_iso(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def replay_market(
    conn: sqlite3.Connection,
    market: dict,
    ticks: list[tuple[int, float]],
    *,
    predictor=None,
    tick_interval_secs: float = 60.0,
) -> int:
    """Replay one Kalshi market: ticks → vol → raw/calibrated prob → write row.

    Args:
        conn: state.db connection (schema must exist; call ``ensure_schema`` first).
        market: dict with keys ticker, asset, strike_cents, open_time, close_time,
                result. ``open_time``/``close_time`` are ISO-8601 UTC strings.
        ticks: list of (timestamp_secs, price) tuples covering the warmup window
               + evaluation moment. Sorted ascending.
        predictor: optional CalMLPPredictor (production: pass the warmed predictor;
                   tests: pass None → ``blended_prob`` stays NULL).
        tick_interval_secs: canonical inter-tick interval used by
                ``_per_5s_vol_from_ticks`` for the per-5s vol rescale. Default
                60.0 matches the production 1-min Coinbase klines this harness
                fetches in ``main()``. Tests with synthetic 1-second tick
                fixtures pass ``tick_interval_secs=1.0`` so vol magnitude
                matches the fixture's frequency.

    Returns:
        1 (one row written per call).
    """
    eval_time_iso = market["open_time"]
    eval_ts = int(_parse_iso(eval_time_iso).timestamp())
    close_ts = int(_parse_iso(market["close_time"]).timestamp())
    seconds_remaining = float(max(close_ts - eval_ts, 0))
    strike_cents = market["strike_cents"]
    # P2.3.b-fu2 (2026-05-13, ticket 86b9xtam7): prefer REAL `threshold` over
    # the legacy `strike_cents/100.0` derivation. For DOGE (spot ~$0.11) the
    # integer-cent round-trip collapses the 1-cent strike-value space, biasing
    # the BS probability calc by 3-10% per market. See
    # `kb/findings/replay-backfill-strike-precision-bug-may13.md`.
    # NaN-defense: `_floor_strike_to_db_fields` never produces NaN, but a
    # future direct caller passing `threshold=float('nan')` would propagate
    # NaN into `ProbabilityEngine.compute(threshold=nan)` silently — fall back
    # to the legacy path on NaN too.
    threshold = market.get("threshold")
    if (threshold is None or (isinstance(threshold, float) and math.isnan(threshold))) \
            and strike_cents is not None:
        threshold = strike_cents / 100.0  # HYPE legacy fallback path
    asset = market["asset"]
    result = market["result"]
    settlement_value = 100 if result == "yes" else 0

    # ── Warmup window: trailing WARMUP_SECS up to eval_ts (inclusive). ──
    # CRITICAL: must bound the lower edge — `bot/engines/volatility.py:128`
    # uses a rolling 15-min deque, so an unbounded "all history before eval_ts"
    # filter would mix months of pre-corpus vol into every market's raw_prob
    # (R2 adv-review C2). ``bisect`` keeps per-market filtering O(log N) over
    # the asset-level tick buffer.
    warmup_lo = eval_ts - WARMUP_SECS
    lo_idx = bisect.bisect_left(ticks, (warmup_lo,))
    hi_idx = bisect.bisect_right(ticks, (eval_ts, float("inf")))
    warmup = ticks[lo_idx:hi_idx]
    spot_at_eval = warmup[-1][1] if warmup else None
    blended_rv = _per_5s_vol_from_ticks(warmup, interval_secs=tick_interval_secs)

    # ── ProbabilityEngine cascade (raw + calibrated; production code path) ──
    raw_prob: Optional[float] = None
    calibrated_prob: Optional[float] = None
    if (
        spot_at_eval is not None
        and blended_rv is not None
        and blended_rv > 0
        and seconds_remaining > 0
    ):
        from bot.engines.probability import ProbabilityEngine
        prob_result = ProbabilityEngine.compute(
            spot=spot_at_eval,
            threshold=threshold,
            seconds_remaining=seconds_remaining,
            blended_rv=blended_rv,
            asset=asset,
        )
        raw_prob = prob_result.get("raw_prob")
        calibrated_prob = prob_result.get("calibrated_prob")

    # ── Canonical-helper lock-step (per bot/CLAUDE.md cal_mlp lock-step) ──
    # Compute the derived features BEFORE cal_mlp so the predict() call can
    # consume the same row_features dict shape the production scan loop uses.
    eval_hour = _parse_iso(eval_time_iso).hour
    hour_sin, hour_cos = compute_hour_sin_cos(eval_hour)
    derived = compute_derived_features(
        spot_price=spot_at_eval,
        threshold=threshold,
        volatility=blended_rv,
        seconds_to_close=seconds_remaining,
        calibrated_prob=calibrated_prob,
        market_price_cents=None,  # Phase 2 v1: no historical Kalshi orderbook
    )
    sigma_winsorize_val = apply_sigma_winsor(derived["spot_distance_to_strike_sigma"])
    prob_breakeven_gap = derived["prob_breakeven_gap"]  # NULL when market_price NULL

    # ── cal_mlp v2 layer ──
    # Phase 2 v1: no HYPE/DOGE trained predictor exists at EITHER deployment
    # site (Mac or VPS) — production ``_calmlp_predictors`` covers only
    # BTC/ETH/SOL/XRP. ``_build_predictor("HYPE")`` warmup raises
    # ``CalMLPError('no_current')`` and returns None; ``predictor=None`` here
    # is the production v1 shape and ``blended_prob`` stays NULL on every row.
    # A future cross-asset transfer eval (fu1 of 86b9wy7v3) would route HYPE/DOGE
    # rows through a ``CalMLPPredictor('BTC')`` instance instead of
    # ``CalMLPPredictor(asset)``.
    blended_prob: Optional[float] = None
    if predictor is not None and raw_prob is not None:
        row_features = {
            "calibrated_prob": calibrated_prob,
            "spot_distance_to_strike_sigma": sigma_winsorize_val,
            "hour_sin": hour_sin,
            "hour_cos": hour_cos,
            "prob_breakeven_gap": prob_breakeven_gap,
            "seconds_to_close": seconds_remaining,
            "asset": asset,
        }
        try:
            cal_prob, _ens_std, _lo, _hi = predictor.predict(
                raw_prob=raw_prob,
                ticker=market["ticker"],
                side="yes",
                entry_price_cents=0,  # market_price unavailable in replay; sentinel
                row_features=row_features,
            )
            blended_prob = float(cal_prob)
        except Exception as e:
            logging.warning(
                "cal_mlp predict failed for %s: %s", market["ticker"], e
            )

    # ── INSERT OR REPLACE — idempotent re-run safe ──
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {REPLAY_TABLE} (
            ticker, evaluation_time, asset, strike_cents, threshold,
            close_time, open_time,
            raw_prob, calibrated_prob, blended_prob,
            spot_at_evaluation, sigma_at_evaluation,
            hour_sin, hour_cos,
            prob_breakeven_gap, sigma_winsorize,
            result, settlement_value,
            data_provenance, replay_run_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            market["ticker"],
            eval_time_iso,
            asset,
            strike_cents,
            threshold,
            market["close_time"],
            market["open_time"],
            raw_prob,
            calibrated_prob,
            blended_prob,
            spot_at_eval,
            blended_rv,
            hour_sin,
            hour_cos,
            prob_breakeven_gap,
            sigma_winsorize_val,
            result,
            settlement_value,
            REPLAY_PROVENANCE,
            int(time.time()),
        ),
    )
    return 1


# ── Kalshi historical market metadata fetch ──────────────────────────


def fetch_kalshi_settled_markets(
    series_ticker: str,
    *,
    end_iso: str = T1_CUTOFF_ISO,
    page_sleep: float = KALSHI_PAGE_SLEEP,
) -> list[dict]:
    """Pull every settled KX*15M market for ``series_ticker`` via cursor pagination.

    Reproduces Phase 1's REST query. ``/markets`` browsing is public (no auth).
    Filters to ``close_time < end_iso`` to scope to pre-T1 history.

    Returns: list of market dicts in Kalshi schema (ticker, event_ticker,
    open_time, close_time, floor_strike, result, ...).
    """
    cursor = ""
    markets: list[dict] = []
    while True:
        params = {
            "series_ticker": series_ticker,
            "status": "settled",
            "limit": "200",
        }
        if cursor:
            params["cursor"] = cursor
        url = KALSHI_MARKETS_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        page = data.get("markets", [])
        for m in page:
            if m.get("close_time", "") < end_iso:
                markets.append(m)
        cursor = data.get("cursor", "")
        if not cursor or not page:
            break
        time.sleep(page_sleep)
    return markets


# ── Historical spot klines fetch ──────────────────────────────────────


def fetch_spot_klines(
    asset: str, start_ts: int, end_ts: int
) -> list[tuple[int, float]]:
    """Fetch 1-min spot klines for HYPE/DOGE from Coinbase over ``[start_ts, end_ts]``.

    Coinbase Exchange `/products/{asset}-USD/candles` is the canonical source:
    - Available from US (Bybit + Binance.com geo-block from Mac).
    - Matches the bot's PRIMARY live feed (``bot/constants.py:700-701`` —
      HYPE-USD/DOGE-USD verified live on Coinbase Exchange).

    Args:
        asset: "HYPE" or "DOGE"
        start_ts: window start (Unix epoch seconds, inclusive)
        end_ts:   window end (Unix epoch seconds, inclusive)

    Returns:
        Ascending list of ``(timestamp_secs, close_price)`` tuples.
    """
    product = f"{asset}-USD"
    return _fetch_coinbase_candles(product, start_ts, end_ts)


def _fetch_coinbase_candles(
    product: str, start_ts: int, end_ts: int
) -> list[tuple[int, float]]:
    """Coinbase ``/products/{X}/candles``, 1-min interval. Public, no auth.

    Coinbase returns candles in DESCENDING time order, capped at
    ``COINBASE_MAX_CANDLES_PER_REQ`` per request. We paginate forward in
    ``COINBASE_MAX_CANDLES_PER_REQ - 1``-minute chunks, advancing
    ``cur_start = cur_end`` between chunks so a non-60-aligned input
    ``start_ts`` doesn't skip the boundary minute (overlap by 1 candle).
    The terminal dedup pass collapses exact-ts duplicates the overlap
    creates; output is sorted ascending.
    """
    out: list[tuple[int, float]] = []
    chunk_secs = (COINBASE_MAX_CANDLES_PER_REQ - 1) * 60  # safety margin under 300 cap
    cur_start = start_ts
    while cur_start < end_ts:
        cur_end = min(cur_start + chunk_secs, end_ts)
        start_iso = datetime.datetime.utcfromtimestamp(cur_start).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        end_iso = datetime.datetime.utcfromtimestamp(cur_end).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        url = (
            COINBASE_CANDLES_URL.format(product)
            + f"?granularity=60&start={start_iso}&end={end_iso}"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "kalshi-bot-replay/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                page = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                logging.warning(
                    "Coinbase 429 for %s; backing off 2s", product
                )
                time.sleep(2.0)
                continue
            raise
        if not page:
            cur_start = cur_end + 60
            continue
        # Coinbase rows: [timestamp_secs, low, high, open, close, volume]
        for row in page:
            ts_sec = int(row[0])
            if start_ts <= ts_sec <= end_ts:
                close = float(row[4])
                out.append((ts_sec, close))
        # Advance to cur_end (overlap by one candle vs. `cur_end + 60`) so
        # if a caller passes a non-60-aligned `start_ts` we don't skip the
        # boundary minute. The `deduped` pass at function end collapses
        # exact-ts duplicates the overlap creates.
        cur_start = cur_end
        time.sleep(EXCHANGE_PAGE_SLEEP)
    # Coinbase returns DESC within each page → ensure ascending overall + de-dup
    out.sort(key=lambda r: r[0])
    deduped: list[tuple[int, float]] = []
    last_ts = -1
    for ts, p in out:
        if ts != last_ts:
            deduped.append((ts, p))
            last_ts = ts
    return deduped


# ── Driver ────────────────────────────────────────────────────────────


def _series_for_asset(asset: str) -> str:
    return f"KX{asset}15M"


def _build_predictor(asset: str):
    """Build a CalMLPPredictor for the asset, or None on failure.

    Production replay: pass the warmed predictor. Tests: don't call this
    (predictor=None → blended_prob NULL)."""
    try:
        from scripts.cal_mlp.integration import CalMLPPredictor
        p = CalMLPPredictor(asset)
        p.warmup()
        return p
    except Exception as e:
        logging.warning("CalMLPPredictor warmup failed for %s: %s", asset, e)
        return None


def _replay_asset(
    conn: sqlite3.Connection,
    asset: str,
    *,
    end_iso: str,
    limit: Optional[int],
    use_cal_mlp: bool,
    dry_run: bool,
) -> int:
    """Replay all (or ``limit``) settled markets for one asset.

    Returns: rows written.
    """
    series = _series_for_asset(asset)
    logging.info("Fetching settled markets for %s (end_iso=%s)", series, end_iso)
    markets = fetch_kalshi_settled_markets(series, end_iso=end_iso)
    if not markets:
        logging.info("No pre-%s markets for %s", end_iso, asset)
        return 0
    # Sort ascending by close_time so the spot-fetch window is determinable
    markets.sort(key=lambda m: m.get("close_time", ""))
    if limit:
        markets = markets[:limit]
    earliest_open = markets[0]["open_time"]
    latest_close = markets[-1]["close_time"]
    logging.info(
        "Replaying %d markets for %s (open %s → close %s)",
        len(markets), asset, earliest_open, latest_close,
    )

    # Dry-run short-circuits BEFORE the heavy spot-fetch: report row counts
    # only. (Network smoke is owned by --limit 5; dry-run is for plan-sizing.)
    if dry_run:
        return sum(
            1
            for m in markets
            if m.get("result", "").lower() in ("yes", "no")
        )

    # Fetch Coinbase klines for the whole [earliest_open - warmup, latest_close]
    start_dt = _parse_iso(earliest_open) - datetime.timedelta(seconds=WARMUP_SECS)
    end_dt = _parse_iso(latest_close)
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    logging.info(
        "Fetching %s spot klines from Coinbase %s → %s",
        asset, start_dt.isoformat(), end_dt.isoformat(),
    )
    ticks = fetch_spot_klines(asset, start_ts, end_ts)
    logging.info("Got %d %s klines (Coinbase 1-min)", len(ticks), asset)

    predictor = _build_predictor(asset) if use_cal_mlp else None

    written = 0
    batch = 0
    for m in markets:
        # Per-market resilience: a malformed Kalshi response or per-market
        # replay failure shouldn't poison a 4,900-market run. Idempotent PK
        # + INSERT OR REPLACE supports re-running once the bad market is
        # fixed upstream.
        try:
            threshold_real, strike_cents_legacy = _floor_strike_to_db_fields(
                m.get("floor_strike")
            )
            market_dict = {
                "ticker": m["ticker"],
                "event_ticker": m.get("event_ticker", ""),
                "asset": asset,
                "threshold": threshold_real,
                "strike_cents": strike_cents_legacy,
                "open_time": m["open_time"],
                "close_time": m["close_time"],
                "result": m.get("result", "").lower(),
            }
            if market_dict["result"] not in ("yes", "no"):
                logging.warning(
                    "Skipping %s — unexpected result %r",
                    market_dict["ticker"], m.get("result"),
                )
                continue
            if market_dict["strike_cents"] is None:
                # Kalshi /markets occasionally returns markets where
                # `floor_strike` is missing or not numerically parseable
                # (the same class the live scanner gates at
                # `bot/scanner/__init__.py:1768-1791` `threshold_unparsable`
                # branch — distinct from the Apr-13 :1794 ratio-drift
                # scale-corruption incident). Without a threshold there's
                # no probability to replay.
                logging.warning(
                    "Skipping %s — floor_strike missing/invalid (%r)",
                    market_dict["ticker"], m.get("floor_strike"),
                )
                continue
            written += replay_market(
                conn, market_dict, ticks, predictor=predictor,
            )
        except Exception as e:
            logging.warning(
                "Replay failed for %s: %s",
                m.get("ticker", "<unknown>"), e,
            )
        batch += 1
        if batch >= 50:
            conn.commit()
            batch = 0
    conn.commit()
    return written


def _floor_strike_to_db_fields(
    floor_strike,
) -> tuple[Optional[float], Optional[int]]:
    """Convert Kalshi ``floor_strike`` (USD float/int) to BOTH the REAL
    threshold (precision-preserving for sub-dollar assets like DOGE) and
    the legacy INTEGER strike_cents (HYPE back-compat — existing 4847-row
    HYPE corpus rows store integer cents).

    P2.3.b-fu2 (2026-05-13, ticket 86b9xtam7) — replaces ``_floor_strike_to_cents``.
    Root cause: integer-cent rounding lost 3-10% of strike value for DOGE
    (spot ~$0.11). See ``kb/findings/replay-backfill-strike-precision-bug-may13.md``.

    Returns ``(None, None)`` on missing, unparseable, or NaN/Inf input —
    matches the legacy single-int helper's None-passthrough so the caller's
    ``if strike_cents is None: skip`` gate keeps working unchanged. The
    NaN/Inf guard prevents ``int(round(nan*100))`` from raising ``ValueError``
    out of the helper (the caller's broad ``except Exception`` would catch it,
    but a typed-None return is the clearer contract).
    """
    if floor_strike is None:
        return (None, None)
    try:
        f = float(floor_strike)
    except (ValueError, TypeError):
        return (None, None)
    if math.isnan(f) or math.isinf(f):
        return (None, None)
    return (f, int(round(f * 100)))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 2 HYPE/DOGE replay backfill (86b9wy7v3)."
    )
    parser.add_argument("--db", required=True, help="state.db path")
    parser.add_argument(
        "--asset",
        choices=ASSETS,
        action="append",
        default=None,
        help="Asset to replay; pass twice for both. Default: both.",
    )
    parser.add_argument(
        "--end-iso",
        default=T1_CUTOFF_ISO,
        help="Cap close_time strictly less than this (default = T1 ship)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max markets per asset (smoke/sample mode)",
    )
    parser.add_argument(
        "--no-cal-mlp",
        action="store_true",
        help="Skip CalMLPPredictor warmup (faster smoke; blended_prob NULL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write rows; report what would happen",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG/INFO/WARNING/ERROR)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    assets = args.asset or list(ASSETS)

    conn = sqlite3.connect(args.db, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    ensure_schema(conn)

    total = 0
    t0 = time.time()
    for asset in assets:
        written = _replay_asset(
            conn,
            asset,
            end_iso=args.end_iso,
            limit=args.limit,
            use_cal_mlp=not args.no_cal_mlp,
            dry_run=args.dry_run,
        )
        total += written
        logging.info("Asset %s: %d rows", asset, written)
    elapsed = time.time() - t0
    logging.info(
        "Phase 2 replay %s: %d rows in %.1fs",
        "DRY-RUN" if args.dry_run else "WROTE",
        total, elapsed,
    )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
