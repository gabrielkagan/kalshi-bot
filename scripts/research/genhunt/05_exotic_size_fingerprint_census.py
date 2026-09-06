#!/usr/bin/env python3
"""05: Exotic-size fingerprint census — follow informed bots, fade fixed-budget bots.

PRE-REGISTRATION (frozen before any test-day data was examined)
================================================================
HYPOTHESIS: count_fp is a participant fingerprint. Recurring exotic fractional
sizes (5.40, 9.85, 19.78, 13.66) are unique unattended bots; recurring round
large sizes (300, 500) are distinct classes with their own information content.
A per-fingerprint settlement-alpha ledger built on early (TRAIN) days yields a
tradeable label on later (TEST) prints.

COUNTERPARTY:
  FADE branch  — the fixed-dollar-budget bots themselves: unattended automation
                 executing a fixed mandate. If their gross markout is negative
                 they are a recurring subsidy to quote against.
  FOLLOW branch — slow quoters who leave stale prices after an informed
                 fingerprint prints (they re-quote on spot ticks, not tape
                 identity).

SIGNAL / ENTRY / EXIT:
  CENSUS on TRAIN days 2026-05-30..2026-06-03: fingerprint key = exact count_fp
  string (per-asset AND pooled; both reported; POOLED labels are the tradeable
  set). For every fingerprint with >=150 train prints compute mean
  settlement-vs-price gross alpha (taker side) with day-bootstrap CI
  (day_bootstrap_ci). Labels FROZEN after train:
    INFORMED   := train CI lower bound > +2c
    UNINFORMED := train CI upper bound < -2c
  TRADE on TEST days 2026-06-04..2026-06-10:
    FOLLOW: on an INFORMED print -> copy the same side as TAKER within 2s at
      the current ask, only if the train alpha estimate clears
      7*p*(1-p) + 1c at the entry price. Ask source: print-price+1c proxy on
      all test days (HEADLINE) + frames NBBO on <=3 sealed test days
      (06-04..06-06) as the realism subset. Report both; flag divergence
      >1c/ct.
    FADE: on an UNINFORMED print -> join the opposite side MAKER at best.
      Mirror PnL = taker GROSS (gotcha 1; maker pays no fee). No-look-ahead
      fill: we join AFTER the trigger print, so the fill is the NEXT print of
      the same fingerprint in the same window (strictly later ts_ms); PnL/ct =
      -(that fill print's taker gross alpha). The optimistic trigger-print
      mirror is reported as a sensitivity only.
  One position per window (ticker) per branch. EXIT at settlement
  (load_determined). Endgame entries (<120s to close, close_epoch_from_ticker)
  reported separately; HEADLINE excludes them.

FEES: taker 7*p*(1-p) cents via kalshi_fee_per_contract_cents; maker zero.

KILL CRITERIA (numeric, per branch, independent):
  K1: fewer than 5 pooled fingerprints survive train census with the relevant
      CI bound beyond +/-2c (>=5 INFORMED needed for FOLLOW; >=5 UNINFORMED
      for FADE) -> branch DEAD.
  K2: test-period day-bootstrap 95% CI lower bound of net PnL/ct <= 0 over
      >=7 days and >=100 events -> DEAD.
  K3: train-labeled informedness sign persists out-of-sample for <60% of
      traded fingerprints -> DEAD even if pooled PnL is positive.
  EDGE_CANDIDATE requires surviving K1+K3 AND test net PnL/ct day-bootstrap CI
  lower bound > 0 with >=100 events.

DATA COVERAGE NOTE (updated 2026-06-11 pre-full-run): the 2026-06-10 session's
settlement cache stopped at 06-07 (lifecycle pull lagged). The cache was
rebuilt 2026-06-11 from lifecycle partitions now covering through 06-10, so
ALL 7 pre-registered TEST days (06-04..06-10) are settle-labeled and K2's
">=7 days" arm formally applies. No test-day set change — this is exactly the
pre-registered window.

RUN RESULT (2026-06-11, full pre-registered window; log /tmp/genhunt05_full2.log):
  FOLLOW = DEAD_K3 (sign persistence 13/35 = 37% < 60%; headline n=2622/7d
  net -3.85c/ct CI [-4.64,-3.05] would also have killed K2).
  FADE = DEAD_K2 (n=867 events / 7 labeled days, net -1.23c/ct,
  day-bootstrap CI [-2.99,+0.64], lower bound <= 0; K1 passed with 92
  survivors, K3 passed at exactly 52/86 = 60%).
  NBBO realism subset TRUNCATED: day 06-04 frames scan alone took 3183s
  (>2x the whole-run budget); killed before per-event summary printed.
  Cannot change either verdict — K3 is fill-price-independent and FADE
  pricing is maker-mirror (no NBBO input). Overall: NO_EDGE.

Usage:
  python3 scripts/research/genhunt/05_exotic_size_fingerprint_census.py --smoke
  python3 scripts/research/genhunt/05_exotic_size_fingerprint_census.py \
      [--realism-days 2026-06-04,2026-06-05,2026-06-06] [--no-realism]
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd

REPO = "/Users/gabrielkagan/Documents/kalshi-bot"
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    ASSETS,
    close_epoch_from_ticker,
    load_determined,
)
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)
from scripts.research.fairvalue_model import day_bootstrap_ci  # noqa: E402
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)

CR = "/Users/gabrielkagan/kalshi-research-data/fairvalue"
TRAIN_DAYS = ["2026-05-30", "2026-05-31", "2026-06-01", "2026-06-02", "2026-06-03"]
TEST_DAYS = ["2026-06-04", "2026-06-05", "2026-06-06", "2026-06-07",
             "2026-06-08", "2026-06-09", "2026-06-10"]
MIN_TRAIN_PRINTS = 150
LABEL_CI_CENTS = 2.0
ENDGAME_S = 120.0
FOLLOW_EXTRA_EDGE_C = 1.0
REALISM_TICKER_CAP_PER_DAY = 40

# fast-path field extraction from the bronze envelope line (escaped inner JSON);
# falls back to double json.loads when it doesn't match.
_TRADE_RE = re.compile(
    r'\\"market_ticker\\":\\"([^"\\]+)\\".*?'
    r'\\"yes_price_dollars\\":\\"([0-9.]+)\\".*?'
    r'\\"count_fp\\":\\"([0-9.]+)\\".*?'
    r'\\"taker_side\\":\\"(yes|no)\\".*?'
    r'\\"ts_ms\\":(\d+)'
)


def _asset_of(ticker: str):
    for a in ASSETS:
        if ticker.startswith(f"KX{a}15M-"):
            return a
    return None


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _zst_stream(path: str):
    """Line iterator over a .jsonl.zst via a zstd -dc subprocess pipe
    (memory-bounded; never buffers the whole file)."""
    proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE,
                            bufsize=1 << 20)
    _exhausted = False
    try:
        for raw in proc.stdout:
            yield raw.decode("utf-8", errors="replace")
        _exhausted = True
    finally:
        proc.stdout.close()
        proc.wait()
        # ticket 86bbvrx1t: only a true EOF can be a truncation; an early
        # break kills zstd with SIGPIPE, which is expected.
        assert_zstd_ok(proc, path, exhausted=_exhausted, require_nonempty=False)


def parse_trades_day(path: str, funnel: dict):
    """Yield (ticker, asset, yes_c, count_fp, taker_side, ts) per print."""
    n_re_fallback = 0
    for line in _zst_stream(path):
        if not line.strip():
            continue
        funnel["lines"] += 1
        m = _TRADE_RE.search(line)
        if m:
            tk, ypd, fp, side, ts_ms = m.groups()
        else:
            try:
                inner = json.loads(json.loads(line)["_raw"])
                msg = inner.get("msg", {})
                tk = msg.get("market_ticker", "")
                ypd = msg.get("yes_price_dollars")
                fp = msg.get("count_fp")
                side = msg.get("taker_side")
                ts_ms = msg.get("ts_ms")
                if not (tk and ypd and fp and side and ts_ms):
                    funnel["unparseable"] += 1
                    continue
                n_re_fallback += 1
            except Exception:
                funnel["unparseable"] += 1
                continue
        asset = _asset_of(tk)
        if asset is None:
            funnel["non_crypto15m"] += 1
            continue
        yes_c = float(ypd) * 100.0
        if not (0.0 < yes_c < 100.0):
            funnel["bad_price"] += 1
            continue
        funnel["prints"] += 1
        yield tk, asset, yes_c, fp, side, float(ts_ms) / 1000.0
    funnel["regex_fallback"] += n_re_fallback


def load_settlements():
    """{ticker: {'asset','result','det_ts','strike'}} cached (lifecycle is 14k
    small zst files; load_determined spawns one zstd per file ~minutes)."""
    cache = os.path.join(CR, "_tmp_genhunt05_determined.pkl")
    if os.path.exists(cache):
        with open(cache, "rb") as fh:
            return pickle.load(fh)
    t0 = time.time()
    det = load_determined(os.path.join(CR, "lifecycle"), ASSETS)
    print(f"[05] load_determined: {len(det):,} tickers in {time.time()-t0:.0f}s")
    with open(cache, "wb") as fh:
        pickle.dump(det, fh)
    return det


def taker_alpha_cents(yes_c: float, taker_side: str, result: str) -> float:
    """Gross settlement-vs-price alpha of the TAKER side of a print."""
    settle_yes = 100.0 if result == "yes" else 0.0
    if taker_side == "yes":
        return settle_yes - yes_c
    return (100.0 - settle_yes) - (100.0 - yes_c)


# --------------------------------------------------------------------------
# CENSUS (train pass)
# --------------------------------------------------------------------------

def census(det: dict, days, funnel: dict):
    by_fp = defaultdict(lambda: defaultdict(list))        # fp -> day -> [alpha]
    by_afp = defaultdict(lambda: defaultdict(list))       # (asset,fp) -> day -> [a]
    dayset = set(days)
    for d in days:
        path = os.path.join(CR, "trades", f"day={d}.jsonl.zst")
        if not os.path.exists(path):
            print(f"[05] WARN train day missing: {path}")
            continue
        for tk, asset, yes_c, fp, side, ts in parse_trades_day(path, funnel):
            day = _utc_day(ts)
            if day not in dayset:
                funnel["train_day_spill"] += 1
                continue
            info = det.get(tk)
            if info is None:
                funnel["train_no_settlement"] += 1
                continue
            a = taker_alpha_cents(yes_c, side, info["result"])
            by_fp[fp][day].append(a)
            by_afp[(asset, fp)][day].append(a)
            funnel["train_prints_labeled"] += 1
    return by_fp, by_afp


def census_table(by_key) -> pd.DataFrame:
    rows = []
    for key, daymap in by_key.items():
        n = sum(len(v) for v in daymap.values())
        if n < MIN_TRAIN_PRINTS:
            continue
        recs = [{"day": d, "alpha": a} for d, vs in daymap.items() for a in vs]
        df = pd.DataFrame(recs)
        mean, lo, hi = day_bootstrap_ci(df, "alpha")
        rows.append({"key": key, "n": n, "n_days": df["day"].nunique(),
                     "mean": mean, "ci_lo": lo, "ci_hi": hi})
    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values("mean", ascending=False).reset_index(drop=True)
    return out


# --------------------------------------------------------------------------
# TRADE pass (test)
# --------------------------------------------------------------------------

def trade_pass(det, informed, uninformed, train_mean, days, funnel):
    """Simulate both branches on test days. Returns (events_df, fp_test_alpha)."""
    events = []
    fp_test_alpha = defaultdict(lambda: defaultdict(list))  # fp -> day -> [alpha]
    dayset = set(days)
    label_set = set(informed) | set(uninformed)
    for d in days:
        path = os.path.join(CR, "trades", f"day={d}.jsonl.zst")
        if not os.path.exists(path):
            print(f"[05] WARN test day missing: {path}")
            continue
        per_ticker = defaultdict(list)
        for tk, asset, yes_c, fp, side, ts in parse_trades_day(path, funnel):
            day = _utc_day(ts)
            if day not in dayset:
                funnel["test_day_spill"] += 1
                continue
            per_ticker[tk].append((ts, fp, yes_c, side))
        for tk, prints in per_ticker.items():
            prints.sort(key=lambda x: x[0])               # ts_ms order per ticker
            info = det.get(tk)
            if info is None:
                funnel["test_windows_no_settlement"] += 1
                continue
            funnel["test_windows_settled"] += 1
            close = close_epoch_from_ticker(tk)
            result = info["result"]
            asset = _asset_of(tk)
            # out-of-sample alpha ledger for K3 persistence
            for ts, fp, yes_c, side in prints:
                if fp in label_set:
                    fp_test_alpha[fp][_utc_day(ts)].append(
                        taker_alpha_cents(yes_c, side, result))
            # ---- FOLLOW: first informed print clearing the fee threshold ----
            for ts, fp, yes_c, side in prints:
                if fp not in informed:
                    continue
                funnel["follow_triggers"] += 1
                side_px = yes_c if side == "yes" else 100.0 - yes_c
                entry = side_px + 1.0                      # print-price+1c proxy
                if entry > 99.0:
                    funnel["follow_untradeable_px"] += 1
                    continue
                fee = kalshi_fee_per_contract_cents(entry)
                if train_mean[fp] <= fee + FOLLOW_EXTRA_EDGE_C:
                    funnel["follow_below_threshold"] += 1
                    continue
                settle_side = (100.0 if result == "yes" else 0.0)
                if side == "no":
                    settle_side = 100.0 - settle_side
                events.append({
                    "branch": "FOLLOW", "ticker": tk, "asset": asset, "fp": fp,
                    "day": _utc_day(ts), "t": ts, "side": side,
                    "entry": entry, "fee": fee,
                    "net": settle_side - entry - fee,
                    "gross": settle_side - entry,
                    "endgame": (close - ts) < ENDGAME_S,
                })
                break                                      # one position/window
            # ---- FADE: trigger on uninformed print; fill = NEXT print of the
            # same fingerprint (no self-fill / no look-ahead) -----------------
            trig = None
            for i, (ts, fp, yes_c, side) in enumerate(prints):
                if fp in uninformed:
                    trig = (i, ts, fp, yes_c, side)
                    break
            if trig is not None:
                i0, t0, fp0, yes0, side0 = trig
                funnel["fade_triggers"] += 1
                fill = None
                for ts, fp, yes_c, side in prints[i0 + 1:]:
                    if fp == fp0 and ts > t0:
                        fill = (ts, yes_c, side)
                        break
                # optimistic sensitivity: mirror of the trigger print itself
                trig_alpha = taker_alpha_cents(yes0, side0, result)
                if fill is None:
                    funnel["fade_unfilled"] += 1
                    events.append({
                        "branch": "FADE_TRIGMIRROR_ONLY", "ticker": tk,
                        "asset": asset, "fp": fp0, "day": _utc_day(t0),
                        "t": t0, "side": side0, "entry": float("nan"),
                        "fee": 0.0, "net": -trig_alpha, "gross": -trig_alpha,
                        "endgame": (close - t0) < ENDGAME_S,
                    })
                else:
                    fts, fyes, fside = fill
                    fill_alpha = taker_alpha_cents(fyes, fside, result)
                    events.append({
                        "branch": "FADE", "ticker": tk, "asset": asset,
                        "fp": fp0, "day": _utc_day(fts), "t": fts,
                        "side": fside, "entry": float("nan"), "fee": 0.0,
                        "net": -fill_alpha, "gross": -fill_alpha,
                        "endgame": (close - fts) < ENDGAME_S,
                        "trig_mirror": -trig_alpha,
                    })
    return pd.DataFrame(events), fp_test_alpha


# --------------------------------------------------------------------------
# NBBO realism subset (FOLLOW taker ask on <=3 sealed test days)
# --------------------------------------------------------------------------

def realism_check(follow_df: pd.DataFrame, realism_days, funnel):
    from scripts.research.early_exit_backtest import reliable_nbbo_timeline
    from scripts.research.phase1b_real_price_economics import _epoch
    results = []
    for d in realism_days:
        path = os.path.join(CR, "frames", f"day={d}.jsonl.zst")
        marker = os.path.join(CR, f".done_{d}")
        if not (os.path.exists(path) and os.path.exists(marker)):
            print(f"[05] realism: skipping {d} (no sealed frames)")
            continue
        sub = follow_df[follow_df["day"] == d]
        if sub.empty:
            continue
        tickers = list(dict.fromkeys(sub.sort_values("t")["ticker"]))
        capped = len(tickers) > REALISM_TICKER_CAP_PER_DAY
        tickers = tickers[:REALISM_TICKER_CAP_PER_DAY]
        tkset = set(tickers)
        pat = "\n".join(tickers) + "\n"
        t0 = time.time()
        frames_by_tk = defaultdict(list)
        # C-speed pre-filter: zstd -dc | grep -F -f <ticker patterns>
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".pat", delete=False) as tf:
            tf.write(pat)
            patfile = tf.name
        try:
            zp = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
            gp = subprocess.Popen(["grep", "-F", "-f", patfile],
                                  stdin=zp.stdout, stdout=subprocess.PIPE,
                                  bufsize=1 << 20)
            zp.stdout.close()
            for raw in gp.stdout:
                try:
                    env = json.loads(raw)
                    inner = json.loads(env["_raw"])
                    tk = inner.get("msg", {}).get("market_ticker")
                    if tk in tkset:
                        frames_by_tk[tk].append((_epoch(env["_wire_recv_ts"]), inner))
                except Exception:
                    continue
            gp.stdout.close()
            gp.wait()
            zp.wait()
            # ticket 86bbvrx1t: check the DECOMPRESSOR (zp), not gp — grep
            # exits 1 on "no matches", which is legitimate, and would mask a
            # zstd that died mid-stream. This loop has no break, so reaching
            # here is a true EOF.
            assert_zstd_ok(zp, path, exhausted=True, require_nonempty=False)
        finally:
            os.unlink(patfile)
        print(f"[05] realism {d}: {len(tickers)} tickers (capped={capped}), "
              f"frames for {len(frames_by_tk)} tickers in {time.time()-t0:.0f}s")
        for tk in tickers:
            fr = sorted(frames_by_tk.get(tk, []), key=lambda x: x[0])
            if not fr:
                funnel["realism_no_frames"] += 1
                continue
            tl = reliable_nbbo_timeline(fr)
            if not tl:
                funnel["realism_no_reliable_nbbo"] += 1
                continue
            for _, ev in sub[sub["ticker"] == tk].iterrows():
                t_trig = ev["t"]
                hit = None
                for ts, bid, ask in tl:
                    if ts <= t_trig:
                        continue
                    if ts > t_trig + 2.0:
                        break
                    px = ask if ev["side"] == "yes" else (
                        100.0 - bid if bid is not None else None)
                    if px is not None:
                        hit = px
                        break
                if hit is None:
                    funnel["realism_no_fill_2s"] += 1
                    continue
                funnel["realism_matched"] += 1
                results.append({"ticker": tk, "day": d, "proxy": ev["entry"],
                                "real": hit, "side": ev["side"],
                                "gross_settle": ev["gross"] + ev["entry"]})
    return pd.DataFrame(results)


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------

def branch_verdict(name, n_survivors, ev: pd.DataFrame, persistence, n_days_avail):
    print(f"\n=== {name} branch verdict vs pre-registered kills ===")
    if n_survivors < 5:
        print(f"K1: only {n_survivors} surviving fingerprints (<5) -> DEAD")
        return "DEAD_K1", None
    print(f"K1: {n_survivors} surviving fingerprints (>=5) -> pass")
    if persistence is not None:
        n_fp, n_persist = persistence
        frac = n_persist / n_fp if n_fp else 0.0
        print(f"K3: sign persistence {n_persist}/{n_fp} = {frac:.0%} "
              f"({'pass' if frac >= 0.60 else 'DEAD'})")
        if n_fp and frac < 0.60:
            return "DEAD_K3", None
    if ev is None or ev.empty:
        print("K2: zero test events -> INCONCLUSIVE")
        return "INCONCLUSIVE", None
    mean, lo, hi = day_bootstrap_ci(ev, "net")
    n = len(ev)
    nd = ev["day"].nunique()
    print(f"K2: n={n} events over {nd} labeled test days "
          f"(only {n_days_avail} settle-labeled days exist; >=7 required)  "
          f"net PnL/ct mean={mean:+.2f}c  95% day-CI=[{lo:+.2f}, {hi:+.2f}]")
    if lo > 0 and n >= 100:
        return "EDGE_CANDIDATE", (n, mean, lo, hi)
    if lo <= 0 and n >= 100 and nd >= 7:
        return "DEAD_K2", (n, mean, lo, hi)
    print("K2: cannot formally trigger (need >=7 days & >=100 events) -> "
          "INCONCLUSIVE on remaining evidence")
    return "INCONCLUSIVE", (n, mean, lo, hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="1 train day + 1 test day, no realism")
    ap.add_argument("--no-realism", action="store_true")
    ap.add_argument("--realism-days", default="2026-06-04,2026-06-05,2026-06-06")
    args = ap.parse_args()

    t_start = time.time()
    train_days = TRAIN_DAYS[:1] if args.smoke else TRAIN_DAYS
    test_days = TEST_DAYS[:1] if args.smoke else TEST_DAYS

    det = load_settlements()
    det_days = sorted({_utc_day(close_epoch_from_ticker(tk)) for tk in det})
    print(f"[05] settlements: {len(det):,} tickers, close-day span "
          f"{det_days[0]}..{det_days[-1]}")

    funnel = defaultdict(int)

    # ---- census ----
    by_fp, by_afp = census(det, train_days, funnel)
    pooled = census_table(by_fp)
    perasset = census_table(by_afp)
    informed = set(pooled[pooled["ci_lo"] > LABEL_CI_CENTS]["key"]) if len(pooled) else set()
    uninformed = set(pooled[pooled["ci_hi"] < -LABEL_CI_CENTS]["key"]) if len(pooled) else set()
    train_mean = dict(zip(pooled["key"], pooled["mean"])) if len(pooled) else {}

    print(f"\n=== TRAIN census (pooled, n>={MIN_TRAIN_PRINTS}) — "
          f"{len(pooled)} fingerprints ===")
    if len(pooled):
        with pd.option_context("display.max_rows", 50, "display.width", 140):
            show = pd.concat([pooled.head(20), pooled.tail(15)]).drop_duplicates()
            print(show.to_string(index=False,
                                 float_format=lambda x: f"{x:+.2f}"))
    n_inf_a = len(perasset[perasset["ci_lo"] > LABEL_CI_CENTS]) if len(perasset) else 0
    n_uninf_a = len(perasset[perasset["ci_hi"] < -LABEL_CI_CENTS]) if len(perasset) else 0
    print(f"\nPooled survivors: INFORMED={len(informed)} {sorted(informed)}  "
          f"UNINFORMED={len(uninformed)} {sorted(uninformed)}")
    print(f"Per-asset census ({len(perasset)} keys n>={MIN_TRAIN_PRINTS}): "
          f"informed={n_inf_a} uninformed={n_uninf_a} (reported only; labels=pooled)")
    if len(perasset):
        pa_show = perasset[(perasset["ci_lo"] > LABEL_CI_CENTS) |
                           (perasset["ci_hi"] < -LABEL_CI_CENTS)]
        if len(pa_show):
            pa_show = pa_show.copy()
            pa_show["key"] = pa_show["key"].astype(str)
            print(pa_show.to_string(index=False, float_format=lambda x: f"{x:+.2f}"))

    # ---- trade pass ----
    ev, fp_test_alpha = trade_pass(det, informed, uninformed, train_mean,
                                   test_days, funnel)

    # K3 persistence per branch (fps actually traded)
    def persistence_for(branch_fps, want_positive):
        n_fp = n_ok = 0
        details = []
        for fp in branch_fps:
            vals = [a for vs in fp_test_alpha.get(fp, {}).values() for a in vs]
            if not vals:
                continue
            m = sum(vals) / len(vals)
            ok = (m > 0) if want_positive else (m < 0)
            n_fp += 1
            n_ok += ok
            details.append((fp, len(vals), m, ok))
        return (n_fp, n_ok), details

    follow_ev = ev[(ev["branch"] == "FOLLOW")] if len(ev) else pd.DataFrame()
    fade_ev = ev[(ev["branch"] == "FADE")] if len(ev) else pd.DataFrame()

    traded_inf = set(follow_ev["fp"]) if len(follow_ev) else set()
    traded_uninf = set(fade_ev["fp"]) if len(fade_ev) else set()
    pers_f, det_f = persistence_for(traded_inf, want_positive=True)
    pers_d, det_d = persistence_for(traded_uninf, want_positive=False)

    # ---- funnel ----
    print("\n=== usable-data funnel ===")
    for k in ["lines", "prints", "unparseable", "regex_fallback", "non_crypto15m",
              "bad_price", "train_day_spill", "train_no_settlement",
              "train_prints_labeled", "test_day_spill", "test_windows_settled",
              "test_windows_no_settlement", "follow_triggers",
              "follow_below_threshold", "follow_untradeable_px",
              "fade_triggers", "fade_unfilled"]:
        print(f"  {k:28s} {funnel[k]:,}")

    # ---- headline (non-endgame) + endgame split ----
    def split_report(name, df):
        if df is None or not len(df):
            print(f"\n{name}: 0 events")
            return None, None
        main_df = df[~df["endgame"]]
        end_df = df[df["endgame"]]
        for label, d in [("HEADLINE (non-endgame)", main_df), ("ENDGAME", end_df)]:
            if len(d):
                mean, lo, hi = day_bootstrap_ci(d, "net")
                print(f"{name} {label}: n={len(d)} days={d['day'].nunique()} "
                      f"net/ct={mean:+.2f}c CI=[{lo:+.2f},{hi:+.2f}]  "
                      f"gross/ct={d['gross'].mean():+.2f}c")
            else:
                print(f"{name} {label}: n=0")
        return main_df, end_df

    print("\n=== TEST results ===")
    follow_main, follow_end = split_report("FOLLOW", follow_ev)
    fade_main, fade_end = split_report("FADE", fade_ev)
    if len(fade_ev) and "trig_mirror" in fade_ev.columns:
        tm = fade_ev[~fade_ev["endgame"]]["trig_mirror"].dropna()
        if len(tm):
            print(f"FADE sensitivity (optimistic trigger-print mirror, "
                  f"non-endgame): mean={tm.mean():+.2f}c n={len(tm)}")

    n_labeled_days = len([d for d in test_days if d in
                          set(ev["day"]) ]) if len(ev) else 0

    # per-fp persistence detail
    print("\n=== K3 persistence detail (traded fps, test-period mean gross alpha) ===")
    for nm, det_list, want in [("FOLLOW", det_f, "+"), ("FADE", det_d, "-")]:
        for fp, n, m, ok in sorted(det_list, key=lambda x: -x[1]):
            print(f"  {nm:6s} fp={fp:>8s} n_test={n:6,} mean={m:+.2f}c "
                  f"want{want} {'PERSIST' if ok else 'FLIP'}")

    v_follow, k_follow = branch_verdict(
        "FOLLOW", len(informed), follow_main, pers_f, n_labeled_days)
    v_fade, k_fade = branch_verdict(
        "FADE", len(uninformed), fade_main, pers_d, n_labeled_days)

    # ---- NBBO realism subset (FOLLOW only; FADE fills are mirror-priced) ----
    if not args.smoke and not args.no_realism and follow_main is not None \
            and len(follow_main):
        rd = [d.strip() for d in args.realism_days.split(",") if d.strip()][:3]
        rr = realism_check(follow_main, rd, funnel)
        if len(rr):
            rr["div"] = rr["real"] - rr["proxy"]
            rr["net_real"] = rr["gross_settle"] - rr["real"] - \
                rr["real"].map(kalshi_fee_per_contract_cents)
            rr["net_proxy"] = rr["gross_settle"] - rr["proxy"] - \
                rr["proxy"].map(kalshi_fee_per_contract_cents)
            mdiv = rr["div"].mean()
            print(f"\n=== FOLLOW NBBO realism subset (sealed days {rd}) ===")
            print(f"matched={len(rr)}  mean(ask_real - proxy)={mdiv:+.2f}c  "
                  f"median={rr['div'].median():+.2f}c")
            print(f"net/ct on matched subset: proxy={rr['net_proxy'].mean():+.2f}c  "
                  f"realism={rr['net_real'].mean():+.2f}c")
            if abs(mdiv) > 1.0:
                print(f"FLAG: proxy-vs-NBBO divergence {mdiv:+.2f}c/ct exceeds "
                      f"1c — headline proxy is biased; trust the realism subset.")
            print(f"realism funnel: no_frames={funnel['realism_no_frames']} "
                  f"no_reliable_nbbo={funnel['realism_no_reliable_nbbo']} "
                  f"no_fill_2s={funnel['realism_no_fill_2s']} "
                  f"matched={funnel['realism_matched']}")
        else:
            print("\n=== FOLLOW NBBO realism subset: 0 matched events ===")

    print(f"\n=== OVERALL ===")
    print(f"FOLLOW: {v_follow}  {k_follow}")
    print(f"FADE:   {v_fade}  {k_fade}")
    print(f"[05] total runtime {time.time()-t_start:.0f}s")


if __name__ == "__main__":
    main()
