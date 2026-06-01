"""spot_barrier_touch_rail_reversion_maker — fade the rail after a FRESH spot
BARRIER TOUCH with time left.

Family: rail (1c/99c) economics + barrier / first-passage microstructure.

THESIS
------
A Kalshi 15M above/below is a barrier option. A spot TOUCH of the strike with
time remaining is a classic over-reaction point: naive flow treats "we touched
the strike" as "it's decided" and bids the now-favored side to the rail
(>=96c). But the risk-neutral price of a touched-but-not-expired barrier is
strictly INSIDE the rail — there is real probability of crossing back before
close. A patient maker resting one tick inside the rail on the touched-favored
side captures the retest decay. The TOUCH-vs-NOTOUCH control isolates the
barrier-conditioning variable from generic rail-queue dynamics.

SIGNAL (no look-ahead)
----------------------
Per ticker:
  - strike = evaluated_opportunities.threshold (the ticker SUFFIX is a window
    index, NOT the strike — verified on real DB: KXBTC15M-...-00 had thr
    73409.39, -15 had 73563.99).
  - spot path: coinbase mid for BTC/ETH/SOL/XRP (from 21Z), kraken venue mid for
    ALL assets incl. the thin HYPE/DOGE/BNB rail-prone set.
  - t_touch = first epoch in [open, close) where the spot path crosses the strike
    (sign of (spot - strike) flips vs the first observed in-window sign),
    requiring TIME_LEFT = close - t_touch >= MIN_TIME_LEFT_S (a touch is not a
    settle).
  - the touch FAVORS the side the spot ended up on at t_touch:
      spot > strike  -> YES favored (above)
      spot < strike  -> NO favored (below)
  - at t_touch, read the reliable Kalshi book. If the favored side's reliable
    mid is at a RAIL (>= RAIL_CENTS on the favored side), POST a resting maker
    bid one tick INSIDE the rail on the favored side (cheaper than the rail).

FILL (honest)
-------------
A resting maker bid fills ONLY when a real trade print crosses it (mm_markout
primitives first_yes_bid_fill_ts / first_no_bid_fill_ts). A fill you got is
usually a fill you regret (adverse selection) — we model it.

LABEL
-----
settlement markout, fee-inclusive: settlement_markout_cents(fill, side, result)
- fee. result from DB market_result, else terminal-book mid at close.
Fee = ceil(0.07 * P * (1-P) * 100) cents/contract (per-contract ceil; maker
rebate = 0 by default). We report the per-fill fee distribution to verify
ceil() near P~1 doesn't eat the edge.

CONTROL ARM
-----------
Identical posts on rails reached WITHOUT a fresh touch (the favored-side mid is
at a rail at a fixed decision offset but the spot path never crossed the strike
in the window — pure drift). Headline gate: TOUCH-arm settlement markout net of
fee, block bootstrap clustered by ticker, CI-lower > 0 AND TOUCH-arm > NOTOUCH
control. YES-rail and NO-rail reported separately.

USAGE
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/spot_barrier_touch_rail_reversion_maker.py
"""
from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr  # noqa: E402
from scripts.research.mm_markout_evaluator import (  # noqa: E402
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
    settlement_markout_cents,
)
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
)
from scripts.research.venue_book_reconstruct import (  # noqa: E402
    VENUE_SYMBOLS,
    KrakenBook,
    _extract_frame,
    parse_envelope,
)

# ----- config --------------------------------------------------------------
FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"
SPOT = "/tmp/edge_daily/coinbase_spot.jsonl"
VENUE_ROOT = "/tmp/edge_daily/venue_pull"
DB = "/tmp/edge_daily/state.db"

MIN_TIME_LEFT_S = 120.0      # a touch is not a settle: need >=120s to retest
RAIL_CENTS = 96.0            # favored-side reliable mid must be >= this (a rail)
TICK_INSIDE = 1.0            # post one tick (1c) inside the rail
WINDOW_LEN_S = 15 * 60       # 15M window
N_BOOT = 2000
SEED = 12345
FEE_FRACTION = 0.07          # Kalshi large-order fee coefficient
MAKER_REBATE_CENTS = 0.0     # default 0 — stated assumption

ASSETS_COINBASE = {"BTC", "ETH", "SOL", "XRP"}  # coinbase spot products
PROD = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}


def fee_per_contract_cents(price_cents: float) -> float:
    """ceil(0.07 * P * (1-P) * 100) cents/contract, per-contract ceil rounding,
    minus maker rebate (>=0 floored)."""
    p = max(0.0, min(1.0, price_cents / 100.0))
    raw = FEE_FRACTION * p * (1.0 - p) * 100.0
    fee = math.ceil(raw)  # per-contract ceil
    return max(0.0, fee - MAKER_REBATE_CENTS)


def _epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def asset_of(ticker: str) -> str:
    return _is_crypto_15m(ticker) or ""


# ----- loaders -------------------------------------------------------------
def load_strikes_and_results(tickers: set) -> dict:
    """{ticker: {strike, result(optional)}} from evaluated_opportunities."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    out: dict[str, dict] = {}
    if not tickers:
        return out
    tlist = list(tickers)
    for i in range(0, len(tlist), 800):
        chunk = tlist[i : i + 800]
        q = ",".join("?" * len(chunk))
        sql = (
            f"SELECT DISTINCT ticker, threshold, market_result FROM "
            f"evaluated_opportunities WHERE ticker IN ({q}) "
            f"AND threshold IS NOT NULL"
        )
        for r in conn.execute(sql, tuple(chunk)):
            res = r["market_result"] if r["market_result"] in ("yes", "no") else None
            prev = out.get(r["ticker"])
            if prev is None:
                out[r["ticker"]] = {"strike": float(r["threshold"]), "result": res}
            elif res is not None and prev["result"] is None:
                prev["result"] = res
    conn.close()
    return out


def load_frames() -> dict:
    """{ticker: [(recv_epoch, inner)]} sorted ascending."""
    frames: dict[str, list] = defaultdict(list)
    with open(FRAMES) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk = inner.get("msg", {}).get("market_ticker", "")
            if _is_crypto_15m(tk):
                frames[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


def load_trades() -> dict:
    """{ticker: [(ts, yes_price_cents, taker_side)]} sorted ascending."""
    out: dict[str, list] = defaultdict(list)
    with open(TRADES) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            if not _is_crypto_15m(tk):
                continue
            try:
                yp = float(msg["yes_price_dollars"]) * 100.0
                side = msg["taker_side"]
                ts = float(msg["ts"])
            except (KeyError, ValueError):
                continue
            out[tk].append((ts, yp, side))
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


def load_coinbase_spot() -> dict:
    """{product_id: [(epoch, mid)]} sorted ascending."""
    out: dict[str, list] = defaultdict(list)
    with open(SPOT) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                mid = e.get("mid")
                if mid is None:
                    continue
                out[e["product_id"]].append((_epoch(e["ts"]), float(mid)))
            except (ValueError, KeyError):
                continue
    for p in out:
        out[p].sort(key=lambda x: x[0])
    return out


def _venue_files(venue: str) -> list:
    root = os.path.join(VENUE_ROOT, venue)
    files = []
    if not os.path.isdir(root):
        return files
    for day in sorted(os.listdir(root)):
        dpath = os.path.join(root, day)
        if not os.path.isdir(dpath):
            continue
        for hr in sorted(os.listdir(dpath)):
            hpath = os.path.join(dpath, hr, "conn=A")
            if not os.path.isdir(hpath):
                continue
            for fn in sorted(os.listdir(hpath)):
                if fn.endswith(".jsonl.zst"):
                    files.append(os.path.join(hpath, fn))
    return files


def load_kraken_spot(assets: set, tmin: float, tmax: float) -> dict:
    """{asset: [(epoch, mid)]} kraken venue mid, decompressed. Only assets
    kraken constitutes. Builds the book incrementally and emits a mid per tick.
    Requires a snapshot before trusting the book (anchored)."""
    try:
        import zstandard as zstd
    except ImportError:
        print("  [warn] zstandard not installed; no kraken spot", flush=True)
        return {}
    out: dict[str, list] = {}
    files = _venue_files("kraken_ws")
    if not files:
        print("  [warn] no kraken files found", flush=True)
        return out

    # Read all files ONCE, dispatch per requested symbol simultaneously.
    want = {a: VENUE_SYMBOLS["kraken"][a] for a in assets
            if a in VENUE_SYMBOLS.get("kraken", {})}
    sym2asset = {v: k for k, v in want.items()}
    books = {a: KrakenBook() for a in want}
    anchored = {a: False for a in want}
    series: dict[str, list] = {a: [] for a in want}
    dctx = zstd.ZstdDecompressor()
    for fp in files:
        with open(fp, "rb") as fh:
            with dctx.stream_reader(fh) as reader:
                data = reader.read()
        for line in data.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                env = json.loads(line)
            except ValueError:
                continue
            if env.get("_source") not in (None, "kraken_ws"):
                continue
            try:
                ts, inner = parse_envelope(env)
            except (ValueError, KeyError):
                continue
            ep = _epoch(ts)
            if not (tmin - 60 <= ep <= tmax + 60):
                continue
            for sym, asset in sym2asset.items():
                frame = _extract_frame("kraken", inner, sym)
                if frame is None:
                    continue
                bk = books[asset]
                if frame.get("type") == "snapshot":
                    bk = KrakenBook()
                    bk.apply_frame(frame)
                    books[asset] = bk
                    anchored[asset] = True
                else:
                    bk.apply_frame(frame)
                if not anchored[asset]:
                    continue
                mid = bk.mid()
                if mid is not None:
                    series[asset].append((ep, mid))
    for a in series:
        series[a].sort(key=lambda x: x[0])
        out[a] = series[a]
    return out


# ----- barrier-touch detection ---------------------------------------------
def detect_touch(spot_series: list, strike: float, open_ep: float,
                 close_ep: float):
    """Return (t_touch, favored_side, crossed_ever) for the FIRST strike
    crossing in [open_ep, close_ep) with TIME_LEFT >= MIN_TIME_LEFT_S, else
    (None, None, crossed_ever). crossing = sign of (spot - strike) flips vs the
    first observed in-window sign. favored = side the spot is on AT the touch.
    NO look-ahead: only spot points <= the touch decide it."""
    pts = [(ep, m) for ep, m in spot_series if open_ep <= ep < close_ep]
    if len(pts) < 2:
        return None, None, False
    first_sign = 1 if pts[0][1] >= strike else -1
    crossed_ever = False
    for ep, m in pts[1:]:
        sign = 1 if m >= strike else -1
        if sign != first_sign:
            crossed_ever = True
            time_left = close_ep - ep
            if time_left >= MIN_TIME_LEFT_S:
                favored = "yes" if sign > 0 else "no"
                return ep, favored, True
    return None, None, crossed_ever


# ----- post / fill ---------------------------------------------------------
def reliable_favored_bid(frames: list, at_ep: float, side: str):
    """Return (favored_mid, favored_best_bid_cents) from reliable nbbo, or
    (None, None) if the book refuses."""
    yb, ya = kbr.reliable_nbbo_at(frames, at_ep)
    if yb is None or ya is None:
        return None, None
    yes_mid = (yb + ya) / 2.0
    if side == "yes":
        return yes_mid, yb
    return 100.0 - yes_mid, 100.0 - ya  # NO mid, NO best bid (=100-yes_ask)


def terminal_result(frames: list, close_ep: float):
    yb, ya = kbr.reliable_nbbo_at(frames, close_ep)
    if yb is None or ya is None:
        yb, ya = kbr.nbbo_at(frames, close_ep)
        if yb is None or ya is None:
            return None
    return "yes" if (yb + ya) / 2.0 >= 50.0 else "no"


def evaluate_post(frames, trades, side, post_ep, close_ep, result, fav_best_bid):
    """Post one-tick-inside-rail maker bid on `side` at post_ep. Honest fill via
    trade-cross before close. Returns dict or None."""
    our_bid = min(RAIL_CENTS - TICK_INSIDE, fav_best_bid)
    if our_bid < 1.0 or our_bid > 98.0:
        return None
    if side == "yes":
        fill_ts = first_yes_bid_fill_ts(trades, post_ep, our_bid)
    else:
        fill_ts = first_no_bid_fill_ts(trades, post_ep, our_bid)
    if fill_ts is None or fill_ts > close_ep:
        return None
    if result is None:
        return None
    fee = fee_per_contract_cents(our_bid)
    settle_mk = settlement_markout_cents(our_bid, side, result)
    return {
        "fill_price": our_bid,
        "fee": fee,
        "settle_markout": settle_mk,
        "net": settle_mk - fee,
        "result": result,
        "side": side,
    }


# ----- cluster bootstrap (by ticker) ---------------------------------------
def block_bootstrap_ci(per_ticker: dict, n_boot=N_BOOT, alpha=0.05, seed=SEED):
    keys = list(per_ticker.keys())
    all_vals = [v for vs in per_ticker.values() for v in vs]
    n_fills = len(all_vals)
    if n_fills == 0 or not keys:
        return float("nan"), float("nan"), float("nan"), 0, 0
    point = sum(all_vals) / n_fills
    rng = random.Random(seed)
    nk = len(keys)
    means = []
    for _ in range(n_boot):
        pool = []
        for _ in range(nk):
            pool.extend(per_ticker[keys[rng.randrange(nk)]])
        if pool:
            means.append(sum(pool) / len(pool))
    if not means:
        return point, float("nan"), float("nan"), n_fills, nk
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return point, lo, hi, n_fills, nk


# ----- main ----------------------------------------------------------------
def main():
    print("[load] frames...", flush=True)
    frames = load_frames()
    print(f"  {len(frames)} tickers", flush=True)
    print("[load] trades...", flush=True)
    trades = load_trades()
    print(f"  {len(trades)} tickers with trades", flush=True)

    tickers = set(frames)
    print("[load] strikes + results from DB...", flush=True)
    meta = load_strikes_and_results(tickers)
    print(f"  {len(meta)} tickers with strike", flush=True)

    print("[load] coinbase spot...", flush=True)
    cb = load_coinbase_spot()
    print(f"  products: {sorted(cb)}", flush=True)

    closes = {tk: close_epoch_from_ticker(tk) for tk in meta}
    if closes:
        tmin = min(closes.values()) - WINDOW_LEN_S
        tmax = max(closes.values())
    else:
        tmin, tmax = 0, 0

    print("[load] kraken venue mid (all assets)...", flush=True)
    kraken_assets = {asset_of(tk) for tk in meta if asset_of(tk)}
    kraken = load_kraken_spot(kraken_assets, tmin, tmax)
    for a in sorted(kraken):
        print(f"  kraken {a}: {len(kraken[a])} mids", flush=True)

    touch = {"yes": defaultdict(list), "no": defaultdict(list)}
    control = {"yes": defaultdict(list), "no": defaultdict(list)}
    touch_fee = {"yes": [], "no": []}
    diag = defaultdict(int)
    posts = {"touch": 0, "control": 0}
    fills = {"touch": 0, "control": 0}

    for tk in sorted(meta):
        asset = asset_of(tk)
        if not asset:
            continue
        strike = meta[tk]["strike"]
        result = meta[tk]["result"]
        close_ep = closes[tk]
        open_ep = close_ep - WINDOW_LEN_S
        fr = frames.get(tk, [])
        tr = trades.get(tk, [])
        if not fr:
            diag["no_frames"] += 1
            continue
        if result is None:
            result = terminal_result(fr, close_ep)
        if result is None:
            diag["no_result"] += 1
            continue

        # spot series: coinbase for majors if in-window-covered, else kraken.
        spot_series = []
        src = None
        if asset in ASSETS_COINBASE and cb.get(PROD[asset]):
            cand = [1 for ep, _ in cb[PROD[asset]] if open_ep <= ep < close_ep]
            if len(cand) >= 2:
                spot_series = cb[PROD[asset]]
                src = "coinbase"
        if not spot_series and kraken.get(asset):
            cand = [1 for ep, _ in kraken[asset] if open_ep <= ep < close_ep]
            if len(cand) >= 2:
                spot_series = kraken[asset]
                src = "kraken"
        if not spot_series:
            diag["no_spot"] += 1
            continue
        diag[f"spot_{src}"] += 1

        t_touch, favored, crossed = detect_touch(spot_series, strike, open_ep, close_ep)

        if t_touch is not None:
            fav_mid, fav_bid = reliable_favored_bid(fr, t_touch, favored)
            if fav_mid is None:
                diag["touch_book_refused"] += 1
            elif fav_mid < RAIL_CENTS:
                diag["touch_not_at_rail"] += 1
            elif fav_bid is None:
                diag["touch_no_bid"] += 1
            else:
                posts["touch"] += 1
                rec = evaluate_post(fr, tr, favored, t_touch, close_ep, result, fav_bid)
                if rec is not None:
                    fills["touch"] += 1
                    touch[favored][tk].append(rec["net"])
                    touch_fee[favored].append(rec["fee"])
                else:
                    diag["touch_no_fill"] += 1
        else:
            # CONTROL: rail reached without ANY crossing (pure drift). Exclude
            # late-touch windows so the control is clean "rail without touch".
            if crossed:
                diag["control_excluded_late_touch"] += 1
                continue
            dec_ep = close_ep - MIN_TIME_LEFT_S
            if dec_ep <= open_ep:
                continue
            yb, ya = kbr.reliable_nbbo_at(fr, dec_ep)
            if yb is None or ya is None:
                diag["control_book_refused"] += 1
                continue
            yes_mid = (yb + ya) / 2.0
            for side in ("yes", "no"):
                fav_mid = yes_mid if side == "yes" else 100.0 - yes_mid
                fav_bid = yb if side == "yes" else 100.0 - ya
                if fav_mid >= RAIL_CENTS and fav_bid is not None:
                    posts["control"] += 1
                    rec = evaluate_post(fr, tr, side, dec_ep, close_ep, result, fav_bid)
                    if rec is not None:
                        fills["control"] += 1
                        control[side][tk].append(rec["net"])
                    else:
                        diag["control_no_fill"] += 1

    # ----- report ----------------------------------------------------------
    print("\n=== DIAGNOSTICS ===")
    for k in sorted(diag):
        print(f"  {k}: {diag[k]}")
    print(f"  posts: {posts}")
    print(f"  fills: {fills}")
    for arm, p in posts.items():
        fr_rate = (fills[arm] / p) if p else float("nan")
        print(f"  fill_rate[{arm}] = {fills[arm]}/{p} = {fr_rate:.3f}")

    results = {}
    for side in ("yes", "no"):
        print(f"\n=== {side.upper()}-RAIL ===")
        tpt, tlo, thi, tnf, tnt = block_bootstrap_ci(touch[side])
        print(f"  TOUCH   n_fills={tnf} n_tickers={tnt} "
              f"mean_net={tpt:.3f}c CI=[{tlo:.3f},{thi:.3f}]")
        if touch_fee[side]:
            ff = sorted(touch_fee[side])
            print(f"    fee dist: min={ff[0]:.0f} med={ff[len(ff)//2]:.0f} "
                  f"max={ff[-1]:.0f} mean={sum(ff)/len(ff):.2f}c")
        cpt, clo, chi, cnf, cnt = block_bootstrap_ci(control[side])
        print(f"  CONTROL n_fills={cnf} n_tickers={cnt} "
              f"mean_net={cpt:.3f}c CI=[{clo:.3f},{chi:.3f}]")
        results[side] = {
            "touch": (tpt, tlo, thi, tnf, tnt),
            "control": (cpt, clo, chi, cnf, cnt),
        }

    # ----- verdict ---------------------------------------------------------
    print("\n=== VERDICT ===")
    best = None
    for side in ("yes", "no"):
        tpt, tlo, thi, tnf, tnt = results[side]["touch"]
        cpt = results[side]["control"][0]
        edge = (
            tnf >= 30
            and not math.isnan(tlo)
            and tlo > 0
            and (math.isnan(cpt) or tpt > cpt)
        )
        print(f"  {side}: TOUCH net={tpt:.3f} CI_lo={tlo:.3f} CI_hi={thi:.3f} "
              f"CONTROL={cpt:.3f} n={tnf} -> {'EDGE' if edge else 'no'}")
        cand = (side, tpt, tlo, thi, tnf, cpt, edge)
        if best is None:
            best = cand
        elif not math.isnan(tpt) and (math.isnan(best[1]) or tpt > best[1]):
            best = cand
    print(f"\n  BEST side: {best}")
    return results, best, posts, fills


if __name__ == "__main__":
    main()
