"""09_dog_didnt_bark — take the ask only when the book PROVES it hasn't repriced.

PRE-REGISTRATION (stated BEFORE any data was run)
-------------------------------------------------
HYPOTHESIS: after a same-asset spot move, asks that have NOT repriced are
provably stale (a repriced ask emits orderbook_delta events; silence = the
pre-move resting order) and can be lifted for positive expectancy. The
unconditional average that killed lead-lag-as-taker was dominated by repriced
asks — this evaluator conditions on DELTA-SILENCE, i.e. proof the standing ask
is the pre-move resting order.

(a) COUNTERPARTY: set-and-forget resting makers — manual quoters and slow
    refresh-loop bots (30-60s timers; Kalshi has no auto-pull). They accept
    occasional pickoffs as the cost of earning spread; population refreshes
    every window. A ~1s bot is plenty fast against a 30-60s refresh timer.

(b) SIGNAL + ENTRY/EXIT (exact):
    TRIGGER: |log spot move over 20s| >= 0.8 * sigma_remaining, where
      sigma_remaining = rv_5s * sqrt(ttc_s / 5) (rv_5s = stdev of per-5s log
      returns over a trailing 300s window, scripts.research.fairvalue_extract
      `_realized_vol`). Final 120s to close excluded from the headline
      (endgame = informed-flow territory, reported separately). Close time via
      `close_epoch_from_ticker`. Trigger scan on a 1s grid; rising-edge
      debounce + 20s refractory between counted triggers (implementation
      choice, fixed before running).
    ABSENCE CONFIRMATION at t_conf = t_trigger + 5s:
      * ZERO orderbook_delta events touching the to-be-lifted ask side with
        EVENT-TIME ts_ms in (t_trigger, t_trigger + 5s]. Spot-up lifts the YES
        ask, which is resting NO-side orders -> silence required on side="no";
        spot-down buys NO (lifts 100 - yes_bid), resting YES-side orders ->
        silence required on side="yes". An orderbook_snapshot (collector
        re-anchor) counts as touching BOTH sides at its arrival time —
        conservative: we cannot prove silence across a re-snapshot.
      * NO trade print in that market with event-time in (t_conf-10s, t_conf].
    ENTRY: TAKER lift the stale ask in the spot-move direction, 1 contract, at
      the ask standing at confirmation time per `reliable_nbbo_timeline`
      (snapshot-anchored, never-crossed). The book must be FRESH-reliable at
      t_conf (the latest frame at-or-before t_conf produced a reliable
      emission — no acting on a quote that newer skipped frames invalidated)
      and the silent side present (price in (0,100)). At most 1 headline entry
      per window (one observation per window — no pseudo-replication).
    EXIT: hold to settlement (lifecycle `determined`) = HEADLINE.
      +120s reliable-mid markout reported as DIAGNOSTIC ONLY (no exit leg —
      early-exit grids are dead).

(c) FEE MATH: taker entry fee = 7*p*(1-p) cents at the entry price via
    `kalshi_fee_per_contract_cents` (amortized large-order rate). Settlement is
    free. The 1-contract ceil-to-1c rounding penalty is printed as a
    sensitivity line, not the headline.

(d) KILL CRITERION (numeric, pre-registered):
    * n >= 30 triggered entries over the 3-day frames pass AND day-bootstrap
      (`day_bootstrap_ci`, resample DAYS never rows) 95% CI lower bound of net
      settlement PnL <= 0  -> DEAD (NO_EDGE).
    * n >= 30 AND CI lower bound > 0 -> EDGE_CANDIDATE.
    * FUNNEL: triggers -> absence-confirmed -> quotable book -> entered; if
      absence-confirmed rate < 5% of triggers -> CAPACITY-DEAD regardless of
      PnL sign.
    * If n < 30: INCONCLUSIVE-capacity with the funnel — do NOT stretch to
      more frames days.

ASSETS: BTC/ETH/SOL/XRP/DOGE (Coinbase spot exists; HYPE/BNB excluded).
DATA: CR/frames (<=3 sealed days, default 2026-06-03..05, zstd -dc subprocess
pipe — never _zst_lines on frames), CR/trades (print-silence), CR/lifecycle
(settlement truth), CR/coinbase_ticker (spot). Single streaming pass over
frames per day; per-market state = buffered frames for currently-open
determined windows only (path_alpha_probe stream_windows pattern, as in
scripts/research/stepfour/liquidity_vacuum_dimes.py).

LOOK-AHEAD DISCIPLINE / HONESTY NOTES
-------------------------------------
- Decisions at t_conf use only quotes/frames with arrival <= t_conf and
  trigger math on spot strictly at/before the grid second.
- Delta/print silence uses EVENT-TIME ts/ts_ms (per kalshi_ws protocol facts:
  deltas carry event-time; snapshots carry only arrival). A delta whose event
  time is in the silence window but whose ARRIVAL is after t_conf would be
  unknowable live; using it here only BLOCKS entries (slightly optimistic
  selection at ~0.5s typical wire lag — caveated).
- reliable_nbbo refuses unanchored/crossed books; the usable-window funnel is
  printed so filter bias is visible.
- Windows are evaluated in the day file matching their close-date (UTC); a
  window straddling midnight may be missing pre-midnight frames (unquotable
  early -> shows in funnel). Affects ~1-2% of windows.

Usage:
  python3 -m scripts.research.genhunt.09_dog_didnt_bark \
      --corpus ~/kalshi-research-data/fairvalue \
      [--days 2026-06-03,2026-06-04,2026-06-05]
(module name starts with a digit — run via runpy/path, see __main__ guard)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.early_exit_backtest import reliable_nbbo_timeline  # noqa: E402
from scripts.research.fairvalue_extract import (  # noqa: E402
    _realized_vol,
    _spot_at,
    load_spot,
)
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _epoch,
    close_epoch_from_ticker,
    load_determined,
)
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)
from scripts.research.zstd_stream import checked_stream_lines  # noqa: E402  (repo root added to sys.path above)

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE")   # Coinbase spot exists
MOVE_S = 20.0            # spot-move lookback
TRIG_K = 0.8             # |move| >= TRIG_K * sigma_remaining
DELTA_SILENCE_S = 5.0    # zero side-touch deltas post-move
PRINT_SILENCE_S = 10.0   # zero prints in market trailing window at t_conf
ENDGAME_S = 120.0        # final 120s excluded from headline
REFRACTORY_S = 20.0      # min spacing between counted triggers
WINDOW_LEN_S = 900.0     # 15M windows
MARKOUT_S = 120.0        # diagnostic markout horizon
MIN_N = 30               # kill-criterion floor
CONF_RATE_FLOOR = 0.05   # absence-confirmed / triggers capacity floor
FLUSH_EVERY = 50000
CLOSE_MARGIN_S = 180.0
WARMUP_S = 320.0         # 300s vol window + 20s move lookback

# matches the escaped ticker inside the _raw envelope without json-parsing
_TK_RE = re.compile(r'market_ticker\\":\\"(KX[A-Z]+15M-[A-Z0-9-]+?)\\"')


# ---------------------------------------------------------------------------
# streaming IO (memory-bounded — NEVER materialize a frames day)
# ---------------------------------------------------------------------------


def _stream_lines(path: str):
    """Yield lines from .jsonl/.jsonl.zst via a zstd -dc subprocess pipe
    (phase1b._zst_lines buffers the whole decompressed file — forbidden on
    multi-GB frames days)."""
    # Delegates to the shared CHECKED reader (ticket 86bbvrx1t): the previous
    # body discarded zstd's exit code, so a truncated day silently yielded a
    # prefix and every downstream count was short without saying so.
    yield from checked_stream_lines(path, require_nonempty=False,
                                    skip_blank=False)


def load_determined_for_days(lifecycle_dir: str, days: list) -> dict:
    """load_determined restricted to the requested day partitions (+1 day for
    midnight straddlers); falls back to the whole tree if per-day dirs are
    absent (pattern from stepfour/liquidity_vacuum_dimes)."""
    want: set = set()
    for d in days:
        dt = date.fromisoformat(d)
        for dd in (dt, dt + timedelta(days=1)):
            want.add((f"{dd.year:04d}", f"{dd.month:02d}", f"{dd.day:02d}"))
    day_dirs = []
    for y, m, dd in sorted(want):
        p = os.path.join(lifecycle_dir, f"year={y}", f"month={m}", f"day={dd}")
        if os.path.isdir(p):
            day_dirs.append(p)
    if not day_dirs:
        return load_determined(lifecycle_dir, ASSETS)
    merged: dict = {}
    for p in day_dirs:
        merged.update(load_determined(p, ASSETS))
    return merged


def load_spot_days(cb_root: str, days: list) -> dict:
    """Merge per-day `load_spot` timelines into one {asset: (secs, px)}."""
    acc: dict = defaultdict(dict)
    for d in days:
        y, m, dd = d.split("-")
        p = os.path.join(cb_root, f"year={y}", f"month={m}", f"day={dd}")
        if not os.path.isdir(p):
            print(f"[dog] WARN: no coinbase_ticker partition for {d}")
            continue
        for a, (secs, px) in load_spot(p).items():
            acc[a].update(zip(secs, px))
    out = {}
    for a, dd in acc.items():
        secs = sorted(dd)
        out[a] = (secs, [dd[s] for s in secs])
    return out


def load_day_trades(path: str, want: set) -> dict:
    """{ticker: [(event_epoch, yes_price_c)]} sorted by EVENT time (ts_ms) —
    trades files are arrival-ordered, NOT time-sorted; sort per ticker."""
    out: dict = defaultdict(list)
    for line in _stream_lines(path):
        if not line.strip():
            continue
        m = _TK_RE.search(line)
        if m is None or m.group(1) not in want:
            continue
        try:
            env = json.loads(line)
            msg = json.loads(env["_raw"])["msg"]
            ev = float(msg["ts_ms"]) / 1000.0
            yc = float(msg["yes_price_dollars"]) * 100.0
        except (ValueError, KeyError, TypeError):
            continue
        out[m.group(1)].append((ev, yc))
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


def stream_day_frames(path: str, todays: dict, on_window) -> tuple:
    """Stream one bronze frames day in arrival order, buffering raw frames ONLY
    for currently-open windows that CLOSE today; finalize each window once the
    stream clock passes det_ts + margin, hand (tk, det, buf) to on_window, free
    the buffer. Returns (n_lines, n_final)."""
    buffers: dict = defaultdict(list)
    done: set = set()
    n_lines = n_final = 0
    stream_ts = 0.0

    def _finalize(tk: str) -> None:
        nonlocal n_final
        buf = buffers[tk]
        buf.sort(key=lambda x: x[0])
        on_window(tk, todays[tk], buf)
        n_final += 1

    for line in _stream_lines(path):
        if not line.strip():
            continue
        n_lines += 1
        m = _TK_RE.search(line)
        if m is None:
            continue
        tk = m.group(1)
        if tk in done or tk not in todays:
            continue
        try:
            env = json.loads(line)
            inner = json.loads(env["_raw"])
            ts = _epoch(env["_wire_recv_ts"])
        except (ValueError, KeyError):
            continue
        stream_ts = ts if ts > stream_ts else stream_ts
        buffers[tk].append((ts, inner))
        if n_lines % FLUSH_EVERY == 0:
            cut = stream_ts - CLOSE_MARGIN_S
            for t in [t for t in list(buffers)
                      if float(todays[t]["det_ts"]) < cut]:
                _finalize(t)
                done.add(t)
                del buffers[t]
    for t in list(buffers):
        _finalize(t)
        done.add(t)
        del buffers[t]
    return n_lines, n_final


# ---------------------------------------------------------------------------
# per-window evaluation
# ---------------------------------------------------------------------------


def _count_in(sorted_ts: list, lo: float, hi: float) -> int:
    """# of events with lo < t <= hi."""
    return bisect_right(sorted_ts, hi) - bisect_right(sorted_ts, lo)


def evaluate_window(day: str, tk: str, det: dict, buf: list, trades: list,
                    spot_tl, coll: dict) -> None:
    asset = det["asset"]
    try:
        close_ts = close_epoch_from_ticker(tk)
    except (ValueError, IndexError):
        coll["n_bad_close"] += 1
        return
    open_ts = close_ts - WINDOW_LEN_S
    coll["n_windows"] += 1

    tl = reliable_nbbo_timeline(buf)
    if not tl:
        coll["n_windows_no_reliable_book"] += 1
        return
    rel_ts = [p[0] for p in tl]
    frame_ts = [b[0] for b in buf]

    # side-touch event times (EVENT-time on deltas; arrival on snapshots,
    # snapshots conservatively touch BOTH sides)
    touch = {"yes": [], "no": []}
    for recv, inner in buf:
        typ = inner.get("type")
        if typ == "orderbook_delta":
            msg = inner.get("msg", {})
            ev = msg.get("ts_ms")
            try:
                ev = float(ev) / 1000.0 if ev is not None else (
                    _epoch(msg["ts"]) if "ts" in msg else recv)
            except (ValueError, KeyError, TypeError):
                ev = recv
            s = msg.get("side")
            if s in touch:
                touch[s].append(ev)
        elif typ == "orderbook_snapshot":
            touch["yes"].append(recv)
            touch["no"].append(recv)
    touch["yes"].sort()
    touch["no"].sort()
    trade_ts = [t[0] for t in trades]

    settle_yes = det["result"] == "yes"
    entered = {"headline": False, "endgame": False}

    def _book_fresh_at(t: float):
        """(bid, ask) iff the latest frame <= t produced a reliable emission."""
        j = bisect_right(frame_ts, t) - 1
        i = bisect_right(rel_ts, t) - 1
        if j < 0 or i < 0 or rel_ts[i] != frame_ts[j]:
            return None
        return tl[i][1], tl[i][2]

    def _markout_mid(t: float):
        i = bisect_left(rel_ts, t)
        while i < len(rel_ts) and rel_ts[i] <= close_ts:
            _, b, a = tl[i]
            if b is not None and a is not None:
                return (b + a) / 2.0
            i += 1
        return None

    def _process_trigger(t_trig: float, direction: int) -> None:
        t_conf = t_trig + DELTA_SILENCE_S
        bucket = "endgame" if (close_ts - t_conf) <= ENDGAME_S else "headline"
        coll[f"trig_{bucket}"] += 1
        coll["trig_by_asset"][(asset, bucket)] += 1

        lifted_side = "no" if direction > 0 else "yes"   # spot-up lifts YES ask
        if _count_in(touch[lifted_side], t_trig, t_trig + DELTA_SILENCE_S) > 0:
            return
        if _count_in(trade_ts, t_conf - PRINT_SILENCE_S, t_conf) > 0:
            return
        coll[f"conf_{bucket}"] += 1

        bk = _book_fresh_at(t_conf)
        if bk is None:
            return
        bid, ask = bk
        if direction > 0:
            if ask is None or not (0.0 < ask < 100.0):
                return
            price = float(ask)
            win = settle_yes
        else:
            if bid is None or not (0.0 < (100.0 - bid) < 100.0):
                return
            price = 100.0 - float(bid)
            win = not settle_yes
        coll[f"quot_{bucket}"] += 1

        if entered[bucket]:
            return
        entered[bucket] = True
        fee = kalshi_fee_per_contract_cents(price)
        gross = (100.0 if win else 0.0) - price
        mid = _markout_mid(t_conf + MARKOUT_S)
        if mid is None:
            mk = None
        else:
            mk = (mid - price) if direction > 0 else ((100.0 - mid) - price)
        coll["entries"].append({
            "day": day, "ticker": tk, "asset": asset, "bucket": bucket,
            "dir": "up" if direction > 0 else "down", "t_conf": t_conf,
            "ttc_s": close_ts - t_conf, "price_c": price, "fee_c": fee,
            "won": win, "gross_c": gross, "net_c": gross - fee,
            "net_minfee_c": gross - math.ceil(fee), "markout_c": mk,
        })

    # ---- trigger scan: 1s grid, rising edge + refractory --------------------
    have_spot = False
    prev_on = False
    last_trig = -1e18
    t = int(open_ts + WARMUP_S)
    end = int(close_ts)
    while t < end:
        s_now = _spot_at(spot_tl, t)
        s_prev = _spot_at(spot_tl, t - MOVE_S)
        if not s_now or not s_prev or s_prev <= 0:
            prev_on = False
            t += 1
            continue
        have_spot = True
        rv = _realized_vol(spot_tl, t)
        if rv is None or rv <= 0:
            prev_on = False
            t += 1
            continue
        ttc = close_ts - t
        sigma_rem = rv * math.sqrt(max(ttc, 1.0) / 5.0)
        mv = math.log(s_now / s_prev)
        on = abs(mv) >= TRIG_K * sigma_rem
        if on and not prev_on and (t - last_trig) >= REFRACTORY_S:
            last_trig = t
            _process_trigger(float(t), 1 if mv > 0 else -1)
        prev_on = on
        t += 1
    if not have_spot:
        coll["n_windows_no_spot"] += 1


# ---------------------------------------------------------------------------
# reporting / verdict
# ---------------------------------------------------------------------------


def _bootstrap(entries: list, col: str):
    import pandas as pd  # local: keep module import light
    from scripts.research.fairvalue_model import day_bootstrap_ci
    df = pd.DataFrame(entries)
    return day_bootstrap_ci(df, col)


def _report_bucket(label: str, rows: list) -> dict:
    print(f"\n== {label} (n={len(rows)}) ==")
    out = {"n": len(rows)}
    if not rows:
        return out
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r["net_c"])
    for d in sorted(by_day):
        v = by_day[d]
        print(f"  {d}: n={len(v):3d}  mean_net={sum(v)/len(v):+7.2f}c")
    by_asset = defaultdict(list)
    for r in rows:
        by_asset[r["asset"]].append(r["net_c"])
    for a in sorted(by_asset):
        v = by_asset[a]
        print(f"  {a:5s}: n={len(v):3d}  mean_net={sum(v)/len(v):+7.2f}c")
    gross = sum(r["gross_c"] for r in rows) / len(rows)
    net = sum(r["net_c"] for r in rows) / len(rows)
    netmf = sum(r["net_minfee_c"] for r in rows) / len(rows)
    wr = sum(1 for r in rows if r["won"]) / len(rows)
    px = sum(r["price_c"] for r in rows) / len(rows)
    print(f"  WR={wr:.3f}  avg_entry_px={px:.1f}c  mean gross={gross:+.2f}c  "
          f"net={net:+.2f}c  net(1c-min-fee sens)={netmf:+.2f}c")
    mks = [r["markout_c"] for r in rows if r["markout_c"] is not None]
    if mks:
        print(f"  +{int(MARKOUT_S)}s markout (DIAGNOSTIC, gross): "
              f"n={len(mks)} mean={sum(mks)/len(mks):+.2f}c")
    n_days = len(by_day)
    if n_days >= 2:
        mean, lo, hi = _bootstrap(rows, "net_c")
        print(f"  day-bootstrap 95% CI of net PnL/ct: mean={mean:+.3f}c  "
              f"[{lo:+.3f}, {hi:+.3f}]  (n_days={n_days})")
        out.update(mean=mean, lo=lo, hi=hi, n_days=n_days)
    else:
        print(f"  n_days={n_days} < 2 — day-bootstrap degenerate, no CI")
        out.update(n_days=n_days)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.path.expanduser(
        "~/kalshi-research-data/fairvalue"))
    ap.add_argument("--days", default="2026-06-03,2026-06-04,2026-06-05")
    a = ap.parse_args(argv)
    days = [d.strip() for d in a.days.split(",") if d.strip()]
    if len(days) > 3:
        raise SystemExit("runtime guardrail: frames <= 3 sealed days")
    for d in days:
        if not os.path.exists(os.path.join(a.corpus, f".done_{d}")):
            raise SystemExit(f"day {d} not sealed (no .done_{d} marker)")

    print(f"[dog] days={days}")
    determined = load_determined_for_days(
        os.path.join(a.corpus, "lifecycle"), days)
    print(f"[dog] determined windows (5 spot-assets, days+1): {len(determined)}")
    spot = load_spot_days(os.path.join(a.corpus, "coinbase_ticker"), days)
    print(f"[dog] spot assets loaded: {sorted(spot)}")

    coll: dict = defaultdict(int)
    coll["entries"] = []
    coll["trig_by_asset"] = defaultdict(int)

    for day in days:
        # windows are owned by the UTC day of their CLOSE
        todays = {}
        for tk, det in determined.items():
            if det["asset"] not in spot:
                continue
            try:
                cts = close_epoch_from_ticker(tk)
            except (ValueError, IndexError):
                continue
            if datetime.fromtimestamp(cts, tz=timezone.utc).strftime(
                    "%Y-%m-%d") == day:
                det = dict(det)
                det["_close_ts"] = cts
                todays[tk] = det
        fpath = os.path.join(a.corpus, "frames", f"day={day}.jsonl.zst")
        tpath = os.path.join(a.corpus, "trades", f"day={day}.jsonl.zst")
        if not os.path.exists(fpath):
            print(f"[dog] WARN missing frames file {fpath} — skipping day")
            continue
        trades = load_day_trades(tpath, set(todays)) if os.path.exists(tpath) \
            else {}
        print(f"[dog] {day}: {len(todays)} windows close today; "
              f"{sum(len(v) for v in trades.values())} prints loaded")

        def _on_window(tk, det, buf, _day=day, _trades=trades):
            evaluate_window(_day, tk, det, buf, _trades.get(tk, []),
                            spot[det["asset"]], coll)

        n_lines, n_final = stream_day_frames(fpath, todays, _on_window)
        print(f"[dog] {day}: streamed {n_lines} frame lines, "
              f"finalized {n_final} windows")

    # ---------------- funnel -------------------------------------------------
    print("\n================ FUNNEL (headline = outside final 120s) ========")
    print(f"  windows evaluated:        {coll['n_windows']}")
    print(f"   - no reliable book ever: {coll['n_windows_no_reliable_book']}")
    print(f"   - no spot coverage:      {coll['n_windows_no_spot']}")
    trig = coll["trig_headline"]
    conf = coll["conf_headline"]
    quot = coll["quot_headline"]
    head = [r for r in coll["entries"] if r["bucket"] == "headline"]
    endg = [r for r in coll["entries"] if r["bucket"] == "endgame"]
    print(f"  triggers (headline):      {trig}")
    rate = (conf / trig) if trig else float("nan")
    print(f"  absence-confirmed:        {conf}  (rate={rate:.3f}, "
          f"floor={CONF_RATE_FLOOR})")
    print(f"  quotable book:            {quot}")
    print(f"  entered (1/window cap):   {len(head)}")
    print(f"  [endgame: trig={coll['trig_endgame']} conf={coll['conf_endgame']}"
          f" quot={coll['quot_endgame']} entered={len(endg)}]")
    for (asset, bkt), n in sorted(coll["trig_by_asset"].items()):
        if bkt == "headline":
            print(f"    triggers {asset}: {n}")

    stats = _report_bucket("HEADLINE — hold to settlement, net of taker fee",
                           head)
    _report_bucket("ENDGAME (<=120s to close) — reported separately, "
                   "NOT in verdict", endg)

    # ---------------- verdict vs pre-registered criterion --------------------
    print("\n================ VERDICT =======================================")
    n = stats["n"]
    if trig >= 20 and rate < CONF_RATE_FLOOR:
        verdict = "NO_EDGE (CAPACITY-DEAD: absence-confirmed rate "
        verdict += f"{rate:.3f} < {CONF_RATE_FLOOR} of {trig} triggers)"
    elif n < MIN_N:
        verdict = (f"INCONCLUSIVE-capacity (n={n} < {MIN_N} entries; funnel "
                   f"above — do not stretch to more days)")
    elif "lo" not in stats:
        verdict = f"INCONCLUSIVE (n={n} but <2 trading days — no honest CI)"
    elif stats["lo"] <= 0:
        verdict = (f"DEAD / NO_EDGE (n={n} >= {MIN_N}, day-bootstrap CI lower "
                   f"bound {stats['lo']:+.3f}c <= 0)")
    else:
        verdict = (f"EDGE_CANDIDATE (n={n} >= {MIN_N}, day-bootstrap CI "
                   f"[{stats['lo']:+.3f}, {stats['hi']:+.3f}]c lower bound > 0)")
    print(f"  {verdict}")


if __name__ == "__main__":
    main()
