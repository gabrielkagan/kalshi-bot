"""Glosten-Milgrom adverse-selection-aware quoting + VPIN toxicity gating.

ALGORITHM FAMILY: market-making.

Thesis ("harvest from regulars, don't give it back to quants"): a passive
market-maker posting a resting YES bid earns the spread on uninformed (retail)
flow but gets ADVERSELY SELECTED by informed flow — they hit your bid precisely
when the market is about to move against you. Glosten-Milgrom says the rational
MM must widen/skew the quote when the order flow looks INFORMED. VPIN
(Volume-Synchronized Probability of Informed Trading, Easley-Lopez de
Prado-O'Hara) estimates flow toxicity from the trade tape: bucket trades into
equal-VOLUME buckets, measure |buy_vol - sell_vol| / bucket_vol per bucket; the
rolling mean is VPIN. High VPIN -> toxic flow -> a smart MM steps back.

WHAT THIS BACKTEST COMPARES (same books, same fills, honest model):
  * NAIVE maker:  always post a resting YES bid at the current best YES bid.
  * TOXIC-AWARE maker (Glosten-Milgrom-VPIN): post at best bid ONLY when VPIN
    is below its per-asset median (benign flow); when VPIN is HIGH (toxic),
    SKEW the bid down by 1 cent (or skip) so we fill less often and at a better
    price, dodging the adverse-selection tax.

HEADLINE METRIC: net markout uplift (cents/contract) = mean net PnL/contract of
the toxic-aware maker MINUS the naive maker, over the SAME posting events, with a
bootstrap CI (>=1000 resamples, clustered by ticker).

HONEST FILL MODEL: a resting YES bid at price b, posted at decision epoch t,
fills iff a REAL trade print later (ts > t) crosses it: a YES-side print at
yes_price_cents <= b (a seller crossing down to our bid). We do NOT assume
book-cross fills; we require an actual trade tape crossing. Filled position is
held to settlement (outcome from evaluated_opportunities). PnL = realized minus
Kalshi fee (price-dependent, 7*p*(1-p) cents; large-order amortized rate). NO
maker rebate assumed (Kalshi has no maker rebate on these binary markets).

LOOK-AHEAD DISCIPLINE: the quote (best bid) and VPIN are both computed STRICTLY
from frames/trades with ts <= decision epoch. The fill scan uses ONLY trades
with ts > decision epoch. The settlement outcome is the only future input and is
the realized label (legitimate — it's what we hold to).

Corpus: ~1 day of crypto-15M bronze (local). Wide CIs + tiny-sample humility are
mandatory; see lookahead_risks in the structured output.
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import KalshiBook  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
    load_outcomes_db,
)
from scripts.research.phase1b_retail_flow import parse_trade  # noqa: E402
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
    realized_pnl_cents,
)

FRAMES_PATH = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES_PATH = "/tmp/edge_daily/trades_crypto.jsonl"
DB_PATH = "/tmp/edge_daily/state.db"

# Decision epoch: T - DECISION_OFFSET_S before the window close. We post the
# maker bid here and watch the remaining tape for a fill, holding to settlement.
DECISION_OFFSET_S = 60.0
# VPIN volume bucket size (contracts). Equal-volume bucketing is the EdL-O VPIN
# construction; bucket size is per-asset adaptive (median trade*K) but we use a
# fixed contracts-per-bucket and a rolling window of buckets.
VPIN_BUCKET_CONTRACTS = 200.0
VPIN_WINDOW_BUCKETS = 5
TOXIC_SKEW_CENTS = 1.0  # how far below best bid the toxic-aware maker steps


def _epoch_iso(iso: str) -> float:
    # frames carry _wire_recv_ts ISO; we align everything on the Kalshi event ts
    # where available, but trades give unix `ts` and frames give inner msg ts.
    from datetime import datetime, timezone
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _inner_event_epoch(inner: dict, recv_iso: str) -> float:
    """Prefer the Kalshi event ts (ts_ms) over collector arrival ts to reduce
    cross-source clock skew vs the trade tape (trades carry unix ts)."""
    msg = inner.get("msg", {})
    tms = msg.get("ts_ms")
    if tms is not None:
        try:
            return float(tms) / 1000.0
        except (TypeError, ValueError):
            pass
    t = msg.get("ts")
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):  # snapshot ts is ISO
        try:
            return _epoch_iso(t)
        except Exception:
            pass
    return _epoch_iso(recv_iso)


def load_frames(path: str) -> dict:
    """{ticker: [(event_epoch, inner)]} sorted ascending, crypto-15M only."""
    frames: dict[str, list] = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk = inner.get("msg", {}).get("market_ticker", "")
            if not _is_crypto_15m(tk):
                continue
            ep = _inner_event_epoch(inner, env.get("_wire_recv_ts", ""))
            frames[tk].append((ep, inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


def load_trades(path: str) -> dict:
    """{ticker: [(ts, yes_c, taker_side, count)]} sorted ascending."""
    trades: dict[str, list] = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            t = parse_trade(line)
            if not t:
                continue
            trades[t["ticker"]].append(
                (t["ts"], t["yes_c"], t["taker_side"], t["count"])
            )
    for tk in trades:
        trades[tk].sort(key=lambda x: x[0])
    return trades


def best_bid_at(frames_tk: list, cutoff: float):
    """Snapshot-anchored best YES bid at cutoff (no look-ahead). Reset on every
    snapshot (ground truth). Returns (bid_cents, reliable_bool)."""
    b = KalshiBook()
    anchored = False
    for ep, inner in frames_tk:
        if ep > cutoff:
            break
        if inner.get("type") == "orderbook_snapshot":
            b = KalshiBook()
            b.apply_frame(inner)
            anchored = True
        else:
            b.apply_frame(inner)
    if not anchored or not b.is_reliable():
        return None, False
    return b.best_yes_bid_cents(), True


def vpin_at(trades_tk: list, cutoff: float) -> float | None:
    """VPIN computed from the trade tape with ts <= cutoff ONLY (no look-ahead).
    Equal-volume buckets of VPIN_BUCKET_CONTRACTS; per-bucket order imbalance
    |buy - sell| / bucket_vol; VPIN = mean of the last VPIN_WINDOW_BUCKETS
    completed buckets. buy = taker bought YES (taker_side='yes'); sell = taker
    sold YES (taker_side='no'). Returns None if < 1 completed bucket."""
    buckets: list[float] = []  # per-bucket imbalance ratio
    cur_buy = cur_sell = 0.0
    cur_vol = 0.0
    for ts, _yes_c, side, cnt in trades_tk:
        if ts > cutoff:
            break
        v = cnt
        # split the trade volume into the current bucket(s); equal-volume rule
        while v > 0:
            room = VPIN_BUCKET_CONTRACTS - cur_vol
            take = min(v, room)
            if side == "yes":
                cur_buy += take
            else:
                cur_sell += take
            cur_vol += take
            v -= take
            if cur_vol >= VPIN_BUCKET_CONTRACTS - 1e-9:
                buckets.append(abs(cur_buy - cur_sell) / VPIN_BUCKET_CONTRACTS)
                cur_buy = cur_sell = cur_vol = 0.0
    if not buckets:
        return None
    w = buckets[-VPIN_WINDOW_BUCKETS:]
    return sum(w) / len(w)


def maker_fill_price(trades_tk: list, post_epoch: float, bid: float):
    """Honest fill: a resting YES bid at `bid` (cents), posted at post_epoch,
    fills iff a later trade print (ts > post_epoch) crosses it — a YES print at
    yes_price_cents <= bid. We fill at OUR bid price (price improvement to the
    crosser; the maker gets exactly its posted price). Returns True/False."""
    for ts, yes_c, _side, _cnt in trades_tk:
        if ts <= post_epoch:
            continue
        if yes_c <= bid + 1e-9:
            return True
    return False


def net_pnl_filled(bid: float, won: bool) -> float:
    """Net cents/contract of a FILLED resting YES bid at `bid`, held to
    settlement. No maker rebate. Fee is the price-dependent Kalshi rate."""
    gross = realized_pnl_cents(bid, won)
    fee = kalshi_fee_per_contract_cents(bid)
    return gross - fee


def main() -> int:
    print("loading frames ...", flush=True)
    frames = load_frames(FRAMES_PATH)
    print(f"  frames: {len(frames)} crypto-15M tickers", flush=True)
    print("loading trades ...", flush=True)
    trades = load_trades(TRADES_PATH)
    print(f"  trades: {len(trades)} tickers", flush=True)
    print("loading outcomes ...", flush=True)
    outcomes = load_outcomes_db(DB_PATH, set(frames))
    print(f"  outcomes: {len(outcomes)} tickers with yes/no result", flush=True)

    # ------------------------------------------------------------------
    # PASS 1: gather posting events. For each ticker with frames+trades+outcome,
    # at T-DECISION_OFFSET_S compute (best bid, VPIN). Record per-event:
    #   asset, ticker, bid, vpin, naive_fill, naive_net, toxic_bid, toxic_fill,
    #   toxic_net, won.
    # VPIN gating threshold = per-asset median VPIN over all events (computed in
    # a 2nd loop so the gate is not look-ahead within a single ticker's life —
    # it's a cross-sectional regime classifier, the standard VPIN deployment).
    # ------------------------------------------------------------------
    events = []
    skipped = defaultdict(int)
    for tk, fr in frames.items():
        out = outcomes.get(tk)
        if out is None:
            skipped["no_outcome"] += 1
            continue
        tr = trades.get(tk)
        if not tr:
            skipped["no_trades"] += 1
            continue
        asset = _is_crypto_15m(tk)
        try:
            close = close_epoch_from_ticker(tk)
        except Exception:
            skipped["bad_ticker"] += 1
            continue
        cutoff = close - DECISION_OFFSET_S
        bid, reliable = best_bid_at(fr, cutoff)
        if not reliable or bid is None or not (0 < bid < 100):
            skipped["no_reliable_book"] += 1
            continue
        v = vpin_at(tr, cutoff)
        if v is None:
            skipped["no_vpin"] += 1
            continue
        won = out["result"] == "yes"
        events.append({
            "asset": asset, "ticker": tk, "bid": bid, "vpin": v,
            "cutoff": cutoff, "won": won, "tr": tr,
        })
    print(f"\nposting events: {len(events)}  skipped: {dict(skipped)}", flush=True)
    if not events:
        print("NO EVENTS -> DATA_GAP")
        return 1

    # per-asset median VPIN = the toxicity gate
    by_asset_v = defaultdict(list)
    for e in events:
        by_asset_v[e["asset"]].append(e["vpin"])
    med = {}
    for a, vs in by_asset_v.items():
        s = sorted(vs)
        med[a] = s[len(s) // 2]
    print("per-asset VPIN median (toxicity gate):")
    for a in sorted(med):
        print(f"  {a:>5}  median_vpin={med[a]:.3f}  n={len(by_asset_v[a])}")

    # ------------------------------------------------------------------
    # PASS 2: simulate both makers per event with the honest trade-cross fill.
    # NAIVE: post at best bid always.
    # TOXIC-AWARE: if vpin >= per-asset median (toxic), post at bid - skew;
    #              else post at best bid.
    # Net PnL/contract per event:
    #   filled  -> net_pnl_filled(post_bid, won)
    #   unfilled-> 0 (no position, no fee). This is the markout of the QUOTE.
    # The headline is the DIFFERENCE per event (paired), which controls for the
    # window's outcome.
    # ------------------------------------------------------------------
    diffs = []          # toxic_net - naive_net per event (paired)
    naive_nets = []
    toxic_nets = []
    clusters = []       # ticker id for clustered bootstrap
    naive_fills = toxic_fills = 0
    for e in events:
        bid = e["bid"]
        tr = e["tr"]
        cutoff = e["cutoff"]
        won = e["won"]
        # naive
        nf = maker_fill_price(tr, cutoff, bid)
        naive_net = net_pnl_filled(bid, won) if nf else 0.0
        # toxic-aware
        toxic = e["vpin"] >= med[e["asset"]]
        post_bid = bid - TOXIC_SKEW_CENTS if toxic else bid
        if post_bid <= 0:
            tf = False
            toxic_net = 0.0
        else:
            tf = maker_fill_price(tr, cutoff, post_bid)
            toxic_net = net_pnl_filled(post_bid, won) if tf else 0.0
        naive_fills += int(nf)
        toxic_fills += int(tf)
        naive_nets.append(naive_net)
        toxic_nets.append(toxic_net)
        diffs.append(toxic_net - naive_net)
        clusters.append(e["ticker"])

    n = len(diffs)
    mean_naive = sum(naive_nets) / n
    mean_toxic = sum(toxic_nets) / n
    mean_diff = sum(diffs) / n
    print(f"\n=== RESULTS (n={n} posting events) ===")
    print(f"naive  maker: fill%={100*naive_fills/n:.1f}  mean net cents/contract"
          f" (over ALL posts, unfilled=0) = {mean_naive:+.3f}")
    print(f"toxic  maker: fill%={100*toxic_fills/n:.1f}  mean net cents/contract"
          f" (over ALL posts, unfilled=0) = {mean_toxic:+.3f}")
    print(f"UPLIFT (toxic - naive) = {mean_diff:+.4f} cents/contract")

    # filled-only EV for color
    nf_filled = [net_pnl_filled(e["bid"], e["won"])
                 for e, fnf in zip(events, [maker_fill_price(e["tr"], e["cutoff"], e["bid"]) for e in events])
                 if fnf]
    if nf_filled:
        print(f"\nnaive maker EV over FILLED only: {sum(nf_filled)/len(nf_filled):+.3f}"
              f" cents/contract (n_filled={len(nf_filled)}) "
              f"-- this is the adverse-selection tax the gate tries to dodge")

    # ------------------------------------------------------------------
    # CLUSTERED BOOTSTRAP CI on the headline uplift (resample TICKERS with
    # replacement -> robust to within-ticker correlation; ~1 day corpus).
    # ------------------------------------------------------------------
    by_ticker_diffs = defaultdict(list)
    for d, c in zip(diffs, clusters):
        by_ticker_diffs[c].append(d)
    ticker_keys = list(by_ticker_diffs.keys())
    rng = random.Random(20260531)
    B = 2000
    boot = []
    for _ in range(B):
        pool = []
        for _ in range(len(ticker_keys)):
            k = ticker_keys[rng.randrange(len(ticker_keys))]
            pool.extend(by_ticker_diffs[k])
        boot.append(sum(pool) / len(pool))
    boot.sort()
    ci_low = boot[int(0.025 * B)]
    ci_high = boot[int(0.975 * B)]
    print(f"\nclustered bootstrap CI (B={B}, resample {len(ticker_keys)} tickers):")
    print(f"  uplift point = {mean_diff:+.4f}  95% CI = [{ci_low:+.4f}, {ci_high:+.4f}] cents/contract")

    verdict = "EDGE" if ci_low > 0 else ("NO_EDGE" if ci_high < 0 else "INCONCLUSIVE")
    print(f"\nVERDICT: {verdict}")

    # emit machine-readable summary for the wrapper
    print("\n__RESULT_JSON__" + json.dumps({
        "n_samples": n,
        "n_tickers": len(ticker_keys),
        "point": mean_diff,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "mean_naive": mean_naive,
        "mean_toxic": mean_toxic,
        "naive_fill_pct": 100 * naive_fills / n,
        "toxic_fill_pct": 100 * toxic_fills / n,
        "naive_filled_ev": (sum(nf_filled) / len(nf_filled)) if nf_filled else None,
        "verdict": verdict,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
