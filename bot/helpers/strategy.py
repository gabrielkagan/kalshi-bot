"""Bit 3.2: Execution-strategy evaluator, extracted from bot/_impl.py."""
import math
from typing import Dict, Optional, Tuple

from bot.constants import *  # noqa: F401,F403 — STRATEGY_*, ESCALATION_*, MIN_/MAX_ENTRY_PRICE, MAX_SECONDS_BEFORE_CLOSE

def evaluate_execution_strategy(market_data: Dict) -> Tuple[str, Dict]:
    """Intelligent decision engine that evaluates current conditions and
    returns the optimal execution strategy.

    Args:
        market_data: Dict with keys:
            z_score (float): signed z-score from ProbabilityEngine
            calibrated_prob (float): calibrated win probability
            spot (float): current spot price
            threshold (float): strike/threshold price
            seconds_to_close (float): seconds remaining until close
            blended_rv (float): blended realized volatility
            vol_regime (str): "normal" or "elevated"
            best_yes_ask (int|None): current best ask in cents
            best_ask_depth (int): contracts at best ask level
            total_ob_depth (int): total orderbook depth (contracts)
            convergence_velocity (float): upward ask movement in cents/30s
            edge (float): calibrated_prob - market_price/100
            min_entry_price (int): product-type min entry price in cents
            max_entry_price (int): product-type max entry price in cents

    Returns:
        (strategy, scores) where strategy is one of STRATEGY_* constants
        and scores is a diagnostic dict with the component scores.
    """
    z = abs(market_data.get("z_score", 0))
    remaining = market_data.get("seconds_to_close", 999)
    best_ask = market_data.get("best_yes_ask")
    ask_depth = market_data.get("best_ask_depth", 999)
    total_depth = market_data.get("total_ob_depth", 999)
    velocity = market_data.get("convergence_velocity", 0)
    _min_price = market_data.get("min_entry_price", MIN_ENTRY_PRICE)
    _max_price = market_data.get("max_entry_price", MAX_ENTRY_PRICE)
    vol_regime = market_data.get("vol_regime", "normal")
    edge = market_data.get("edge", 0)
    spot = market_data.get("spot", 0)
    threshold = market_data.get("threshold", 0)
    blended_rv = market_data.get("blended_rv", 0)

    # ── 1. Outcome Certainty Score (0-10) ─────────────────────────────────
    # z-score contribution: maps |z| 0→0, 2→3, 3→5, 4→7, 5+→9
    certainty_z = min(9.0, z * 1.8)

    # Distance from threshold: how far is spot from strike in vol terms?
    # If spot is far above threshold (for "above" bets), outcome is more certain
    certainty_distance = 0.0
    if spot > 0 and blended_rv > 0 and remaining > 0:
        sigma_move = spot * blended_rv * math.sqrt(remaining / 5.0)
        if sigma_move > 0:
            # How many sigma away is threshold? (positive = spot above threshold)
            dist_sigma = (spot - threshold) / sigma_move
            # Map: 0σ→0, 1σ→2, 2σ→4, 3σ→6, 4+σ→8
            certainty_distance = min(8.0, max(0.0, dist_sigma * 2.0))

    # Vol trend: collapsing vol = more certain, spiking = less certain
    certainty_vol_adj = 0.0
    if vol_regime == "elevated":
        certainty_vol_adj = -1.5  # spiking vol = less certain

    certainty_score = min(10.0, max(0.0,
        0.5 * certainty_z + 0.4 * certainty_distance + 0.1 * 5.0
        + certainty_vol_adj
    ))

    # ── 2. Orderbook State Score (0-10) ───────────────────────────────────
    # Higher score = more urgency to take (thin/converging book)
    if best_ask is None:
        # Empty orderbook = extreme signal
        ob_score = 10.0
    else:
        # Depth score: fewer contracts = more urgent to take
        # 0 contracts → 10, 10 → 5, 50+ → 0
        depth_score = max(0.0, 10.0 - ask_depth * 0.2)

        # Total book thinness: <20 contracts total = very thin
        book_thin_score = max(0.0, min(10.0, (50 - total_depth) * 0.25))

        # Price level: higher ask = more converged = more urgent
        # 85¢→0, 90¢→3, 95¢→7, 99¢→10
        price_score = max(0.0, min(10.0, (best_ask - 85) * 0.71))

        # Convergence velocity: ask moving up fast
        velocity_score = min(10.0, max(0.0, velocity * 1.5))

        ob_score = (0.25 * depth_score + 0.20 * book_thin_score
                    + 0.25 * price_score + 0.30 * velocity_score)

    # ── 3. Urgency Score (0-10) ───────────────────────────────────────────
    # NOT a hard cutoff — continuous function of time remaining
    # 240s→1, 180s→2.5, 90s→5.5, 60s→6.8, 30s→8.5, 15s→9.5
    if remaining <= 0:
        urgency_time = 10.0
    elif remaining >= MAX_SECONDS_BEFORE_CLOSE:
        urgency_time = 1.0
    else:
        # Exponential curve: more urgency as time shrinks
        urgency_time = 10.0 - 9.0 * (remaining / MAX_SECONDS_BEFORE_CLOSE) ** 0.6

    # Combine time urgency with convergence signal
    urgency_convergence = min(3.0, velocity * 0.5)
    # Thin book adds urgency
    urgency_liquidity = 0.0
    if ask_depth < 10:
        urgency_liquidity = min(3.0, (10 - ask_depth) * 0.4)

    urgency_score = min(10.0, 0.6 * urgency_time
                        + 0.2 * urgency_convergence
                        + 0.2 * urgency_liquidity)

    # ── Composite & Decision ──────────────────────────────────────────────
    # Weighted composite — certainty matters most
    composite = (0.45 * certainty_score
                 + 0.25 * ob_score
                 + 0.30 * urgency_score)

    scores = {
        "certainty": round(certainty_score, 2),
        "certainty_detail": {
            "z_score": round(z, 4),
            "distance_from_threshold": round(certainty_distance, 2),
            "vol_trend": vol_regime,
            "vol_adj": round(certainty_vol_adj, 2),
        },
        "orderbook": round(ob_score, 2),
        "orderbook_detail": {
            "depth": ask_depth,
            "total_depth": total_depth,
            "price_level": best_ask,
            "convergence_velocity": round(velocity, 2),
        },
        "urgency": round(urgency_score, 2),
        "urgency_detail": {
            "time_remaining": round(remaining, 1),
            "convergence_velocity": round(velocity, 2),
            "liquidity_trend": round(urgency_liquidity, 2),
        },
        "composite": round(composite, 2),
        "strategy": None,   # filled below
        "reason": None,      # filled below
    }

    def _decide(strategy: str, reason: str) -> Tuple[str, Dict]:
        scores["strategy"] = strategy
        scores["reason"] = reason
        return (strategy, scores)

    # ── PANIC_CAPTURE: outcome obvious + book dried up ────────────────────
    if (certainty_score >= 7.0
            and (best_ask is None or best_ask > 95 or total_depth < 20)
            and z >= 3.5):
        return _decide(STRATEGY_PANIC_CAPTURE,
                        f"certainty={certainty_score:.1f} z={z:.1f} "
                        f"depth={total_depth} ask={best_ask}")

    # ── TAKER_NOW: conditions demand immediate execution ──────────────────
    if best_ask is not None and _min_price <= best_ask <= ESCALATION_MAX_ENTRY:
        if composite >= 6.5:
            return _decide(STRATEGY_TAKER_NOW,
                            f"composite={composite:.1f}>=6.5")
        if velocity > 5 and _min_price <= best_ask <= _max_price:
            return _decide(STRATEGY_TAKER_NOW,
                            f"velocity={velocity:.1f}>5 ask={best_ask}")
        if certainty_score >= 6.0 and ob_score >= 6.0:
            return _decide(STRATEGY_TAKER_NOW,
                            f"certainty={certainty_score:.1f}>=6 "
                            f"ob={ob_score:.1f}>=6")

    # ── MAKER_AGGRESSIVE: confident but book still has depth ──────────────
    if composite >= 4.5:
        return _decide(STRATEGY_MAKER_AGGRESSIVE,
                        f"composite={composite:.1f}>=4.5")
    if certainty_score >= 5.0 and urgency_score >= 5.0:
        return _decide(STRATEGY_MAKER_AGGRESSIVE,
                        f"certainty={certainty_score:.1f}>=5 "
                        f"urgency={urgency_score:.1f}>=5")

    # ── MAKER_PATIENT: normal conditions ──────────────────────────────────
    if edge > 0 and best_ask is not None and _min_price <= best_ask <= _max_price:
        return _decide(STRATEGY_MAKER_PATIENT,
                        f"edge={edge:.4f}>0 ask={best_ask}")

    # ── WAIT: not confident enough ────────────────────────────────────────
    return _decide(STRATEGY_WAIT,
                    f"composite={composite:.1f} edge={edge:.4f}")
