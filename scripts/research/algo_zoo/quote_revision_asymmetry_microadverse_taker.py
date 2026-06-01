"""quote_revision_asymmetry_microadverse_taker — trade WITH the side that is
silently CANCELLING liquidity (one-sided quote withdrawal), not with depth.

FAMILY: order-book dynamics / liquidity-withdrawal as informed-flow proxy.

THE NON-OBVIOUS BET
-------------------
OFI (static depth imbalance) was too noisy (corr 0.033, -1.5c); VPIN / taker
toxicity (executed flow) was too late. The REVISION between them — a maker's
one-sided CANCEL — is the cheapest, fastest signal a maker emits (cancels are
free, trades are costly). On thin 15M crypto books the few resting makers carry
information; a taker reading one-sided quote withdrawal may trade ahead of the
print the cancel anticipates. Mechanically distinct from every tried depth/flow
idea; markout@30s honestly kills it if the mid already moved by the time the
cancel completes.

MECHANISM
---------
Per ticker, on a 15s grid t:
  1. Build a RELIABLE book at t-W and at t (W=20s) using the tested
     `kalshi_book_reconstruct` replay (snapshot-anchored, drift-refusing).
     REQUIRE is_reliable at BOTH t-W and t — spurious deltas in the drifted
     bronze would FAKE a withdrawal, so refusals are DROPPED (critical guard).
  2. NO-PRINT GATE: require zero trade prints in (t-W, t] (pure quote revision,
     not trade-driven), causally from trades_crypto.jsonl.
  3. Compute top-of-book depth deltas:
       yes_bid_depth  (makers willing to BUY yes / sell no)
       yes_ask_depth  (= best NO bid depth; makers willing to SELL yes / buy no)
     WITHDRAWAL ASYMMETRY: one side's depth fell >= THRESH% while the other is
     ~flat (|Delta| < FLAT_THRESH%).
       - yes-bid depth withdrawn  => makers leaving YES-buy => BEARISH => buy NO.
       - yes-ask depth withdrawn  => makers leaving NO-buy  => BULLISH => buy YES.
  4. TAKE by crossing the reliable ask on the implied side. Pay the taker fee
     (ceil(7*P*(1-P)) cents); rebate 0.
  5. markout@30s: reliable mid 30s later minus entry mid, signed by side.
  6. Settle to the TERMINAL reliable-book outcome (mid at close >= 50 => yes).

HEADLINE: per-contract net cents; block bootstrap clustered by (ticker, hour).
Gate (EDGE): CI-lower > 0 net of fee AND markout@30s mean > 0. Stratify by band,
contested 60-89c MIDDLE first.

DATA: frames_crypto.jsonl (reliable book deltas/depth), trades_crypto.jsonl
(no-print gate). Settlement derived from terminal reliable book (in-window
windows are just-settling in state.db). No spot needed.

Run:
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/quote_revision_asymmetry_microadverse_taker.py
"""
from __future__ import annotations

import bisect
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import KalshiBook  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"

# ----- parameters -----------------------------------------------------------
GRID_S = 15.0           # decision grid cadence
W_S = 20.0              # withdrawal look-back window
MARKOUT_S = 30.0        # markout horizon
THRESH = 0.40           # one side must withdraw >= 40% of its depth
FLAT_THRESH = 0.15      # the other side must stay within +/-15%
MIN_DEPTH = 5.0         # require >= 5 contracts on the withdrawing side at t-W
MAX_DELTAS_SINCE_SNAP = 100000
MAX_LEVELS = 220
N_BOOT = 2000
SEED = 17

MIN_SECONDS_TO_CLOSE = 30.0
MAX_SECONDS_TO_CLOSE = 14 * 60.0   # ignore the first ~minute (warmup) of window


def _epoch_iso(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _tick(price_dollars: str) -> int:
    return int(round(float(price_dollars) * 10000))


def kalshi_taker_fee_cents(price_cents: float) -> int:
    """Kalshi per-contract taker fee, rounded UP per spec:
    ceil(0.07 * 1 * P * (1-P)) cents, P in dollars. Rebate 0."""
    p = price_cents / 100.0
    return int(math.ceil(7.0 * p * (1.0 - p)))


def _band(ask):
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


# ----- compact event model --------------------------------------------------
# Stream the 4.4GB frames file ONCE into lean per-ticker event lists:
#   ('S', recv_epoch, yes_levels_ticks, no_levels_ticks)   snapshot
#   ('D', recv_epoch, side, price_tick, delta_fp)           delta


def load_frame_events(path: str):
    events: dict[str, list] = defaultdict(list)
    n = 0
    bad = 0
    with open(path) as fh:
        for line in fh:
            if not line:
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                bad += 1
                continue
            msg = inner.get("msg", {})
            tk = msg.get("market_ticker", "")
            if not _is_crypto_15m(tk):
                continue
            try:
                ts = _epoch_iso(env["_wire_recv_ts"])
            except Exception:
                bad += 1
                continue
            typ = inner.get("type")
            if typ == "orderbook_snapshot":
                yl = tuple(
                    (_tick(p), float(s))
                    for p, s in msg.get("yes_dollars_fp", [])
                    if float(s) > 0
                )
                nl = tuple(
                    (_tick(p), float(s))
                    for p, s in msg.get("no_dollars_fp", [])
                    if float(s) > 0
                )
                events[tk].append(("S", ts, yl, nl))
            elif typ == "orderbook_delta":
                try:
                    events[tk].append(
                        ("D", ts, msg["side"], _tick(msg["price_dollars"]),
                         float(msg["delta_fp"]))
                    )
                except (KeyError, ValueError):
                    bad += 1
                    continue
            n += 1
    for tk in events:
        events[tk].sort(key=lambda e: e[1])
    return events, n, bad


def load_trade_times(path: str):
    """{ticker: sorted [trade_event_ts_unix]} for the no-print gate."""
    out: dict[str, list] = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line:
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            if not _is_crypto_15m(tk):
                continue
            ts = msg.get("ts")
            if ts is not None:
                out[tk].append(float(ts))
    for tk in out:
        out[tk].sort()
    return out


def _apply_event(book: KalshiBook, ev):
    if ev[0] == "S":
        book.yes = {t: s for t, s in ev[2] if s > 0}
        book.no = {t: s for t, s in ev[3] if s > 0}
    else:
        _, _, side, t, d = ev
        b = book.yes if side == "yes" else book.no
        new = b.get(t, 0.0) + d
        if new > 1e-9:
            b[t] = new
        else:
            b.pop(t, None)


def reliable_book_state(events, idx_upto):
    """Replay events[0:idx_upto] (all ts <= cutoff) snapshot-anchored. Returns
    (KalshiBook, ok); ok = anchored + reliable. Mirrors reliable_nbbo_at."""
    b = KalshiBook()
    anchored = False
    since_snap = 0
    for i in range(idx_upto):
        ev = events[i]
        if ev[0] == "S":
            b = KalshiBook()
            _apply_event(b, ev)
            anchored = True
            since_snap = 0
        else:
            _apply_event(b, ev)
            since_snap += 1
    if not anchored or since_snap > MAX_DELTAS_SINCE_SNAP:
        return None, False
    if not b.is_reliable(MAX_LEVELS):
        return None, False
    return b, True


def book_states_at_cutoffs(events, ev_ts, cutoffs):
    """Single forward replay over `events`, capturing the reliable book state at
    each cutoff in `cutoffs` (must be sorted ascending). Returns a dict
    {cutoff: (yes_bid_cents, yes_ask_cents, yes_bid_depth, yes_ask_depth) or None}.
    None == refused (not anchored, drift overflow, or not reliable). This is O(F+C)
    per ticker instead of O(grid*F) of replaying from 0 each time."""
    out = {}
    if not cutoffs:
        return out
    b = KalshiBook()
    anchored = False
    since_snap = 0
    ci = 0
    nC = len(cutoffs)
    i = 0
    F = len(events)
    while ci < nC:
        cut = cutoffs[ci]
        # advance events with ts <= cut
        while i < F and ev_ts[i] <= cut:
            ev = events[i]
            if ev[0] == "S":
                b = KalshiBook()
                _apply_event(b, ev)
                anchored = True
                since_snap = 0
            else:
                _apply_event(b, ev)
                since_snap += 1
            i += 1
        # capture state at this cutoff
        if not anchored or since_snap > MAX_DELTAS_SINCE_SNAP or not b.is_reliable(MAX_LEVELS):
            out[cut] = None
        else:
            out[cut] = (b.best_yes_bid_cents(), b.best_yes_ask_cents(),
                        b.best_yes_bid_depth(), b.best_yes_ask_depth())
        ci += 1
    return out


def terminal_outcome(events, ev_ts, close_ep):
    """Derive the 15M above/below outcome from the TERMINAL reliable book.

    The very last frame in the stream is often an empty/wiped post-settlement
    snapshot, so we DON'T trust events[-1]. Instead we walk a cutoff backward
    from the close (T-0) in 5s steps over the last ~3 min and take the LATEST
    cutoff whose reliable book has a two-sided mid. Near close the mid converges
    to the outcome (mid -> ~100 yes-win, ~0 no-win). Returns (won_yes, mid) or
    (None, None) if no reliable two-sided book exists in the pre-close window."""
    # cutoffs from close backward, last 180s, 5s spacing (newest first)
    cuts = sorted({close_ep - off for off in range(0, 185, 5)})
    states = book_states_at_cutoffs(events, ev_ts, cuts)
    for cut in reversed(cuts):  # newest (closest to close) first
        st = states.get(cut)
        if st is None:
            continue
        yb, ya, _, _ = st
        if yb is not None and ya is not None:
            mid = (yb + ya) / 2.0
            return (mid >= 50.0), mid
    return None, None


def _count_in(times, lo, hi):
    """# of trade ts in (lo, hi]."""
    return bisect.bisect_right(times, hi) - bisect.bisect_right(times, lo)


def _result(verdict, n, mean_net, ci_low, ci_high, mean_mk, one, K=0):
    return {
        "verdict": verdict,
        "metric_name": "net_cents_per_contract",
        "point_estimate": mean_net,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "n_samples": n,
        "n_clusters": K,
        "markout_30s_mean": mean_mk,
        "one_line": one,
    }


def main() -> dict:
    random.seed(SEED)
    print("loading frames (single streaming pass over 4.4GB)...", flush=True)
    events, n_frames, bad = load_frame_events(FRAMES)
    print(f"  frames parsed: {n_frames}  bad: {bad}  tickers: {len(events)}",
          flush=True)
    print("loading trade times (no-print gate)...", flush=True)
    trade_times = load_trade_times(TRADES)
    print(f"  tickers with trades: {len(trade_times)}", flush=True)

    records = []
    diag = defaultdict(int)

    for tk, evs in events.items():
        if not evs:
            continue
        asset = _is_crypto_15m(tk)
        try:
            close_ep = close_epoch_from_ticker(tk)
        except Exception:
            diag["bad_close_epoch"] += 1
            continue

        ttimes = trade_times.get(tk, [])
        ev_ts = [e[1] for e in evs]
        first_ts = ev_ts[0]
        last_ts = ev_ts[-1]

        won_yes, term_mid = terminal_outcome(evs, ev_ts, close_ep)
        if won_yes is None:
            diag["no_terminal_mid"] += 1
            continue

        t0 = max(first_ts + W_S, close_ep - MAX_SECONDS_TO_CLOSE)
        end_t = min(last_ts, close_ep - MIN_SECONDS_TO_CLOSE)
        # enumerate valid grid points, collect the cutoffs we need books at
        grid_ts = []
        t = t0
        while t <= end_t:
            secs_to_close = close_ep - t
            if MIN_SECONDS_TO_CLOSE <= secs_to_close <= MAX_SECONDS_TO_CLOSE:
                if bisect.bisect_right(ev_ts, t) > 0 and bisect.bisect_right(ev_ts, t - W_S) > 0:
                    grid_ts.append(t)
            t += GRID_S
        if not grid_ts:
            continue

        cutset = set()
        for t in grid_ts:
            cutset.add(t)
            cutset.add(t - W_S)
            cutset.add(t + MARKOUT_S)
        cutoffs = sorted(cutset)
        states = book_states_at_cutoffs(evs, ev_ts, cutoffs)

        for t in grid_ts:
            diag["grid_points"] += 1
            tw = t - W_S
            if ttimes and _count_in(ttimes, tw, t) > 0:
                diag["gated_trade_print"] += 1
                continue
            st_tw = states.get(tw)
            if st_tw is None:
                diag["refused_tw"] += 1
                continue
            st_t = states.get(t)
            if st_t is None:
                diag["refused_t"] += 1
                continue

            yes_bid_tw, yes_ask_tw, yb0, ya0 = st_tw
            yes_bid, yes_ask, yb1, ya1 = st_t
            if yb0 is None or ya0 is None or yb1 is None or ya1 is None:
                diag["depth_missing"] += 1
                continue

            d_bid = (yb1 - yb0) / yb0 if yb0 > 0 else 0.0
            d_ask = (ya1 - ya0) / ya0 if ya0 > 0 else 0.0

            side = None
            if (d_bid <= -THRESH and abs(d_ask) < FLAT_THRESH and yb0 >= MIN_DEPTH):
                side = "no"          # yes-bid withdrawn -> bearish
            elif (d_ask <= -THRESH and abs(d_bid) < FLAT_THRESH and ya0 >= MIN_DEPTH):
                side = "yes"         # yes-ask withdrawn -> bullish
            if side is None:
                diag["no_signal"] += 1
                continue
            diag["signal_fired"] += 1

            if yes_ask is None or yes_bid is None:
                diag["no_cross_price"] += 1
                continue
            if side == "yes":
                entry = yes_ask
                if not (0 < entry < 100):
                    continue
                gross = (100.0 - entry) if won_yes else -entry
            else:
                entry = 100.0 - yes_bid
                if not (0 < entry < 100):
                    continue
                gross = (100.0 - entry) if (not won_yes) else -entry

            fee = kalshi_taker_fee_cents(entry)
            net = gross - fee

            entry_mid = (yes_bid + yes_ask) / 2.0
            markout = None
            st_mk = states.get(t + MARKOUT_S)
            if st_mk is not None:
                mb, ma, _, _ = st_mk
                if mb is not None and ma is not None:
                    mk_mid = (mb + ma) / 2.0
                    markout = (mk_mid - entry_mid) if side == "yes" else (entry_mid - mk_mid)

            hour = int(t // 3600)
            records.append({
                "cluster": (tk, hour),
                "net": net,
                "markout": markout,
                "side": side,
                "entry": entry,
                "asset": asset,
                "band": _band(entry),
            })

    print("\n=== diagnostics ===", flush=True)
    for k in sorted(diag):
        print(f"  {k}: {diag[k]}")
    n = len(records)
    print(f"\nretained samples (signals taken): {n}")

    if n == 0:
        gap = (diag.get("no_terminal_reliable", 0) >= max(1, len(events)))
        return _result("DATA_GAP" if gap else "INCONCLUSIVE", n, None, None,
                       None, None, "no signals fired / all refused")

    nets = [r["net"] for r in records]
    mks = [r["markout"] for r in records if r["markout"] is not None]
    mean_net = sum(nets) / n
    mean_mk = (sum(mks) / len(mks)) if mks else float("nan")

    clusters = defaultdict(list)
    for r in records:
        clusters[r["cluster"]].append(r["net"])
    cluster_keys = list(clusters.keys())
    cluster_means = {k: (sum(v) / len(v)) for k, v in clusters.items()}
    cluster_n = {k: len(v) for k, v in clusters.items()}
    K = len(cluster_keys)

    boot_means = []
    for _ in range(N_BOOT):
        num = 0.0
        den = 0
        for _ in range(K):
            ck = cluster_keys[random.randrange(K)]
            num += cluster_means[ck] * cluster_n[ck]
            den += cluster_n[ck]
        boot_means.append(num / den if den else 0.0)
    boot_means.sort()
    ci_low = boot_means[int(0.025 * N_BOOT)]
    ci_high = boot_means[int(0.975 * N_BOOT)]

    print("\n=== headline ===")
    print(f"  net cents/contract: {mean_net:+.3f}")
    print(f"  95% block-bootstrap CI: [{ci_low:+.3f}, {ci_high:+.3f}]  "
          f"(clusters={K}, resamples={N_BOOT})")
    print(f"  markout@30s mean: {mean_mk:+.3f}  (n_markout={len(mks)})")

    print("\n=== by band (net cents/contract) ===")
    by_band = defaultdict(lambda: {"n": 0, "net": 0.0, "mk": 0.0, "mkn": 0})
    for r in records:
        b = by_band[r["band"]]
        b["n"] += 1
        b["net"] += r["net"]
        if r["markout"] is not None:
            b["mk"] += r["markout"]
            b["mkn"] += 1
    for band in ("60-89", "90-99", "40-59", "1-39", "0", "none"):
        b = by_band.get(band)
        if not b or not b["n"]:
            continue
        mk = (b["mk"] / b["mkn"]) if b["mkn"] else float("nan")
        print(f"  {band:>6}  n={b['n']:>5}  net={b['net']/b['n']:+.3f}  markout={mk:+.3f}")

    print("\n=== by side ===")
    by_side = defaultdict(lambda: {"n": 0, "net": 0.0})
    for r in records:
        by_side[r["side"]]["n"] += 1
        by_side[r["side"]]["net"] += r["net"]
    for s in ("yes", "no"):
        sd = by_side.get(s)
        if sd and sd["n"]:
            print(f"  {s}: n={sd['n']}  net={sd['net']/sd['n']:+.3f}")

    fee_net_positive = ci_low > 0.0
    markout_positive = (len(mks) > 0 and mean_mk > 0.0)
    if fee_net_positive and markout_positive:
        verdict = "EDGE"
    elif ci_low <= 0.0 <= ci_high:
        verdict = "NO_EDGE" if mean_net <= 0 else "INCONCLUSIVE"
    else:
        verdict = "NO_EDGE"

    one = (f"{verdict}: net {mean_net:+.2f}c CI[{ci_low:+.2f},{ci_high:+.2f}] "
           f"markout@30s {mean_mk:+.2f}c, n={n} clusters={K}")
    print(f"\n=== VERDICT: {verdict} ===")
    print("  " + one)

    return _result(verdict, n, mean_net, ci_low, ci_high, mean_mk, one, K)


if __name__ == "__main__":
    out = main()
    print("\nRESULT_JSON:" + json.dumps(out, default=str))
