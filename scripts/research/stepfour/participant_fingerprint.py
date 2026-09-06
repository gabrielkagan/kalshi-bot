"""Participant fingerprinting on the Kalshi 15M-crypto trade tape (StepFour).

PRE-REGISTERED HYPOTHESIS
-------------------------
The Kalshi 15M-crypto trade tape, though anonymous, contains a small number of
algorithmic participants with stable behavioral signatures (fixed/quantized
order sizes, metronomic inter-trade intervals, fixed reaction latency to spot
moves, always-same-side patterns). If we can cluster prints into putative
participants and any cluster's trades are PREDICTABLY wrong (its taker trades
lose to settlement net of fees), then being the resting counterparty to THAT
cluster (or front-running its predictable schedule) is an edge against a
specific opponent rather than the market aggregate. Aggregate microstructure
tested dead May-31; opponent-specific exploitation is untested. This is poker,
not econometrics.

WHAT THIS MEASURES (pre-registered)
-----------------------------------
1. SIGNATURE CENSUS: distribution of `count_fp` values (quantized sizes; mass
   on exactly-1, exactly-5, repeated weird fractionals like 15.13);
   inter-arrival time histogram per size-signature — metronomic spikes
   (e.g. exactly every 5.000s) = bot; per-signature side bias + time-of-day
   footprint. Output: top ~20 candidate signatures with n_trades, share of
   tape, interval-regularity score (fraction of inter-arrivals within ±50ms
   of the modal interval), side bias.
2. CLUSTER VIABILITY: are signatures stable across days (same size+cadence
   reappears)? Day-by-day presence of top signatures.
3. EXPLOITABILITY SCORE per signature-cluster: all taker prints attributed to
   the cluster; settlement PnL net of fee from the TAKER's perspective
   (bought side S at price P -> settle). A cluster whose takes systematically
   LOSE (day-bootstrap CI of mean taker PnL < 0) is a money-pump
   counterparty: being its passive counterparty earns the mirror. Report mean
   counterparty-PnL/ct + day-bootstrap CI + n per cluster.
   KILL CRITERION: exploitable iff CI_hi < 0 for the taker (mirror lo > 0 for
   us) AND n >= 100 AND signature present on >= 70% of days.
4. PREDICTABILITY PROBE (only if a metronomic cluster exists): does the
   cluster's next trade time/side follow from its past pattern with >70%
   accuracy? If yes it is NOTED — the trade-ahead strategy is a follow-up,
   not built here.

CAUTION (pre-registered)
------------------------
`count_fp` identity is a HEURISTIC for participant identity — never claim
de-anonymization certainty; multiple users can share a size. Mitigation:
require a joint (size x cadence x side-bias) match before treating a cluster
as one participant — size alone is NOT identity (generic sizes like 1.00 /
10.00 are pooled retail and will show low interval-regularity + ~50/50 side
mix; a true single bot shows high regularity + persistent side bias + stable
day-by-day presence). Any exploitation derived from this must remain
passive/reactive (resting counterparty) — no manipulative tactics.

DATA (trades + lifecycle only; no orderbook reconstruction)
-----------------------------------------------------------
- <corpus>/trades/day=*.jsonl.zst — envelope {"_wire_recv_ts","_raw"}; inner
  msg: trade_id, market_ticker, yes_price_dollars (str dollars),
  no_price_dollars, count_fp (str — KEPT AS EXACT STRING for signature
  matching), taker_side, ts, ts_ms.
- <corpus>/lifecycle/ via `load_determined` (scripts.research.
  phase1b_real_price_economics); fees via `kalshi_fee_per_contract_cents`
  (scripts.research.settlement_convergence_p1a).

Fee convention: taker pays 7*P*(1-P) cents/ct (amortized large-order rate);
the passive mirror is reported GROSS (Kalshi maker fee is 0 on most series —
verify per-series maker fee before acting on any mirror number).
IMPORTANT fee asymmetry: mirror GROSS = -(taker GROSS), NOT -(taker net) —
the taker's fee goes to Kalshi, not to the resting counterparty. A cluster
whose taker-net CI_hi < 0 purely because of the fee (gross ~ 0) is NOT
exploitable; the kill criterion therefore requires BOTH taker-net CI_hi < 0
AND mirror-gross CI_lo > 0 (the "(mirror lo > 0 for us)" clause of the
pre-registration, which fees make non-equivalent to the taker clause).

Same-ms prints (one taker order sweeping multiple resting levels) are
collapsed to ONE event for inter-arrival/cadence purposes; raw print counts
are reported separately.

Usage:
  python3 -m scripts.research.stepfour.participant_fingerprint \
      --corpus ~/kalshi-research-data/fairvalue [--days day=2026-05-30 ...]

Branch: algo-zoo-edge-hunt. Research-only; reads the local pre-pulled corpus.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
from collections import Counter, defaultdict
from datetime import date, timedelta

from scripts.research.phase1b_real_price_economics import load_determined
from scripts.research.settlement_convergence_p1a import (
    kalshi_fee_per_contract_cents,
)
from scripts.research.zstd_stream import checked_stream_lines  # noqa: E402  (repo root added to sys.path above)

# All 9 Kalshi 15M crypto series (matches phase1b ASSETS).
ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH")

TOP_N_DEFAULT = 20
REGULARITY_BIN_S = 0.1          # 100ms bin => "within ±50ms of modal interval"
METRONOME_REGULARITY_MIN = 0.30  # gate for running the predictability probe
KILL_MIN_N = 100                 # pre-registered
KILL_DAY_PRESENCE = 0.70         # pre-registered
N_BOOT = 1000
BOOT_SEED = 12345

_TICKER_RE = re.compile(r"^KX(" + "|".join(ASSETS) + r")15M-")


# ----- tape iteration --------------------------------------------------------


def _zst_lines(path: str):
    """Stream lines of a .jsonl.zst (subprocess zstd, mirrors phase1b helper —
    re-declared locally so the per-day trade files (~100K lines) stream rather
    than re-using phase1b's whole-file decode for clarity of memory behavior)."""
    # Delegates to the shared CHECKED reader (ticket 86bbvrx1t) — the previous
    # body discarded zstd's exit code AND used a strict decode; the shared
    # reader replaces undecodable bytes rather than aborting a whole day.
    yield from checked_stream_lines(path, require_nonempty=False,
                                    skip_blank=True)


def iter_trades(day_file: str):
    """Yield dicts per crypto-15M trade print:
    {ts_ms, ticker, side, yes_c, no_c, count_fp (EXACT string), count}."""
    for line in _zst_lines(day_file):
        try:
            inner = json.loads(json.loads(line)["_raw"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        msg = inner.get("msg", {})
        tk = msg.get("market_ticker", "")
        if not _TICKER_RE.match(tk):
            continue
        side = msg.get("taker_side")
        if side not in ("yes", "no"):
            continue
        cf = msg.get("count_fp")
        if cf is None:
            continue
        ts_ms = msg.get("ts_ms")
        if ts_ms is None:
            ts = msg.get("ts")
            if ts is None:
                continue
            ts_ms = int(ts) * 1000
        try:
            yes_c = round(float(msg["yes_price_dollars"]) * 100.0)
            no_c = round(float(msg["no_price_dollars"]) * 100.0)
            cnt = float(cf)
        except (KeyError, ValueError, TypeError):
            continue
        yield {
            "ts_ms": int(ts_ms),
            "ticker": tk,
            "side": side,
            "yes_c": yes_c,
            "no_c": no_c,
            "count_fp": str(cf),  # exact-string signature key
            "count": cnt,
        }


def discover_day_files(corpus: str, days):
    """List trades/day=*.jsonl.zst, optionally restricted to --days tokens
    (accepts 'day=2026-05-30' or bare '2026-05-30')."""
    all_files = sorted(glob.glob(os.path.join(corpus, "trades", "day=*.jsonl.zst")))
    if not days:
        return all_files
    wanted = {d.replace("day=", "") for d in days}
    out = [f for f in all_files
           if re.search(r"day=(\d{4}-\d{2}-\d{2})", f).group(1) in wanted]
    missing = wanted - {re.search(r"day=(\d{4}-\d{2}-\d{2})", f).group(1) for f in out}
    if missing:
        raise SystemExit(f"--days not found in corpus: {sorted(missing)}")
    return out


def _day_of(day_file: str) -> str:
    return re.search(r"day=(\d{4}-\d{2}-\d{2})", day_file).group(1)


def load_determined_for_days(corpus: str, day_strs) -> dict:
    """Determined-results map. When a day subset is requested, only load the
    matching lifecycle day partitions (+ next day, for windows determined just
    after midnight UTC) instead of all ~12K files. Full corpus -> single
    recursive load on the lifecycle root."""
    root = os.path.join(corpus, "lifecycle")
    if day_strs is None:
        return load_determined(root, ASSETS)
    dirs = []
    for ds in sorted(day_strs):
        d0 = date.fromisoformat(ds)
        for d in (d0, d0 + timedelta(days=1)):
            p = os.path.join(root, f"year={d.year}", f"month={d.month:02d}",
                             f"day={d.day:02d}")
            if os.path.isdir(p) and p not in dirs:
                dirs.append(p)
    merged: dict = {}
    for p in dirs:
        merged.update(load_determined(p, ASSETS))
    return merged


# ----- economics -------------------------------------------------------------


def taker_net_pnl_cents(side: str, yes_c: int, no_c: int, result: str) -> float:
    """Settlement PnL/ct net of taker fee, from the TAKER's perspective."""
    p = yes_c if side == "yes" else no_c
    gross = (100.0 - p) if side == result else -float(p)
    return gross - kalshi_fee_per_contract_cents(p)


def taker_gross_pnl_cents(side: str, yes_c: int, no_c: int, result: str) -> float:
    p = yes_c if side == "yes" else no_c
    return (100.0 - p) if side == result else -float(p)


def day_bootstrap_ci(per_day, n_boot: int = N_BOOT, alpha: float = 0.05,
                     seed: int = BOOT_SEED):
    """Percentile bootstrap CI for the count-weighted mean, resampling DAYS
    with replacement (clustered bootstrap — trades within a day are not
    independent). per_day: list of (sum_pnl_x_count, sum_count) per day."""
    per_day = [d for d in per_day if d[1] > 0]
    if not per_day:
        return (float("nan"), float("nan"))
    if len(per_day) == 1:
        m = per_day[0][0] / per_day[0][1]
        return (m, m)  # single day: no between-day variance estimable
    rng = random.Random(seed)
    nd = len(per_day)
    means = []
    for _ in range(n_boot):
        s = c = 0.0
        for _ in range(nd):
            sp, sc = per_day[rng.randrange(nd)]
            s += sp
            c += sc
        means.append(s / c if c else float("nan"))
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (lo, hi)


# ----- signature analytics ---------------------------------------------------


def collapse_same_ms(ts_list):
    """Distinct event timestamps (s): same-ms prints = one sweep = one event."""
    return sorted(set(ts_list))


def regularity(ts_ms_sorted):
    """(score, modal_interval_s, n_intervals): fraction of inter-arrivals in
    the modal 100ms bin (== within ±50ms of the modal interval)."""
    if len(ts_ms_sorted) < 3:
        return (float("nan"), float("nan"), 0)
    dts = [(b - a) / 1000.0 for a, b in zip(ts_ms_sorted, ts_ms_sorted[1:])]
    bins = Counter(round(dt / REGULARITY_BIN_S) for dt in dts)
    modal_bin, modal_n = bins.most_common(1)[0]
    return (modal_n / len(dts), modal_bin * REGULARITY_BIN_S, len(dts))


def side_bias(yes_n: int, no_n: int):
    tot = yes_n + no_n
    if not tot:
        return ("-", float("nan"))
    return (("yes", yes_n / tot) if yes_n >= no_n else ("no", no_n / tot))


def hour_footprint(hours: Counter):
    """(top3_hours, concentration): fraction of prints in the 3 busiest UTC
    hours — flat ~0.125 for an always-on participant, ~1.0 for a session bot."""
    tot = sum(hours.values())
    if not tot:
        return ([], float("nan"))
    top3 = hours.most_common(3)
    return ([h for h, _ in top3], sum(n for _, n in top3) / tot)


def predictability_probe(ts_ms_sorted, modal_interval_s, sides_in_time_order):
    """Only for metronomic clusters. Two mechanical predictors:
    - next-time: predict next event at prev + modal interval; correct if the
      realized inter-arrival lands within ±50ms of modal.
    - next-side: predict next print's side = previous print's side (markov-1)
      and = majority side; report the better.
    Returns dict of accuracies (these are honest in-sample pattern rates, not
    out-of-sample forecasts — flagged as follow-up territory if >0.70)."""
    out = {"time_acc": float("nan"), "side_acc": float("nan"), "side_rule": "-"}
    if len(ts_ms_sorted) >= 3 and modal_interval_s == modal_interval_s:
        dts = [(b - a) / 1000.0 for a, b in zip(ts_ms_sorted, ts_ms_sorted[1:])]
        hit = sum(1 for dt in dts if abs(dt - modal_interval_s) <= 0.050)
        out["time_acc"] = hit / len(dts)
    s = sides_in_time_order
    if len(s) >= 3:
        markov = sum(1 for a, b in zip(s, s[1:]) if a == b) / (len(s) - 1)
        maj = max(s.count("yes"), s.count("no")) / len(s)
        out["side_acc"], out["side_rule"] = (
            (markov, "markov-1") if markov >= maj else (maj, "majority"))
    return out


# ----- driver ----------------------------------------------------------------


def run(corpus: str, days, top_n: int, n_boot: int) -> None:
    corpus = os.path.expanduser(corpus)
    day_files = discover_day_files(corpus, days)
    day_strs = [_day_of(f) for f in day_files]
    print(f"corpus={corpus}")
    print(f"days analyzed ({len(day_files)}): {', '.join(day_strs)}")

    print("loading lifecycle (determined results)...", flush=True)
    determined = load_determined_for_days(
        corpus, day_strs if days else None)
    print(f"  determined crypto-15M markets: {len(determined)}")

    # ---- PASS 1: census (aggregates only; full tape) ------------------------
    sig_n: Counter = Counter()                 # raw prints per signature
    sig_ct: defaultdict = defaultdict(float)   # contracts per signature
    sig_yes: Counter = Counter()
    sig_day_n: Counter = Counter()             # (sig, day) -> prints
    sig_days: defaultdict = defaultdict(set)
    n_prints = 0
    int_prints = 0  # prints whose count_fp is integer-valued ("5.00")
    frac_top: Counter = Counter()  # census of fractional signatures
    for f in day_files:
        d = _day_of(f)
        for t in iter_trades(f):
            n_prints += 1
            k = t["count_fp"]
            sig_n[k] += 1
            sig_ct[k] += t["count"]
            if t["side"] == "yes":
                sig_yes[k] += 1
            sig_days[k].add(d)
            sig_day_n[(k, d)] += 1
            if t["count"] == int(t["count"]):
                int_prints += 1
            else:
                frac_top[k] += 1

    print(f"\n=== 1. SIGNATURE CENSUS ({n_prints:,} prints, "
          f"{len(sig_n):,} distinct count_fp) ===")
    print(f"integer-size prints: {int_prints:,} ({int_prints/n_prints:.1%}); "
          f"fractional: {n_prints-int_prints:,} ({1-int_prints/n_prints:.1%})")
    print("top repeated FRACTIONAL sizes (candidate partial-fill echoes "
          "or odd-lot bots):")
    for k, n in frac_top.most_common(10):
        print(f"  count_fp={k:>10}  n={n}")

    top_sigs = [k for k, _ in sig_n.most_common(top_n)]

    # ---- PASS 2: detail for top signatures ----------------------------------
    # pnl_by_day[d] = [sum(net*ct), sum(gross*ct), sum(ct)]
    detail = {k: {"ts": [], "sides_t": [], "hours": Counter(),
                  "pnl_by_day": defaultdict(lambda: [0.0, 0.0, 0.0]),
                  "matched": 0, "unmatched": 0}
              for k in top_sigs}
    top_set = set(top_sigs)
    for f in day_files:
        d = _day_of(f)
        for t in iter_trades(f):
            k = t["count_fp"]
            if k not in top_set:
                continue
            dd = detail[k]
            dd["ts"].append(t["ts_ms"])
            dd["sides_t"].append((t["ts_ms"], t["side"]))
            dd["hours"][(t["ts_ms"] // 3_600_000) % 24] += 1
            rec = determined.get(t["ticker"])
            if rec is None:
                dd["unmatched"] += 1
                continue
            dd["matched"] += 1
            gross = taker_gross_pnl_cents(t["side"], t["yes_c"], t["no_c"],
                                          rec["result"])
            p = t["yes_c"] if t["side"] == "yes" else t["no_c"]
            net = gross - kalshi_fee_per_contract_cents(p)
            acc = dd["pnl_by_day"][d]
            acc[0] += net * t["count"]
            acc[1] += gross * t["count"]
            acc[2] += t["count"]

    rows = []
    for k in top_sigs:
        dd = detail[k]
        ev = collapse_same_ms(dd["ts"])
        reg, modal, n_int = regularity(ev)
        bias_side, bias = side_bias(sig_yes[k], sig_n[k] - sig_yes[k])
        hrs, conc = hour_footprint(dd["hours"])
        present = len(sig_days[k]) / len(day_files)
        per_day = list(dd["pnl_by_day"].values())
        tot_pc = sum(c for _, _, c in per_day)
        mean_taker = (sum(s for s, _, _ in per_day) / tot_pc) if tot_pc else float("nan")
        mean_gross = (sum(g for _, g, _ in per_day) / tot_pc) if tot_pc else float("nan")
        lo, hi = day_bootstrap_ci([(s, c) for s, _, c in per_day], n_boot=n_boot)
        g_lo, g_hi = day_bootstrap_ci([(g, c) for _, g, c in per_day], n_boot=n_boot)
        n_m = dd["matched"]
        n_pnl_days = sum(1 for _, _, c in per_day if c > 0)
        # A 1-day "CI" is the point estimate — never a basis for EXPLOITABLE.
        # Joint criterion (see docstring fee-asymmetry note): taker NET loses
        # AND the passive mirror's GROSS (= -taker gross) is positive.
        exploitable = (n_pnl_days >= 2 and hi == hi and hi < 0.0
                       and g_hi == g_hi and -g_hi > 0.0
                       and n_m >= KILL_MIN_N and present >= KILL_DAY_PRESENCE)
        rows.append({
            "sig": k, "n": sig_n[k], "share": sig_n[k] / n_prints,
            "n_events": len(ev), "reg": reg, "modal": modal,
            "bias_side": bias_side, "bias": bias, "hours": hrs, "conc": conc,
            "present": present, "n_matched": n_m,
            "match_rate": n_m / (n_m + dd["unmatched"]) if (n_m + dd["unmatched"]) else float("nan"),
            "taker_mean": mean_taker, "ci": (lo, hi),
            "mirror_mean": -mean_gross, "mirror_ci": (-g_hi, -g_lo),
            "n_pnl_days": n_pnl_days, "exploitable": exploitable,
        })

    print(f"\n=== TOP-{len(rows)} SIGNATURES (by raw prints) ===")
    hdr = (f"{'count_fp':>10} {'n':>7} {'share':>6} {'events':>7} "
           f"{'reg':>5} {'modal_s':>8} {'bias':>9} {'hr_conc':>7} {'days':>5}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['sig']:>10} {r['n']:>7,} {r['share']:>6.1%} "
              f"{r['n_events']:>7,} {r['reg']:>5.2f} {r['modal']:>8.1f} "
              f"{r['bias_side']:>3}:{r['bias']:>4.0%} {r['conc']:>7.0%} "
              f"{r['present']:>5.0%}")

    print("\n=== 2. CLUSTER VIABILITY (day-by-day prints; '.' = absent) ===")
    print(f"{'count_fp':>10} | " + " ".join(f"{d[5:]:>5}" for d in day_strs))
    for k in top_sigs:
        cells = " ".join(
            (f"{sig_day_n[(k, d)]:>5,}" if (k, d) in sig_day_n else f"{'.':>5}")
            for d in day_strs)
        print(f"{k:>10} | {cells}")

    print("\n=== 3. EXPLOITABILITY (taker net PnL/ct, settlement, "
          "day-bootstrap 95% CI) ===")
    hdr2 = (f"{'count_fp':>10} {'n_match':>8} {'tk_net':>7} "
            f"{'CI_lo':>7} {'CI_hi':>7} {'mir_gr':>7} {'mCI_lo':>7} "
            f"{'days':>5} {'verdict':>13}")
    print(hdr2)
    print("-" * len(hdr2))
    any_exploit = False
    for r in rows:
        lo, hi = r["ci"]
        m_lo, _ = r["mirror_ci"]
        verdict = "EXPLOITABLE" if r["exploitable"] else (
            "1-day NA" if r["n_pnl_days"] < 2 else
            "n<100" if r["n_matched"] < KILL_MIN_N else
            f"days<{KILL_DAY_PRESENCE:.0%}" if r["present"] < KILL_DAY_PRESENCE
            else "tk CI spans 0" if not (hi < 0)
            else "fee-only loss" if not (m_lo > 0) else "?")
        any_exploit |= r["exploitable"]
        print(f"{r['sig']:>10} {r['n_matched']:>8,} {r['taker_mean']:>7.2f} "
              f"{lo:>7.2f} {hi:>7.2f} {r['mirror_mean']:>7.2f} {m_lo:>7.2f} "
              f"{r['present']:>5.0%} {verdict:>13}")
    print("(tk_net = taker PnL/ct net of 7*P*(1-P) amortized fee; mir_gr = "
          "-(taker GROSS) = what the passive counterparty earns pre-maker-fee."
          "\n 'fee-only loss' = taker loses ~the fee, gross ~ 0 -> the mirror "
          "earns nothing; that money goes to Kalshi, not to us.)")
    print("note: 'EXPLOITABLE' on a single analyzed day is NEVER actionable — "
          "the day-bootstrap degenerates; require the full corpus.")

    print("\n=== 4. PREDICTABILITY PROBE (metronomic clusters only, "
          f"reg >= {METRONOME_REGULARITY_MIN}) ===")
    ran_any = False
    for r in rows:
        if not (r["reg"] == r["reg"] and r["reg"] >= METRONOME_REGULARITY_MIN
                and r["n_events"] >= 50):
            continue
        ran_any = True
        dd = detail[r["sig"]]
        ev = collapse_same_ms(dd["ts"])
        sides_sorted = [s for _, s in sorted(dd["sides_t"])]
        pr = predictability_probe(ev, r["modal"], sides_sorted)
        flag = (" <- >70%: trade-ahead FOLLOW-UP candidate (do not build here)"
                if (pr["time_acc"] > 0.70 or pr["side_acc"] > 0.70) else "")
        print(f"  {r['sig']:>10}: time_acc={pr['time_acc']:.2f} "
              f"(modal {r['modal']:.1f}s) side_acc={pr['side_acc']:.2f} "
              f"[{pr['side_rule']}]{flag}")
    if not ran_any:
        print("  no metronomic cluster (no top signature reached "
              f"regularity >= {METRONOME_REGULARITY_MIN} with >=50 events)")

    print("\n=== VERDICT ===")
    if any_exploit:
        print("At least one signature met the pre-registered exploitability "
              "criterion (taker CI_hi < 0, n >= 100, present >= 70% of days). "
              "Verify with joint size x cadence x side-bias before treating "
              "as one participant; exploitation must remain passive/reactive.")
    else:
        print("NO signature met the pre-registered exploitability criterion "
              "(taker CI_hi < 0 AND n >= 100 AND present >= 70% of days).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", default="~/kalshi-research-data/fairvalue")
    ap.add_argument("--days", nargs="*", default=None,
                    help="restrict to specific days, e.g. day=2026-05-30")
    ap.add_argument("--top", type=int, default=TOP_N_DEFAULT)
    ap.add_argument("--boot", type=int, default=N_BOOT)
    args = ap.parse_args()
    run(args.corpus, args.days, args.top, args.boot)


if __name__ == "__main__":
    main()
