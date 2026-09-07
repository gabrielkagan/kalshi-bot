"""Behavioral-premia probe — Track C of the 2026-09-05 model-first program.

THE HYPOTHESIS CATEGORY (never searched before 2026-09-05): the counterparty on
Kalshi 15M crypto is RETAIL — momentum chasers and longshot buyers — not only
arb bots. If retail systematically overpays in identifiable states, the market's
residual `settle − mid` is reliably non-zero in those states, and the other side
of that flow is an edge. Two explicit questions:
  Q1 (chase):    after a 5-min spot jump, is the MOMENTUM side overpriced?
  Q2 (longshot): are longshots (cheap side 5–15c) overpriced at night / on
                 weekends / on meme assets?

DESIGN. Two stages, both streaming (never a whole day in RAM):
  extract  one row per (settled window, offset in {30,90,180,300}s before the
           nominal close): reliable NBBO + depth from an INCREMENTAL snapshot-
           anchored KalshiBook (no per-window frame buffering), Coinbase spot
           move over the trailing 5 min (z-scored by trailing 5s realized vol),
           Kalshi-mid move over the trailing 5 min (the venue's own momentum —
           available for HYPE/BNB which have no Coinbase spot), taker-side
           imbalance over the trailing 60s from `kalshi_ws/trade` prints,
           5c-band book imbalance, and the post-checkpoint maker-fill facts
           (first crossing print; queue-realistic fill when crossing volume
           exceeds the touch depth we sat behind). Per-day CSV + .done marker.
  analyze  per conditioning cell (feature-bin x offset [x asset class]):
           mean residual, n, n_days, DAY-CLUSTER bootstrap CI (>=1000 resamples
           of days). PRE-REGISTERED flag rule: CI excludes 0 AND |mean| > 3c.
           Multiple-comparison guard: expected false flags under the null,
           plus a split-half check (flag on odd days, confirm on even days).
           Every flagged cell then gets the HONEST ECONOMIC TEST on the
           implied side: taker (cross the spread, Kalshi fee 7*P*(1-P) c/ct)
           and maker (post at the touch, fee-free, fills only when a real
           print crosses — optimistic and queue-realistic), with capacity/day.

DIRECTIONAL RESIDUALS. residual = settle − mid (settle in {0,100}); positive
means YES was UNDER-priced. For a directional feature with row sign d (+1 when
the feature points at YES: spot moved up / takers bought YES / book heavier on
the bid), the directional residual is residual*d: NEGATIVE means the side the
feature points at was OVER-priced — the "retail overpays" signature. For the
longshot question the longshot side is the cheaper side; resid_long = settle_long
− price_long, negative = longshot overpriced.

Reuses: KalshiBook (kalshi_book_reconstruct), _stream_lines (fairvalue_extract),
close_epoch_from_ticker/_epoch (phase1b_real_price_economics), fee formula
(settlement_convergence_p1a). Deliberately does NOT reuse mm_markout_evaluator's
iid bootstrap — the unit of independence here is the DAY.

Usage:
  python3 -m scripts.research.behavioral_premia_probe extract \
      --corpus ~/kalshi-research-data/fairvalue --out ~/kalshi-research-data/behavioral \
      [--days 2026-06-03,2026-06-04] [--workers 4]
  python3 -m scripts.research.behavioral_premia_probe analyze \
      --rows-dir ~/kalshi-research-data/behavioral [--min-n 50] [--min-days 5]
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.fairvalue_extract import _stream_lines
from scripts.research.phase1b_real_price_economics import _epoch, close_epoch_from_ticker
from scripts.research import probe_multiplicity as pm

# ============================================================================
# Universe / constants
# ============================================================================

ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH", "NEAR", "ZEC")
ASSET_CLASS = {
    "BTC": "major", "ETH": "major",
    "HYPE": "meme", "DOGE": "meme", "NEAR": "meme", "ZEC": "meme",   # per Track C prompt
    "SOL": "alt", "XRP": "alt", "BNB": "alt", "ADA": "alt", "BCH": "alt",
}
CB_PRODUCT = {
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD",
    "DOGE": "DOGE-USD", "BCH": "BCH-USD", "ADA": "ADA-USD", "NEAR": "NEAR-USD",
    "ZEC": "ZEC-USD", "HYPE": None, "BNB": None,
}
OFFSETS_S = (30.0, 90.0, 180.0, 300.0)
MOVE_LOOKBACK_S = 300.0      # "5-min spot move" (Q1)
FLOW_LOOKBACK_S = 60.0       # taker-side imbalance window
VOL_STEP_S = 5.0             # rv sampling step (mirrors fairvalue_extract / bot blended_rv)
VOL_WINDOW_S = 300.0
DEPTH_BAND_TICKS = 500       # 5c band (ticks of $0.0001) for book imbalance
CLOSE_MARGIN_S = 120.0       # finalize once the stream clock passes close+margin
SMALL_PRINT_CT = 5.0         # "retail-sized" print threshold (contracts)
FLAG_MIN_ABS_MEAN_C = 3.0    # pre-registered economic bar (1/2 spread + fee)
MAX_QUOTE_STALE_S = 15.0     # checkpoint fallback: last reliable book no older than this


def asset_of(ticker: str) -> Optional[str]:
    for a in ASSETS:
        if ticker.startswith(f"KX{a}15M-"):
            return a
    return None


def kalshi_fee_cents(price_cents: float) -> float:
    """Kalshi taker fee, amortized large-order rate: 7*P*(1-P) cents/contract."""
    p = price_cents / 100.0
    return 7.0 * p * (1.0 - p)


def kalshi_fee_cents_small_order(price_cents: float, contracts: int) -> float:
    """Per-contract fee for a small order: Kalshi rounds the ORDER fee up to the
    next cent, so ceil(0.07*C*P*(1-P) dollars) / C. At C=1 this is >=1c."""
    p = price_cents / 100.0
    order_fee_dollars = math.ceil(0.07 * contracts * p * (1.0 - p) * 100.0 - 1e-12) / 100.0
    return order_fee_dollars * 100.0 / contracts


# ============================================================================
# Pure statistical / feature core (IO-free; unit-tested)
# ============================================================================


def residual_cents(result: str, mid_cents: float) -> float:
    """settle − mid; settle=100 if YES else 0. Positive => YES under-priced."""
    return (100.0 if result == "yes" else 0.0) - float(mid_cents)


def directional_residual(residual: float, sign: int) -> float:
    """residual * sign(feature). sign=+1 when the feature points at YES.
    Negative => the side the feature points at was OVER-priced."""
    return float(residual) * (1 if sign > 0 else -1)


def longshot_view(result: str, mid_cents: float) -> Tuple[str, float, float]:
    """(longshot_side, longshot_price_c, resid_long). The longshot side is the
    CHEAPER side. resid_long = settle_long − price_long; negative => longshot
    overpriced (its buyers overpay). At mid=50 the YES side is arbitrarily chosen."""
    if mid_cents <= 50.0:
        side, price = "yes", float(mid_cents)
    else:
        side, price = "no", 100.0 - float(mid_cents)
    settle_long = 100.0 if result == side else 0.0
    return side, price, settle_long - price


def sign_of(x: Optional[float], dead_zone: float = 0.0) -> int:
    """+1 / −1 / 0 (None or |x|<=dead_zone -> 0)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0
    if x > dead_zone:
        return 1
    if x < -dead_zone:
        return -1
    return 0


def day_bootstrap_ci(
    values: Sequence[float], days: Sequence[str], *, n_boot: int = 1000,
    alpha: float = 0.05, seed: int = 20260905,
) -> Tuple[float, float]:
    """Percentile CI for the pooled mean under a DAY-CLUSTER bootstrap: resample
    days with replacement, pool all rows of the drawn days (row-weighted mean).
    Deterministic seed. Returns (nan, nan) for empty input; degenerate (lo==hi)
    when only one day is present — callers must gate on n_days."""
    if not values:
        return (float("nan"), float("nan"))
    by_day: Dict[str, List[float]] = defaultdict(list)
    for v, d in zip(values, days):
        by_day[d].append(float(v))
    day_keys = sorted(by_day)
    sums = [sum(by_day[d]) for d in day_keys]
    cnts = [len(by_day[d]) for d in day_keys]
    k = len(day_keys)
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        s = c = 0.0
        for _ in range(k):
            i = rng.randrange(k)
            s += sums[i]
            c += cnts[i]
        means.append(s / c)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (lo, hi)


def flag_cell(mean: float, ci_lo: float, ci_hi: float,
              min_abs_mean: float = FLAG_MIN_ABS_MEAN_C) -> bool:
    """PRE-REGISTERED rule: CI excludes 0 AND |mean| > min_abs_mean (3c)."""
    if any(math.isnan(x) for x in (mean, ci_lo, ci_hi)):
        return False
    excludes_zero = (ci_lo > 0.0) or (ci_hi < 0.0)
    return excludes_zero and abs(mean) > min_abs_mean


def hour_bucket(close_ts: float) -> str:
    """UTC 6h buckets. 00-06Z = US night / Asia day; 06-12 = EU morning;
    12-18 = US day; 18-24 = US evening."""
    h = datetime.fromtimestamp(close_ts, tz=timezone.utc).hour
    return ("00-06Z", "06-12Z", "12-18Z", "18-24Z")[h // 6]


def is_weekend(close_ts: float) -> bool:
    return datetime.fromtimestamp(close_ts, tz=timezone.utc).weekday() >= 5


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def z_bucket(z: Optional[float]) -> str:
    """5-min move in trailing-vol sigmas: big_down/down/flat/up/big_up/na."""
    if z is None or math.isnan(z):
        return "na"
    if z <= -1.0:
        return "big_down"
    if z <= -0.3:
        return "down"
    if z < 0.3:
        return "flat"
    if z < 1.0:
        return "up"
    return "big_up"


def imbalance_bucket(imb: Optional[float], n: float) -> str:
    """(yes − no)/(yes + no) in [-1,1]; 'none' when no prints."""
    if n <= 0 or imb is None or math.isnan(imb):
        return "none"
    if imb <= -0.5:
        return "strong_no"
    if imb < -0.1:
        return "lean_no"
    if imb <= 0.1:
        return "balanced"
    if imb < 0.5:
        return "lean_yes"
    return "strong_yes"


def size_bucket(small_share: Optional[float]) -> str:
    """Print-size mix of the trailing-60s taker flow (share of prints <= SMALL_PRINT_CT
    contracts): 'retail' (>=0.6) / 'block' (<=0.4) / 'mixed' / 'na'. Pre-registered
    2026-09-06 (Track C phase 2) for the market-maker informed-vs-noise question."""
    if small_share is None or (isinstance(small_share, float) and math.isnan(small_share)):
        return "na"
    if small_share >= 0.6:
        return "retail"
    if small_share <= 0.4:
        return "block"
    return "mixed"


def price_band(mid: float) -> str:
    """Bands on the MID (symmetric): 1-4 / 5-15 / 16-30 / 31-49 / 50 / 51-69 / 70-84 / 85-95 / 96-99."""
    m = float(mid)
    if m < 5:
        return "01-04"
    if m <= 15:
        return "05-15"
    if m <= 30:
        return "16-30"
    if m < 50:
        return "31-49"
    if m == 50:
        return "50"
    if m < 70:
        return "51-69"
    if m < 85:
        return "70-84"
    if m <= 95:
        return "85-95"
    return "96-99"


def longshot_band(price_long: float) -> str:
    p = float(price_long)
    if p < 5:
        return "01-04"
    if p <= 15:
        return "05-15"
    if p <= 30:
        return "16-30"
    return "31-50"


def flow_imbalance(trades: Sequence[tuple], t: float, lookback_s: float) -> dict:
    """Taker-side flow in (t−lookback, t]: `trades` sorted by ts, tuples
    (ts, yes_c, count, taker_side). Returns yes_ct, no_ct, n, small_share, imb."""
    ts_list = [x[0] for x in trades]
    lo = bisect.bisect_right(ts_list, t - lookback_s)
    hi = bisect.bisect_right(ts_list, t)
    yes_ct = no_ct = 0.0
    n = 0
    small = 0
    for i in range(lo, hi):
        _, _, cnt, side = trades[i]
        n += 1
        if cnt <= SMALL_PRINT_CT:
            small += 1
        if side == "yes":
            yes_ct += cnt
        else:
            no_ct += cnt
    tot = yes_ct + no_ct
    return {
        "yes_ct": yes_ct, "no_ct": no_ct, "n": n,
        "small_share": (small / n) if n else float("nan"),
        "imb": ((yes_ct - no_ct) / tot) if tot > 0 else float("nan"),
    }


def maker_fill(trades: Sequence[tuple], post_ts: float, close_ts: float, side: str,
               yes_price_c: float, queue_depth: float) -> dict:
    """Resting order posted at `post_ts` at the touch on `side`:
      side='yes': we BID yes at yes_price_c (=best yes bid). Fills when a taker
                  SELLS yes (taker_side='no') at yes_c <= our bid, strictly after
                  post and before close.
      side='no' : we BID no at 100−yes_price_c (yes_price_c = best yes ask).
                  Fills when a taker BUYS yes (taker_side='yes') at yes_c >=
                  yes_price_c.
    Optimistic fill = first crossing print. Queue-realistic fill = the print at
    which cumulative crossing volume exceeds `queue_depth` (the size already
    resting at our level when we joined the back of the queue). Returns
    {opt_ts, q_ts, cross_vol, q_fill_ct} (None ts when unfilled)."""
    ts_list = [x[0] for x in trades]
    i0 = bisect.bisect_right(ts_list, post_ts)
    opt_ts = q_ts = None
    cum = 0.0
    q_fill_ct = 0.0
    for i in range(i0, len(trades)):
        ts, yes_c, cnt, taker = trades[i]
        if ts > close_ts:
            break
        crossed = (taker == "no" and yes_c <= yes_price_c) if side == "yes" \
            else (taker == "yes" and yes_c >= yes_price_c)
        if not crossed:
            continue
        if opt_ts is None:
            opt_ts = ts
        prev = cum
        cum += cnt
        if q_ts is None and cum > queue_depth:
            q_ts = ts
            q_fill_ct = cum - max(prev, queue_depth)  # our share of that print
    return {"opt_ts": opt_ts, "q_ts": q_ts, "cross_vol": cum, "q_fill_ct": q_fill_ct}


# ============================================================================
# Incremental book tracker (no per-window frame buffering)
# ============================================================================


def book_depth_band(book: kbr.KalshiBook, side: str, band_ticks: int) -> float:
    """Contracts resting within `band_ticks` of the touch on one side."""
    levels = book.yes if side == "yes" else book.no
    live = [t for t, s in levels.items() if s > 0]
    if not live:
        return 0.0
    best = max(live)
    return sum(s for t, s in levels.items() if s > 0 and t >= best - band_ticks)


def book_state(book: kbr.KalshiBook, anchored: bool) -> Optional[dict]:
    """Snapshot-anchored + physically reliable book -> touch/depth dict; else None."""
    if not anchored or not book.is_reliable():
        return None
    bid = book.best_yes_bid_cents()
    ask = book.best_yes_ask_cents()
    if bid is None or ask is None or not (0 < ask < 100) or not (0 < bid < 100):
        return None
    return {
        "yes_bid": bid, "yes_ask": ask,
        "bid_depth1": book.best_yes_bid_depth() or 0.0,
        "ask_depth1": book.best_yes_ask_depth() or 0.0,
        "bid_depth5": book_depth_band(book, "yes", DEPTH_BAND_TICKS),
        "ask_depth5": book_depth_band(book, "no", DEPTH_BAND_TICKS),
    }


class WindowTracker:
    """One settled window: incremental KalshiBook + ordered checkpoints. On each
    incoming frame, every pending checkpoint with time < frame ts is recorded
    from the book state BEFORE the frame is applied (state as-of the checkpoint;
    no look-ahead). At finalize, still-pending checkpoints take the current state
    (no frame arrived after them, so it is exactly the as-of state)."""

    __slots__ = ("book", "anchored", "checkpoints", "recorded", "n_frames", "last_ts",
                 "last_reliable", "max_stale_s")

    def __init__(self, checkpoint_times: Iterable[float], max_stale_s: float = MAX_QUOTE_STALE_S):
        self.book = kbr.KalshiBook()
        self.anchored = False
        self.checkpoints = sorted(set(float(t) for t in checkpoint_times))
        self.recorded: Dict[float, Optional[dict]] = {}
        self.n_frames = 0
        self.last_ts: Optional[float] = None
        self.last_reliable: Optional[Tuple[float, dict]] = None  # (ts, state) after the last frame
        self.max_stale_s = max_stale_s

    def _record_pending(self, upto_ts: float) -> None:
        while self.checkpoints and self.checkpoints[0] < upto_ts:
            cp = self.checkpoints.pop(0)
            st = book_state(self.book, self.anchored)
            if st is not None and self.last_ts is not None and (cp - self.last_ts) > self.max_stale_s:
                # frames stopped arriving long before the checkpoint (collector gap /
                # missing chunk): not a live quote — refuse, do not record a frozen mid.
                # RCA 2026-09-06, kb/failures/track-c-frozen-book-rows-sep06.md.
                st = None
            if st is not None:
                st["quote_age_s"] = 0.0 if self.last_ts is None else max(0.0, cp - self.last_ts)
            elif self.last_reliable is not None and (cp - self.last_reliable[0]) <= self.max_stale_s:
                # exact-instant book transiently crossed/one-sided (a delta burst mid-
                # flight): fall back to the last reliable state within max_stale_s,
                # the bounded version of what reliable_nbbo_timeline callers did.
                st = dict(self.last_reliable[1])
                st["quote_age_s"] = cp - self.last_reliable[0]
                st["fallback"] = 1
            self.recorded[cp] = st

    def _note_reliable(self, ts: float) -> None:
        if not self.checkpoints:
            return  # nothing left to record; skip the O(levels) reliability scan
        st = book_state(self.book, self.anchored)
        if st is not None:
            self.last_reliable = (ts, st)

    def on_frame(self, ts: float, inner: dict) -> None:
        self._record_pending(ts)
        if inner.get("type") == "orderbook_snapshot":
            self.book = kbr.KalshiBook()
            self.anchored = True
        self.book.apply_frame(inner)
        self.n_frames += 1
        self.last_ts = ts
        self._note_reliable(ts)

    def on_compact(self, ts: float, rec: tuple) -> None:
        """Apply a compact record from `compact_frame` (same semantics as on_frame)."""
        self._record_pending(ts)
        if rec[0] == 1:
            self.book = kbr.KalshiBook()
            self.anchored = True
            self.book.apply_snapshot(rec[1], rec[2])
        else:
            levels = self.book.yes if rec[1] else self.book.no
            new = levels.get(rec[2], 0.0) + rec[3]
            if new > 1e-9:
                levels[rec[2]] = new
            else:
                levels.pop(rec[2], None)
        self.n_frames += 1
        self.last_ts = ts
        self._note_reliable(ts)

    def finalize(self) -> Dict[float, Optional[dict]]:
        self._record_pending(float("inf"))
        return self.recorded


def compact_frame(inner: dict) -> Optional[tuple]:
    """Bronze inner frame -> compact tuple so a buffered window costs ~100 B/frame:
      delta:    (0, is_yes: bool, tick: int, delta_fp: float)
      snapshot: (1, yes_levels: list[(price_str, size_str)], no_levels: list)
    None for anything else (control frames)."""
    t = inner.get("type")
    msg = inner.get("msg", {})
    try:
        if t == "orderbook_delta":
            return (0, msg["side"] == "yes", kbr._tick(msg["price_dollars"]), float(msg["delta_fp"]))
        if t == "orderbook_snapshot":
            return (1, [tuple(x) for x in msg.get("yes_dollars_fp", [])],
                    [tuple(x) for x in msg.get("no_dollars_fp", [])])
    except (KeyError, ValueError, TypeError):
        return None
    return None


def replay_window(records: List[tuple], checkpoint_times: Iterable[float]) -> Tuple[Dict[float, Optional[dict]], int]:
    """SORT a window's buffered (ts, compact) records by recv time, then replay
    through a WindowTracker. The bronze day files are concatenations of 5-min
    per-connection chunks in ARBITRARY order within each hour, so incremental
    application in file order crosses the book (2026-09-05 smoke: 57% of
    windows unreliable; sorted replay is what every prior harness did)."""
    records.sort(key=lambda x: x[0])
    tr = WindowTracker(checkpoint_times)
    for ts, rec in records:
        tr.on_compact(ts, rec)
    return tr.finalize(), tr.n_frames


def hour_complete(close_ts: float, stream_ts: float, margin_s: float = CLOSE_MARGIN_S) -> bool:
    """A window's frames are all on disk once the stream has entered the UTC hour
    AFTER the hour containing close+margin (hours are pulled sequentially; only
    the chunk order WITHIN an hour is arbitrary)."""
    return stream_ts >= (math.floor((close_ts + margin_s) / 3600.0) + 1) * 3600.0


# ============================================================================
# Spot (Coinbase ticker) per-day loader
# ============================================================================


def load_spot_day(cb_dir: str, day: str) -> Dict[str, Tuple[list, list]]:
    """{asset: (secs_sorted, prices)} for one UTC day partition, <=1 obs/sec."""
    y, m, d = day.split("-")
    want = {p: a for a, p in CB_PRODUCT.items() if p}
    raw: Dict[str, Dict[int, float]] = defaultdict(dict)
    for f in glob.glob(os.path.join(cb_dir, f"year={y}", f"month={m}", f"day={d}", "**", "*.zst"),
                       recursive=True):
        for line in _stream_lines(f):
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                a = want.get(inner.get("product_id"))
                if a is None:
                    continue
                px = inner.get("price")
                if px is None:
                    continue
                raw[a][int(_epoch(env["_wire_recv_ts"]))] = float(px)
            except (ValueError, KeyError, TypeError):
                continue
    return {a: (sorted(dd), [dd[s] for s in sorted(dd)]) for a, dd in raw.items()}


def merge_spot(*days: Dict[str, Tuple[list, list]]) -> Dict[str, Tuple[list, list]]:
    merged: Dict[str, Dict[int, float]] = defaultdict(dict)
    for sp in days:
        for a, (secs, px) in sp.items():
            for s, p in zip(secs, px):
                merged[a][s] = p
    return {a: (sorted(dd), [dd[s] for s in sorted(dd)]) for a, dd in merged.items()}


def spot_at(tl: Optional[Tuple[list, list]], t: float, max_age_s: float = 120.0) -> Optional[float]:
    """Last spot print at or before t, refusing prints older than max_age_s."""
    if tl is None:
        return None
    secs, px = tl
    i = bisect.bisect_right(secs, t) - 1
    if i < 0 or (t - secs[i]) > max_age_s:
        return None
    return px[i]


def realized_vol_5s(tl, t: float) -> Optional[float]:
    """stdev of per-5s log returns over [t−300s, t]; None if any sample missing."""
    k = int(VOL_WINDOW_S // VOL_STEP_S)
    samples = []
    for j in range(k, -1, -1):
        s = spot_at(tl, t - j * VOL_STEP_S)
        if s is None or s <= 0:
            return None
        samples.append(s)
    rets = [math.log(samples[i] / samples[i - 1]) for i in range(1, len(samples))]
    if len(rets) < 10:
        return None
    return statistics.stdev(rets)


def spot_move_z(tl, t: float, lookback_s: float = MOVE_LOOKBACK_S) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(log move over lookback, rv_5s, z = move / (rv*sqrt(lookback/5)))."""
    a, b = spot_at(tl, t - lookback_s), spot_at(tl, t)
    if a is None or b is None or a <= 0:
        return None, None, None
    mv = math.log(b / a)
    rv = realized_vol_5s(tl, t)
    if rv is None or rv <= 0:
        return mv, rv, None
    return mv, rv, mv / (rv * math.sqrt(lookback_s / VOL_STEP_S))


# ============================================================================
# Trades per-day loader
# ============================================================================


def parse_trade_line(line: str) -> Optional[Tuple[str, float, float, float, str]]:
    """-> (ticker, ts, yes_c, count, taker_side) or None."""
    try:
        env = json.loads(line)
        msg = json.loads(env["_raw"])["msg"]
        tk = msg["market_ticker"]
        if asset_of(tk) is None:
            return None
        ts = float(msg["ts_ms"]) / 1000.0 if "ts_ms" in msg else float(msg["ts"])
        return (tk, ts, float(msg["yes_price_dollars"]) * 100.0,
                float(msg["count_fp"]), msg["taker_side"])
    except (ValueError, KeyError, TypeError):
        return None


def load_trades_day(trades_dir: str, day: str) -> Dict[str, list]:
    """{ticker: sorted [(ts, yes_c, count, taker_side)]} for one day file."""
    out: Dict[str, list] = defaultdict(list)
    for path in (os.path.join(trades_dir, f"day={day}.jsonl.zst"),
                 os.path.join(trades_dir, f"day={day}.jsonl")):
        if not os.path.exists(path):
            continue
        for line in _stream_lines(path):
            p = parse_trade_line(line)
            if p is not None:
                out[p[0]].append(p[1:])
        break
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


def merge_trades(*days: Dict[str, list]) -> Dict[str, list]:
    out: Dict[str, list] = defaultdict(list)
    for d in days:
        for tk, lst in d.items():
            out[tk].extend(lst)
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


# ============================================================================
# Lifecycle labels
# ============================================================================


def _lifecycle_lines(path: str):
    """Lifecycle chunks are tiny (KBs); decompress in-process when the
    `zstandard` module is available (20K subprocess spawns is the bottleneck
    on a loaded machine), else fall back to the zstd pipe."""
    try:
        import zstandard  # type: ignore
    except ImportError:
        yield from _stream_lines(path)
        return
    # ticket 86bbvrx1t: `stream_reader(fh).read()` returns a silent PREFIX on a
    # truncated frame — the zstandard LIBRARY raises nothing at all (measured:
    # 44,617 lines from a half file), and byte counts cannot detect it because a
    # truncated file IS fully consumed; only the frame is incomplete.
    # decompressobj.eof is the reliable signal. Flagged by the Track C session
    # 2026-09-07: a truncated lifecycle read here yields MISSING determined
    # windows, i.e. under-coverage biasing toward fewer rows — the direction
    # that manufactures a null.
    dobj = zstandard.ZstdDecompressor().decompressobj()
    chunks = []
    with open(path, "rb") as fh:
        while True:
            blk = fh.read(1 << 20)
            if not blk:
                break
            out = dobj.decompress(blk)
            if out:
                chunks.append(out)
    if not dobj.eof:
        raise RuntimeError(
            f"{path}: zstd frame did NOT terminate — lifecycle chunk is "
            f"TRUNCATED, so determined windows would be silently MISSING.")
    for raw in b"".join(chunks).split(b"\n"):
        if raw.strip():
            yield raw.decode("utf-8", "replace")


def _lifecycle_files(lifecycle_dir: str, days: Optional[Sequence[str]] = None) -> List[str]:
    """Lifecycle chunk paths. With `days`, only the day partitions of those days
    PLUS one day after each (a window closing 23:5x is `determined` in the next
    UTC day's partition) — the full-corpus glob is 20K+ files and the bottleneck
    on a loaded machine."""
    if not days:
        return sorted(glob.glob(os.path.join(lifecycle_dir, "**", "*.zst"), recursive=True))
    want = set()
    for d in days:
        dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        for k in (0, 1):
            want.add(datetime.fromtimestamp(dt.timestamp() + k * 86400, tz=timezone.utc))
    out: List[str] = []
    for dt in sorted(want):
        part = os.path.join(lifecycle_dir, f"year={dt:%Y}", f"month={dt:%m}", f"day={dt:%d}")
        out.extend(glob.glob(os.path.join(part, "**", "*.zst"), recursive=True))
    return sorted(out)


def load_determined(lifecycle_dir: str, assets: Sequence[str],
                    cache_path: Optional[str] = None,
                    days: Optional[Sequence[str]] = None) -> Dict[str, dict]:
    """{ticker: {asset, result, det_ts, strike}} for crypto-15M `determined`
    events. Optional pickle cache keyed by the SET of (path, size) of lifecycle
    chunks — never mtimes (rclone preserves source mtimes; GENHUNT §4 trap)."""
    import hashlib
    import pickle
    assets = set(assets)
    files = _lifecycle_files(lifecycle_dir, days)
    key = hashlib.sha1("\n".join(f"{f}:{os.path.getsize(f)}" for f in files).encode()).hexdigest()
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as fh:
                cached = pickle.load(fh)
            if cached.get("key") == key and cached.get("assets") == sorted(assets):
                return cached["determined"]
        except Exception:  # noqa: BLE001 — a bad cache is just a miss
            pass
    out: Dict[str, dict] = {}
    strikes: Dict[str, float] = {}
    for f in files:
        for line in _lifecycle_lines(f):
            try:
                msg = json.loads(json.loads(line)["_raw"]).get("msg", {})
            except (ValueError, KeyError, TypeError):
                continue
            tk = msg.get("market_ticker", "")
            a = asset_of(tk)
            if a is None or a not in assets:
                continue
            et = msg.get("event_type")
            if et == "metadata_updated" and msg.get("floor_strike") is not None:
                strikes[tk] = float(msg["floor_strike"])
            elif et == "determined" and msg.get("result") in ("yes", "no"):
                out[tk] = {"asset": a, "result": msg["result"],
                           "det_ts": float(msg["determination_ts"])}
    for tk, d in out.items():
        d["strike"] = strikes.get(tk)
    if cache_path:
        tmp = cache_path + ".tmp"
        with open(tmp, "wb") as fh:
            pickle.dump({"key": key, "assets": sorted(assets), "determined": out}, fh, protocol=4)
        os.replace(tmp, cache_path)
    return out


# ============================================================================
# Extract driver
# ============================================================================

ROW_FIELDS = [
    "ticker", "asset", "cls", "day", "close_ts", "det_ts", "result", "strike",
    "offset_s", "t", "yes_bid", "yes_ask", "mid", "spread",
    "bid_depth1", "ask_depth1", "bid_depth5", "ask_depth5", "book_imb5",
    "quote_age_s", "fallback",
    "mid_prev5m", "kmid_move5m",
    "spot", "spot_move5m", "rv_5s", "spot_z5",
    "flow_yes_ct60", "flow_no_ct60", "flow_n60", "flow_small_share60", "flow_imb60",
    "yes_fill_opt_ts", "yes_fill_q_ts", "yes_cross_vol", "yes_q_fill_ct",
    "no_fill_opt_ts", "no_fill_q_ts", "no_cross_vol", "no_q_fill_ct",
    "n_frames",
]


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        return f"{v:.6g}" if abs(v) < 1e6 else f"{v:.1f}"
    return v


def build_rows(tk: str, meta: dict, recorded: Dict[float, Optional[dict]], close_ts: float,
               offsets: Sequence[float], trades: Sequence[tuple], spot_tl,
               n_frames: int) -> List[dict]:
    rows = []
    a = meta["asset"]
    for off in offsets:
        t = close_ts - off
        st = recorded.get(t)
        if st is None:
            continue
        mid = (st["yes_bid"] + st["yes_ask"]) / 2.0
        prev = recorded.get(t - MOVE_LOOKBACK_S)
        mid_prev = ((prev["yes_bid"] + prev["yes_ask"]) / 2.0) if prev else None
        fl = flow_imbalance(trades, t, FLOW_LOOKBACK_S)
        mv, rv, z = spot_move_z(spot_tl, t)
        sp = spot_at(spot_tl, t)
        yf = maker_fill(trades, t, close_ts, "yes", st["yes_bid"], st["bid_depth1"])
        nf = maker_fill(trades, t, close_ts, "no", st["yes_ask"], st["ask_depth1"])
        d5 = st["bid_depth5"] + st["ask_depth5"]
        rows.append({
            "ticker": tk, "asset": a, "cls": ASSET_CLASS[a], "day": utc_day(close_ts),
            "close_ts": f"{close_ts:.0f}", "det_ts": f"{meta['det_ts']:.0f}",
            "result": meta["result"], "strike": meta.get("strike"),
            "offset_s": int(off), "t": f"{t:.0f}",
            "yes_bid": st["yes_bid"], "yes_ask": st["yes_ask"], "mid": mid,
            "spread": st["yes_ask"] - st["yes_bid"],
            "bid_depth1": st["bid_depth1"], "ask_depth1": st["ask_depth1"],
            "bid_depth5": st["bid_depth5"], "ask_depth5": st["ask_depth5"],
            "book_imb5": ((st["bid_depth5"] - st["ask_depth5"]) / d5) if d5 > 0 else None,
            "quote_age_s": st.get("quote_age_s"), "fallback": st.get("fallback", 0),
            "mid_prev5m": mid_prev,
            "kmid_move5m": (mid - mid_prev) if mid_prev is not None else None,
            "spot": sp, "spot_move5m": mv, "rv_5s": rv, "spot_z5": z,
            "flow_yes_ct60": fl["yes_ct"], "flow_no_ct60": fl["no_ct"], "flow_n60": fl["n"],
            "flow_small_share60": fl["small_share"], "flow_imb60": fl["imb"],
            "yes_fill_opt_ts": yf["opt_ts"], "yes_fill_q_ts": yf["q_ts"],
            "yes_cross_vol": yf["cross_vol"], "yes_q_fill_ct": yf["q_fill_ct"],
            "no_fill_opt_ts": nf["opt_ts"], "no_fill_q_ts": nf["q_ts"],
            "no_cross_vol": nf["cross_vol"], "no_q_fill_ct": nf["q_fill_ct"],
            "n_frames": n_frames,
        })
    return rows


def extract_days(corpus: str, out_dir: str, days: Sequence[str], determined: Dict[str, dict],
                 offsets: Sequence[float] = OFFSETS_S, log=print, max_lines: int = 0) -> dict:
    """Stream the given CONSECUTIVE day files with shared trackers (a window that
    straddles midnight finalizes once, from its full pre-close book). Writes
    <out_dir>/rows_<day>.csv per day (rows keyed by the window's close day) and
    a .done_<day> marker per day (JSON funnel). Trades/spot loaded for the
    current + previous day so a straddler's pre-checkpoint flow is complete."""
    frames_dir = os.path.join(corpus, "frames")
    trades_dir = os.path.join(corpus, "trades")
    cb_dir = os.path.join(corpus, "coinbase_ticker")
    os.makedirs(out_dir, exist_ok=True)

    buffers: Dict[str, List[tuple]] = {}
    closes: Dict[str, float] = {}
    done: set = set()
    stream_ts = 0.0
    funnel = defaultdict(int)
    writers: Dict[str, Tuple[csv.DictWriter, object]] = {}
    prev_trades: Dict[str, list] = {}
    prev_spot: Dict[str, Tuple[list, list]] = {}
    cur_trades = cur_spot = None

    def writer_for(day: str) -> csv.DictWriter:
        if day not in writers:
            fh = open(os.path.join(out_dir, f"rows_{day}.csv.part"), "w", newline="")
            w = csv.DictWriter(fh, fieldnames=ROW_FIELDS)
            w.writeheader()
            writers[day] = (w, fh)
        return writers[day][0]

    def finalize(tk: str) -> None:
        recs = buffers.pop(tk)
        close_ts = closes.pop(tk)
        meta = determined[tk]
        cps = [close_ts - off for off in offsets] + [close_ts - off - MOVE_LOOKBACK_S for off in offsets]
        recorded, n_frames = replay_window(recs, cps)
        trades = merged_trades.get(tk, [])
        spot_tl = merged_spot.get(meta["asset"])
        rows = build_rows(tk, meta, recorded, close_ts, offsets, trades, spot_tl, n_frames)
        funnel["windows_finalized"] += 1
        funnel["rows"] += len(rows)
        if not rows:
            funnel["windows_no_reliable_book"] += 1
        w = writer_for(utc_day(close_ts))
        for r in rows:
            w.writerow({k: _fmt(r.get(k)) for k in ROW_FIELDS})

    for di, day in enumerate(days):
        t0 = time.time()
        fp = os.path.join(frames_dir, f"day={day}.jsonl.zst")
        if not os.path.exists(fp):
            log(f"[bp] {day} frames missing -> skip")
            continue
        cur_trades = load_trades_day(trades_dir, day)
        cur_spot = load_spot_day(cb_dir, day)
        merged_trades = merge_trades(prev_trades, cur_trades)
        merged_spot = merge_spot(prev_spot, cur_spot)
        log(f"[bp] {day} trades tickers={len(cur_trades)} spot assets={sorted(cur_spot)} "
            f"load={time.time()-t0:.0f}s")
        n_lines = 0
        for line in _stream_lines(fp):
            n_lines += 1
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                tk = inner["msg"]["market_ticker"]
            except (ValueError, KeyError, TypeError):
                continue
            if tk in done:
                continue
            meta = determined.get(tk)
            if meta is None:
                done.add(tk)
                funnel["tickers_unsettled_or_out_of_universe"] += 1
                continue
            rec = compact_frame(inner)
            if rec is None:
                continue
            ts = _epoch(env["_wire_recv_ts"])
            if ts > stream_ts:
                stream_ts = ts
            buf = buffers.get(tk)
            if buf is None:
                buf = buffers[tk] = []
                closes[tk] = close_epoch_from_ticker(tk)
                funnel["windows_seen"] += 1
            buf.append((ts, rec))
            if n_lines % 200000 == 0:
                for t in [t for t, c in closes.items() if hour_complete(c, stream_ts)]:
                    finalize(t)
                    done.add(t)
            if max_lines and n_lines >= max_lines:
                break
        # end of day file: finalize hour-complete windows (keep midnight straddlers open)
        for t in [t for t, c in closes.items() if hour_complete(c, stream_ts)]:
            finalize(t)
            done.add(t)
        prev_trades, prev_spot = cur_trades, cur_spot
        log(f"[bp] {day} DONE lines={n_lines} open_windows={len(buffers)} "
            f"buffered_frames={sum(len(b) for b in buffers.values())} "
            f"funnel={dict(funnel)} {time.time()-t0:.0f}s")
    # flush stragglers (last day of this chunk): sorted replay is still exact for
    # every window whose frames are entirely on disk; a window cut by the chunk
    # boundary just has fewer checkpoints recorded.
    for t in list(buffers):
        finalize(t)
        done.add(t)
    for day, (w, fh) in writers.items():
        fh.close()
        os.replace(os.path.join(out_dir, f"rows_{day}.csv.part"),
                   os.path.join(out_dir, f"rows_{day}.csv"))
    for day in days:
        if os.path.exists(os.path.join(out_dir, f"rows_{day}.csv")):
            with open(os.path.join(out_dir, f".done_{day}"), "w") as fh:
                json.dump({"day": day, "funnel_chunk": dict(funnel), "days_in_chunk": list(days)}, fh)
    return dict(funnel)


# ============================================================================
# Analyze
# ============================================================================


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_rows(rows_dir: str) -> List[dict]:
    rows: List[dict] = []
    for path in sorted(glob.glob(os.path.join(rows_dir, "rows_*.csv"))):
        day = os.path.basename(path)[len("rows_"):-len(".csv")]
        if not os.path.exists(os.path.join(rows_dir, f".done_{day}")):
            continue  # unsealed partial — refuse
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                for k in ROW_FIELDS:
                    if k in ("ticker", "asset", "cls", "day", "result"):
                        continue
                    r[k] = _f(r.get(k))
                rows.append(r)
    return rows


def valid_rows(rows: List[dict], max_quote_age_s: Optional[float], exclude_fallback: bool) -> List[dict]:
    """Data-validity filter applied at analyze time (2026-09-06 phase-2 RCA): drop rows
    whose recorded book is older than `max_quote_age_s` (the tracker only bounded the
    age of FALLBACK states; a non-fallback state whose last frame was minutes old was
    recorded as if live) and, optionally, fallback rows. None => no age filter."""
    out = []
    for r in rows:
        if exclude_fallback and (r.get("fallback") or 0) >= 1:
            continue
        age = r.get("quote_age_s")
        if max_quote_age_s is not None and (age is None or age > max_quote_age_s):
            continue
        out.append(r)
    return out


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def spot_fair_value_c(spot: Optional[float], strike: Optional[float], rv_5s: Optional[float],
                      offset_s: float) -> Optional[float]:
    """Normal-model P(spot_T > strike) in cents from spot at the checkpoint, the
    trailing 5s realized vol, and the time left (mirrors fairvalue_extract).
    DIAGNOSTIC ONLY: `lag = fair − mid` tells whether a conditional residual is
    the market lagging spot (Track B's stale-quote seam) rather than a premium."""
    if spot is None or strike is None or rv_5s is None or spot <= 0 or rv_5s <= 0:
        return None
    sigma_move = spot * rv_5s * math.sqrt(offset_s / VOL_STEP_S)
    if sigma_move <= 0:
        return None
    return 100.0 * _norm_cdf((spot - strike) / sigma_move)


def enrich(r: dict) -> dict:
    """Derived per-row fields used by the cell map."""
    mid = r["mid"]
    r["residual"] = residual_cents(r["result"], mid)
    fv = spot_fair_value_c(r.get("spot"), r.get("strike"), r.get("rv_5s"), float(r["offset_s"]))
    r["fair_c"] = fv
    r["lag_c"] = (fv - mid) if fv is not None else None
    r["hour_bkt"] = hour_bucket(r["close_ts"])
    r["weekend"] = "wkend" if is_weekend(r["close_ts"]) else "wkday"
    r["band"] = price_band(mid)
    side_l, price_l, res_l = longshot_view(r["result"], mid)
    r["long_side"], r["long_price"], r["resid_long"] = side_l, price_l, res_l
    r["long_band"] = longshot_band(price_l)
    r["z_bkt"] = z_bucket(r["spot_z5"])
    r["spot_sign"] = sign_of(r["spot_z5"], 0.3)
    r["kmid_bkt"] = ("na" if r["kmid_move5m"] is None else
                     "k_down" if r["kmid_move5m"] <= -3 else
                     "k_up" if r["kmid_move5m"] >= 3 else "k_flat")
    r["kmid_sign"] = sign_of(r["kmid_move5m"], 3.0 - 1e-9)
    r["flow_bkt"] = imbalance_bucket(r["flow_imb60"], r["flow_n60"] or 0)
    r["flow_sign"] = sign_of(r["flow_imb60"], 0.1)
    r["size_bkt"] = size_bucket(r.get("flow_small_share60"))
    r["book_bkt"] = ("na" if r["book_imb5"] is None else
                     "bid_heavy" if r["book_imb5"] >= 0.3 else
                     "ask_heavy" if r["book_imb5"] <= -0.3 else "book_even")
    r["book_sign"] = sign_of(r["book_imb5"], 0.3 - 1e-9)
    return r


# Cell families: (name, key_fn, sign_key, value_key)
#   sign_key None  -> plain residual (settle − mid); flagged mean>0 => buy YES
#   sign_key 'x'   -> directional residual: residual * r[x] (rows with sign 0 dropped)
#   value_key 'resid_long' -> longshot residual (negative => longshot overpriced)
def cell_families() -> List[Tuple[str, callable, Optional[str], str]]:
    return [
        ("band",             lambda r: (r["band"],), None, "residual"),
        ("band_x_cls",       lambda r: (r["band"], r["cls"]), None, "residual"),
        ("hour",             lambda r: (r["hour_bkt"],), None, "residual"),
        ("hour_x_band",      lambda r: (r["hour_bkt"], r["band"]), None, "residual"),
        ("weekend_x_band",   lambda r: (r["weekend"], r["band"]), None, "residual"),
        # Q1: chase — momentum side after a 5-min spot move
        ("spot_move",        lambda r: (r["z_bkt"],), "spot_sign", "residual"),
        ("spot_move_x_cls",  lambda r: (r["z_bkt"], r["cls"]), "spot_sign", "residual"),
        ("spot_move_x_band", lambda r: (r["z_bkt"], r["band"]), "spot_sign", "residual"),
        ("kmid_move",        lambda r: (r["kmid_bkt"],), "kmid_sign", "residual"),
        ("kmid_move_x_cls",  lambda r: (r["kmid_bkt"], r["cls"]), "kmid_sign", "residual"),
        # flow: side takers were buying in the last 60s
        ("flow60",           lambda r: (r["flow_bkt"],), "flow_sign", "residual"),
        ("flow60_x_cls",     lambda r: (r["flow_bkt"], r["cls"]), "flow_sign", "residual"),
        ("flow60_x_band",    lambda r: (r["flow_bkt"], r["band"]), "flow_sign", "residual"),
        # added 2026-09-06 (pre-registered before the 13-day outcomes were read; kb/findings/track-c-retail-behavior-sep06.md §0.2)
        ("flow60_x_size",    lambda r: (r["flow_bkt"], r["size_bkt"]), "flow_sign", "residual"),
        ("flow60_x_asset",   lambda r: (r["flow_bkt"], r["asset"]), "flow_sign", "residual"),
        ("book_imb",         lambda r: (r["book_bkt"],), "book_sign", "residual"),
        ("book_imb_x_cls",   lambda r: (r["book_bkt"], r["cls"]), "book_sign", "residual"),
        # Q2: longshots — cheaper side by band x context
        ("long_band",            lambda r: (r["long_band"],), None, "resid_long"),
        ("long_band_x_hour",     lambda r: (r["long_band"], r["hour_bkt"]), None, "resid_long"),
        ("long_band_x_weekend",  lambda r: (r["long_band"], r["weekend"]), None, "resid_long"),
        ("long_band_x_cls",      lambda r: (r["long_band"], r["cls"]), None, "resid_long"),
        ("long_band_x_asset",    lambda r: (r["long_band"], r["asset"]), None, "resid_long"),
    ]


def build_cells(rows: List[dict], min_n: int, min_days: int, n_boot: int = 1000) -> List[dict]:
    """Every (family, key, offset) cell with n>=min_n and n_days>=min_days:
    mean, day-cluster CI, flag, split-half (odd/even day index) means + CIs."""
    all_days = sorted({r["day"] for r in rows})
    half = {d: (i % 2) for i, d in enumerate(all_days)}  # 0 = even-index half A, 1 = odd-index half B
    day_idx = {d: i for i, d in enumerate(all_days)}
    groups: Dict[tuple, List[tuple]] = defaultdict(list)
    for r in rows:
        for fam, keyf, sign_key, vkey in cell_families():
            v = r[vkey]
            if sign_key is not None:
                s = r[sign_key]
                if s == 0:
                    continue
                v = directional_residual(v, s)
            lag = r["lag_c"]
            if lag is not None and sign_key is not None:
                lag = directional_residual(lag, r[sign_key])
            elif lag is not None and vkey == "resid_long":
                lag = lag if r["long_side"] == "yes" else -lag
            groups[(fam, keyf(r), int(r["offset_s"]))].append((v, r["day"], half[r["day"]], lag))
    out = []
    for (fam, key, off), vals in groups.items():
        n = len(vals)
        days = {d for _, d, _, _ in vals}
        if n < min_n or len(days) < min_days:
            continue
        xs = [v for v, _, _, _ in vals]
        ds = [d for _, d, _, _ in vals]
        mean = sum(xs) / n
        lo, hi = day_bootstrap_ci(xs, ds, n_boot=n_boot)
        lags = [lg for _, _, _, lg in vals if lg is not None]
        by_day: Dict[str, List[float]] = defaultdict(list)
        for v, d, _, _ in vals:
            by_day[d].append(v)
        day_means = [sum(v) / len(v) for v in by_day.values()]
        same_sign_days = sum(1 for m in day_means if (m > 0) == (mean > 0) and m != 0)
        cell = {
            "family": fam, "key": "|".join(map(str, key)), "offset_s": off, "n": n,
            "n_days": len(days), "mean": mean, "ci_lo": lo, "ci_hi": hi,
            "sigma": statistics.pstdev(xs) if n > 1 else 0.0,
            "flag": flag_cell(mean, lo, hi),
            "lag_c": (sum(lags) / len(lags)) if lags else float("nan"),
            "lag_cov": len(lags) / n,
            "days_same_sign": same_sign_days,
            "_pairs": [(v, day_idx[d]) for v, d, _, _ in vals],   # consumed (and stripped) by add_holm
        }
        for h, tag in ((0, "A"), (1, "B")):
            sub = [(v, d) for v, d, hh, _ in vals if hh == h]
            if sub:
                sx = [v for v, _ in sub]
                sd = [d for _, d in sub]
                m = sum(sx) / len(sx)
                l2, h2 = day_bootstrap_ci(sx, sd, n_boot=max(200, n_boot // 2))
                cell[f"mean_{tag}"], cell[f"n_{tag}"], cell[f"lo_{tag}"], cell[f"hi_{tag}"] = m, len(sx), l2, h2
            else:
                cell[f"mean_{tag}"], cell[f"n_{tag}"], cell[f"lo_{tag}"], cell[f"hi_{tag}"] = float("nan"), 0, float("nan"), float("nan")
        cell["split_confirm"] = bool(
            cell["flag"] and not math.isnan(cell["mean_A"]) and not math.isnan(cell["mean_B"])
            and (cell["mean_A"] > 0) == (cell["mean_B"] > 0) == (mean > 0)
            and ((cell["lo_A"] > 0 and cell["lo_B"] > 0) or (cell["hi_A"] < 0 and cell["hi_B"] < 0))
        )
        out.append(cell)
    return out


def add_holm(cells: List[dict], alpha: float = 0.05, n_boot: int = pm.N_BOOT_DEFAULT) -> List[dict]:
    """Pre-registered multiplicity clause (2026-09-06, findings doc §0.4): within each
    offset, Holm step-down over ALL eligible cells pooled across families (the scan
    actually performed), on the two-sided day-cluster bootstrap p (numpy, seed 12345,
    floored at 1/n_boot; probe_multiplicity.cluster_bootstrap_p). Sets per cell:
    p_cluster, mc_se, m_family, holm_thr, holm_ok, borderline, chain_stopped,
    holm_fam_ok (secondary: Holm within family x offset) and
    final = flag AND split_confirm AND holm_ok. Strips the `_pairs` payload."""
    by_off: Dict[int, Dict[int, float]] = defaultdict(dict)
    by_fam: Dict[Tuple[str, int], Dict[int, float]] = defaultdict(dict)
    for i, c in enumerate(cells):
        # an exactly-zero pooled mean is untestable (every resample sits on the boundary):
        # cluster_bootstrap_p counts `<= 0` on one side only and would return its floor.
        p = 1.0 if c["mean"] == 0.0 else pm.cluster_bootstrap_p(c["_pairs"], n_boot=n_boot)
        c["p_cluster"] = p
        c["mc_se"] = pm.mc_se(p, n_boot)
        by_off[c["offset_s"]][i] = p
        by_fam[(c["family"], c["offset_s"])][i] = p
    for off, pv in by_off.items():
        res = pm.holm_reject_with_thresholds(pv, alpha)
        for i, (ok, thr) in res.items():
            c = cells[i]
            c["m_family"], c["holm_thr"], c["holm_ok"] = len(pv), thr, ok
            c["borderline"] = abs(c["p_cluster"] - thr) < 2.0 * c["mc_se"]
            c["chain_stopped"] = (not ok) and c["p_cluster"] <= thr
    for key, pv in by_fam.items():
        res = pm.holm_reject_with_thresholds(pv, alpha)
        for i, (ok, thr) in res.items():
            cells[i]["holm_fam_ok"], cells[i]["holm_fam_thr"], cells[i]["m_fam"] = ok, thr, len(pv)
    for c in cells:
        c["final"] = bool(c["flag"] and c["split_confirm"] and c["holm_ok"])
        del c["_pairs"]
    return cells


def holdout_stats(rows: List[dict], cell: dict, holdout_days: set, n_boot: int = 1000) -> dict:
    """Pre-registered sensitivity (a): the cell's statistic recomputed on `holdout_days`
    only (no eligibility threshold — every row of the cell on those days). Returns
    n, n_days, mean, lo, hi, excl0."""
    fam_map = {f[0]: f for f in cell_families()}
    _, keyf, sign_key, vkey = fam_map[cell["family"]]
    xs: List[float] = []
    ds: List[str] = []
    for r in rows:
        if r["day"] not in holdout_days or int(r["offset_s"]) != cell["offset_s"]:
            continue
        if "|".join(map(str, keyf(r))) != cell["key"]:
            continue
        v = r[vkey]
        if sign_key is not None:
            s = r[sign_key]
            if s == 0:
                continue
            v = directional_residual(v, s)
        xs.append(v)
        ds.append(r["day"])
    if not xs:
        return {"n": 0, "n_days": 0, "mean": float("nan"), "lo": float("nan"), "hi": float("nan"), "excl0": False}
    lo, hi = day_bootstrap_ci(xs, ds, n_boot=n_boot)
    return {"n": len(xs), "n_days": len(set(ds)), "mean": sum(xs) / len(xs), "lo": lo, "hi": hi,
            "excl0": bool(lo > 0 or hi < 0)}


def economic_test(rows: List[dict], cell: dict, n_boot: int = 1000) -> dict:
    """Honest economics for one flagged cell. Trade side per row: the side the
    cell says is UNDER-priced (plain: YES if mean>0 else NO; directional: the
    feature side if mean>0 else the opposite; longshot: the longshot side if
    mean>0 else the favourite). Taker: cross at the touch, fee 7P(1−P).
    Maker: post at the touch, fee-free; optimistic and queue-realistic fills."""
    fam_map = {f[0]: f for f in cell_families()}
    fam, keyf, sign_key, vkey = fam_map[cell["family"]]
    off = cell["offset_s"]
    key = cell["key"]
    want_pos = cell["mean"] > 0
    tk_pnl: List[float] = []
    tk_days: List[str] = []
    mk_opt: List[float] = []
    mk_q: List[float] = []
    mk_post_days: List[str] = []
    mk_q_ct: List[float] = []
    n_post = 0
    for r in rows:
        if int(r["offset_s"]) != off or "|".join(map(str, keyf(r))) != key:
            continue
        if sign_key is not None:
            s = r[sign_key]
            if s == 0:
                continue
            feat_side = "yes" if s > 0 else "no"
            side = feat_side if want_pos else ("no" if feat_side == "yes" else "yes")
        elif vkey == "resid_long":
            side = r["long_side"] if want_pos else ("no" if r["long_side"] == "yes" else "yes")
        else:
            side = "yes" if want_pos else "no"
        won = 1.0 if r["result"] == side else 0.0
        # taker
        entry = r["yes_ask"] if side == "yes" else 100.0 - r["yes_bid"]
        gross = (100.0 - entry) if won else -entry
        tk_pnl.append(gross - kalshi_fee_cents(entry))
        tk_days.append(r["day"])
        # maker at the touch on `side`
        n_post += 1
        mk_post_days.append(r["day"])
        price = r["yes_bid"] if side == "yes" else 100.0 - r["yes_ask"]
        pnl = (100.0 - price) if won else -price
        pre = "yes_" if side == "yes" else "no_"
        mk_opt.append(pnl if r[pre + "fill_opt_ts"] is not None else 0.0)
        filled_q = r[pre + "fill_q_ts"] is not None
        mk_q.append(pnl if filled_q else 0.0)
        mk_q_ct.append((r[pre + "q_fill_ct"] or 0.0) if filled_q else 0.0)
    n_days = len(set(tk_days)) or 1

    def summ(xs, ds):
        if not xs:
            return {"n": 0, "mean": float("nan"), "lo": float("nan"), "hi": float("nan")}
        lo, hi = day_bootstrap_ci(xs, ds, n_boot=n_boot)
        return {"n": len(xs), "mean": sum(xs) / len(xs), "lo": lo, "hi": hi}

    fills_opt = sum(1 for r_ in mk_opt if r_ != 0.0)
    fills_q = sum(1 for r_ in mk_q if r_ != 0.0)
    out = {
        "taker": summ(tk_pnl, tk_days),
        "maker_opt_per_post": summ(mk_opt, mk_post_days),
        "maker_q_per_post": summ(mk_q, mk_post_days),
        "maker_opt_fill_rate": fills_opt / n_post if n_post else float("nan"),
        "maker_q_fill_rate": fills_q / n_post if n_post else float("nan"),
        "maker_q_fills_per_day": fills_q / n_days,
        "maker_q_contracts_per_day": sum(mk_q_ct) / n_days,
        "taker_posts_per_day": len(tk_pnl) / n_days,
    }
    # per-FILL maker mean (conditional on fill) — the number that must beat 0
    q_fill_days = [d for p, d in zip(mk_q, mk_post_days) if p != 0.0]
    out["maker_q_per_fill"] = summ([p for p in mk_q if p != 0.0], q_fill_days)
    return out


def _fmtc(x) -> str:
    return "nan" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.2f}"


def analyze(rows_dir: str, min_n: int, min_days: int, n_boot: int, top: int, out_json: Optional[str],
            holm_n_boot: int = pm.N_BOOT_DEFAULT, alpha: float = 0.05,
            holdout_days: Optional[Sequence[str]] = None,
            max_quote_age_s: Optional[float] = None, exclude_fallback: bool = False) -> int:
    raw_rows = [r for r in load_rows(rows_dir) if r.get("mid") is not None]
    rows = [enrich(r) for r in valid_rows(raw_rows, max_quote_age_s, exclude_fallback)]
    if max_quote_age_s is not None or exclude_fallback:
        print(f"# VALIDITY FILTER: max_quote_age_s={max_quote_age_s} exclude_fallback={exclude_fallback}: "
              f"{len(raw_rows)} -> {len(rows)} rows ({len(raw_rows)-len(rows)} dropped)")
    if not rows:
        print("no sealed rows found", file=sys.stderr)
        return 2
    days = sorted({r["day"] for r in rows})
    n_win = len({(r["ticker"]) for r in rows})
    print(f"# Behavioral-premia probe  rows={len(rows)}  windows={n_win}  days={len(days)} "
          f"({days[0]}..{days[-1]})  min_n={min_n} min_days={min_days} n_boot={n_boot}")
    by_asset = defaultdict(int)
    for r in rows:
        by_asset[r["asset"]] += 1
    print("# rows/asset: " + ", ".join(f"{a}={n}" for a, n in sorted(by_asset.items())))
    cov = defaultdict(int)
    for r in rows:
        cov["spot_z5"] += r["spot_z5"] is not None
        cov["kmid_move5m"] += r["kmid_move5m"] is not None
        cov["flow_n60>0"] += (r["flow_n60"] or 0) > 0
        cov["book_imb5"] += r["book_imb5"] is not None
    print("# feature coverage: " + ", ".join(f"{k}={v/len(rows):.1%}" for k, v in cov.items()))

    cells = build_cells(rows, min_n, min_days, n_boot)
    add_holm(cells, alpha=alpha, n_boot=holm_n_boot)
    n_cells = len(cells)
    flagged = [c for c in cells if c["flag"]]
    confirmed = [c for c in flagged if c["split_confirm"]]
    final = [c for c in cells if c["final"]]
    per_off = {off: sum(1 for c in cells if c["offset_s"] == off) for off in sorted({c["offset_s"] for c in cells})}
    print(f"\n# cells tested={n_cells}  flagged(CI excl 0 & |mean|>3c)={len(flagged)}  "
          f"expected false flags under null (CI part only, alpha=.05)≈{0.05*n_cells:.1f}  "
          f"split-half confirmed={len(confirmed)}  "
          f"Holm(all cells within offset, alpha={alpha}, n_boot={holm_n_boot}) m per offset={per_off}  "
          f"FINAL(flag & split & Holm)={len(final)}")

    # Q1 / Q2 headline tables (always printed, flagged or not)
    def table(title, fam, sort_key=None):
        sub = [c for c in cells if c["family"] == fam]
        if not sub:
            print(f"\n## {title}: (no cell reached min_n/min_days)")
            return
        print(f"\n## {title}  [{fam}]  (directional: NEGATIVE = feature side OVER-priced; "
              f"lag = spot-normal fair − mid, same sign convention; d+/D = days with the cell's sign)")
        print(f"{'key':<24}{'off':>5}{'n':>7}{'days':>5}{'mean':>8}{'ci_lo':>8}{'ci_hi':>8}{'A':>8}{'B':>8}{'lag':>8}{'d+/D':>7} flag")
        for c in sorted(sub, key=sort_key or (lambda c: (c["key"], c["offset_s"]))):
            print(f"{c['key']:<24}{c['offset_s']:>5}{c['n']:>7}{c['n_days']:>5}{_fmtc(c['mean']):>8}"
                  f"{_fmtc(c['ci_lo']):>8}{_fmtc(c['ci_hi']):>8}{_fmtc(c['mean_A']):>8}{_fmtc(c['mean_B']):>8}"
                  f"{_fmtc(c['lag_c']):>8}{c['days_same_sign']:>4}/{c['n_days']:<2} "
                  f"{'FLAG' if c['flag'] else ''}{'+CONFIRM' if c['split_confirm'] else ''}")

    table("Q1 chase: 5-min SPOT move (Coinbase), momentum-side residual", "spot_move")
    table("Q1 chase by asset class", "spot_move_x_cls")
    table("Q1 chase: 5-min KALSHI-mid move (covers HYPE/BNB)", "kmid_move")
    table("Flow: taker-side imbalance last 60s, bought-side residual", "flow60")
    table("Book imbalance (5c band), heavy-side residual", "book_imb")
    table("Q2 longshot band (cheaper side), NEGATIVE = longshot over-priced", "long_band")
    table("Q2 longshot x hour", "long_band_x_hour")
    table("Q2 longshot x weekend", "long_band_x_weekend")
    table("Q2 longshot x asset class", "long_band_x_cls")
    table("Calibration by mid band (plain residual)", "band")

    print(f"\n## ALL FLAGGED CELLS ({len(flagged)}), sorted by |mean|  (p = day-cluster bootstrap p; thr = Holm threshold "
          f"over all m cells at that offset; fam = Holm within family x offset)")
    print(f"{'family':<22}{'key':<26}{'off':>5}{'n':>7}{'days':>5}{'mean':>8}{'ci_lo':>8}{'ci_hi':>8}{'A':>8}{'B':>8}{'lag':>8}{'d+/D':>7} "
          f"split {'p':>9}{'thr':>9} holm fam FINAL")
    for c in sorted(flagged, key=lambda c: -abs(c["mean"])):
        print(f"{c['family']:<22}{c['key']:<26}{c['offset_s']:>5}{c['n']:>7}{c['n_days']:>5}{_fmtc(c['mean']):>8}"
              f"{_fmtc(c['ci_lo']):>8}{_fmtc(c['ci_hi']):>8}{_fmtc(c['mean_A']):>8}{_fmtc(c['mean_B']):>8}"
              f"{_fmtc(c['lag_c']):>8}{c['days_same_sign']:>4}/{c['n_days']:<2} "
              f"{'YES' if c['split_confirm'] else 'no ':<5} {c['p_cluster']:>9.5f}{c['holm_thr']:>9.5f} "
              f"{'PASS' if c['holm_ok'] else ('STOP' if c['chain_stopped'] else 'fail'):<4} "
              f"{'ok' if c['holm_fam_ok'] else '--':<3} {'FINAL' if c['final'] else ''}"
              f"{' BORDERLINE' if c['borderline'] else ''}")

    hold = set(holdout_days or [])
    hold_with_rows = sorted(hold & set(days))
    print(f"\n## FINAL CELLS ({len(final)}) = flag & split-half & Holm(all cells within offset)"
          + (f"  + clean-holdout recompute on {sorted(hold)[0]}..{sorted(hold)[-1]} "
             f"({len(hold_with_rows)} days with rows of {len(hold)} requested)" if hold else ""))
    print(f"{'family':<22}{'key':<26}{'off':>5}{'n':>7}{'mean':>8}{'ci_lo':>8}{'ci_hi':>8}{'p':>9}{'lag':>8} | "
          f"{'hold_n':>7}{'h_days':>7}{'h_mean':>8}{'h_lo':>8}{'h_hi':>8} verdict")
    for c in sorted(final, key=lambda c: -abs(c["mean"])):
        h = holdout_stats(rows, c, hold, n_boot=n_boot) if hold else None
        c["holdout"] = h
        if h is None:
            verdict = "FINAL"
        else:
            verdict = "FINAL" if (h["excl0"] and (h["mean"] > 0) == (c["mean"] > 0)) else "SUGGESTIVE(holdout CI covers 0 or sign flips)"
        lag_note = ""
        if c["lag_c"] is not None and not math.isnan(c["lag_c"]) and c["family"] not in ("band", "band_x_cls", "hour", "hour_x_band", "weekend_x_band") \
                and abs(c["lag_c"]) >= 0.5 * abs(c["mean"]) and (c["lag_c"] > 0) == (c["mean"] > 0):
            lag_note = " spot-lag?"
        c["verdict"] = verdict + lag_note
        hs = (f"{h['n']:>7}{h['n_days']:>7}{_fmtc(h['mean']):>8}{_fmtc(h['lo']):>8}{_fmtc(h['hi']):>8}" if h else f"{'':>38}")
        print(f"{c['family']:<22}{c['key']:<26}{c['offset_s']:>5}{c['n']:>7}{_fmtc(c['mean']):>8}"
              f"{_fmtc(c['ci_lo']):>8}{_fmtc(c['ci_hi']):>8}{c['p_cluster']:>9.5f}{_fmtc(c['lag_c']):>8} | {hs} {c['verdict']}")

    # economic test on every FINAL cell (pre-registered: only FINAL cells get economics)
    econ_out = []
    print(f"\n## ECONOMIC TEST on FINAL cells (c/contract; fee=7P(1−P) taker; maker fee-free)")
    print(f"{'family':<22}{'key':<26}{'off':>4} | {'taker':>7}{'[lo,hi]':>17}{'/day':>6} | "
          f"{'mkr/fill(q)':>11}{'[lo,hi]':>17}{'fill%':>6}{'fills/d':>8}{'ct/d':>7} | {'mkr/post(q)':>11}")
    for c in sorted(final, key=lambda c: -abs(c["mean"]))[:top]:
        e = economic_test(rows, c, n_boot=n_boot)
        econ_out.append({"cell": c, "econ": e})
        t, mq, mp = e["taker"], e["maker_q_per_fill"], e["maker_q_per_post"]
        print(f"{c['family']:<22}{c['key']:<26}{c['offset_s']:>4} | {_fmtc(t['mean']):>7}"
              f"[{_fmtc(t['lo'])},{_fmtc(t['hi'])}]{e['taker_posts_per_day']:>6.1f} | "
              f"{_fmtc(mq['mean']):>11}[{_fmtc(mq['lo'])},{_fmtc(mq['hi'])}]"
              f"{e['maker_q_fill_rate']*100:>5.0f}%{e['maker_q_fills_per_day']:>8.1f}{e['maker_q_contracts_per_day']:>7.0f} | "
              f"{_fmtc(mp['mean']):>11}")
    surv_taker = [x for x in econ_out if x["econ"]["taker"]["lo"] > 0]
    surv_maker = [x for x in econ_out if x["econ"]["maker_q_per_fill"]["lo"] > 0 and x["econ"]["maker_q_per_post"]["lo"] > 0]
    print(f"\n# ECONOMIC SURVIVORS: taker CI lo>0: {len(surv_taker)}  |  maker(queue-realistic) per-fill AND per-post CI lo>0: {len(surv_maker)}")
    for x in surv_taker + surv_maker:
        c = x["cell"]
        print(f"   {c['family']} {c['key']} off={c['offset_s']} split_confirm={c['split_confirm']}")
    if out_json:
        with open(out_json, "w") as fh:
            json.dump({"days": days, "rows": len(rows), "max_quote_age_s": max_quote_age_s,
                       "exclude_fallback": exclude_fallback, "min_n": min_n, "min_days": min_days, "alpha": alpha,
                       "holm_n_boot": holm_n_boot, "holdout_days": sorted(hold), "cells": cells, "econ": econ_out},
                      fh, default=str)
        print(f"# wrote {out_json}")
    return 0


# ============================================================================
# CLI
# ============================================================================


def _split_chunks(days: List[str], workers: int) -> List[List[str]]:
    workers = max(1, min(workers, len(days)))
    k = math.ceil(len(days) / workers)
    return [days[i:i + k] for i in range(0, len(days), k)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("--corpus", default="~/kalshi-research-data/fairvalue")
    ex.add_argument("--out", default="~/kalshi-research-data/behavioral")
    ex.add_argument("--days", default=None, help="comma list; default = every sealed .done_<day> in corpus")
    ex.add_argument("--workers", type=int, default=1, help="split days into consecutive chunks, one subprocess each")
    ex.add_argument("--force", action="store_true")
    ex.add_argument("--determined-pkl", default=None,
                    help="seed settlement labels from a {ticker: {asset,result,det_ts,strike}} pickle "
                         "(e.g. the GENHUNT 2026-06-11 7,384-window corpus) instead of scanning lifecycle")
    ex.add_argument("--_chunk", default=None, help=argparse.SUPPRESS)
    ex.add_argument("--max-lines", type=int, default=0, help="smoke: stop each day file after N lines")
    an = sub.add_parser("analyze")
    an.add_argument("--rows-dir", default="~/kalshi-research-data/behavioral")
    an.add_argument("--min-n", type=int, default=50)
    an.add_argument("--min-days", type=int, default=5)
    an.add_argument("--n-boot", type=int, default=1000)
    an.add_argument("--top", type=int, default=40)
    an.add_argument("--out-json", default=None)
    an.add_argument("--holm-n-boot", type=int, default=pm.N_BOOT_DEFAULT)
    an.add_argument("--alpha", type=float, default=0.05)
    an.add_argument("--max-quote-age", type=float, default=None,
                    help="drop rows whose recorded book is older than this many seconds (validity filter)")
    an.add_argument("--exclude-fallback", action="store_true", help="drop fallback=1 rows")
    an.add_argument("--holdout-days", default=None,
                    help="comma list of days for the pre-registered clean-holdout recompute of FINAL cells")
    args = ap.parse_args(argv)

    if args.cmd == "analyze":
        return analyze(os.path.expanduser(args.rows_dir), args.min_n, args.min_days, args.n_boot, args.top,
                       os.path.expanduser(args.out_json) if args.out_json else None,
                       holm_n_boot=args.holm_n_boot, alpha=args.alpha,
                       holdout_days=[d.strip() for d in args.holdout_days.split(",")] if args.holdout_days else None,
                       max_quote_age_s=args.max_quote_age, exclude_fallback=args.exclude_fallback)

    corpus = os.path.expanduser(args.corpus)
    out = os.path.expanduser(args.out)
    if args.days:
        days = sorted(d.strip() for d in args.days.split(",") if d.strip())
    else:
        days = sorted(n[len(".done_"):] for n in os.listdir(corpus)
                      if n.startswith(".done_") and os.path.exists(os.path.join(corpus, "frames", f"day={n[6:]}.jsonl.zst")))
    if not args.force:
        days = [d for d in days if not os.path.exists(os.path.join(out, f".done_{d}"))]
    if not days:
        print("[bp] nothing to do (all days sealed in out dir)")
        return 0
    if args._chunk is None and args.workers > 1:
        os.makedirs(out, exist_ok=True)
        procs = []
        for ci, chunk in enumerate(_split_chunks(days, args.workers)):
            log = open(os.path.join(out, f"extract_chunk{ci}.log"), "a")
            cmd = [sys.executable, "-m", "scripts.research.behavioral_premia_probe", "extract",
                   "--corpus", corpus, "--out", out, "--days", ",".join(chunk), "--_chunk", str(ci)]
            if args.force:
                cmd.append("--force")
            if args.determined_pkl:
                cmd += ["--determined-pkl", args.determined_pkl]
            procs.append((subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT), chunk))
            print(f"[bp] chunk{ci} pid={procs[-1][0].pid} days={chunk[0]}..{chunk[-1]}")
        rc = 0
        for p, chunk in procs:
            rc |= p.wait()
        return rc
    t0 = time.time()
    print(f"[bp] load_determined({corpus}/lifecycle) ...", flush=True)
    os.makedirs(out, exist_ok=True)
    if args.determined_pkl:
        import pickle
        with open(os.path.expanduser(args.determined_pkl), "rb") as fh:
            determined = pickle.load(fh)
        determined = {tk: m for tk, m in determined.items() if asset_of(tk) in ASSETS}
        print(f"[bp] determined seeded from {args.determined_pkl}", flush=True)
    else:
        determined = load_determined(os.path.join(corpus, "lifecycle"), ASSETS,
                                     cache_path=os.path.join(out, "determined_cache.pkl"), days=days)
    print(f"[bp] determined windows={len(determined)}  ({time.time()-t0:.0f}s)", flush=True)
    funnel = extract_days(corpus, out, days, determined, log=lambda s: print(s, flush=True),
                          max_lines=args.max_lines)
    print(f"[bp] ALL DONE funnel={funnel} {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
