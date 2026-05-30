"""Phase 1b — real-price economics on ALL crypto 15M markets (selection-bias-free).

Answers the question the proxy could not (R1-C1/C2): at REAL, coherent, fillable
prices, is there an economic edge buying crypto 15M YES near close — across the
WHOLE crypto 15M universe (every settled window, traded or not), including the
contested 60-89¢ middle?

Sources (Kalshi bronze, Mac-local via `kalshi-restore:`):
  - `market_lifecycle_v2` `determined` events → unbiased universe + outcome
    (`result` yes/no) + close time (`determination_ts`, unix s). `metadata_updated`
    → `floor_strike`. Filtered hard to `KX<ASSET>15M-` (crypto only; esports/
    politics/etc. in the same files are ignored).
  - `orderbook_delta` → replayed to a coherent NEVER-CROSSED NBBO at T-15s
    (`kalshi_book_reconstruct`, validated 0.000% crossed on real bronze).

This driver reads LOCAL pre-pulled bronze dirs (the pull is done in shell to keep
bandwidth controlled). Usage:
  rclone copy "kalshi-restore:.../market_lifecycle_v2/year=2026/month=05/day=29/hour=05" /tmp/lc05
  rclone copy "kalshi-restore:.../orderbook_delta/year=2026/month=05/day=29/hour=05" /tmp/ob05
  python3 scripts/research/phase1b_real_price_economics.py --lifecycle-dir /tmp/lc05 --orderbook-dir /tmp/ob05

Parent plan: kb/decisions/settlement-lag-convergence-edge-spike-plan.md (Phase 1b)
Worklist: kb/decisions/settlement-convergence-worklist.md
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.settlement_convergence_p1a import (
    kalshi_fee_per_contract_cents,
    realized_pnl_cents,
)

# All 9 Kalshi 15M crypto series (vs the bot's traded 7). ADA + BCH are UNTRADED
# and thinner -> less MM/quant competition -> the edge-hunt targets. Coinbase has
# ADA-USD + BCH-USD spot, so the full reconstruction/harness stack handles all 9.
ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH")
DECISION_OFFSETS_S = (15, 30)  # T-Xs before close


def _is_crypto_15m(ticker: str) -> Optional[str]:
    for a in ASSETS:
        if ticker.startswith(f"KX{a}15M-"):
            return a
    return None


def _epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _zst_lines(path: str):
    raw = subprocess.run(["zstd", "-dc", path], capture_output=True).stdout.decode()
    for line in raw.splitlines():
        if line.strip():
            yield line


def load_determined(lifecycle_dir: str, assets) -> dict:
    """{ticker: {asset, result, det_ts, strike}} for crypto 15M `determined` events."""
    out: dict[str, dict] = {}
    strikes: dict[str, float] = {}
    for f in glob.glob(f"{lifecycle_dir}/**/*.zst", recursive=True):
        for line in _zst_lines(f):
            inner = json.loads(json.loads(line)["_raw"])
            msg = inner.get("msg", {})
            tk = msg.get("market_ticker", "")
            a = _is_crypto_15m(tk)
            if not a or a not in assets:
                continue
            et = msg.get("event_type")
            if et == "metadata_updated" and msg.get("floor_strike") is not None:
                strikes[tk] = float(msg["floor_strike"])
            elif et == "determined" and msg.get("result") in ("yes", "no"):
                out[tk] = {"asset": a, "result": msg["result"],
                           "det_ts": float(msg["determination_ts"])}
    for tk, d in out.items():
        d["strike"] = strikes.get(tk)
    return out


def load_orderbook_frames(orderbook_dir: str, tickers: set) -> dict:
    """{ticker: [(recv_epoch, inner)]} sorted, for the target tickers only.
    Also records whether the ticker ever received a snapshot (incomplete book
    without one)."""
    frames: dict[str, list] = defaultdict(list)
    for f in glob.glob(f"{orderbook_dir}/**/*.zst", recursive=True):
        for line in _zst_lines(f):
            env = json.loads(line)
            inner = json.loads(env["_raw"])
            tk = inner.get("msg", {}).get("market_ticker")
            if tk in tickers:
                frames[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


def load_crypto15m_frames(orderbook_dirs) -> dict:
    """One-pass: {ticker: [(recv_epoch, inner)]} for EVERY crypto-15M ticker
    present (so we can discover the covered universe from the bronze itself,
    independent of lifecycle)."""
    frames: dict[str, list] = defaultdict(list)
    for d in orderbook_dirs:
        for f in glob.glob(f"{d}/**/*.zst", recursive=True):
            for line in _zst_lines(f):
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                tk = inner.get("msg", {}).get("market_ticker", "")
                if _is_crypto_15m(tk):
                    frames[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


def load_frames_jsonl(path: str) -> dict:
    """Like load_crypto15m_frames but from a single plain-JSONL file of bronze
    envelopes (pre-filtered to crypto-15M by an upstream grep — far faster than
    re-parsing every non-crypto frame in the full-universe .zst)."""
    frames: dict[str, list] = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:  # skip rare malformed lines (file-boundary joins in concat grep)
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


try:  # DST-correct ET->UTC (R1-MAJOR-3: a hardcoded +4h breaks pre-DST data)
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = None


def close_epoch_from_ticker(ticker: str) -> float:
    """KX<ASSET>15M-<YYMMMDDHHMM>-<...> close time. The label is ET wall-clock;
    convert to UTC honoring DST (EDT in May). Verified against settled_at
    (label 0215 -> 06:15 UTC). Fallback to +4h (EDT) only if zoneinfo is absent."""
    mid = ticker.split("-")[1]
    naive = datetime.strptime(mid, "%y%b%d%H%M")
    if _ET is not None:
        return naive.replace(tzinfo=_ET).timestamp()
    return naive.replace(tzinfo=timezone.utc).timestamp() + 4 * 3600


def load_outcomes_db(db_path: str, tickers: set) -> dict:
    """{ticker: {result, strike}} from evaluated_opportunities (covers ~all
    evaluated crypto 15M windows; the covered orderbook subset is in here)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    out: dict[str, dict] = {}
    if not tickers:
        return out
    qmarks = ",".join("?" * len(tickers))
    sql = (f"SELECT DISTINCT ticker, market_result, threshold FROM "
           f"evaluated_opportunities WHERE ticker IN ({qmarks}) "
           f"AND market_result IN ('yes','no') AND threshold IS NOT NULL")
    for r in conn.execute(sql, tuple(tickers)):
        out[r["ticker"]] = {"result": r["market_result"], "strike": r["threshold"]}
    return out


def _band(ask: Optional[float]) -> str:
    if ask is None:
        return "none"
    if ask >= 90:
        return "90-99"
    if ask >= 60:
        return "60-89"
    if ask >= 40:
        return "40-59"
    if ask >= 1:
        return "1-39"
    return "0"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lifecycle-dir", default=None)
    ap.add_argument("--outcomes-db", default=None,
                    help="state.db for outcomes (alternative to --lifecycle-dir)")
    ap.add_argument("--orderbook-dir", default=None,
                    help="comma-separated orderbook dir(s) of .zst")
    ap.add_argument("--frames-file", default=None,
                    help="plain JSONL of crypto-15M-prefiltered bronze envelopes (fast path)")
    ap.add_argument("--assets", default=",".join(ASSETS))
    args = ap.parse_args(argv)
    assets = tuple(a.strip().upper() for a in args.assets.split(","))
    ob_dirs = [d.strip() for d in args.orderbook_dir.split(",")] if args.orderbook_dir else []

    if args.outcomes_db:
        frames = (load_frames_jsonl(args.frames_file) if args.frames_file
                  else load_crypto15m_frames(ob_dirs))
        outcomes = load_outcomes_db(args.outcomes_db, set(frames))
        windows = {tk: {"asset": _is_crypto_15m(tk), "result": o["result"],
                        "strike": o["strike"], "det_ts": close_epoch_from_ticker(tk)}
                   for tk, o in outcomes.items() if _is_crypto_15m(tk) in assets}
        print(f"covered crypto-15M windows (orderbook ∩ db-outcomes): {len(windows)} "
              f"of {len(frames)} crypto-15M tickers in bronze")
    else:
        windows = load_determined(args.lifecycle_dir, assets)
        print(f"determined crypto-15M windows in lifecycle dir: {len(windows)}")
        frames = load_orderbook_frames(ob_dirs[0], set(windows))

    # Per decision offset × band: count, win%, and REAL-price economics.
    # Three strategies on the SAME reconstructed coherent NBBO:
    #  YES-taker  = buy YES at the ask (the naive long; tested first).
    #  NO-taker   = buy NO at no_ask=100-yes_bid (fade the favorite).
    #  YES-maker  = buy YES at the BID (earn the spread; assumes fill — optimistic upper bound).
    def _mk():
        return defaultdict(lambda: {"n": 0, "win": 0, "net_sum": 0.0,
                                    "net1_sum": 0.0, "px_sum": 0.0})
    tally, tally_no, tally_maker = _mk(), _mk(), _mk()
    # Maker WITH adverse-selection fill model: n=posted, filled=got a position,
    # win/px/net are over FILLED only (the real maker EV).
    tally_mf = defaultdict(lambda: {"n": 0, "filled": 0, "win": 0,
                                    "px_sum": 0.0, "net_sum": 0.0, "net1_sum": 0.0})
    skipped_no_book = skipped_no_frames = 0
    import math
    per_asset = defaultdict(lambda: {"n": 0, "net_sum": 0.0})
    for tk, d in windows.items():
        fr = frames.get(tk)
        if not fr:
            skipped_no_frames += 1
            continue
        won = (d["result"] == "yes")
        for off in DECISION_OFFSETS_S:
            cutoff = d["det_ts"] - off
            # require a snapshot at/before cutoff (else book is incomplete)
            applied = [(ts, inner) for ts, inner in fr if ts <= cutoff]
            if not any(i.get("type") == "orderbook_snapshot" for _, i in applied):
                if off == 15:
                    skipped_no_book += 1
                continue
            bid, ask = kbr.nbbo_at(fr, cutoff)
            transactable = (ask is not None and bid is not None
                            and bid <= ask and 0 < ask < 100)
            if not transactable:
                continue
            gross = realized_pnl_cents(ask, won)
            fee = kalshi_fee_per_contract_cents(ask)
            key = (off, _band(ask))
            t = tally[key]
            t["n"] += 1
            t["win"] += int(won)
            t["px_sum"] += ask
            t["net_sum"] += gross - fee
            t["net1_sum"] += gross - math.ceil(fee)
            if off == 15:
                pa = per_asset[d["asset"]]
                pa["n"] += 1
                pa["net_sum"] += gross - fee

            def _acc(tl, price, win_side):
                if not (0 < price < 100):
                    return
                g = realized_pnl_cents(price, win_side)
                f = kalshi_fee_per_contract_cents(price)
                e = tl[(off, _band(price))]
                e["n"] += 1
                e["win"] += int(win_side)
                e["px_sum"] += price
                e["net_sum"] += g - f
                e["net1_sum"] += g - math.ceil(f)

            # NO-taker: pay no_ask = 100 - yes_bid, win if outcome == no.
            _acc(tally_no, 100 - bid, d["result"] == "no")
            # YES-maker (assume-fill, optimistic): buy at the bid, win if yes.
            _acc(tally_maker, bid, won)
            # YES-maker WITH adverse-selection fill model.
            mb, mfilled = kbr.simulate_maker_bid_fill(fr, cutoff)
            if mb is not None and 0 < mb < 100:
                e = tally_mf[(off, _band(mb))]
                e["n"] += 1
                if mfilled:
                    g = realized_pnl_cents(mb, won)
                    f = kalshi_fee_per_contract_cents(mb)
                    e["filled"] += 1
                    e["win"] += int(won)
                    e["px_sum"] += mb
                    e["net_sum"] += g - f
                    e["net1_sum"] += g - math.ceil(f)

    def _print(name, tl):
        print(f"\n=== {name} === (cents/contract, net of fee)")
        print(f"{'T-Xs':>5}{'band':>7}{'n':>6}{'win%':>7}{'avgPx':>7}{'netEV':>8}{'netEV1':>8}")
        for key in sorted(tl):
            off, band = key
            t = tl[key]
            n = t["n"] or 1
            print(f"{off:>5}{band:>7}{t['n']:>6}{100*t['win']/n:>7.1f}"
                  f"{t['px_sum']/n:>7.1f}{t['net_sum']/n:>+8.2f}{t['net1_sum']/n:>+8.2f}")

    print(f"reconstructed (skipped: {skipped_no_frames} no-frames, "
          f"{skipped_no_book} no-snapshot-before-T-15s)")
    _print("YES-taker (buy at ask, hold)", tally)
    _print("NO-taker (fade favorite: buy NO at 100-yes_bid)", tally_no)
    _print("YES-maker (buy at bid, earn spread; assumes fill = optimistic)", tally_maker)

    print(f"\n=== YES-maker WITH adverse-selection fill model (the REAL maker EV) ===")
    print(f"netEV here is over FILLED windows only; fill%=got a position.")
    print(f"{'T-Xs':>5}{'band':>7}{'posted':>7}{'fill%':>7}{'winF%':>7}{'avgPx':>7}{'netEV_F':>9}{'netEV1_F':>9}")
    for key in sorted(tally_mf):
        off, band = key
        t = tally_mf[key]
        post = t["n"] or 1
        fl = t["filled"] or 1
        print(f"{off:>5}{band:>7}{t['n']:>7}{100*t['filled']/post:>7.1f}"
              f"{100*t['win']/fl:>7.1f}{t['px_sum']/fl:>7.1f}"
              f"{t['net_sum']/fl:>+9.2f}{t['net1_sum']/fl:>+9.2f}")
    print(f"\nper-asset YES-taker (T-15s, all bands): {'asset':>5}{'n':>6}{'netEV':>8}")
    for a in sorted(per_asset):
        pa = per_asset[a]
        print(f"{'':>26}{a:>5}{pa['n']:>6}{pa['net_sum']/(pa['n'] or 1):>+8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
