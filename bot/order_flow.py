"""OrderFlowEngine + KalshiOrderFlowTracker.

Sprint 9 Bit 9.3.5 — Sprint 9 closing sister leaf (2026-05-10).

Extracted verbatim from bot/_impl.py:658-777 (OrderFlowEngine) and
bot/_impl.py:780-934 (KalshiOrderFlowTracker). Re-imported into
bot/_impl.py via:

    from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker

so the proxy chain (`bot.OrderFlowEngine` → `bot._impl.OrderFlowEngine`
→ `bot.order_flow.OrderFlowEngine`; identical for KOFT) stays stable.

## Clean-leaf shape (no carve-out, no late-binding)

This is the smallest extraction in Sprint 9. Both classes are
self-contained: they import only stdlib + bot.constants. AST free-var
scan returned ONLY stdlib names (logging, time, collections.deque, typing
generics) and 21 bot.constants-resident constants. Zero `_telegram_state`,
zero `_cal_state`, zero `bot._impl`-below-line-119 references, zero
numpy/scipy/torch. Mirrors Bit 9.2 SettlementTracker shape but smaller
surface.

## Bit 9.3.5 marker-collapse + 9.3-iii.a HPSB relocation

Pre-9.3.5, bot/main_loop.py used method-body late-binding to access OFE
+ KOFT inside `MainLoop.__init__`, with two `# REMOVE BIT 9.3.5` markers
documenting the cleanup contract:

    from bot._impl import (
        _HPSB_MISSING_BLEEDERS,
        _HPSB_VALIDATOR_UNAVAILABLE_REASON,
        OrderFlowEngine,            # REMOVE BIT 9.3.5
        KalshiOrderFlowTracker,     # REMOVE BIT 9.3.5
    )

Post-9.3.5, the 2 marker lines collapsed to a top-level
`from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker` in
bot/main_loop.py.

Post-Bit-9.3-iii.a (2026-05-11), the remaining HPSB pair was also relocated
out of bot._impl to clean-leaf `bot/boot.py` and is now top-imported via
`from bot.boot import _HPSB_MISSING_BLEEDERS, _HPSB_VALIDATOR_UNAVAILABLE_REASON`.
The bot/main_loop.py `MainLoop.__init__` late-binding block is GONE.

## Sister cleanup atomic in same commit

- bot/scanner/__init__.py: `Optional["OrderFlowEngine"]` and
  `Optional["KalshiOrderFlowTracker"]` forward-refs UNQUOTED post-9.3.5
  (the quoted form was a Bit 8.1 cycle-avoidance workaround no longer
  needed — bot.order_flow has zero bot.scanner edges).
- bot/_impl.py: ~1,035 → ~767 LOC. Class bodies deleted; re-export
  added immediately after the line-119 MainLoop re-export.
- bot/main_loop.py late-binding block: 4 names → 2 names (HPSB only).
- .importlinter: `helpers-leaf` `forbidden_modules` extended with
  `bot.order_flow`; net contracts unchanged at 5.

## Cross-module access patterns

- OFE accepts `cross_feed`, `coinglass`, `kalshi_oft` as constructor
  injections (all default None). The instances are created in
  `MainLoop.__init__` and passed in — OFE itself does not import
  `CrossExchangeFeed`, `CoinGlassFetcher`, or KOFT at class-body level.
- KOFT is constructor-less (just `__init__(self)`) and consumed by OFE
  via the injected `kalshi_oft` parameter.
- Neither class consumes `_telegram_state._TELEGRAM` or
  `_cal_state._CALIBRATION_ENGINE` — they're orderbook flow signals, not
  alert emitters or probability-blend consumers.

Related lessons:
  L32, L33, L38, L40, L41, L78, L86, L90, L93.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

from bot.constants import (
    CROSS_EXCHANGE_CONSENSUS_MIN,
    FUNDING_RATE_ELEVATED,
    FUNDING_RATE_EXTREME,
    KALSHI_OFT_BUFFER_SIZE,
    KALSHI_OFT_DEPTH_DRAIN_PCT,
    KALSHI_OFT_IMBALANCE_STRONG,
    KALSHI_OFT_IMBALANCE_WEAK,
    KALSHI_OFT_LOG_INTERVAL,
    KALSHI_OFT_MIN_SNAPSHOTS,
    KALSHI_OFT_SHADOW_MODE,
    KALSHI_OFT_STALE_SECONDS,
    OFA_CONSENSUS_BOOST,
    OFA_CONSENSUS_REDUCE,
    OFA_ELEVATED_FUNDING_REDUCE,
    OFA_EXTREME_FUNDING_REDUCE,
    OFA_KALSHI_CONVERGENCE_BOOST,
    OFA_KALSHI_DEPTH_DRAIN_BOOST,
    OFA_KALSHI_IMBALANCE_BOOST,
    OFA_KALSHI_IMBALANCE_REDUCE,
    OFA_LEAD_BOOST,
    OFA_MAX_ADJUSTMENT,
)


class OrderFlowEngine:
    """Aggregates cross-exchange and derivatives signals into a probability adjustment."""

    def __init__(self, cross_feed=None, coinglass=None, kalshi_oft=None):
        self._cross = cross_feed
        self._coinglass = coinglass
        self._kalshi_oft = kalshi_oft

    def get_signals(self, asset: str, **kwargs) -> Dict:
        """Compute order flow adjustment for the given asset.

        Returns:
            {
                "prob_adjustment": float,
                "confidence": "high"|"moderate"|"low"|"none",
                "signals": {
                    "cross_exchange": {...lead_lag dict...},
                    "funding": {"rate": float|None, "level": str},
                },
                "adjustments_applied": [str, ...],
            }
        """
        adjustments: List[Tuple[str, float]] = []
        cross_exchange = {}
        funding_info = {"rate": None, "level": "unknown"}

        # 1. Cross-exchange consensus
        if self._cross is not None:
            try:
                lead_lag = self._cross.get_lead_lag(asset)
                cross_exchange = lead_lag
                direction = lead_lag.get("consensus_direction", "none")
                above = lead_lag.get("exchanges_above", 0)
                below = lead_lag.get("exchanges_below", 0)

                if direction == "above" and above >= CROSS_EXCHANGE_CONSENSUS_MIN:
                    adjustments.append((
                        f"consensus_above_{above}ex",
                        OFA_CONSENSUS_BOOST,
                    ))
                elif direction == "below" and below >= CROSS_EXCHANGE_CONSENSUS_MIN:
                    adjustments.append((
                        f"consensus_below_{below}ex",
                        OFA_CONSENSUS_REDUCE,
                    ))
                elif direction == "mixed":
                    # Weaker signal: at least one exchange leads
                    if above > below:
                        adjustments.append(("lead_above_mixed", OFA_LEAD_BOOST))
                    elif below > above:
                        adjustments.append(("lead_below_mixed", -OFA_LEAD_BOOST))
            except Exception:
                logging.debug("CrossExchangeFeed.get_lead_lag failed", exc_info=True)

        # 2. Funding rate
        if self._coinglass is not None:
            try:
                rate = self._coinglass.get_funding_rate(asset)
                if rate is not None:
                    abs_rate = abs(rate)
                    if abs_rate >= FUNDING_RATE_EXTREME:
                        funding_info = {"rate": rate, "level": "extreme"}
                        adjustments.append((
                            f"extreme_funding_{rate:+.6f}",
                            OFA_EXTREME_FUNDING_REDUCE,
                        ))
                    elif abs_rate >= FUNDING_RATE_ELEVATED:
                        funding_info = {"rate": rate, "level": "elevated"}
                        adjustments.append((
                            f"elevated_funding_{rate:+.6f}",
                            OFA_ELEVATED_FUNDING_REDUCE,
                        ))
                    else:
                        funding_info = {"rate": rate, "level": "normal"}
                else:
                    funding_info = {"rate": None, "level": "unknown"}
            except Exception:
                logging.debug("CoinGlassFetcher.get_funding_rate failed", exc_info=True)

        # 3. Kalshi orderbook flow
        kalshi_flow = {}
        if self._kalshi_oft is not None:
            try:
                ticker = kwargs.get("ticker")
                if ticker:
                    koft = self._kalshi_oft.get_signals(ticker)
                    if koft is not None:
                        kalshi_flow = koft
                        if not KALSHI_OFT_SHADOW_MODE and koft["prob_adjustment"] != 0:
                            adjustments.append(("kalshi_oft", koft["prob_adjustment"]))
            except Exception:
                logging.debug("KalshiOFT.get_signals failed", exc_info=True)

        # 4. Sum and clamp
        total = sum(v for _, v in adjustments)
        total = max(-OFA_MAX_ADJUSTMENT, min(OFA_MAX_ADJUSTMENT, total))

        # 5. Confidence
        abs_total = abs(total)
        if abs_total >= 0.015:
            confidence = "high"
        elif abs_total >= 0.005:
            confidence = "moderate"
        elif abs_total > 0:
            confidence = "low"
        else:
            confidence = "none"

        return {
            "prob_adjustment": total,
            "confidence": confidence,
            "signals": {
                "cross_exchange": cross_exchange,
                "funding": funding_info,
                "kalshi_orderbook": kalshi_flow,
            },
            "adjustments_applied": [
                f"{name}: {val:+.3f}" for name, val in adjustments
            ],
        }


class KalshiOrderFlowTracker:
    """Tracks Kalshi orderbook snapshots over time for flow signals.

    Records full depth-5 snapshots from the scanner's existing orderbook
    fetches (no additional API calls). Computes:
    - Bid/ask imbalance ratio (YES depth vs total)
    - Depth velocity (total depth change rate)
    - Spread dynamics (bid-ask spread trend)
    - Ask convergence velocity (cents/sec)
    """

    def __init__(self):
        self._snapshots: Dict[str, deque] = {}
        self._last_seen: Dict[str, float] = {}
        self._last_log: Dict[str, float] = {}

    def record_snapshot(self, ticker: str, ob_data: Dict, best_ask: int):
        """Record orderbook snapshot. Called from scanner after each OB fetch.

        ob_data format: {"no": [[price_cents, qty], ...], "yes": [[price_cents, qty], ...]}
        """
        now = time.time()
        if ticker not in self._snapshots:
            self._snapshots[ticker] = deque(maxlen=KALSHI_OFT_BUFFER_SIZE)

        # Sum depth per side
        yes_total_qty = 0
        no_total_qty = 0
        best_yes_bid_price = 0

        for entry in (ob_data.get("yes") or []):
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = int(entry[0]), int(entry[1])
                yes_total_qty += qty
                if price > best_yes_bid_price:
                    best_yes_bid_price = price

        for entry in (ob_data.get("no") or []):
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                no_total_qty += int(entry[1])

        spread = (best_ask - best_yes_bid_price) if best_yes_bid_price > 0 else 99

        self._snapshots[ticker].append({
            "ts": now,
            "best_ask": best_ask,
            "best_yes_bid": best_yes_bid_price,
            "yes_total_qty": yes_total_qty,
            "no_total_qty": no_total_qty,
            "total_depth": yes_total_qty + no_total_qty,
            "spread": spread,
        })
        self._last_seen[ticker] = now

    def get_signals(self, ticker: str) -> Optional[Dict]:
        """Compute order flow signals from snapshot history. Returns None if insufficient data."""
        snaps = self._snapshots.get(ticker)
        if not snaps or len(snaps) < KALSHI_OFT_MIN_SNAPSHOTS:
            return None

        snap_list = list(snaps)
        latest = snap_list[-1]
        earliest = snap_list[0]
        time_span = latest["ts"] - earliest["ts"]
        if time_span <= 0:
            return None

        # 1. Imbalance: YES bids / total depth
        total_qty = latest["yes_total_qty"] + latest["no_total_qty"]
        imbalance = latest["yes_total_qty"] / total_qty if total_qty > 0 else 0.5

        if imbalance >= KALSHI_OFT_IMBALANCE_STRONG:
            imbalance_level = "strong_buy"
        elif imbalance <= KALSHI_OFT_IMBALANCE_WEAK:
            imbalance_level = "strong_sell"
        else:
            imbalance_level = "neutral"

        # 2. Depth velocity
        depth_velocity = (latest["total_depth"] - earliest["total_depth"]) / time_span
        depth_pct_change = ((latest["total_depth"] - earliest["total_depth"])
                           / earliest["total_depth"]) if earliest["total_depth"] > 0 else 0.0
        depth_drain = depth_pct_change < KALSHI_OFT_DEPTH_DRAIN_PCT

        # 3. Spread trend
        spread_trend = (latest["spread"] - earliest["spread"]) / time_span

        # 4. Ask velocity
        ask_velocity = (latest["best_ask"] - earliest["best_ask"]) / time_span

        # 5. Prob adjustment (shadow or live)
        adjustments = []
        if imbalance_level == "strong_buy":
            adjustments.append(("kalshi_imbalance_buy", OFA_KALSHI_IMBALANCE_BOOST))
        elif imbalance_level == "strong_sell":
            adjustments.append(("kalshi_imbalance_sell", OFA_KALSHI_IMBALANCE_REDUCE))
        if depth_drain and ask_velocity > 0:
            adjustments.append(("kalshi_depth_drain", OFA_KALSHI_DEPTH_DRAIN_BOOST))
        if ask_velocity > 0.5:
            adjustments.append(("kalshi_convergence", OFA_KALSHI_CONVERGENCE_BOOST))

        total_adj = max(-0.02, min(0.02, sum(v for _, v in adjustments)))

        # Confidence
        n_snaps = len(snap_list)
        if n_snaps >= 30 and total_qty >= 20:
            confidence = "high"
        elif n_snaps >= 15 or total_qty >= 10:
            confidence = "moderate"
        else:
            confidence = "low"

        result = {
            "imbalance_ratio": round(imbalance, 4),
            "imbalance_level": imbalance_level,
            "depth_velocity": round(depth_velocity, 2),
            "depth_drain": depth_drain,
            "depth_pct_change": round(depth_pct_change, 4),
            "spread_current": latest["spread"],
            "spread_trend": round(spread_trend, 4),
            "ask_velocity": round(ask_velocity, 4),
            "prob_adjustment": round(total_adj, 6),
            "adjustments_applied": [f"{n}: {v:+.3f}" for n, v in adjustments],
            "n_snapshots": n_snaps,
            "confidence": confidence,
        }

        # Periodic per-ticker diagnostic logging
        now = time.time()
        last_log = self._last_log.get(ticker, 0.0)
        if now - last_log >= KALSHI_OFT_LOG_INTERVAL:
            self._last_log[ticker] = now
            adj_str = ", ".join(f"{n}: {v:+.3f}" for n, v in adjustments) if adjustments else "none"
            logging.info(
                "KalshiOFT %s: imbal=%.3f (%s) depth_vel=%.1f depth_pct=%.1f%% "
                "spread=%d trend=%.3f ask_vel=%.3f adj=%.4f [%s] snaps=%d conf=%s shadow=%s",
                ticker, imbalance, imbalance_level, depth_velocity,
                depth_pct_change * 100, latest["spread"], spread_trend,
                ask_velocity, total_adj, adj_str, n_snaps, confidence,
                KALSHI_OFT_SHADOW_MODE,
            )

        return result

    def cleanup_stale(self, active_tickers: Set[str]):
        """Evict tickers no longer in active windows."""
        now = time.time()
        stale = [t for t, ts in self._last_seen.items()
                 if now - ts > KALSHI_OFT_STALE_SECONDS or t not in active_tickers]
        for t in stale:
            self._snapshots.pop(t, None)
            self._last_seen.pop(t, None)

    def get_tracked_count(self) -> int:
        return len(self._snapshots)
