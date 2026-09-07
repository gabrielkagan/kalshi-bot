"""taker_sweep_vwap_reversion — microstructure / taker-impact mean reversion.

HYPOTHESIS
----------
A *sweep* = a cluster of >=MIN_PRINTS same-direction TAKER prints clearing
>=K contracts within a WINDOW_S-second window. An impatient liquidity demander
walking a thin Kalshi book can push the executed VWAP past the pre-sweep reliable
mid (an *overshoot*). The book then refills and repairs it. If the gross
reversion is large enough vs the tiny near-resolved Kalshi fee, the fade survives.

Distinct from OFI (resting-book imbalance, already -1.5c) and microprice (already
refuted): this trades the EXECUTED-FLOW overshoot — the transient impact of the
demander — not a static book feature.

TRADE
-----
Sweep lifted YES (taker_book_side='ask' -> takers BOUGHT yes) overshoots mid UP ->
we SELL YES (= buy NO) at the now-rich quote, expecting the mid to fall back.
Sweep hit YES bids (taker_book_side='bid' -> takers SOLD yes) overshoots DOWN ->
we BUY YES at the now-cheap quote, expecting the mid to rise.
Entry is a TAKER cross at the prevailing opposite quote (we pay the spread).
Exit is a TAKER cross-out at the reliable mid H seconds later (H in {10,30}).

LOOK-AHEAD DISCIPLINE
---------------------
- Everything keyed on `_wire_recv_ts` (arrival clock). Trades + frames share it.
- Pre-sweep mid: reliable NBBO at (first_print_recv - EPS) — only frames that
  ARRIVED strictly before the sweep started.
- Entry quote: reliable NBBO at (last_print_recv) — book at sweep end.
- Label/exit book: reliable NBBO at (last_print_recv + H) — same reliable
  reconstruction, queried at a strictly-later cutoff; never uses sweep internals.
- The NBBO reconstruction is SNAPSHOT-ANCHORED and REFUSES drifted/unanchored or
  crossed books (returns None) exactly like reliable_nbbo_at; refusals honored.
  Equivalence to reliable_nbbo_at is asserted in _selftest() against the canonical
  scripts.research.kalshi_book_reconstruct.reliable_nbbo_at on real frames.

PERFORMANCE
-----------
Rather than re-replaying each ticker's frame list 4x per sweep (O(sweeps*frames)),
we make ONE forward pass per ticker, advancing the book and snapshotting reliable
NBBO at each pre-sorted cutoff time. Tickers are SUBSAMPLED (TICKER_CAP, stratified
across assets) to bound RAM (the full 4.4GB frames file does not fit comfortably)
and runtime; n_samples + subsample reported honestly.

FEES
----
Kalshi: ceil(0.07*P*(1-P)*100) cents per contract per leg, at each leg's own
price. Maker rebate = 0 (pure taker, both legs).

EDGE BAR
--------
Block bootstrap CI (2000 resamples, clustered by ticker). EDGE only if mean net
cents/contract CI excludes zero on the positive side. A reversion < 2 half-spreads
+ 2 fees is NO_EDGE.

Corpus: local crypto-15M bronze, ~31h (2026-05-30T10Z -> 05-31T17Z).
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import numpy as np  # noqa: E402

from scripts.research.kalshi_book_reconstruct import (  # noqa: E402
    KalshiBook,
    reliable_nbbo_at,
)

FRAMES_PATH = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES_PATH = "/tmp/edge_daily/trades_crypto.jsonl"

# --- signal params ---
MIN_PRINTS = 3          # >=3 same-direction taker prints
WINDOW_S = 2.0          # within a 2s window
K_CONTRACTS = 20.0      # cluster must clear >= K contracts
D_OVERSHOOT_C = 1.0     # |VWAP - pre_mid| >= D cents to qualify as overshoot
EPS = 0.05              # epsilon guard on the pre-sweep cutoff (s)
H_LIST = (10.0, 30.0)   # exit horizons (s)

FEE_RATE = 0.07
MAKER_REBATE = 0.0      # pure taker

# subsample to bound RAM/time: cap tickers, stratified across assets.
TICKER_CAP = 140
ASSET_RE = re.compile(r"^KX([A-Z]+)15M-")
MAX_LEVELS_PER_SIDE = 220


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _asset(tk: str) -> str:
    m = ASSET_RE.match(tk)
    return m.group(1) if m else "?"


def fee_cents(price_c: float) -> int:
    """Kalshi per-contract fee at `price_c` (cents): ceil(0.07*P*(1-P)*100) for one
    contract; P in (0,1). Zero at the 0/100 rails."""
    p = max(0.0, min(1.0, price_c / 100.0))
    if not (0.0 < p < 1.0):
        return 0
    return math.ceil(FEE_RATE * p * (1.0 - p) * 100.0)


# ----------------------------------------------------------------------------
# Loading (streamed; ticker-subsampled to bound memory)
# ----------------------------------------------------------------------------
def select_tickers() -> set:
    """First pass over the SMALL trades file: which crypto-15M tickers have trades?
    Then stratified-subsample TICKER_CAP across assets. Only these tickers' frames
    are loaded — bounds RAM since the 4.4GB frames file won't fit whole."""
    by_asset: dict[str, set] = defaultdict(set)
    with open(TRADES_PATH) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            if tk.startswith("KX") and "15M-" in tk:
                by_asset[_asset(tk)].add(tk)
    # round-robin across assets up to the cap (deterministic: sorted)
    pools = {a: sorted(s) for a, s in by_asset.items()}
    order = sorted(pools)
    chosen: list[str] = []
    idx = 0
    while len(chosen) < TICKER_CAP and any(pools[a] for a in order):
        a = order[idx % len(order)]
        if pools[a]:
            chosen.append(pools[a].pop(0))
        idx += 1
    return set(chosen)


def load_frames_subset(keep: set) -> dict:
    """Stream the big frames file; keep only `keep` tickers' (recv_epoch, inner)."""
    frames: dict[str, list] = defaultdict(list)
    with open(FRAMES_PATH) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk = inner.get("msg", {}).get("market_ticker", "")
            if tk in keep:
                frames[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


def load_trades_subset(keep: set) -> dict:
    """ticker -> [(recv_epoch, book_side, count, yes_c), ...] sorted, keep-filtered."""
    out: dict[str, list] = defaultdict(list)
    with open(TRADES_PATH) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            if tk not in keep:
                continue
            bs = msg.get("taker_book_side")
            if bs not in ("ask", "bid"):
                continue
            try:
                recv = _epoch(env["_wire_recv_ts"])
                cnt = float(msg["count_fp"])
                yc = float(msg["yes_price_dollars"]) * 100.0
            except (KeyError, ValueError, TypeError):
                continue
            out[tk].append((recv, bs, cnt, yc))
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


# ----------------------------------------------------------------------------
# Sweep detection + single-pass reliable-NBBO sampling
# ----------------------------------------------------------------------------
def detect_sweeps(trades: list) -> list:
    """Greedy non-overlapping clusters of >=MIN_PRINTS same-direction prints whose
    recv-time span <= WINDOW_S and summed count >= K_CONTRACTS. VWAP = contract-
    weighted YES exec price."""
    sweeps = []
    n = len(trades)
    i = 0
    while i < n:
        recv0, bs0, _, _ = trades[i]
        j = i
        contracts = 0.0
        wsum = 0.0
        last_recv = recv0
        while j < n:
            recv, bs, cnt, yc = trades[j]
            if bs != bs0 or recv - recv0 > WINDOW_S:
                break
            contracts += cnt
            wsum += cnt * yc
            last_recv = recv
            j += 1
        if (j - i) >= MIN_PRINTS and contracts >= K_CONTRACTS:
            sweeps.append({
                "start_recv": recv0, "last_recv": last_recv, "book_side": bs0,
                "n_prints": j - i, "contracts": contracts,
                "vwap_yes_c": wsum / contracts,
            })
            i = j
        else:
            i += 1
    return sweeps


def sample_reliable_nbbo(frames: list, cutoffs: list) -> dict:
    """ONE forward pass: for each (sorted, unique) cutoff epoch, return the reliable
    NBBO (yes_bid, yes_ask) at that cutoff. Snapshot-anchored + reliability refusal,
    identical semantics to reliable_nbbo_at. Returns {cutoff: (yb, ya) or (None,None)}."""
    out: dict[float, tuple] = {}
    cuts = sorted(set(cutoffs))
    if not cuts:
        return out
    ci = 0
    b = KalshiBook()
    anchored = False
    fi = 0
    nf = len(frames)
    while ci < len(cuts):
        cut = cuts[ci]
        # advance book through all frames with ts <= cut
        while fi < nf and frames[fi][0] <= cut:
            _, inner = frames[fi]
            if inner.get("type") == "orderbook_snapshot":
                b = KalshiBook()
                b.apply_frame(inner)
                anchored = True
            else:
                b.apply_frame(inner)
            fi += 1
        if not anchored or not b.is_reliable(MAX_LEVELS_PER_SIDE):
            out[cut] = (None, None)
        else:
            out[cut] = (b.best_yes_bid_cents(), b.best_yes_ask_cents())
        ci += 1
    return out


def evaluate(frames_by_tk: dict, trades_by_tk: dict, H: float) -> list:
    results = []
    tickers = sorted(set(frames_by_tk) & set(trades_by_tk))
    for tk in tickers:
        fr = frames_by_tk[tk]
        sweeps = detect_sweeps(trades_by_tk[tk])
        if not sweeps:
            continue
        cutoffs = []
        for sw in sweeps:
            cutoffs.append(sw["start_recv"] - EPS)
            cutoffs.append(sw["last_recv"])
            cutoffs.append(sw["last_recv"] + H)
        nbbo = sample_reliable_nbbo(fr, cutoffs)
        for sw in sweeps:
            start, last = sw["start_recv"], sw["last_recv"]
            bs, vwap = sw["book_side"], sw["vwap_yes_c"]
            pb, pa = nbbo[start - EPS]
            if pb is None or pa is None:
                continue
            pre_mid = (pb + pa) / 2.0
            half_spread = (pa - pb) / 2.0
            if half_spread <= 0:
                continue
            eb, ea = nbbo[last]
            if eb is None or ea is None:
                continue
            overshoot = vwap - pre_mid
            if abs(overshoot) < D_OVERSHOOT_C:
                continue
            if bs == "ask":
                if overshoot <= 0:
                    continue
                side, entry_yes_px = "SELL_YES", eb   # cross to hit YES bid
            else:
                if overshoot >= 0:
                    continue
                side, entry_yes_px = "BUY_YES", ea    # cross to lift YES ask
            xb, xa = nbbo[last + H]
            if xb is None or xa is None:
                continue
            exit_mid = (xb + xa) / 2.0
            if side == "SELL_YES":
                gross = entry_yes_px - exit_mid
                f_entry = fee_cents(100.0 - entry_yes_px)
                f_exit = fee_cents(exit_mid)
            else:
                gross = exit_mid - entry_yes_px
                f_entry = fee_cents(entry_yes_px)
                f_exit = fee_cents(exit_mid)
            net = gross - f_entry - f_exit + 2 * MAKER_REBATE
            results.append({
                "ticker": tk, "side": side, "book_side": bs,
                "overshoot": overshoot, "half_spread": half_spread,
                "gross": gross, "fees": f_entry + f_exit, "net": net,
                "reverted": gross > 0, "contracts": sw["contracts"],
            })
    return results


def block_bootstrap_ci(values_by_ticker: dict, n_boot: int = 2000,
                       seed: int = 7) -> tuple:
    rng = np.random.default_rng(seed)
    keys = list(values_by_ticker.keys())
    if not keys:
        return (float("nan"), float("nan"), float("nan"))
    arrs = {k: np.asarray(v, dtype=float) for k, v in values_by_ticker.items()}
    point = float(np.concatenate([arrs[k] for k in keys]).mean())
    means = np.empty(n_boot)
    idx = np.arange(len(keys))
    for bi in range(n_boot):
        pick = rng.choice(idx, size=len(keys), replace=True)
        means[bi] = np.concatenate([arrs[keys[p]] for p in pick]).mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return point, float(lo), float(hi)


def _selftest(frames_by_tk: dict) -> None:
    """Assert the single-pass sampler matches canonical reliable_nbbo_at on a few
    real cutoffs (guards against silent divergence in the perf rewrite)."""
    checked = 0
    for tk, fr in frames_by_tk.items():
        if len(fr) < 50:
            continue
        cuts = [fr[len(fr) // 4][0], fr[len(fr) // 2][0], fr[-1][0]]
        got = sample_reliable_nbbo(fr, cuts)
        for c in cuts:
            ref = reliable_nbbo_at(fr, c, max_levels_per_side=MAX_LEVELS_PER_SIDE)
            assert got[c] == ref, f"sampler mismatch {tk}@{c}: {got[c]} vs {ref}"
        checked += 1
        if checked >= 8:
            break
    print(f"  selftest: sampler==reliable_nbbo_at on {checked} tickers OK", flush=True)


def main() -> dict:
    print("selecting subsample tickers (from trades)...", flush=True)
    keep = select_tickers()
    print(f"  kept {len(keep)} tickers (cap {TICKER_CAP}); assets: "
          f"{sorted(set(_asset(t) for t in keep))}", flush=True)
    print("loading trades subset...", flush=True)
    trades_by_tk = load_trades_subset(keep)
    print(f"  trades: {len(trades_by_tk)} tickers", flush=True)
    print("loading frames subset (streamed)...", flush=True)
    frames_by_tk = load_frames_subset(keep)
    print(f"  frames: {len(frames_by_tk)} tickers", flush=True)

    _selftest(frames_by_tk)

    total_sweeps = sum(len(detect_sweeps(v)) for v in trades_by_tk.values())
    print(f"  raw sweeps detected: {total_sweeps}", flush=True)

    summary = {"subsample_tickers": len(keep), "raw_sweeps": total_sweeps}
    for H in H_LIST:
        res = evaluate(frames_by_tk, trades_by_tk, H)
        n = len(res)
        if n == 0:
            summary[H] = {"n": 0}
            print(f"\nH={H:.0f}s: NO TRADES", flush=True)
            continue
        net_by_tk, gross_by_tk = defaultdict(list), defaultdict(list)
        for r in res:
            net_by_tk[r["ticker"]].append(r["net"])
            gross_by_tk[r["ticker"]].append(r["gross"])
        pt, lo, hi = block_bootstrap_ci(net_by_tk)
        gpt, glo, ghi = block_bootstrap_ci(gross_by_tk)
        nets = np.array([r["net"] for r in res])
        grosses = np.array([r["gross"] for r in res])
        fees = np.array([r["fees"] for r in res])
        rev = np.array([r["reverted"] for r in res])
        n_rev, win = int(rev.sum()), int((nets > 0).sum())
        summary[H] = {
            "n": n, "n_tickers": len(net_by_tk),
            "net_mean": pt, "net_lo": lo, "net_hi": hi,
            "gross_mean": gpt, "gross_lo": glo, "gross_hi": ghi,
            "mean_fee": float(fees.mean()),
            "mean_half_spread": float(np.mean([r["half_spread"] for r in res])),
            "rev_frac": n_rev / n, "win_frac": win / n,
            "rev_mean_gross": float(grosses[rev].mean()) if n_rev else float("nan"),
            "cont_mean_gross": float(grosses[~rev].mean()) if n - n_rev else float("nan"),
        }
        s = summary[H]
        print(f"\nH={H:.0f}s  n={n} over {s['n_tickers']} tickers", flush=True)
        print(f"  gross mean = {gpt:+.3f}c  CI[{glo:+.3f}, {ghi:+.3f}]", flush=True)
        print(f"  mean fees(2-leg) = {s['mean_fee']:.2f}c  mean half-spread = {s['mean_half_spread']:.2f}c", flush=True)
        print(f"  NET   mean = {pt:+.3f}c  CI[{lo:+.3f}, {hi:+.3f}]", flush=True)
        print(f"  reverted frac = {s['rev_frac']:.1%}  win(net>0) = {s['win_frac']:.1%}", flush=True)
        print(f"  reverted gross = {s['rev_mean_gross']:+.2f}c  continued gross = {s['cont_mean_gross']:+.2f}c", flush=True)

    return summary


if __name__ == "__main__":
    out = main()
    print("\n=== SUMMARY ===")
    print(json.dumps({str(k): v for k, v in out.items()}, indent=2, default=str))
