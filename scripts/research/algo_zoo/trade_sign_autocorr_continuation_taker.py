"""trade_sign_autocorr_continuation_taker — Hawkes / self-exciting order-flow continuation.

MECHANISM (informed-flow continuation, distinct from hawkes_trade_burst_fade which FADED):
  For each crypto-15M ticker, build the taker-sign series from trades_crypto
  (sign = +1 yes-taker, -1 no-taker, weighted by count_fp). At decision = close-240s
  compute the signed-EWMA + consistency of the last K prints. When flow is strongly
  one-directional (|EWMA| > thresh) AND consistent (>70% same sign of last K) AND that
  side's book ask < 65c, TAKE that side at the ask (continuation bet). Also run the FADE
  leg (take the opposite side at its ask) as a sign-control. We are our own assassin.

FILL: cross to the resting ask at decision (reliable book only); skip if no ask.
FEES: ceil(0.07 * C * P * (1-P)) cents/contract on entry, P = ask/100, C=1. Maker rebate 0.
LABEL: terminal reliable book mid at close_epoch_from_ticker; YES wins iff mid > 50.
NO look-ahead: signal uses trades with ts <= decision; entry book uses frames recv<=decision;
  label book is built independently up to close.
CI: block bootstrap clustered by ticker (the independent unit), >=2000 resamples.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    close_epoch_from_ticker,
)
from scripts.research.phase1b_retail_flow import parse_trade  # noqa: E402


def _epoch(iso: str) -> float:
    from datetime import datetime, timezone
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_frames_for_tickers(path: str, wanted: set) -> dict:
    """Stream the (huge) frames JSONL once. Cheap substring pre-filter on the raw
    line for any wanted ticker BEFORE paying json.loads — only parse lines we need.
    Returns {ticker: sorted [(recv_epoch, inner_frame_dict)]} (same shape as
    load_frames_jsonl). This is the I/O optimization vs loading all 9.77M frames."""
    frames = defaultdict(list)
    # Cheap ticker extraction from the raw line BEFORE json.loads: the market_ticker
    # appears as the escaped substring  \"market_ticker\":\"KX...15M-...\"  inside _raw.
    # Pull it out with str.find + a delimiter slice, set-test, and only THEN pay
    # json.loads for the small subset of wanted lines.
    NEEDLE = 'market_ticker\\":\\"'   # escaped-quote form inside the JSON-string _raw
    nlen = len(NEEDLE)
    with open(path) as fh:
        for line in fh:
            i = line.find(NEEDLE)
            if i < 0:
                continue
            j = line.find('\\"', i + nlen)
            if j < 0:
                continue
            tk = line[i + nlen:j]
            if tk not in wanted:
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk2 = inner.get("msg", {}).get("market_ticker", "")
            if tk2 in wanted:
                frames[tk2].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames

TRADES = "/tmp/edge_daily/trades_crypto.jsonl"
FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"

DECISION_OFFSET = 240.0   # seconds before close
MIN_PRINTS = 8            # require >=8 prints in window (thin tickers excluded)
K = 8                     # last-K prints for EWMA + consistency
EWMA_ALPHA = 0.4          # EWMA weight on the most recent print
EWMA_THRESH = 0.50        # |signed EWMA| must exceed this (strongly directional)
CONSISTENCY = 0.70        # > this fraction of last-K prints same sign
MAX_ASK = 65.0            # that side's ask must be < this (continuation entry)
N_BOOT = 2000
SEED = 1234


def fee_cents(price_cents: float) -> float:
    p = price_cents / 100.0
    return math.ceil(0.07 * 1 * p * (1.0 - p))


def build_sign_series(trades_path):
    """{ticker: sorted list of (ts, signed_weight)} ; sign +1 yes-taker, -1 no-taker."""
    series = defaultdict(list)
    with open(trades_path) as fh:
        for line in fh:
            if not line.strip():
                continue
            t = parse_trade(line)
            if t is None:
                continue
            sgn = 1.0 if t["taker_side"] == "yes" else -1.0
            series[t["ticker"]].append((t["ts"], sgn, t["count"]))
    for tk in series:
        series[tk].sort(key=lambda x: x[0])
    return series


def signal_for_ticker(prints, decision_ts):
    """prints: list of (ts, sign, weight). Returns (fires, signal_side, ewma, consist, n)
    using only prints with ts <= decision_ts. signal_side in {'yes','no'} = the side
    persistent flow is pushing toward."""
    window = [(s, w) for ts, s, w in prints if ts <= decision_ts]
    n = len(window)
    if n < MIN_PRINTS:
        return False, None, 0.0, 0.0, n
    lastK = window[-K:]
    # signed EWMA weighted by count_fp, recency-weighted
    ewma = 0.0
    for s, w in lastK:
        ewma = EWMA_ALPHA * (s * math.copysign(1.0, w) if w else s) + (1 - EWMA_ALPHA) * ewma
    # consistency: fraction of last-K prints matching the dominant sign
    pos = sum(1 for s, _ in lastK if s > 0)
    neg = len(lastK) - pos
    dom = 1 if pos >= neg else -1
    consist = max(pos, neg) / len(lastK)
    fires = abs(ewma) > EWMA_THRESH and consist > CONSISTENCY
    if not fires:
        return False, None, ewma, consist, n
    # sign of EWMA dictates the continuation side; require agreement with dom for cleanliness
    sig_dir = 1 if ewma > 0 else -1
    if sig_dir != dom:
        return False, None, ewma, consist, n
    side = "yes" if sig_dir > 0 else "no"
    return True, side, ewma, consist, n


def main():
    print("Loading trade sign series...", flush=True)
    series = build_sign_series(TRADES)
    print(f"  {len(series)} tickers with trades", flush=True)

    # PHASE 1 (trade-only, cheap): find which tickers fire the signal.
    # Only those need book reconstruction -> avoids parsing all 9.77M frames.
    fired = {}  # ticker -> (side, n_prints, decision_ep, close_ep)
    n_eval = 0
    n_no_signal = 0
    for tk, prints in series.items():
        try:
            close_ep = close_epoch_from_ticker(tk)
        except Exception:
            continue
        decision_ep = close_ep - DECISION_OFFSET
        fires, side, ewma, consist, n = signal_for_ticker(prints, decision_ep)
        n_eval += 1
        if not fires:
            n_no_signal += 1
            continue
        fired[tk] = (side, n, decision_ep, close_ep)
    print(f"  signal fired on {len(fired)} / {n_eval} tickers", flush=True)

    if not fired:
        print("\nNo signal ever fired -> INCONCLUSIVE")
        return {"verdict": "INCONCLUSIVE", "n": 0, "n_eval": n_eval}

    # PHASE 2: load frames ONLY for fired tickers (one streamed pass, pre-filtered).
    print(f"Loading frames for {len(fired)} fired tickers...", flush=True)
    frames = load_frames_for_tickers(FRAMES, set(fired.keys()))
    print(f"  {len(frames)} fired tickers have frames", flush=True)

    rows = []          # per-trade outcome rows
    print_counts = []
    n_no_book = 0
    n_ask_too_high = 0
    n_no_label = 0

    for tk, (side, n, decision_ep, close_ep) in fired.items():
        if tk not in frames:
            continue
        print_counts.append(n)

        fr = frames[tk]
        # reliable book at decision -> entry prices (no look-ahead)
        yb, ya = reliable_nbbo_at(fr, decision_ep)
        if yb is None or ya is None:
            n_no_book += 1
            continue
        # YES ask = ya ; NO ask = 100 - YES bid
        no_ask = 100.0 - yb
        yes_ask = ya
        # signal-side ask
        sig_ask = yes_ask if side == "yes" else no_ask
        if not (sig_ask < MAX_ASK):
            n_ask_too_high += 1
            continue

        # terminal label: reliable book mid at close (independent build)
        cb, ca = reliable_nbbo_at(fr, close_ep)
        if cb is None or ca is None:
            n_no_label += 1
            continue
        term_mid = (cb + ca) / 2.0
        yes_wins = term_mid > 50.0

        # ---- CONTINUATION leg: take signal side at its ask ----
        if side == "yes":
            cont_entry = yes_ask
            cont_win = yes_wins
        else:
            cont_entry = no_ask
            cont_win = (not yes_wins)
        if cont_entry is None or cont_entry <= 0 or cont_entry >= 100:
            continue
        cont_payoff = (100.0 - cont_entry) if cont_win else (-cont_entry)
        cont_net = cont_payoff - fee_cents(cont_entry)

        # ---- FADE leg: take the OPPOSITE side at its ask ----
        fade_side = "no" if side == "yes" else "yes"
        fade_entry = no_ask if fade_side == "no" else yes_ask
        if fade_entry is None or fade_entry <= 0 or fade_entry >= 100:
            fade_net = None
        else:
            fade_win = (not yes_wins) if fade_side == "no" else yes_wins
            fade_payoff = (100.0 - fade_entry) if fade_win else (-fade_entry)
            fade_net = fade_payoff - fee_cents(fade_entry)

        rows.append({
            "ticker": tk, "side": side, "n_prints": n,
            "cont_net": cont_net, "fade_net": fade_net,
            "cont_entry": cont_entry, "yes_wins": yes_wins,
        })

    print("\n=== Funnel ===", flush=True)
    print(f"  evaluated tickers (>=frames):     {n_eval}")
    print(f"  no signal fired:                  {n_no_signal}")
    print(f"  signal fired, no reliable book:   {n_no_book}")
    print(f"  signal fired, ask >= {MAX_ASK:.0f}:        {n_ask_too_high}")
    print(f"  signal fired, no reliable label:  {n_no_label}")
    print(f"  TRADED rows:                      {len(rows)}")

    if not rows:
        print("\nNo tradeable rows -> INCONCLUSIVE/DATA_GAP")
        return {"verdict": "INCONCLUSIVE", "n": 0}

    cont = [r["cont_net"] for r in rows]
    fade = [r["fade_net"] for r in rows if r["fade_net"] is not None]
    tickers = sorted({r["ticker"] for r in rows})

    if print_counts:
        pc = sorted(print_counts)
        print(f"\nprint-count dist (in-window, fired): min={pc[0]} "
              f"p50={pc[len(pc)//2]} max={pc[-1]} mean={sum(pc)/len(pc):.1f}")

    mean_cont = sum(cont) / len(cont)
    mean_fade = sum(fade) / len(fade) if fade else float("nan")
    print(f"\nCONTINUATION mean net = {mean_cont:+.3f} c/contract  (n={len(cont)})")
    print(f"FADE         mean net = {mean_fade:+.3f} c/contract  (n={len(fade)})")
    print(f"unique tickers (cluster units) = {len(tickers)}")

    # block bootstrap clustered by ticker
    import random
    rng = random.Random(SEED)
    by_tk_cont = defaultdict(list)
    for r in rows:
        by_tk_cont[r["ticker"]].append(r["cont_net"])
    tk_list = list(by_tk_cont.keys())

    def boot(metric_by_tk):
        keys = list(metric_by_tk.keys())
        means = []
        for _ in range(N_BOOT):
            pooled = []
            for _ in range(len(keys)):
                k = keys[rng.randrange(len(keys))]
                pooled.extend(metric_by_tk[k])
            if pooled:
                means.append(sum(pooled) / len(pooled))
        means.sort()
        lo = means[int(0.025 * len(means))]
        hi = means[int(0.975 * len(means))]
        return lo, hi

    cont_lo, cont_hi = boot(by_tk_cont)
    print(f"\nCONTINUATION 95% block-bootstrap CI = [{cont_lo:+.3f}, {cont_hi:+.3f}]")

    by_tk_fade = defaultdict(list)
    for r in rows:
        if r["fade_net"] is not None:
            by_tk_fade[r["ticker"]].append(r["fade_net"])
    if by_tk_fade:
        fade_lo, fade_hi = boot(by_tk_fade)
        print(f"FADE         95% block-bootstrap CI = [{fade_lo:+.3f}, {fade_hi:+.3f}]")
    else:
        fade_lo = fade_hi = float("nan")

    # verdict on continuation (the named hypothesis)
    if cont_lo > 0:
        verdict = "EDGE"
    elif cont_hi < 0:
        verdict = "NO_EDGE"
    else:
        verdict = "NO_EDGE" if mean_cont <= 0 else "INCONCLUSIVE"
    print(f"\nVERDICT (continuation) = {verdict}")

    return {
        "verdict": verdict, "n": len(cont), "n_tickers": len(tickers),
        "mean_cont": mean_cont, "cont_lo": cont_lo, "cont_hi": cont_hi,
        "mean_fade": mean_fade, "fade_lo": fade_lo, "fade_hi": fade_hi,
    }


if __name__ == "__main__":
    out = main()
    print("\nRESULT_JSON", json.dumps(out, default=str))
