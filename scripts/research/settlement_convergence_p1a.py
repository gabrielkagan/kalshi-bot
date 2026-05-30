"""P1a — Settlement-convergence proxy edge (Phase 1a of the convergence spike).

Hypothesis (per plan-doc § Phase 1a):
  Kalshi 15M settles to the arithmetic mean of the 60 per-second CF Benchmarks
  RTI readings over the final 60s before expiry (Phase 0, 3 sources). So at a
  decision point T-Xs (X ≤ 60) the readings already inside [close-60s, T] are
  LOCKED into the settlement; only the remaining X seconds are unknown. The
  PARTIAL running settlement average should predict the settled side better
  than instantaneous-spot-at-decision — especially on late-reversal windows
  (the 90-99¢ bleed cohort, Finding B).

Kill threshold (per plan-doc § 5):
  If the partial-settlement-average sign does NOT beat instantaneous-spot-at-
  close materially (esp. on the held-loser cohort), the convergence thesis
  fails on the proxy and Phase 1b (bronze) is not worth building for this edge.

Data source (PRIMARY — proxy):
  position_price_observations (state.db), back to 2026-04-06, ~7.5 weeks,
  sub-second near close. `spot_price` is Coinbase — a ~2-3bps PROXY for the
  RTI (Principle 0 caveat), held-positions-only (selection bias: ideal for the
  held-loser bleed cohort, thin for windows we passed on). Label/asset from
  settled_trades. Exact-RTI refinement = Phase 1b (bronze).

Run:
  scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
  python3 scripts/research/settlement_convergence_p1a.py --db /tmp/state.db

Parent plan: kb/decisions/settlement-lag-convergence-edge-spike-plan.md
Parent ClickUp: 86ba747mc (Spike P1) under umbrella 86ba747ke
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Optional, Sequence

from bot.constants import SERIES_TICKERS

# 7-asset 15M universe, mirrored from bot.constants.SERIES_TICKERS (values
# "KXBTC15M" ... "KXBNB15M"). Anti-drift: test_seven_asset_universe_pinned_*.
ASSET_TICKER_PREFIX: dict[str, str] = dict(SERIES_TICKERS)

# Drift guard on the PRIMARY data source schema (PRAGMA-verified 2026-05-30).
PPO_REQUIRED_COLUMNS = (
    "ticker", "asset", "observation_time", "seconds_to_close",
    "spot_price", "threshold", "yes_ask_cents", "yes_bid_cents",
    "orderbook_levels_json",
)

# Decision points (seconds-to-close) at which we read the convergence signal.
DECISION_POINTS_S = (60, 30, 15)


# ----- Timestamp parsing --------------------------------------------------


def _parse_iso(s: str) -> datetime:
    """Parse an ISO8601 timestamp (with trailing Z and optional micros) to an
    aware UTC datetime."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ----- Convergence estimator (NO look-ahead) ------------------------------


def partial_settlement_average(
    observations: Sequence[dict],
    close_time: str,
    decision_time: str,
    window_seconds: int = 60,
) -> Optional[float]:
    """Mean of `spot_price` over the settlement window readings already
    observed by `decision_time` — i.e. over [close-window, decision].

    This is the portion of the 60s-average settlement that is already LOCKED at
    the decision point. NO LOOK-AHEAD: readings strictly after `decision_time`
    are excluded (they are the future, unknown part of the settlement).

    Returns None if the final-minute window has not opened yet at the decision
    point (no convergence information available). Raises ValueError if the
    decision is after close (label leak).
    """
    close = _parse_iso(close_time)
    decision = _parse_iso(decision_time)
    if decision > close:
        raise ValueError(
            f"decision_time {decision_time} is after close {close_time} — look-ahead/label leak"
        )
    window_start = close - timedelta(seconds=window_seconds)
    if decision < window_start:
        return None  # final-minute window not yet open
    eligible = [
        o["spot_price"]
        for o in observations
        if o.get("spot_price") is not None
        and window_start <= _parse_iso(o["observation_time"]) <= decision
    ]
    if not eligible:
        return None
    return mean(eligible)


def predict_side(estimate: float, threshold: float) -> str:
    """Map a price estimate vs the strike to the settled side. Strictly-above
    contract: an exact tie at the strike settles NO."""
    return "yes" if estimate > threshold else "no"


def is_locked(
    locked_sum: float,
    n_locked: int,
    n_total: int,
    future_extreme: float,
    threshold: float,
    side: str,
) -> bool:
    """Would the prediction still hold under the worst plausible remaining move?

    `future_extreme` is the worst-case value the remaining (n_total - n_locked)
    per-second readings could take for the held `side` (the lowest plausible
    spot for a YES, the highest for a NO). The window is LOCKED iff even that
    worst case keeps the final 60s-average on the predicted side.
    """
    n_remaining = n_total - n_locked
    if n_remaining < 0 or n_total <= 0:
        raise ValueError(f"bad reading counts: n_locked={n_locked} n_total={n_total}")
    final_worst = (locked_sum + n_remaining * future_extreme) / n_total
    return predict_side(final_worst, threshold) == side


# ----- Per-window evaluation ----------------------------------------------


def _spot_at_or_before(
    observations: Sequence[dict], decision_time: str, max_staleness_s: Optional[float] = None
) -> Optional[float]:
    """Instantaneous spot = the most recent reading at-or-before the decision.
    If `max_staleness_s` is set and the most recent reading is older than that,
    return None (no fresh price — avoids the sparse-corpus stale-price artifact)."""
    decision = _parse_iso(decision_time)
    candidates = [
        (o["observation_time"], o["spot_price"])
        for o in observations
        if o.get("spot_price") is not None and _parse_iso(o["observation_time"]) <= decision
    ]
    if not candidates:
        return None
    obs_time, val = max(candidates, key=lambda c: _parse_iso(c[0]))
    if max_staleness_s is not None and \
            (decision - _parse_iso(obs_time)).total_seconds() > max_staleness_s:
        return None
    return val


def evaluate_window(
    observations: Sequence[dict],
    close_time: str,
    decision_time: str,
    threshold: float,
    actual_result: str,
    window_seconds: int = 60,
) -> dict:
    """Compare the convergence signal vs instantaneous spot at one decision
    point against the realized settlement side."""
    conv_avg = partial_settlement_average(
        observations, close_time, decision_time, window_seconds
    )
    inst_spot = _spot_at_or_before(observations, decision_time)
    conv_side = predict_side(conv_avg, threshold) if conv_avg is not None else None
    inst_side = predict_side(inst_spot, threshold) if inst_spot is not None else None
    return {
        "convergence_avg": conv_avg,
        "instantaneous_spot": inst_spot,
        "convergence_side": conv_side,
        "instantaneous_side": inst_side,
        "actual_result": actual_result,
        "convergence_correct": (conv_side == actual_result) if conv_side else None,
        "instantaneous_correct": (inst_side == actual_result) if inst_side else None,
    }


def lock_decision(
    observations: Sequence[dict],
    close_time: str,
    decision_time: str,
    threshold: float,
    side: str,
    move_bound: float,
    n_total: int = 60,
    window_seconds: int = 60,
    max_staleness_s: Optional[float] = None,
) -> str:
    """Is the held `side` safe even under the worst plausible remaining move?

    Uses the locked partial sum (readings already in [close-window, decision])
    plus a remaining-move bound: `move_bound` is the absolute worst adverse move
    of the remaining readings from current spot (DOWN for yes, UP for no). The
    window is "locked" iff even that worst case keeps the final 60s-average on
    `side`. Returns "locked" / "not_locked" / "no_data".
    """
    close = _parse_iso(close_time)
    decision = _parse_iso(decision_time)
    if decision > close:
        raise ValueError(f"decision_time {decision_time} after close {close_time}")
    window_start = close - timedelta(seconds=window_seconds)
    if decision < window_start:
        return "no_data"
    locked = [
        o["spot_price"]
        for o in observations
        if o.get("spot_price") is not None
        and window_start <= _parse_iso(o["observation_time"]) <= decision
    ]
    if not locked:
        return "no_data"
    current = _spot_at_or_before(observations, decision_time, max_staleness_s)
    if current is None:
        return "no_data"
    locked_sum = sum(locked)
    n_locked = len(locked)
    # If we already have >= n_total readings, the window is fully observed:
    # scale to n_total equivalents so n_remaining collapses to 0.
    if n_locked > n_total:
        locked_sum = locked_sum * n_total / n_locked
        n_locked = n_total
    worst = current - move_bound if side == "yes" else current + move_bound
    return "locked" if is_locked(
        locked_sum, n_locked, n_total, worst, threshold, side
    ) else "not_locked"


# ----- Driver (data-dependent; pulls the proxy corpus) --------------------


def _load_settled_windows(conn: sqlite3.Connection, since: Optional[str]) -> list[dict]:
    """Settled 15M windows with their final-minute proxy trajectory.

    Joins settled_trades (label/asset) to position_price_observations (the
    per-second spot + Kalshi-quote trajectory). Returns one dict per ticker
    with its observation list. close_time is derived per-window from
    observation_time + seconds_to_close (cross-checked for clock consistency).
    """
    conn.row_factory = sqlite3.Row
    label_sql = (
        "SELECT ticker, asset, market_result FROM settled_trades "
        "WHERE product_type='15m' AND market_result IN ('yes','no')"
    )
    labels = {r["ticker"]: r for r in conn.execute(label_sql)}
    if not labels:
        return []
    windows: list[dict] = []
    obs_sql = (
        "SELECT observation_time, spot_price, threshold, seconds_to_close, "
        "yes_ask_cents, yes_bid_cents FROM position_price_observations "
        "WHERE ticker = ? AND spot_price IS NOT NULL "
        "AND seconds_to_close IS NOT NULL AND seconds_to_close <= 180 "
        "ORDER BY observation_time"
    )
    for ticker, lab in labels.items():
        rows = [dict(r) for r in conn.execute(obs_sql, (ticker,))]
        rows = [r for r in rows if r["threshold"] is not None]
        if len(rows) < 5:
            continue
        if since and rows[-1]["observation_time"] < since:
            continue
        # Derive close_time = obs_time + seconds_to_close; cross-check consistency.
        closes = [
            _parse_iso(r["observation_time"]) + timedelta(seconds=r["seconds_to_close"])
            for r in rows
        ]
        closes.sort()
        close_dt = closes[len(closes) // 2]  # median, robust to a few bad rows
        spread_s = (closes[-1] - closes[0]).total_seconds()
        thresholds = {r["threshold"] for r in rows}
        windows.append({
            "ticker": ticker,
            "asset": lab["asset"],
            "actual_result": lab["market_result"],
            "threshold": rows[-1]["threshold"],
            "close_time": close_dt.isoformat().replace("+00:00", "Z"),
            "observations": rows,
            "close_spread_s": spread_s,
            "threshold_drift": len(thresholds) > 1,
            "final_yes_ask": rows[-1]["yes_ask_cents"],
        })
    return windows


def _load_eval_opps_windows(conn: sqlite3.Connection, since: Optional[str]) -> list[dict]:
    """ALL-OPPORTUNITIES corpus (traded + passed) from evaluated_opportunities —
    removes the held-only selection bias of position_price_observations. COARSER:
    most windows have only 1-2 final-minute eval rows, so this is a DIRECTIONAL
    robustness check, not a precise EV. `market_price` is the bot's evaluated
    price (ask/mid ambiguity → EV may be slightly optimistic); the dense/rigorous
    version is Phase 1b (Coinbase bronze)."""
    conn.row_factory = sqlite3.Row
    from collections import defaultdict
    sql = (
        "SELECT ticker, asset, evaluation_time, spot_price, threshold, "
        "seconds_to_close, market_price, market_result "
        "FROM evaluated_opportunities "
        "WHERE product_type='15m' AND market_result IN ('yes','no') "
        "AND side='yes' "  # market_price is the YES price only for yes-side rows
        "AND seconds_to_close IS NOT NULL AND seconds_to_close <= 180 "
        "AND spot_price IS NOT NULL AND threshold IS NOT NULL "
        "AND market_price IS NOT NULL "
        "ORDER BY ticker, evaluation_time"
    )
    by_ticker: dict[str, list] = defaultdict(list)
    meta: dict[str, tuple] = {}
    for r in conn.execute(sql):
        by_ticker[r["ticker"]].append(r)
        meta[r["ticker"]] = (r["asset"], r["market_result"])
    windows: list[dict] = []
    for ticker, rows in by_ticker.items():
        final_rows = [r for r in rows if r["seconds_to_close"] <= 60]
        if len(final_rows) < 2 or len(rows) < 3:
            continue
        if since and rows[-1]["evaluation_time"] < since:
            continue
        obs = [{
            "observation_time": r["evaluation_time"], "spot_price": r["spot_price"],
            "threshold": r["threshold"], "seconds_to_close": r["seconds_to_close"],
            "yes_ask_cents": r["market_price"],
        } for r in rows]
        closes = sorted(
            _parse_iso(r["evaluation_time"]) + timedelta(seconds=r["seconds_to_close"])
            for r in rows
        )
        close_dt = closes[len(closes) // 2]
        asset, result = meta[ticker]
        windows.append({
            "ticker": ticker, "asset": asset, "actual_result": result,
            "threshold": rows[-1]["threshold"],
            "close_time": close_dt.isoformat().replace("+00:00", "Z"),
            "observations": obs,
            "close_spread_s": (closes[-1] - closes[0]).total_seconds(),
            "threshold_drift": len({r["threshold"] for r in rows}) > 1,
            "final_yes_ask": rows[-1]["market_price"],
        })
    return windows


def _band(yes_ask: Optional[int]) -> str:
    if yes_ask is None:
        return "unknown"
    if yes_ask >= 90:
        return "90-99"
    if yes_ask >= 60:
        return "60-89"
    if yes_ask >= 40:
        return "40-59"
    return "1-39"


# ----- Economics primitives -----------------------------------------------


def kalshi_fee_per_contract_cents(price_cents: float) -> float:
    """Kalshi trading fee per contract (large-order limit, in cents):
    0.07 * P * (1-P) dollars -> *100 cents = 7 * P * (1-P). Tiny near 99c,
    peaks at 50c. NOTE: Kalshi rounds the per-ORDER fee up to the next cent,
    so a 1-contract order pays a ~1c minimum — this is the amortized large-order
    rate; the 1-contract rounding penalty is reported separately."""
    p = price_cents / 100.0
    return 7.0 * p * (1.0 - p)


def realized_pnl_cents(price_cents: float, won: bool) -> float:
    """Gross PnL per contract from buying YES at `price_cents` and holding to
    settlement: win -> (100 - price), lose -> (-price)."""
    return (100.0 - price_cents) if won else (-float(price_cents))


def _yes_ask_at_or_before(
    observations: Sequence[dict], decision_time: str, max_staleness_s: Optional[float] = None
) -> Optional[int]:
    decision = _parse_iso(decision_time)
    cands = [
        (o["observation_time"], o.get("yes_ask_cents"))
        for o in observations
        if o.get("yes_ask_cents") is not None
        and _parse_iso(o["observation_time"]) <= decision
    ]
    if not cands:
        return None
    obs_time, val = max(cands, key=lambda c: _parse_iso(c[0]))
    if max_staleness_s is not None and \
            (decision - _parse_iso(obs_time)).total_seconds() > max_staleness_s:
        return None
    return val


def _quote_at_or_before(
    observations: Sequence[dict], decision_time: str, max_staleness_s: Optional[float] = None
) -> tuple:
    """Most recent contemporaneous (yes_ask, yes_bid) from a SINGLE row at-or-
    before the decision. Same-row so the crossed-book coherence check (R1-C1) is
    valid — `position_price_observations` ask/bid are independently-lagging
    fields, so a recorded ask can sit BELOW the contemporaneous bid; such a quote
    is not transactable. Returns (None, None) if no fresh ask-bearing row."""
    decision = _parse_iso(decision_time)
    cands = [
        o for o in observations
        if o.get("yes_ask_cents") is not None
        and _parse_iso(o["observation_time"]) <= decision
    ]
    if not cands:
        return (None, None)
    row = max(cands, key=lambda o: _parse_iso(o["observation_time"]))
    if max_staleness_s is not None and \
            (decision - _parse_iso(row["observation_time"])).total_seconds() > max_staleness_s:
        return (None, None)
    return (row.get("yes_ask_cents"), row.get("yes_bid_cents"))


def is_transactable_quote(ask: Optional[int], bid: Optional[int]) -> bool:
    """A YES taker buy is only realistic at a coherent, sub-100 ask. Rejects:
    missing ask/bid, non-positive, ask>=100 (no upside), and CROSSED books
    (ask<bid) — the R1-C1 artifact where the recorded ask is below the bid."""
    if ask is None or bid is None:
        return False
    if ask <= 0 or bid <= 0:
        return False
    if ask >= 100:
        return False
    return bid <= ask


# Remaining-move bound sweep (bps of current spot). Traces the conservatism
# knob: tighter bound -> more windows called "locked" (buy); wider -> more
# "not_locked" (pass). The WWJD tradeoff: dodge losers without giving up winners.
LOCK_BPS_SWEEP = (2, 5, 10, 20, 40, 80)

# Max age of the decision-time price/spot. Dense held data (sub-second) is
# unaffected; sparse all-opportunities data drops windows whose only pre-decision
# price is a stale, cheap, early reading (the evalopps +18c artifact).
MAX_PRICE_STALENESS_S = 20.0


def run_lock_sweep(windows: list[dict], side: str = "yes",
                   max_staleness_s: Optional[float] = MAX_PRICE_STALENESS_S) -> int:
    """For each decision point × move-bound, measure the lock detector as a
    classifier: locked-call win-rate, fraction of losers correctly passed
    (loss dodged), and fraction of winners wrongly passed (opportunity given up)."""
    import math
    from collections import defaultdict
    agg = defaultdict(lambda: {
        "locked_n": 0, "locked_win": 0, "losers": 0, "losers_passed": 0,
        "winners": 0, "winners_passed": 0,
        "econ_n": 0, "price_sum": 0.0, "gross_sum": 0.0,
        "net_sum": 0.0, "net1_sum": 0.0, "dropped": 0,
    })
    # Principle-0 baseline: buy EVERY window at the ask (no lock filter). If this
    # is already +EV, the lock detector is incidental; if it's -EV and the filter
    # flips it +EV, the detector is doing the work.
    base = defaultdict(lambda: {"n": 0, "net_sum": 0.0, "win": 0})
    for w in windows:
        actual = w["actual_result"]
        for s in DECISION_POINTS_S:
            decision_dt = _parse_iso(w["close_time"]) - timedelta(seconds=s)
            decision_iso = decision_dt.isoformat().replace("+00:00", "Z")
            current = _spot_at_or_before(w["observations"], decision_iso, max_staleness_s)
            if current is None:
                continue
            _ask, _bid = _quote_at_or_before(w["observations"], decision_iso, max_staleness_s)
            if is_transactable_quote(_ask, _bid):
                b = base[s]
                b["n"] += 1
                b["win"] += int(actual == "yes")
                b["net_sum"] += (realized_pnl_cents(_ask, actual == "yes")
                                 - kalshi_fee_per_contract_cents(_ask))
            for bps in LOCK_BPS_SWEEP:
                bound = bps / 1e4 * current
                dec = lock_decision(
                    w["observations"], w["close_time"], decision_iso,
                    w["threshold"], side, bound, max_staleness_s=max_staleness_s,
                )
                if dec == "no_data":
                    continue
                a = agg[(s, bps)]
                if actual == "yes":
                    a["winners"] += 1
                    if dec == "not_locked":
                        a["winners_passed"] += 1
                else:
                    a["losers"] += 1
                    if dec == "not_locked":
                        a["losers_passed"] += 1
                if dec == "locked":
                    a["locked_n"] += 1
                    won = (actual == "yes")
                    if won:
                        a["locked_win"] += 1
                    ask, bid = _quote_at_or_before(w["observations"], decision_iso, max_staleness_s)
                    if is_transactable_quote(ask, bid):
                        gross = realized_pnl_cents(ask, won)
                        fee = kalshi_fee_per_contract_cents(ask)
                        a["econ_n"] += 1
                        a["price_sum"] += ask
                        a["gross_sum"] += gross
                        a["net_sum"] += gross - fee              # large-order (amortized fee)
                        a["net1_sum"] += gross - math.ceil(fee)  # 1-contract (fee rounds up to >=1c)
                    else:
                        a["dropped"] += 1                        # crossed book / ask>=100 (R1-C1)
    print("\nBASELINE — buy EVERY window at the ask at T-Xs (no lock filter), net of fee:")
    print(f"{'T-Xs':>5}{'n':>7}{'win%':>7}{'netEV':>8}")
    for s in sorted(base):
        b = base[s]
        if not b["n"]:
            continue
        print(f"{s:>5}{b['n']:>7}{100*b['win']/b['n']:>7.1f}{b['net_sum']/b['n']:>+8.2f}")

    print(f"\nLock-detection sweep (side={side}). 'locked'=we'd BUY at the ask, hold to settle.")
    print(f"EV columns are cents/contract, net of Kalshi fee. netEV=large-order (amortized "
          f"fee); netEV1=1-contract (fee rounds up to >=1c).")
    print("Economics counted ONLY on transactable quotes (coherent bid<=ask<100); "
          "crossed/no-upside dropped (R1-C1).")
    print(f"{'T-Xs':>5}{'bps':>5}{'buy_n':>7}{'txN':>6}{'drop%':>7}{'win%':>7}"
          f"{'lossDodge%':>11}{'winGiveup%':>11}{'avgPx':>7}{'grossEV':>8}{'netEV':>7}{'netEV1':>7}")
    for key in sorted(agg):
        s, bps = key
        a = agg[key]
        lw = 100 * a["locked_win"] / a["locked_n"] if a["locked_n"] else float("nan")
        ld = 100 * a["losers_passed"] / a["losers"] if a["losers"] else float("nan")
        wg = 100 * a["winners_passed"] / a["winners"] if a["winners"] else float("nan")
        considered = a["econ_n"] + a["dropped"]
        drop_pct = 100 * a["dropped"] / considered if considered else float("nan")
        n = a["econ_n"] or 1
        avg_px = a["price_sum"] / n
        gross_ev = a["gross_sum"] / n
        net_ev = a["net_sum"] / n
        net1_ev = a["net1_sum"] / n
        print(f"{s:>5}{bps:>5}{a['locked_n']:>7}{a['econ_n']:>6}{drop_pct:>7.1f}{lw:>7.1f}"
              f"{ld:>11.1f}{wg:>11.1f}{avg_px:>7.1f}{gross_ev:>+8.2f}{net_ev:>+7.2f}{net1_ev:>+7.2f}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/tmp/state.db", help="Path to state.db")
    ap.add_argument("--since", default=None, help="ISO date lower bound (observation_time)")
    ap.add_argument("--asset", default=None, help="Restrict to one asset")
    ap.add_argument("--lock", action="store_true", help="Run the lock-detection sweep")
    ap.add_argument("--source", default="ppo", choices=("ppo", "evalopps"),
                    help="ppo=position_price_observations (held, dense); "
                         "evalopps=all-opportunities (traded+passed, coarse)")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    if args.source == "evalopps":
        windows = _load_eval_opps_windows(conn, args.since)
    else:
        windows = _load_settled_windows(conn, args.since)
    if args.asset:
        windows = [w for w in windows if w["asset"] == args.asset]

    # Principle-0 data-quality gate: flag windows whose derived close is
    # inconsistent (clock skew) or whose strike drifted mid-window.
    bad = [w for w in windows if w["close_spread_s"] > 5 or w["threshold_drift"]]
    print(f"Loaded {len(windows)} settled 15M windows ({len(bad)} flagged "
          f"close-skew/strike-drift, excluded from tallies).")
    windows = [w for w in windows if w["close_spread_s"] <= 5 and not w["threshold_drift"]]

    if args.lock:
        return run_lock_sweep(windows)

    # Per decision point × band × outcome: convergence vs instantaneous accuracy
    # + McNemar discordant pairs (conv-right/inst-wrong vs conv-wrong/inst-right).
    from collections import defaultdict
    tally = defaultdict(lambda: {
        "n": 0, "conv_ok": 0, "inst_ok": 0, "conv_only": 0, "inst_only": 0,
    })
    for w in windows:
        for s in DECISION_POINTS_S:
            decision_dt = _parse_iso(w["close_time"]) - timedelta(seconds=s)
            res = evaluate_window(
                observations=w["observations"],
                close_time=w["close_time"],
                decision_time=decision_dt.isoformat().replace("+00:00", "Z"),
                threshold=w["threshold"],
                actual_result=w["actual_result"],
            )
            if res["convergence_side"] is None or res["instantaneous_side"] is None:
                continue
            key = (s, _band(w["final_yes_ask"]), w["actual_result"])
            t = tally[key]
            t["n"] += 1
            t["conv_ok"] += int(res["convergence_correct"])
            t["inst_ok"] += int(res["instantaneous_correct"])
            if res["convergence_correct"] and not res["instantaneous_correct"]:
                t["conv_only"] += 1
            if res["instantaneous_correct"] and not res["convergence_correct"]:
                t["inst_only"] += 1

    print(f"\n{'T-Xs':>5} {'band':>6} {'side':>4} {'n':>6} "
          f"{'conv%':>7} {'inst%':>7} {'Δpp':>6} {'conv_only':>9} {'inst_only':>9}")
    for key in sorted(tally):
        s, band, side = key
        t = tally[key]
        if t["n"] == 0:
            continue
        cp = 100 * t["conv_ok"] / t["n"]
        ip = 100 * t["inst_ok"] / t["n"]
        print(f"{s:>5} {band:>6} {side:>4} {t['n']:>6} "
              f"{cp:>7.1f} {ip:>7.1f} {cp-ip:>+6.1f} "
              f"{t['conv_only']:>9} {t['inst_only']:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
