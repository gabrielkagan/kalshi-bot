"""venue_microprice_disagree_settlement — settlement-clock 3-venue consensus convergence.

Family: multi-venue consensus microstructure (settlement-window).

MECHANISM
---------
In the terminal window (final ~60s) before a Kalshi crypto-15M close, the
settlement print is forming. We build a 3-venue CONSENSUS spot microprice from
kraken + bitstamp + gemini reconstructed L2 books (top-of-book mid via `mid_at`
on each venue, then MEDIAN across the reliable venues), and compare its
DIRECTIONAL position vs the strike to the Kalshi reliable-NBBO mid.

Trade rule (per terminal window, decision cutoff = close - off):
  - require >= 2 of 3 venues reliable at the cutoff -> consensus microprice C.
  - require Kalshi reliable NBBO at the cutoff (else refuse — drifted book).
  - signal = the side the consensus points to RELATIVE TO STRIKE:
        C > strike * (1 + margin)  -> consensus says YES will settle (above)
        C < strike * (1 - margin)  -> consensus says NO  (below)
  - LAG gate: only fire when the Kalshi NBBO has NOT yet repriced to match
    consensus (consensus says YES but kalshi mid < LAG_HI; or consensus says NO
    but kalshi mid > LAG_LO). This is the convergence edge — front-run the
    Kalshi NBBO catching up to the near-certain settlement print.

LABEL: derived from the TERMINAL Kalshi reliable book (mid at close), NOT the DB
(in-window outcomes are sparse). yes settles if terminal mid > 50.

FILLS (HONEST, both reported):
  - TAKER (conservative headline): cross the Kalshi reliable book at the ask
    (YES) / no-ask (NO). Full taker fee. The survivable case.
  - MAKER-CROSS: post a resting bid; fill ONLY if a real trade print later
    crosses it (mm_markout_evaluator.first_{yes,no}_bid_fill_ts over parsed
    crypto-15M trade prints). Unfilled posts -> the unfilled split.

FEES: ceil(0.07 * C * P * (1-P)) cents/contract, P=price/100, taker worst-case.
Maker rebate assumed 0.

CI: block bootstrap CLUSTERED BY WINDOW (ticker) — the independent unit.
EDGE only if the TAKER variant CI lower bound > 0.

Local corpus only (~31h, 2026-05-30T10Z -> 05-31T17Z). Wide CIs, humility.
"""

from __future__ import annotations

import sys
sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import glob
import json
import math
import random
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone

import scripts.research.kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    close_epoch_from_ticker,
    load_frames_jsonl,
    load_outcomes_db,
)
from scripts.research.phase1b_retail_flow import parse_trade
from scripts.research.venue_book_reconstruct import (
    VENUE_SYMBOLS,
    BitstampBook,
    GeminiBook,
    KrakenBook,
    mid_at,
    parse_envelope as venue_parse_envelope,
)
from scripts.research.mm_markout_evaluator import (
    first_yes_bid_fill_ts,
    first_no_bid_fill_ts,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"
DB = "/tmp/edge_daily/state.db"
VENUE_ROOT = "/tmp/edge_daily/venue_pull"
SPOT = "/tmp/edge_daily/coinbase_spot.jsonl"

# Assets with venue L2 (>=2 of 3) AND coinbase spot. Focus the first pass on the
# two most liquid (most terminal windows, deepest books).
ASSETS = ["BTC", "ETH"]
SPOT_PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}

VENUE_BOOK = {"kraken": KrakenBook, "bitstamp": BitstampBook, "gemini": GeminiBook}
VENUE_SOURCE = {"kraken": "kraken_ws", "bitstamp": "bitstamp_ws", "gemini": "gemini_ws"}

DECISION_OFFSET_S = 60.0   # terminal window: 60s before close
STRIKE_MARGIN = 0.0005     # consensus must be past strike by 5 bps to fire
LAG_HI = 80.0              # consensus=yes fires only if kalshi mid still < 80c
LAG_LO = 20.0             # consensus=no  fires only if kalshi mid still > 20c

N_BOOT = 2000
RNG = random.Random(20260531)


def _epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fee(price_cents: float) -> float:
    """ceil(0.07 * C * P * (1-P)) cents/contract, C=1, P=price/100."""
    p = price_cents / 100.0
    return math.ceil(0.07 * p * (1.0 - p) * 100.0) / 100.0


# ---------------------------------------------------------------------------
# Coinbase spot anchor (pre-parsed: mid/bid/ask/ts per line).
# ---------------------------------------------------------------------------
def load_spot() -> dict:
    out = defaultdict(list)
    with open(SPOT) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            pid, mid, ts = d.get("product_id"), d.get("mid"), d.get("ts")
            if pid is None or mid is None or ts is None:
                continue
            out[pid].append((_epoch(ts), float(mid)))
    for pid in out:
        out[pid].sort(key=lambda x: x[0])
    return out


def spot_mid_at(spot_series, cutoff_epoch):
    val = None
    for ts, mid in spot_series:
        if ts > cutoff_epoch:
            break
        val = mid
    return val


# ---------------------------------------------------------------------------
# Kalshi trade prints -> {ticker: [(ts, yes_c, taker_side)]} sorted.
# ---------------------------------------------------------------------------
def load_trades_by_ticker(assets) -> dict:
    out = defaultdict(list)
    aset = set(assets)
    with open(TRADES) as fh:
        for line in fh:
            if not line.strip():
                continue
            t = parse_trade(line)
            if t is None or t["asset"] not in aset:
                continue
            out[t["ticker"]].append((t["ts"], t["yes_c"], t["taker_side"]))
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


# ---------------------------------------------------------------------------
# Venue L2 frames for one venue+asset across all local hour-chunks.
# ---------------------------------------------------------------------------
def load_venue_asset_frames(venue: str, asset: str) -> list:
    sym = VENUE_SYMBOLS.get(venue, {}).get(asset)
    if sym is None:
        return []
    src = VENUE_SOURCE[venue]
    pattern = f"{VENUE_ROOT}/{src}/**/*.jsonl.zst"
    out = []
    for f in sorted(glob.glob(pattern, recursive=True)):
        raw = subprocess.run(["zstd", "-dc", f], capture_output=True).stdout.decode(
            "utf-8", "replace"
        )
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                ts_iso, inner = venue_parse_envelope(line)
            except (ValueError, KeyError):
                continue
            if venue == "kraken":
                data = [e for e in inner.get("data", []) if e.get("symbol") == sym]
                if not data:
                    continue
                inner = {**inner, "data": data}
            elif venue == "bitstamp":
                if inner.get("channel") != f"order_book_{sym}":
                    continue
            elif venue == "gemini":
                if not (inner.get("type") == "l2_updates"
                        and inner.get("symbol") == sym):
                    continue
            out.append((_epoch(ts_iso), inner))
    out.sort(key=lambda x: x[0])
    return out


def venue_mid_at(venue, frames, cutoff_epoch):
    """Reliable venue mid at cutoff. kraken/gemini need a baseline snapshot at or
    before cutoff; bitstamp is self-complete. None if no clean mid."""
    sub = [(ts, inner) for ts, inner in frames if ts <= cutoff_epoch]
    if not sub:
        return None
    if venue == "kraken":
        if not any(i.get("type") == "snapshot" for _, i in sub):
            return None
    elif venue == "gemini":
        if not any(i.get("type") == "l2_updates" for _, i in sub):
            return None
    book = VENUE_BOOK[venue]()
    return mid_at(sub, cutoff_epoch, book)


def _is_crypto15m(tk, asset):
    return bool(re.match(rf"KX{asset}15M", tk))


def block_bootstrap_mean(per_window_vals, n_boot=N_BOOT):
    """Cluster bootstrap by window: resample windows with replacement; each is
    the independent unit (one fired-window value per cluster)."""
    if not per_window_vals:
        return (float("nan"), float("nan"), float("nan"))
    vals = list(per_window_vals)
    n = len(vals)
    point = sum(vals) / n
    boots = []
    for _ in range(n_boot):
        s = sum(vals[RNG.randrange(n)] for _ in range(n))
        boots.append(s / n)
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[int(0.975 * n_boot)]
    return point, lo, hi


def run():
    print("Loading Kalshi crypto-15M frames (4.4GB)...", flush=True)
    all_frames = load_frames_jsonl(FRAMES)
    print(f"  loaded {len(all_frames)} tickers", flush=True)

    print("Loading Kalshi trade prints...", flush=True)
    trades_by_tk = load_trades_by_ticker(ASSETS)
    print(f"  trade tickers: {len(trades_by_tk)}", flush=True)

    print("Loading coinbase spot anchor...", flush=True)
    spot = load_spot()
    print(f"  spot products: {list(spot)}", flush=True)

    venue_span_lo = _epoch("2026-05-30T00:00:00Z")
    venue_span_hi = _epoch("2026-05-31T17:59:59Z")
    spot_lo = _epoch("2026-05-30T21:00:00Z")  # spot starts here

    diag = {
        "n_windows_total": 0,
        "skip_no_strike": 0,
        "skip_no_kalshi_reliable": 0,
        "skip_no_terminal_label": 0,
        "skip_no_spot": 0,
        "skip_venue_lt2": 0,
        "no_signal": 0,
        "lag_gate_blocked": 0,
        "signal_fired": 0,
        "venue_reliable_2of3": 0,
        "venue_reliable_3of3": 0,
        "taker_fills": 0,
        "maker_filled": 0,
        "maker_unfilled": 0,
    }

    taker_net = []
    maker_net = []
    fired_meta = []

    for asset in ASSETS:
        prod = SPOT_PRODUCT[asset]
        spot_series = spot.get(prod, [])
        if not spot_series:
            print(f"[{asset}] NO coinbase spot -> skip", flush=True)
            continue

        print(f"[{asset}] loading venue L2...", flush=True)
        vframes = {}
        for venue in ("kraken", "bitstamp", "gemini"):
            vframes[venue] = load_venue_asset_frames(venue, asset)
            print(f"    {venue}: {len(vframes[venue])} frames", flush=True)
        venues_for_asset = [v for v in ("kraken", "bitstamp", "gemini")
                            if vframes[v]]

        tickers = []
        for tk in all_frames:
            if not _is_crypto15m(tk, asset):
                continue
            try:
                ce = close_epoch_from_ticker(tk)
            except Exception:
                continue
            if venue_span_lo <= ce <= venue_span_hi and ce >= spot_lo:
                tickers.append((tk, ce))
        tickers.sort(key=lambda x: x[1])
        print(f"[{asset}] candidate windows in span: {len(tickers)}", flush=True)

        outcomes = load_outcomes_db(DB, set(tk for tk, _ in tickers))

        asset_taker, asset_maker = [], []
        for tk, close_ep in tickers:
            diag["n_windows_total"] += 1
            fr = all_frames.get(tk)
            if not fr:
                continue
            cutoff = close_ep - DECISION_OFFSET_S

            strike = None
            if tk in outcomes and outcomes[tk].get("strike") is not None:
                strike = float(outcomes[tk]["strike"])
            if strike is None:
                diag["skip_no_strike"] += 1
                continue

            k_bid, k_ask = kbr.reliable_nbbo_at(fr, cutoff)
            if (k_bid is None or k_ask is None or not (0 < k_ask <= 100)
                    or k_bid > k_ask):
                diag["skip_no_kalshi_reliable"] += 1
                continue
            kalshi_mid = (k_bid + k_ask) / 2.0

            t_bid, t_ask = kbr.reliable_nbbo_at(fr, close_ep)
            if t_bid is None or t_ask is None:
                diag["skip_no_terminal_label"] += 1
                continue
            terminal_mid = (t_bid + t_ask) / 2.0
            won = terminal_mid > 50.0

            cb_mid = spot_mid_at(spot_series, cutoff)
            if cb_mid is None:
                diag["skip_no_spot"] += 1
                continue

            venue_mids = []
            for venue in venues_for_asset:
                m = venue_mid_at(venue, vframes[venue], cutoff)
                if m is not None and m > 0:
                    venue_mids.append(m)
            if len(venue_mids) < 2:
                diag["skip_venue_lt2"] += 1
                continue
            if len(venue_mids) == 2:
                diag["venue_reliable_2of3"] += 1
            else:
                diag["venue_reliable_3of3"] += 1
            venue_mids.sort()
            nv = len(venue_mids)
            consensus = (venue_mids[nv // 2] if nv % 2 == 1
                         else (venue_mids[nv // 2 - 1] + venue_mids[nv // 2]) / 2.0)

            if consensus > strike * (1.0 + STRIKE_MARGIN):
                sig = "yes"
            elif consensus < strike * (1.0 - STRIKE_MARGIN):
                sig = "no"
            else:
                diag["no_signal"] += 1
                continue

            if sig == "yes" and kalshi_mid >= LAG_HI:
                diag["lag_gate_blocked"] += 1
                continue
            if sig == "no" and kalshi_mid <= LAG_LO:
                diag["lag_gate_blocked"] += 1
                continue

            diag["signal_fired"] += 1
            fired_meta.append((asset, tk, sig, won))

            # ---- TAKER (conservative headline) ----
            if sig == "yes":
                entry = k_ask
                win_side = won
            else:
                entry = 100.0 - k_bid
                win_side = (not won)
            if not (0 < entry < 100):
                continue
            gross = (100.0 - entry) if win_side else (-entry)
            net = gross - _fee(entry)
            asset_taker.append(net)
            taker_net.append(net)
            diag["taker_fills"] += 1

            # ---- MAKER-CROSS (honest; fill only if a real trade crosses) ----
            trd = trades_by_tk.get(tk, [])
            if sig == "yes":
                post_px = k_bid
                fill_ts = first_yes_bid_fill_ts(trd, cutoff, post_px)
            else:
                post_px = 100.0 - k_ask
                fill_ts = first_no_bid_fill_ts(trd, cutoff, post_px)
            if fill_ts is None or not (0 < post_px < 100):
                diag["maker_unfilled"] += 1
            else:
                m_gross = (100.0 - post_px) if win_side else (-post_px)
                m_net = m_gross - _fee(post_px)
                asset_maker.append(m_net)
                maker_net.append(m_net)
                diag["maker_filled"] += 1

        if asset_taker:
            tp, tlo, thi = block_bootstrap_mean(asset_taker)
            print(f"[{asset}] TAKER n={len(asset_taker)} mean={tp:.3f}c "
                  f"CI[{tlo:.3f},{thi:.3f}]", flush=True)
        if asset_maker:
            mp, mlo, mhi = block_bootstrap_mean(asset_maker)
            print(f"[{asset}] MAKER-filled n={len(asset_maker)} mean={mp:.3f}c "
                  f"CI[{mlo:.3f},{mhi:.3f}]", flush=True)

    print("\n=== DIAG ===", flush=True)
    print(json.dumps(diag, indent=2), flush=True)

    out = {"taker_net": taker_net, "maker_net": maker_net, "diag": diag}
    if taker_net:
        tp, tlo, thi = block_bootstrap_mean(taker_net)
        out["taker_point"], out["taker_lo"], out["taker_hi"] = tp, tlo, thi
        print(f"POOLED TAKER n={len(taker_net)} mean={tp:.4f}c "
              f"CI[{tlo:.4f},{thi:.4f}]")
    if maker_net:
        mp, mlo, mhi = block_bootstrap_mean(maker_net)
        out["maker_point"], out["maker_lo"], out["maker_hi"] = mp, mlo, mhi
        print(f"POOLED MAKER n={len(maker_net)} mean={mp:.4f}c "
              f"CI[{mlo:.4f},{mhi:.4f}]")
    return out


if __name__ == "__main__":
    run()
