#!/usr/bin/env python3
"""hawkes_trade_burst_fade  (family: self_exciting_intensity / microstructure)

SPEC
----
Trade arrivals on a 15M Kalshi market are self-exciting (clustered): a burst of
same-direction taker prints often signals a transient liquidity grab that
mean-reverts, OR an informed sweep that continues. This fits a univariate Hawkes
intensity to the trade-print stream and tests whether the post-burst book is a
TRADEABLE fade or follow.

MECHANISM
  1. From TRADES, per ticker, build the signed trade-print event stream (sign =
     +1 yes-taker, -1 no-taker; size-weighted by count_fp). Fit a simple Hawkes
     intensity online (exponential kernel, branching ratio + decay estimated via
     a fast EM or method-of-moments per asset; no look-ahead — params from a
     burn-in window, applied forward).
  2. DECISION: flag a "burst" when conditional intensity spikes > k * baseline
     AND net signed flow over the burst is one-sided. At burst-end time,
     reconstruct reliable NBBO (reliable_nbbo_at; honor refusal). Test BOTH
     directions: FADE (enter opposite the burst, betting on reversion) and FOLLOW
     (enter with the burst, betting on continuation) as a TAKER at the ask.
  3. LABEL by terminal book mid at close. PnL/ct net of taker fee. Compare FADE
     vs FOLLOW vs baseline. Also test a MAKER variant: rest a bid one tick inside
     the post-burst spread on the fade side, honest fill via trade-cross.
  4. HEADLINE = mean net PnL per entered contract for the better of fade/follow,
     ticker-clustered bootstrap CI (>=1000). EDGE iff CI > 0 net of cost. Report
     intensity-spike count (n) and the realized post-burst short-horizon mid drift
     (gross) so a real-but-unmonetizable signal is labeled NO_EDGE not EDGE.

WHY IT MIGHT BEAT AN EFFICIENT MARKET
  The hunt lists Hawkes/self-exciting trade intensity as untested. OFI (a static
  imbalance snapshot) was real but unmonetizable; a Hawkes burst is a DYNAMIC
  intensity feature that conditions on the TIME-CLUSTERING of flow, not a single
  snapshot — a different mechanism. Retail bursts in the contested middle may
  over-shoot and revert; informed sweeps near rails may continue. Either way the
  monetization test is honest (terminal label + real fee + honest maker fill).

DATA: TRADES (event stream + Hawkes fit), FRAMES (NBBO at decision + terminal
label, honest maker fill). No spot -> all 7 assets.
"""
from __future__ import annotations

import json
import math
import sys
import random
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at
from scripts.research.phase1b_real_price_economics import (
    close_epoch_from_ticker,
    load_frames_jsonl,
)

TRADES_PATH = "/tmp/edge_daily/trades_crypto.jsonl"
FRAMES_PATH = "/tmp/edge_daily/frames_crypto.jsonl"

ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB")

# Hawkes / burst params
BURNIN_FRAC = 0.35       # fraction of each asset's trade span used to FIT (no fwd use)
K_SPIKE = 3.0            # burst iff lambda(t) > K*mu
FLOW_ONESIDE = 0.6       # |net|/gross over recent window must exceed this
RECENT_WINDOW_S = 30.0   # window for one-sidedness check (signed flow)
MIN_DECISION_BEFORE_CLOSE_S = 5.0
COOLDOWN_S = 60.0        # one burst entry per ticker per cooldown (de-cluster)

FEE_RATE = 0.07
N_BOOT = 2000
RNG_SEED = 12345


def _asset_of(tk: str):
    for a in ASSETS:
        if tk.startswith("KX" + a + "15M"):
            return a
    return None


def fee_cents(price_cents: float) -> float:
    """ceil(0.07 * C * P * (1-P)) cents/contract, C=1. price in cents."""
    p = max(0.0, min(1.0, price_cents / 100.0))
    return math.ceil(FEE_RATE * p * (1.0 - p) * 100.0)


def load_trades():
    """{ticker: [(ts_seconds, sign, weight, yes_c)]} sorted; sign=+1 yes-taker."""
    per = defaultdict(list)
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
            if _asset_of(tk) is None:
                continue
            try:
                ts = float(msg["ts_ms"]) / 1000.0
                taker = msg["taker_side"]
                w = float(msg["count_fp"])
                yes_c = float(msg["yes_price_dollars"]) * 100.0
            except (KeyError, ValueError):
                continue
            sign = 1.0 if taker == "yes" else -1.0
            per[tk].append((ts, sign, w, yes_c))
    for tk in per:
        per[tk].sort(key=lambda x: x[0])
    return per


def fit_hawkes_mom(event_times, T):
    """Method-of-moments fit of an exp-kernel Hawkes on burn-in [0,T].
    Returns (mu, eta, beta). eta = branching ratio in [0,0.85]. No look-ahead."""
    n = len(event_times)
    if n < 20 or T <= 0:
        return None
    rate = n / T
    gaps = [event_times[i + 1] - event_times[i] for i in range(n - 1)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    gaps.sort()
    med = gaps[len(gaps) // 2]
    beta = 1.0 / max(med, 0.5)
    tau = 1.0 / beta
    close_pairs = 0
    j = 0
    for i in range(n):
        if j < i + 1:
            j = i + 1
        while j < n and event_times[j] - event_times[i] <= tau:
            j += 1
        close_pairs += (j - i - 1)
    expected_poisson = n * rate * tau
    if expected_poisson <= 0:
        return None
    excess = (close_pairs - expected_poisson) / max(close_pairs, 1.0)
    eta = max(0.0, min(0.85, excess))
    mu = rate * (1.0 - eta)
    if mu <= 0:
        mu = rate * 0.5
    return (mu, eta, beta)


def detect_bursts(trades, params):
    """Forward-only burst detection. Returns [(burst_ts, direction, yes_c)].
    direction=+1 => net yes-taker flow (price pushed up). lambda(t) uses only
    events <= t (recursive exp-kernel)."""
    mu, eta, beta = params
    out = []
    R = 0.0
    last_t = None
    recent = []
    last_burst = -1e18
    for (ts, sign, w, yes_c) in trades:
        if last_t is not None:
            R *= math.exp(-beta * (ts - last_t))
        lam = mu + R
        last_t = ts
        recent.append((ts, sign * w))
        cutoff = ts - RECENT_WINDOW_S
        while recent and recent[0][0] < cutoff:
            recent.pop(0)
        net = sum(x for _, x in recent)
        gross = sum(abs(x) for _, x in recent)
        R += eta * beta  # add THIS event AFTER reading lambda at its arrival
        if gross > 0 and lam > K_SPIKE * mu and abs(net) / gross >= FLOW_ONESIDE:
            if ts - last_burst >= COOLDOWN_S:
                out.append((ts, 1.0 if net > 0 else -1.0, yes_c))
                last_burst = ts
    return out


def _first_yes_bid_fill(tk_trades, post_ts, bid_cents):
    for ts, yp, side in tk_trades:
        if ts > post_ts and side == "no" and yp <= bid_cents:
            return ts
    return None


def _first_no_bid_fill(tk_trades, post_ts, no_bid_cents):
    thresh = 100.0 - no_bid_cents
    for ts, yp, side in tk_trades:
        if ts > post_ts and side == "yes" and yp >= thresh:
            return ts
    return None


def cluster_bootstrap(recs, n_boot=N_BOOT):
    by_tk = defaultdict(list)
    for tk, v in recs:
        by_tk[tk].append(v)
    keys = list(by_tk.keys())
    allv = [v for _, v in recs]
    pt = sum(allv) / len(allv)
    means = []
    for _ in range(n_boot):
        samp = []
        for _ in range(len(keys)):
            k = keys[random.randrange(len(keys))]
            samp.extend(by_tk[k])
        if samp:
            means.append(sum(samp) / len(samp))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    return pt, lo, hi, len(allv)


def main():
    random.seed(RNG_SEED)
    print("loading trades...", file=sys.stderr)
    trades = load_trades()
    print(f"  {len(trades)} tickers with trades", file=sys.stderr)
    print("loading frames (4.4GB, ~minutes)...", file=sys.stderr)
    frames = load_frames_jsonl(FRAMES_PATH)
    print(f"  {len(frames)} tickers with frames", file=sys.stderr)

    # Fit Hawkes per asset on burn-in (first BURNIN_FRAC of asset span); burn-in
    # trades are NOT used for detection (forward-only application).
    asset_events = defaultdict(list)
    for tk, evs in trades.items():
        a = _asset_of(tk)
        for (ts, *_rest) in evs:
            asset_events[a].append(ts)
    asset_params = {}
    asset_burnin_cut = {}
    for a, ev in asset_events.items():
        ev.sort()
        t0, t1 = ev[0], ev[-1]
        span = t1 - t0
        cut = t0 + BURNIN_FRAC * span
        burnin = [t - t0 for t in ev if t <= cut]
        p = fit_hawkes_mom(burnin, max(cut - t0, 1.0))
        asset_params[a] = p
        asset_burnin_cut[a] = cut
        if p:
            mu, eta, beta = p
            print(f"  fit {a}: mu={mu:.4f}/s eta={eta:.3f} beta={beta:.3f} "
                  f"1/beta={1/beta:.1f}s burnin_n={len(burnin)}", file=sys.stderr)
        else:
            print(f"  fit {a}: FAILED (too few)", file=sys.stderr)

    fade_recs = []
    follow_recs = []
    maker_recs = []
    drift_recs = []
    n_bursts = 0
    n_book_refused = 0
    n_label_refused = 0

    for tk, evs in trades.items():
        a = _asset_of(tk)
        p = asset_params.get(a)
        if not p:
            continue
        cut = asset_burnin_cut[a]
        fwd = [e for e in evs if e[0] > cut]
        if len(fwd) < 5:
            continue
        bursts = detect_bursts(fwd, p)
        if not bursts:
            continue
        fr = frames.get(tk)
        if not fr:
            continue
        close_ep = close_epoch_from_ticker(tk)
        lb_bid, lb_ask = reliable_nbbo_at(fr, close_ep)
        if lb_bid is None or lb_ask is None:
            n_label_refused += len(bursts)
            continue
        terminal_mid = (lb_bid + lb_ask) / 2.0

        tk_trades_for_fill = [(ts, yc, "yes" if sg > 0 else "no")
                              for (ts, sg, w, yc) in evs]

        for (bts, bdir, yes_c_at) in bursts:
            tleft = close_ep - bts
            if tleft < MIN_DECISION_BEFORE_CLOSE_S or tleft > 15 * 60:
                continue
            d_bid, d_ask = reliable_nbbo_at(fr, bts)
            if d_bid is None or d_ask is None:
                n_book_refused += 1
                continue
            n_bursts += 1
            yes_ask = d_ask
            no_ask = 100.0 - d_bid
            dmid = (d_bid + d_ask) / 2.0
            drift_recs.append(bdir * (terminal_mid - dmid))

            # TAKER FADE (opposite the burst)
            if bdir > 0:
                entry = no_ask
                win = 100.0 if terminal_mid < 50.0 else 0.0
            else:
                entry = yes_ask
                win = 100.0 if terminal_mid > 50.0 else 0.0
            if 0 < entry < 100:
                f = fee_cents(entry)
                fade_recs.append((tk, win - entry - f))

            # TAKER FOLLOW (with the burst)
            if bdir > 0:
                entry = yes_ask
                win = 100.0 if terminal_mid > 50.0 else 0.0
            else:
                entry = no_ask
                win = 100.0 if terminal_mid < 50.0 else 0.0
            if 0 < entry < 100:
                f = fee_cents(entry)
                follow_recs.append((tk, win - entry - f))

            # MAKER FADE: rest a bid one tick inside the spread on the fade side
            spread = d_ask - d_bid
            if spread >= 2:
                if bdir > 0:
                    no_bid = (100.0 - d_ask) + 1.0
                    fill_ts = _first_no_bid_fill(tk_trades_for_fill, bts, no_bid)
                    if fill_ts is not None:
                        entry = no_bid
                        win = 100.0 if terminal_mid < 50.0 else 0.0
                        maker_recs.append((tk, win - entry - fee_cents(entry), True))
                    else:
                        maker_recs.append((tk, 0.0, False))
                else:
                    yes_bid = d_bid + 1.0
                    fill_ts = _first_yes_bid_fill(tk_trades_for_fill, bts, yes_bid)
                    if fill_ts is not None:
                        entry = yes_bid
                        win = 100.0 if terminal_mid > 50.0 else 0.0
                        maker_recs.append((tk, win - entry - fee_cents(entry), True))
                    else:
                        maker_recs.append((tk, 0.0, False))

    print(f"\nbursts evaluated (reliable decision book): {n_bursts}", file=sys.stderr)
    print(f"  decision-book refusals: {n_book_refused}; "
          f"label-book refusals: {n_label_refused}", file=sys.stderr)

    def summarize(recs, name):
        if not recs:
            return None
        vals = [v for _, v in recs]
        mean = sum(vals) / len(vals)
        print(f"  {name}: n={len(vals)} mean_net_pnl/ct={mean:+.3f}c", file=sys.stderr)
        return mean

    fade_mean = summarize(fade_recs, "TAKER FADE")
    follow_mean = summarize(follow_recs, "TAKER FOLLOW")
    if drift_recs:
        gd = sum(drift_recs) / len(drift_recs)
        print(f"  GROSS post-burst mid-drift (signed in burst dir): "
              f"{gd:+.3f}c (n={len(drift_recs)})", file=sys.stderr)
    filled = [v for (_, v, fl) in maker_recs if fl]
    if maker_recs:
        n_fill = len(filled)
        msg = (f"  MAKER FADE: posted={len(maker_recs)} filled={n_fill} "
               f"({100*n_fill/len(maker_recs):.0f}%)")
        if filled:
            msg += f" mean_net_on_FILLED={sum(filled)/len(filled):+.3f}c"
        print(msg, file=sys.stderr)

    if fade_mean is None and follow_mean is None:
        return _emit("DATA_GAP", 0, 0.0, 0.0, 0.0,
                     "no reliable bursts evaluated", "n/a", n_bursts)
    if follow_mean is None or (fade_mean is not None and fade_mean >= follow_mean):
        headline_recs, headline_name = fade_recs, "TAKER_FADE"
    else:
        headline_recs, headline_name = follow_recs, "TAKER_FOLLOW"

    pt, lo, hi, nn = cluster_bootstrap(headline_recs)
    n_clusters = len(set(t for t, _ in headline_recs))
    print(f"\nHEADLINE = {headline_name}: mean={pt:+.3f}c  "
          f"CI95=[{lo:+.3f}, {hi:+.3f}]  n={nn} entries, "
          f"clusters={n_clusters}", file=sys.stderr)

    verdict = "EDGE" if lo > 0 else "NO_EDGE"
    if nn < 30:
        verdict = "INCONCLUSIVE"
    one = (f"{headline_name} {pt:+.2f}c/ct net CI95=[{lo:+.2f},{hi:+.2f}] "
           f"n={nn} clusters={n_clusters}; {verdict}")
    return _emit(verdict, nn, pt, lo, hi, one, headline_name, n_bursts)


def _emit(verdict, n, pt, lo, hi, one, fillname, n_spikes):
    result = {
        "verdict": verdict,
        "metric_name": "mean_net_pnl_per_contract_cents",
        "point_estimate": round(pt, 4),
        "ci_low": round(lo, 4),
        "ci_high": round(hi, 4),
        "n_samples": n,
        "n_intensity_spikes": n_spikes,
        "fill_model": fillname,
        "one_line": one,
    }
    print("\n=== RESULT JSON ===")
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    main()
