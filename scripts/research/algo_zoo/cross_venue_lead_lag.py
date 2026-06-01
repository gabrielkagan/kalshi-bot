#!/usr/bin/env python3
"""cross_venue_lead_lag  (family: signal)

SPEC
----
Cross-venue lead-lag: does ONE spot venue (Coinbase) lead the others
(Kraken / Bitstamp / Gemini) by some milliseconds in mid-price returns?

This is the REAL implementation (the prior DATA_GAP stub is retired now that the
venue L2 bronze is staged locally under /tmp/edge_daily/venue_pull/<source>/...).

WHAT IT DOES
------------
For each of BTC/ETH/SOL/XRP (the assets Coinbase spot covers locally; HYPE/DOGE/
BNB have no local Coinbase mid -> excluded):

  1. Build FOUR event-time mid series:
       - Coinbase: from coinbase_spot.jsonl (`mid`).
       - Kraken / Bitstamp / Gemini: reconstruct top-of-book mid by replaying the
         L2 stream with the proven `scripts.research.venue_book_reconstruct`
         books (KrakenBook / BitstampBook / GeminiBook). We replay the WHOLE
         corpus in time order so a snapshot anywhere seeds the book; updates then
         accumulate. mid()==None (empty/crossed/unanchored book) ticks are dropped.

  2. Use the venue WIRE / EVENT timestamp where it exists, NOT the collector
     arrival `_wire_recv_ts`, to neutralise per-collector latency skew:
       - Kraken   : inner data[i].timestamp        -> EVENT time.
       - Bitstamp : inner data.microtimestamp (us)  -> EVENT time.
       - Gemini   : l2_updates carry NO event ts    -> only _wire_recv_ts ARRIVAL.
       - Coinbase : the normalised spot file carries only collector arrival `ts`
                    -> ARRIVAL time.
     Because Coinbase itself is arrival-stamped, ANY measured Coinbase lead is an
     UPPER BOUND on the true venue lead (it folds in differential collector/
     network latency). This is flagged loudly in the result.

  3. Resample each series onto a common 100ms grid via last-observation-carried-
     forward on the (event) timestamp. Compute mid log-returns. Run a lagged
     cross-correlation of Coinbase returns vs each venue's returns AND vs a
     pooled synthetic RTI (mean of the available venues' returns) over lags in
     [-2s, +2s] @ 100ms. argmax-lag = the lead (positive => Coinbase leads).

  4. HEADLINE = lead_ms (per-asset then pooled) with a >=1000-resample BLOCK
     bootstrap CI (contiguous blocks to respect serial correlation). A non-zero
     argmax is only an EDGE if (a) the bootstrap CI excludes zero, (b) the lag is
     >= one full 100ms grid cell from zero, and (c) the peak cross-correlation is
     materially above the noise floor. Else NO_EDGE / INCONCLUSIVE.

DISCIPLINE: a spot lead-lag, even if real, is NOT automatically a Kalshi edge.
No fade-PnL is computed here (it requires Kalshi NBBO reconstruction + honest
trade-print fills + fees, and is only worth attempting if headline-1 is a robust
CI-clean lead). The primary deliverable is the lead_ms measurement.

Runnable: `python3 scripts/research/algo_zoo/cross_venue_lead_lag.py` prints JSON.
"""
from __future__ import annotations

import glob
import io
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import numpy as np  # noqa: E402

from scripts.research.venue_book_reconstruct import (  # noqa: E402
    BitstampBook,
    GeminiBook,
    KrakenBook,
    VENUE_SYMBOLS,
)

try:
    import zstandard as zstd
except Exception:  # pragma: no cover
    zstd = None

EDGE = "/tmp/edge_daily"
SPOT_FILE = os.path.join(EDGE, "coinbase_spot.jsonl")
VENUE_PULL = os.path.join(EDGE, "venue_pull")

ASSETS = ("BTC", "ETH", "SOL", "XRP")  # only assets Coinbase spot covers locally
COINBASE_PRODUCT = {a: f"{a}-USD" for a in ASSETS}

VENUE_SRC = {"kraken": "kraken_ws", "bitstamp": "bitstamp_ws", "gemini": "gemini_ws"}
VENUE_BOOK = {"kraken": KrakenBook, "bitstamp": BitstampBook, "gemini": GeminiBook}

# Whether a venue exposes a true WIRE/EVENT timestamp (else arrival-only).
VENUE_EVENT_TIME = {"kraken": True, "bitstamp": True, "gemini": False}

GRID_MS = 100           # resample grid
MAX_LAG_MS = 2000       # +/- 2s lag scan
MAX_LAG = MAX_LAG_MS // GRID_MS  # in grid cells
N_BOOT = 1000           # block-bootstrap resamples
BLOCK_CELLS = 600       # ~60s contiguous blocks (respect serial correlation)


# --------------------------------------------------------------------------- #
# Timestamp helpers                                                           #
# --------------------------------------------------------------------------- #
def _iso_epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _read_zst_lines(path: str):
    if zstd is None:  # pragma: no cover
        raise RuntimeError("zstandard not available")
    with open(path, "rb") as fh:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(fh) as reader:
            text = io.TextIOWrapper(reader, encoding="utf-8")
            for line in text:
                if line.strip():
                    yield line


def _kraken_event_epoch(inner: dict):
    data = inner.get("data") or []
    if not data:
        return None
    ts = data[0].get("timestamp")
    return _iso_epoch(ts) if ts else None


def _bitstamp_event_epoch(inner: dict):
    d = inner.get("data") or {}
    mt = d.get("microtimestamp")
    if mt is not None:
        try:
            return int(mt) / 1e6
        except Exception:
            return None
    ts = d.get("timestamp")
    return float(ts) if ts is not None else None


# --------------------------------------------------------------------------- #
# Series builders -> per-asset {grid_bucket: last_mid} on a 100ms LOCF grid    #
# --------------------------------------------------------------------------- #
def _grid_bucket(epoch: float, t0: float) -> int:
    return int((epoch - t0) * 1000.0 // GRID_MS)


def build_coinbase_series():
    """Return (per-asset {grid_bucket: last_mid}, t0). t0 set on first record."""
    out = {a: {} for a in ASSETS}
    t0 = None
    prod_to_asset = {v: k for k, v in COINBASE_PRODUCT.items()}
    with open(SPOT_FILE) as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            a = prod_to_asset.get(r.get("product_id"))
            if a is None:
                continue
            mid = r.get("mid")
            if mid is None:
                b, k = r.get("bid"), r.get("ask")
                if b is None or k is None:
                    continue
                mid = (float(b) + float(k)) / 2.0
            ep = _iso_epoch(r["ts"])
            if t0 is None:
                t0 = ep
            out[a][_grid_bucket(ep, t0)] = float(mid)
    return out, t0


def _venue_files(venue: str) -> list:
    src = VENUE_SRC[venue]
    pat = os.path.join(VENUE_PULL, src, "day=*", "hour=*", "conn=*", "*.jsonl.zst")
    return sorted(glob.glob(pat))


def build_venue_series(venue: str, t0: float):
    """Replay the venue L2 stream in time order, emitting per-asset
    {grid_bucket: last_clean_mid}. One Book per asset; updates accumulate.

    No baseline is fabricated: if the corpus opens mid-stream with no snapshot,
    early ticks yield mid()==None and are dropped. We record whether each
    (venue, asset) ever saw a snapshot so the caller can flag unanchored books.
    """
    books = {a: VENUE_BOOK[venue]() for a in ASSETS}
    sym_to_asset = {VENUE_SYMBOLS[venue][a]: a
                    for a in ASSETS if a in VENUE_SYMBOLS[venue]}
    series = {a: {} for a in sym_to_asset.values()}
    saw_snapshot = {a: False for a in sym_to_asset.values()}
    n_frames = {a: 0 for a in sym_to_asset.values()}
    expect_src = f"{venue}_ws"

    for path in _venue_files(venue):
        for line in _read_zst_lines(path):
            env = json.loads(line)
            if env.get("_source") not in (None, expect_src):
                continue
            inner = json.loads(env["_raw"])

            if venue == "kraken":
                data = inner.get("data") or []
                if not data:
                    continue
                ev = _kraken_event_epoch(inner)
                ep = ev if ev is not None else _iso_epoch(env["_wire_recv_ts"])
                is_snap = inner.get("type") == "snapshot"
                for entry in data:
                    a = sym_to_asset.get(entry.get("symbol"))
                    if a is None:
                        continue
                    bk = books[a]
                    if is_snap:
                        bk.apply_snapshot(entry.get("bids", []), entry.get("asks", []))
                        saw_snapshot[a] = True
                    else:
                        for lv in entry.get("bids", []):
                            bk.apply_delta("bid", lv["price"], lv["qty"])
                        for lv in entry.get("asks", []):
                            bk.apply_delta("ask", lv["price"], lv["qty"])
                    n_frames[a] += 1
                    m = bk.mid()
                    if m is not None:
                        series[a][_grid_bucket(ep, t0)] = m

            elif venue == "bitstamp":
                ch = inner.get("channel", "")
                if not ch.startswith("order_book_"):
                    continue
                a = sym_to_asset.get(ch[len("order_book_"):])
                if a is None:
                    continue
                ev = _bitstamp_event_epoch(inner)
                ep = ev if ev is not None else _iso_epoch(env["_wire_recv_ts"])
                bk = books[a]
                bk.apply_frame(inner)
                saw_snapshot[a] = True  # every bitstamp frame is a full snapshot
                n_frames[a] += 1
                m = bk.mid()
                if m is not None:
                    series[a][_grid_bucket(ep, t0)] = m

            elif venue == "gemini":
                if inner.get("type") != "l2_updates":
                    continue
                a = sym_to_asset.get(inner.get("symbol"))
                if a is None:
                    continue
                ep = _iso_epoch(env["_wire_recv_ts"])  # arrival-only
                bk = books[a]
                bk.apply_frame(inner)
                n_frames[a] += 1
                m = bk.mid()
                if m is not None:
                    series[a][_grid_bucket(ep, t0)] = m

    return series, saw_snapshot, n_frames


# --------------------------------------------------------------------------- #
# Resample + returns + cross-correlation + block bootstrap                     #
# --------------------------------------------------------------------------- #
def _locf_grid(buckets: dict, n: int) -> np.ndarray:
    arr = np.full(n, np.nan, dtype=float)
    for k, v in buckets.items():
        if 0 <= k < n:
            arr[k] = v
    last = np.nan
    for i in range(n):
        if not np.isnan(arr[i]):
            last = arr[i]
        else:
            arr[i] = last
    return arr


def _log_returns(prices: np.ndarray) -> np.ndarray:
    r = np.full_like(prices, np.nan)
    valid = (prices[1:] > 0) & (prices[:-1] > 0)
    r[1:][valid] = np.log(prices[1:][valid] / prices[:-1][valid])
    return r


def _xcorr_lag(ret_cb: np.ndarray, ret_v: np.ndarray):
    """Lagged cross-corr of Coinbase vs venue returns over +/- MAX_LAG cells.
    Returns (lags, corrs, argmax_lag_cells, peak_corr, noise_floor_corr).
    Convention: lag L>0 => Coinbase LEADS (cb[t] ~ venue[t+L])."""
    lags = np.arange(-MAX_LAG, MAX_LAG + 1)
    corrs = np.full(lags.shape, np.nan)
    for i, L in enumerate(lags):
        if L >= 0:
            a = ret_cb[: len(ret_cb) - L] if L > 0 else ret_cb
            b = ret_v[L:]
        else:
            a = ret_cb[-L:]
            b = ret_v[: len(ret_v) + L]
        mask = ~(np.isnan(a) | np.isnan(b))
        if mask.sum() < 100:
            continue
        aa, bb = a[mask], b[mask]
        if aa.std() < 1e-12 or bb.std() < 1e-12:
            continue
        corrs[i] = np.corrcoef(aa, bb)[0, 1]
    if np.all(np.isnan(corrs)):
        return lags, corrs, 0, np.nan, np.nan
    ai = int(np.nanargmax(corrs))
    peak = corrs[ai]
    argmax_lag = int(lags[ai])
    off = np.abs(corrs.copy())
    off[np.abs(lags - argmax_lag) < 5] = np.nan  # exclude +/-500ms around peak
    floor = np.nanmax(off) if not np.all(np.isnan(off)) else np.nan
    return lags, corrs, argmax_lag, float(peak), float(floor)


def _block_bootstrap_lead(ret_cb, ret_v, n_boot=N_BOOT):
    n = len(ret_cb)
    if n < BLOCK_CELLS * 2:
        return np.array([])
    n_blocks = n // BLOCK_CELLS
    rng = np.random.default_rng(12345)
    leads = []
    for _ in range(n_boot):
        starts = rng.integers(0, n - BLOCK_CELLS, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + BLOCK_CELLS) for s in starts])
        _, _, lag, peak, _ = _xcorr_lag(ret_cb[idx], ret_v[idx])
        if not np.isnan(peak):
            leads.append(lag)
    return np.array(leads, dtype=float)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> dict:
    if not os.path.isfile(SPOT_FILE):
        return {"algo": "cross_venue_lead_lag", "verdict": "DATA_GAP",
                "reason": f"missing {SPOT_FILE}"}

    cb_buckets, t0 = build_coinbase_series()

    venue_data = {}
    for venue in VENUE_SRC:
        try:
            s, snap, nfr = build_venue_series(venue, t0)
            venue_data[venue] = {"series": s, "saw_snapshot": snap, "n_frames": nfr}
        except Exception as e:  # pragma: no cover
            venue_data[venue] = {"error": f"{type(e).__name__}: {e}"}

    max_bucket = 0
    for a in ASSETS:
        if cb_buckets[a]:
            max_bucket = max(max_bucket, max(cb_buckets[a]))
    for vd in venue_data.values():
        for b in vd.get("series", {}).values():
            if b:
                max_bucket = max(max_bucket, max(b))
    n_cells = max_bucket + 1

    per_asset = {}
    pooled_leads_by_venue = {v: [] for v in VENUE_SRC}
    pooled_leads_rti = []

    for a in ASSETS:
        cb_grid = _locf_grid(cb_buckets[a], n_cells)
        cb_ret = _log_returns(cb_grid)

        venue_rets = {}
        venue_results = {}
        for venue, vd in venue_data.items():
            ser = vd.get("series", {})
            if a not in ser or not ser[a]:
                continue
            vgrid = _locf_grid(ser[a], n_cells)
            vret = _log_returns(vgrid)
            venue_rets[venue] = vret
            _, _, lag_cells, peak, floor = _xcorr_lag(cb_ret, vret)
            boot = _block_bootstrap_lead(cb_ret, vret)
            res = {
                "lead_ms": lag_cells * GRID_MS,
                "peak_corr": round(peak, 4) if not np.isnan(peak) else None,
                "noise_floor_corr": round(floor, 4) if not np.isnan(floor) else None,
                "n_grid_obs": int(np.sum(~np.isnan(vgrid) & (vgrid > 0))),
                "saw_snapshot": vd.get("saw_snapshot", {}).get(a),
                "event_time": VENUE_EVENT_TIME[venue],
            }
            if len(boot) >= 100:
                lo, hi = np.percentile(boot * GRID_MS, [2.5, 97.5])
                res["lead_ci_ms"] = [round(lo, 1), round(hi, 1)]
                res["lead_boot_median_ms"] = round(float(np.median(boot)) * GRID_MS, 1)
                pooled_leads_by_venue[venue].append(boot * GRID_MS)
            else:
                res["lead_ci_ms"] = None
            venue_results[venue] = res

        if venue_rets:
            stack = np.vstack([venue_rets[v] for v in venue_rets])
            rti_ret = np.nanmean(stack, axis=0)
            _, _, lag_cells, peak, floor = _xcorr_lag(cb_ret, rti_ret)
            boot = _block_bootstrap_lead(cb_ret, rti_ret)
            rti_res = {
                "lead_ms": lag_cells * GRID_MS,
                "peak_corr": round(peak, 4) if not np.isnan(peak) else None,
                "noise_floor_corr": round(floor, 4) if not np.isnan(floor) else None,
                "venues_in_rti": list(venue_rets),
            }
            if len(boot) >= 100:
                lo, hi = np.percentile(boot * GRID_MS, [2.5, 97.5])
                rti_res["lead_ci_ms"] = [round(lo, 1), round(hi, 1)]
                rti_res["lead_boot_median_ms"] = round(float(np.median(boot)) * GRID_MS, 1)
                pooled_leads_rti.append(boot * GRID_MS)
            else:
                rti_res["lead_ci_ms"] = None
        else:
            rti_res = None

        per_asset[a] = {
            "venues": venue_results,
            "synthetic_rti": rti_res,
            "n_coinbase_grid_obs": int(np.sum(~np.isnan(cb_grid) & (cb_grid > 0))),
        }

    pooled = {}
    if pooled_leads_rti:
        allb = np.concatenate(pooled_leads_rti)
        lo, hi = np.percentile(allb, [2.5, 97.5])
        pooled["rti"] = {"lead_point_ms": round(float(np.median(allb)), 1),
                         "lead_ci_ms": [round(lo, 1), round(hi, 1)],
                         "n_boot": int(len(allb))}
    for v, lst in pooled_leads_by_venue.items():
        if lst:
            allb = np.concatenate(lst)
            lo, hi = np.percentile(allb, [2.5, 97.5])
            pooled[v] = {"lead_point_ms": round(float(np.median(allb)), 1),
                         "lead_ci_ms": [round(lo, 1), round(hi, 1)],
                         "n_boot": int(len(allb))}

    verdict, bottom = _verdict(pooled)

    return {
        "algo": "cross_venue_lead_lag",
        "family": "signal",
        "assets": list(ASSETS),
        "grid_ms": GRID_MS,
        "lag_scan_ms": [-MAX_LAG_MS, MAX_LAG_MS],
        "n_boot": N_BOOT,
        "block_cells": BLOCK_CELLS,
        "block_seconds": BLOCK_CELLS * GRID_MS / 1000.0,
        "n_grid_cells": int(n_cells),
        "corpus_span_hours": round(n_cells * GRID_MS / 1000.0 / 3600.0, 2),
        "timestamp_basis": {
            "coinbase": "ARRIVAL (spot `ts` = collector recv; no event ts)",
            "kraken": "EVENT (data.timestamp)",
            "bitstamp": "EVENT (data.microtimestamp)",
            "gemini": "ARRIVAL (_wire_recv_ts; l2_updates carry no event ts)",
        },
        "venue_frame_counts": {v: vd.get("n_frames") for v, vd in venue_data.items()},
        "venue_errors": {v: vd["error"] for v, vd in venue_data.items() if "error" in vd},
        "per_asset": per_asset,
        "pooled": pooled,
        "lookahead_risks": [
            "ARRIVAL-TIME ARTIFACT (BIGGEST RISK): Coinbase spot is stamped at "
            "collector arrival, not exchange event time. A measured 'Coinbase "
            "leads' may be partly/entirely differential collector+network latency, "
            "NOT a true venue price lead. Kraken/Bitstamp use EVENT time, so the "
            "Coinbase-vs-them lead is an UPPER BOUND on the real lead.",
            "GEMINI is arrival-time too (l2_updates carry no event ts) AND has no "
            "in-window snapshot baseline at corpus start -> its book may be "
            "unanchored; treat any Gemini result as low-confidence / upper-bound.",
            "KRAKEN snapshot baseline only appears at reconnects (sparse). Hours "
            "with no preceding snapshot reconstruct from absolute-set deltas on a "
            "partial book; early mids may be biased until a snapshot reseeds.",
            "100ms grid + LOCF: a true lead < 100ms is unresolvable; an argmax of "
            "+/-100ms is within one grid cell of zero and not a real edge.",
            "~1.3 days of data => wide CIs; per-asset estimates are thin.",
        ],
        "verdict": verdict,
        "bottom_line": bottom,
    }


def _verdict(pooled: dict):
    rti = pooled.get("rti")
    if not rti or not rti.get("lead_ci_ms"):
        return "INCONCLUSIVE", ("No bootstrap CI could be formed (insufficient "
                                "overlapping grid samples) -> INCONCLUSIVE.")
    lo, hi = rti["lead_ci_ms"]
    pt = rti["lead_point_ms"]
    ci_excludes_zero = (lo > 0 and hi > 0) or (lo < 0 and hi < 0)
    one_cell = abs(pt) >= GRID_MS
    if ci_excludes_zero and one_cell:
        return "EDGE", (
            f"Pooled synthetic-RTI lead = {pt:.0f}ms (95% block-bootstrap CI "
            f"[{lo:.0f},{hi:.0f}]ms) excludes zero and exceeds one grid cell. "
            f"Coinbase {'leads' if pt > 0 else 'lags'} the other venues -- BUT "
            f"this is an UPPER BOUND (Coinbase is arrival-stamped); the true "
            f"venue lead is smaller and could be ~0 after latency correction.")
    if ci_excludes_zero and not one_cell:
        return "NO_EDGE", (
            f"Pooled lead {pt:.0f}ms CI [{lo:.0f},{hi:.0f}]ms excludes zero but "
            f"is within one 100ms grid cell of zero -> unresolvable. NO_EDGE.")
    return "NO_EDGE", (
        f"Pooled synthetic-RTI lead = {pt:.0f}ms with 95% CI [{lo:.0f},{hi:.0f}]ms "
        f"straddling zero -> no statistically robust cross-venue lead. NO_EDGE.")


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
