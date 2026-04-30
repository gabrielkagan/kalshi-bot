#!/usr/bin/env python3
"""Backtesting harness for parameter optimization.

Replays historical signals through configurable filter/sizing pipeline.
Two modes:
  - FILTER mode: What if we applied different caps/thresholds to ACTUAL trades?
    Uses settled_trades. 100% accurate for restriction analysis.
  - EXPANSION mode: What if we loosened thresholds to capture more signals?
    Uses evaluated_opportunities. Approximate — doesn't model fill rate or
    execution mechanics. Directionally useful, not PnL-precise.

Usage:
    # Filter mode (default) — what if we capped/gated actual trades?
    python3 scripts/backtest.py --db /tmp/state.db --since 2026-03-30

    # Sweep SOL interventions
    python3 scripts/backtest.py --db /tmp/state.db --since 2026-03-30 --sweep sol

    # Expansion mode — what if we loosened edge thresholds?
    python3 scripts/backtest.py --db /tmp/state.db --since 2026-03-30 --mode expand --sweep edge

    # Gate an asset
    python3 scripts/backtest.py --db /tmp/state.db --since 2026-03-30 --gate SOL

    # With train/validation split
    python3 scripts/backtest.py --db /tmp/state.db --since 2026-03-30 --validate
"""

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── Current live config ───────────────────────────────────────────────────

DEFAULT_MIN_EDGE_BY_PRICE = [
    (97, 0.010), (95, 0.0075), (93, 0.005),
    (91, 0.0020), (89, 0.0025), (0, 0.0025),
]
DEFAULT_ENTRY_FLOORS = {"BTC": 89, "ETH": 90, "SOL": 80, "XRP": 92}
DEFAULT_ASSET_RISK = {"BTC": 0.15, "ETH": 0.20, "SOL": 0.15, "XRP": 0.15}
DEFAULT_SOL_MIN_EDGE = 0.010
DEFAULT_MAX_RISK = 0.25

SIZING_TIERS = [
    (0.04, 0.25), (0.025, 0.20), (0.018, 0.15), (0.012, 0.10),
    (0.009, 0.07), (0.007, 0.05), (0.005, 0.03), (0.0025, 0.02),
]

MAIN_STRATEGIES = ('MAKER_PATIENT', 'TAKER_NOW', 'MAKER_AGGRESSIVE', 'PANIC_CAPTURE')


@dataclass
class Config:
    name: str = "baseline"
    entry_floors: dict = field(default_factory=lambda: dict(DEFAULT_ENTRY_FLOORS))
    asset_risk: dict = field(default_factory=lambda: dict(DEFAULT_ASSET_RISK))
    sol_min_edge: float = DEFAULT_SOL_MIN_EDGE
    max_risk: float = DEFAULT_MAX_RISK
    max_contracts: dict = field(default_factory=dict)
    gate_assets: set = field(default_factory=set)
    # For expansion mode only:
    min_edge_by_price: list = field(default_factory=lambda: list(DEFAULT_MIN_EDGE_BY_PRICE))

    def get_min_edge(self, price: int) -> float:
        for floor, edge in self.min_edge_by_price:
            if price >= floor:
                return edge
        return 0.005


def calc_fee(count: int, price: int) -> int:
    return math.ceil(0.07 * count * price * (100 - price) / 100)


def get_risk_tier(fee_adj_edge: float) -> float:
    for min_e, frac in SIZING_TIERS:
        if fee_adj_edge >= min_e:
            return frac
    return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# FILTER MODE — Replay ACTUAL trades with alternate caps/gates
# ═══════════════════════════════════════════════════════════════════════════

def load_actual_trades(db_path: str, since: Optional[str] = None) -> List[Dict]:
    """Load settled 15M main pipeline trades. Ground truth."""
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA busy_timeout=10000")
    db.row_factory = sqlite3.Row
    strats = "','".join(MAIN_STRATEGIES)
    where = f"WHERE product_type='15m' AND strategy IN ('{strats}')"
    if since:
        where += f" AND settled_at >= '{since}'"
    rows = db.execute(f"""
        SELECT ticker, event_ticker, asset, strategy, entry_price_cents,
               count, pnl_cents, fee_cents, market_result, settled_at,
               calibrated_prob, edge, kelly_f, seconds_to_close,
               escalation_type
        FROM settled_trades {where}
        ORDER BY settled_at
    """).fetchall()
    return [dict(r) for r in rows]


def filter_replay(config: Config, trades: List[Dict], balance: float = 90000) -> 'Result':
    """Replay actual trades with alternate caps/gates.

    Uses ACTUAL trade counts and PnL by default. Only modifies count when
    an explicit contract cap (max_contracts) applies. Does NOT apply
    balance-dependent risk caps — those are already baked into the actual
    trade's sizing from the real bot's balance at trade time. Applying them
    with a fixed simulated balance produces path-dependent errors.

    For balance-dependent what-ifs, use expansion mode.
    """
    result_trades = []
    by_asset = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0})
    peak = balance
    max_dd = 0.0

    for t in trades:
        asset = t["asset"]
        price = t["entry_price_cents"]
        count = t["count"]  # actual count from real trade
        result = t["market_result"]

        # Gate check — exclude entire asset
        if asset in config.gate_assets:
            continue

        # Entry floor check — exclude trades below floor
        floor = config.entry_floors.get(asset, 75)
        if price < floor:
            continue

        # Contract cap — reduce count if explicit cap set (balance-independent)
        capped = False
        if asset in config.max_contracts and count > config.max_contracts[asset]:
            count = config.max_contracts[asset]
            capped = True

        # PnL: use actual if not capped, recompute if capped
        if capped:
            fee = calc_fee(count, price)
            if result in ("yes", "all_yes"):
                pnl = count * (100 - price) - fee
            else:
                pnl = -(count * price) - fee
        else:
            pnl = t["pnl_cents"]  # actual PnL from real trade

        result_trades.append({
            "ticker": t["ticker"], "asset": asset, "price": price,
            "contracts": count, "pnl": pnl, "result": result,
            "strategy": t["strategy"], "edge": t.get("edge", 0),
        })

        balance += pnl
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

        by_asset[asset]["n"] += 1
        if pnl > 0:
            by_asset[asset]["wins"] += 1
        by_asset[asset]["pnl"] += pnl

    return build_result(config.name, result_trades, by_asset, balance, peak, max_dd)


# ═══════════════════════════════════════════════════════════════════════════
# EXPANSION MODE — Replay evaluated signals with alternate edge thresholds
# ═══════════════════════════════════════════════════════════════════════════

EXPANSION_STAGES = (
    'candidate', 'insufficient_edge', 'relaxed_edge_shadow',
    'zero_sizing', 'single_asset_selection',
    # R-bleed-1 R9-MED: bleed-cell blocks intercept candidates and write
    # them under cell tags. Include those tags so expansion-signal
    # backtest universe stays complete post-activation.
    '96C_SOL_XRP_STC_DANGER_BAND',
    'TM98_97_98C_2_5MIN_BLEED',
    'SOL_TAKER_85_89C_2_5MIN_BLEED',
)


def load_expansion_signals(db_path: str, since: Optional[str] = None) -> List[Dict]:
    """Load signals that reached the edge gate (candidates + rejections)."""
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA busy_timeout=10000")
    db.row_factory = sqlite3.Row
    stages = "','".join(EXPANSION_STAGES)
    where = f"WHERE status='settled' AND market_result IS NOT NULL AND product_type='15m'"
    where += f" AND filter_stage IN ('{stages}')"
    if since:
        where += f" AND evaluation_time >= '{since}'"

    rows = db.execute(f"""
        SELECT ticker, asset, market_price, calibrated_prob, edge,
               fee_adjusted_edge, seconds_to_close, market_result,
               filter_stage, evaluation_time
        FROM evaluated_opportunities {where}
        ORDER BY ticker,
                 CASE WHEN filter_stage='candidate' THEN 0 ELSE 1 END,
                 fee_adjusted_edge DESC
    """).fetchall()

    # Dedup: one per ticker (prefer candidate row)
    seen = set()
    signals = []
    for r in rows:
        if r["ticker"] in seen:
            continue
        seen.add(r["ticker"])
        signals.append(dict(r))
    signals.sort(key=lambda s: s["evaluation_time"])
    return signals


def expansion_replay(config: Config, signals: List[Dict], balance: float = 90000) -> 'Result':
    """Replay signals with alternate edge thresholds.

    IMPORTANT: This mode is APPROXIMATE. It doesn't model:
    - Fill rate (~33% of candidates actually fill)
    - Execution mechanics (maker/taker, slippage)
    - Per-asset locks and ticker cooldowns
    Use for DIRECTIONAL comparison between configs, not absolute PnL.
    """
    result_trades = []
    by_asset = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0})
    peak = balance
    max_dd = 0.0

    for sig in signals:
        asset = sig["asset"]
        price = sig["market_price"]
        fee_adj_edge = sig["fee_adjusted_edge"]
        result = sig["market_result"]

        if not price or not fee_adj_edge:
            continue
        if asset in config.gate_assets:
            continue

        # Floor check
        floor = config.entry_floors.get(asset, 75)
        if price < floor:
            continue

        # Edge check
        min_edge = config.get_min_edge(price)
        if asset == "SOL":
            min_edge = max(min_edge, config.sol_min_edge)
        if fee_adj_edge < min_edge:
            continue

        if balance <= 0:
            continue

        # Sizing
        risk_frac = get_risk_tier(fee_adj_edge)
        if risk_frac <= 0:
            continue
        contracts = max(1, int(balance * risk_frac / price))

        # Caps
        asset_cap = config.asset_risk.get(asset, config.max_risk)
        contracts = min(contracts, max(1, int(balance * asset_cap / price)))
        contracts = min(contracts, max(1, int(balance * config.max_risk / price)))
        if asset in config.max_contracts:
            contracts = min(contracts, config.max_contracts[asset])

        # PnL
        fee = calc_fee(contracts, price)
        if result in ("yes", "all_yes"):
            pnl = contracts * (100 - price) - fee
        else:
            pnl = -(contracts * price) - fee

        result_trades.append({
            "ticker": sig["ticker"], "asset": asset, "price": price,
            "contracts": contracts, "pnl": pnl, "result": result,
            "edge": fee_adj_edge, "stage": sig["filter_stage"],
        })

        balance += pnl
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

        by_asset[asset]["n"] += 1
        if pnl > 0:
            by_asset[asset]["wins"] += 1
        by_asset[asset]["pnl"] += pnl

    return build_result(config.name, result_trades, by_asset, balance, peak, max_dd)


# ═══════════════════════════════════════════════════════════════════════════
# Results and display
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Result:
    config_name: str
    trades: list
    n_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: int = 0
    total_fees: int = 0
    max_drawdown_pct: float = 0.0
    final_balance: float = 0.0
    by_asset: dict = field(default_factory=dict)

    @property
    def wr(self):
        return self.wins / self.n_trades if self.n_trades else 0

    @property
    def avg_pnl(self):
        return self.total_pnl / self.n_trades if self.n_trades else 0


def build_result(name, trades, by_asset, final_bal, peak, max_dd):
    return Result(
        config_name=name, trades=trades,
        n_trades=len(trades),
        wins=sum(1 for t in trades if t["pnl"] > 0),
        losses=sum(1 for t in trades if t["pnl"] <= 0),
        total_pnl=sum(t["pnl"] for t in trades),
        max_drawdown_pct=round(max_dd * 100, 2),
        final_balance=final_bal,
        by_asset=dict(by_asset),
    )


def print_result(r: Result, verbose=False):
    print(f"\n{'=' * 60}")
    print(f"  {r.config_name}")
    print(f"{'=' * 60}")
    print(f"  Trades: {r.n_trades} ({r.wins}W/{r.losses}L)")
    print(f"  Win Rate: {r.wr:.1%}")
    print(f"  PnL: ${r.total_pnl / 100:.2f}  (avg ${r.avg_pnl / 100:.2f}/trade)")
    print(f"  Max Drawdown: {r.max_drawdown_pct:.1f}%")
    print(f"  Final Balance: ${r.final_balance / 100:.2f}")
    if r.by_asset:
        print(f"\n  {'Asset':<6} {'N':>5} {'W':>5} {'WR':>7} {'PnL':>10}")
        print(f"  {'-' * 35}")
        for a in sorted(r.by_asset):
            d = r.by_asset[a]
            wr = d["wins"] / d["n"] * 100 if d["n"] else 0
            print(f"  {a:<6} {d['n']:>5} {d['wins']:>5} {wr:>6.1f}% ${d['pnl'] / 100:>8.2f}")
    if verbose:
        losses = [t for t in r.trades if t["pnl"] <= 0]
        if losses:
            print(f"\n  Losses ({len(losses)}):")
            for t in losses:
                print(f"    {t['ticker']}: {t['asset']} {t['contracts']}ct@{t['price']}c ${t['pnl']/100:.2f}")


def print_sweep(results: List[Result], mode="filter"):
    label = "[FILTER]" if mode == "filter" else "[EXPAND ~approx]"
    print(f"\n{label}")
    print(f"{'Config':<28} {'N':>5} {'WR':>7} {'PnL':>10} {'Avg':>7} {'MaxDD':>6}")
    print("-" * 68)
    for r in results:
        print(f"{r.config_name:<28} {r.n_trades:>5} {r.wr:>6.1%} "
              f"${r.total_pnl / 100:>8.2f} ${r.avg_pnl / 100:>5.2f} {r.max_drawdown_pct:>5.1f}%")


# ═══════════════════════════════════════════════════════════════════════════
# Sweep presets
# ═══════════════════════════════════════════════════════════════════════════

def sweep_sol(trades, balance):
    """SOL-specific interventions on actual trades."""
    configs = [
        Config(name="BASELINE (current)"),
        Config(name="gate SOL", gate_assets={"SOL"}),
        Config(name="SOL floor 85c", entry_floors={"BTC": 89, "ETH": 90, "SOL": 85, "XRP": 92}),
        Config(name="SOL floor 88c", entry_floors={"BTC": 89, "ETH": 90, "SOL": 88, "XRP": 92}),
        Config(name="SOL floor 90c", entry_floors={"BTC": 89, "ETH": 90, "SOL": 90, "XRP": 92}),
        Config(name="SOL cap 30ct", max_contracts={"SOL": 30}),
        Config(name="SOL cap 50ct", max_contracts={"SOL": 50}),
        # Note: risk % sweeps require expansion mode (balance-dependent sizing).
        # Filter mode uses actual trade sizes, so risk % changes have no effect.
    ]
    return [filter_replay(c, trades, balance) for c in configs]


def sweep_btc(trades, balance):
    """BTC-specific interventions on actual trades."""
    configs = [
        Config(name="BASELINE"),
        Config(name="gate BTC", gate_assets={"BTC"}),
        Config(name="BTC cap 15ct", max_contracts={"BTC": 15}),
        Config(name="BTC cap 30ct", max_contracts={"BTC": 30}),
        Config(name="BTC floor 90c", entry_floors={"BTC": 90, "ETH": 90, "SOL": 80, "XRP": 92}),
        Config(name="BTC floor 93c", entry_floors={"BTC": 93, "ETH": 90, "SOL": 80, "XRP": 92}),
        # Note: risk % sweeps require expansion mode (balance-dependent).
    ]
    return [filter_replay(c, trades, balance) for c in configs]


def sweep_caps(trades, balance):
    """Per-asset risk cap sweep on actual trades."""
    results = []
    for sol_r in [0.06, 0.08, 0.12, 0.15]:
        for btc_r in [0.10, 0.15, 0.20]:
            c = Config(
                name=f"SOL={sol_r:.0%} BTC={btc_r:.0%}",
                asset_risk={"BTC": btc_r, "ETH": 0.20, "SOL": sol_r, "XRP": 0.15},
            )
            results.append(filter_replay(c, trades, balance))
    return results


def sweep_edge_expand(signals, balance):
    """Edge threshold sweep in expansion mode."""
    results = []
    for mult in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]:
        c = Config(
            name=f"edge {mult:.1f}x",
            min_edge_by_price=[(p, e * mult) for p, e in DEFAULT_MIN_EDGE_BY_PRICE],
            sol_min_edge=DEFAULT_SOL_MIN_EDGE * mult,
        )
        results.append(expansion_replay(c, signals, balance))
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Backtest parameter configurations")
    parser.add_argument("--db", default="/tmp/state.db")
    parser.add_argument("--since", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--mode", choices=["filter", "expand"], default="filter",
                        help="filter=actual trades, expand=include rejections (approximate)")
    parser.add_argument("--sweep", choices=["sol", "btc", "caps", "edge", "all"])
    parser.add_argument("--validate", action="store_true", help="60/40 train/validate split")
    parser.add_argument("--balance", type=float, default=900.0, help="Starting balance ($)")
    parser.add_argument("--gate", nargs="+", help="Assets to exclude")
    parser.add_argument("--btc-cap", type=int, help="Max BTC contracts")
    parser.add_argument("--sol-cap", type=int, help="Max SOL contracts")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    balance = int(args.balance * 100)

    if args.mode == "filter":
        print(f"Loading actual main pipeline trades from {args.db}...")
        data = load_actual_trades(args.db, since=args.since)
        print(f"Loaded {len(data)} settled trades")
        if not data:
            print("No trades found.")
            sys.exit(1)
        print(f"Date range: {data[0]['settled_at'][:10]} to {data[-1]['settled_at'][:10]}")

        if args.validate:
            split = int(len(data) * 0.6)
            train, val = data[:split], data[split:]
            print(f"Train: {len(train)} | Validate: {len(val)}")
        else:
            train, val = data, None

        if args.sweep:
            if args.sweep == "sol":
                print_sweep(sweep_sol(train, balance))
            elif args.sweep == "btc":
                print_sweep(sweep_btc(train, balance))
            elif args.sweep == "caps":
                print_sweep(sweep_caps(train, balance))
            elif args.sweep == "all":
                print("\n── SOL Interventions ──")
                print_sweep(sweep_sol(train, balance))
                print("\n── BTC Interventions ──")
                print_sweep(sweep_btc(train, balance))
                print("\n── Asset Cap Sweep ──")
                print_sweep(sweep_caps(train, balance))
            if val:
                print("\n── Validation (baseline config) ──")
                print_result(filter_replay(Config(name="VALIDATE"), val, balance))
        else:
            cfg = Config(name="current_live")
            if args.gate:
                cfg.gate_assets = set(args.gate)
            if args.btc_cap:
                cfg.max_contracts["BTC"] = args.btc_cap
            if args.sol_cap:
                cfg.max_contracts["SOL"] = args.sol_cap
            print_result(filter_replay(cfg, train, balance), verbose=args.verbose)

    elif args.mode == "expand":
        print(f"Loading expansion signals from {args.db}... (APPROXIMATE MODE)")
        data = load_expansion_signals(args.db, since=args.since)
        print(f"Loaded {len(data)} unique settled signals")
        if not data:
            sys.exit(1)
        print(f"Date range: {data[0]['evaluation_time'][:10]} to {data[-1]['evaluation_time'][:10]}")
        print("⚠  Expansion mode doesn't model fill rate (~33%), execution, or asset locks.")
        print("   Use for RELATIVE comparison between configs, not absolute PnL.\n")

        if args.sweep == "edge":
            print_sweep(sweep_edge_expand(data, balance), mode="expand")
        else:
            cfg = Config(name="expand_baseline")
            if args.gate:
                cfg.gate_assets = set(args.gate)
            print_result(expansion_replay(cfg, data, balance), verbose=args.verbose)


if __name__ == "__main__":
    main()
