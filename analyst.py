#!/usr/bin/env python3
"""
AI Analyst System for Kalshi Crypto Trading Bot.

Standalone script — does NOT import bot/_impl.py.
Reads state.db read-only, calls Claude API for pattern recognition,
pushes results to Firebase dashboard and analyst_journal.jsonl.

Design principle: Python computes, LLM interprets.
All statistics pre-computed; LLM does pattern recognition + recommendations.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

import anthropic
import requests
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANALYST_JOURNAL = "analyst_journal.jsonl"
DEFAULT_DB_PATH = "state.db"

SONNET_MODEL = "claude-sonnet-4-5-20250929"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

MIN_SETTLED_FOR_CALIBRATION = 200
MIN_SETTLED_FOR_EDGE_DISCOVERY = 500
MIN_SETTLED_FOR_PARAM_OPTIMIZER = 500

# Current bot config (for param optimizer context)
# !! Keep in sync with the `bot` package — last verified 2026-05-08 (Bit 4.2.5.1 sweep) !!
# Source of truth lives in bot/constants.py (Bit 3.1) — reach via
# `import bot.constants; bot.constants.<NAME>`. Constants that historically
# lived in `config.py` (MAX_RISK_PER_TRADE, MARKET_BLEND_W, HOURLY_KELLY_FRACTION)
# reach via `import config; config.<NAME>` directly.
# Post-Bit-9.3-iii.b (2026-05-11) the legacy `bot.<NAME>` proxy form is RETIRED.
# Pinned by tests/test_analyst_current_config_sync.py (AST-based; fails on drift).
CURRENT_CONFIG = {
    "MIN_ENTRY_PRICE": 75,
    "BTC_MIN_ENTRY_PRICE": 88,
    "ETH_MIN_ENTRY_PRICE": 90,
    "XRP_MIN_ENTRY_PRICE": 92,
    "XRP_15M_SHADOW": False,
    "MAX_ENTRY_PRICE": 99,
    "MIN_EDGE_BY_PRICE": "97c→1.0%, 95c→0.75%, 93c→0.5%, 91c→0.2%, 89c→0.25%, default→0.25%",
    "MARKET_BLEND_W": 0.40,
    "MAX_RISK_PER_TRADE": 0.25,
    "MAX_SECONDS_BEFORE_CLOSE": 900,
    "STC_SHADOW_THRESHOLD": 600,
    "XRP_MAX_RISK_PER_TRADE": 0.15,
    "BTC_MAX_RISK_PER_TRADE": 0.15,
    "SOL_MIN_EDGE": 0.010,
    "MAKER_ONLY_THRESHOLD": 0.0,
    "SIZING_TIERS": "[(0.04,0.25),(0.025,0.2),(0.018,0.15),(0.012,0.1),(0.009,0.07),(0.007,0.05),(0.005,0.03),(0.0025,0.02)]",
    "DRAWDOWN_HALF_THRESHOLD": 0.85,
    "DRAWDOWN_QUARTER_THRESHOLD": 0.75,
    "DRAWDOWN_HALT_THRESHOLD": 0.65,
    "HOURLY_OBSERVATION_ONLY": True,
    "HOURLY_MARKET_BLEND_W": 0.40,
    "HOURLY_MIN_ENTRY_PRICE": 50,
    "HOURLY_MAX_RISK_PER_TRADE": 0.15,
    "HOURLY_MAX_SECONDS_BEFORE_CLOSE": 1800,
    "HOURLY_TEMPERATURE_T": 1.45,
    "HOURLY_KELLY_FRACTION": 0.25,
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("analyst")

# ---------------------------------------------------------------------------
# Pydantic Models (structured output schemas)
# ---------------------------------------------------------------------------


class LossAnalysis(BaseModel):
    root_cause: Literal[
        "volatility_underestimate",
        "calibration_overconfidence",
        "tail_event",
        "timing_issue",
        "edge_too_thin",
        "market_microstructure",
        "unknown",
    ]
    confidence: Literal["high", "medium", "low"]
    analysis: str
    model_blind_spot: str
    preventable: bool
    suggested_action: str
    pattern_with_previous_losses: Optional[str] = None


class CalibrationFinding(BaseModel):
    bias_type: str
    description: str
    affected_asset: Optional[str] = None
    estimated_pnl_impact_cents: int
    confidence: Literal["high", "medium", "low"]
    suggested_action: str


class CalibrationAuditResult(BaseModel):
    findings: List[CalibrationFinding]
    overall_brier: float
    per_asset_brier: Dict[str, float]
    summary: str


class EdgeFinding(BaseModel):
    filter_name: str
    missed_winners: int
    missed_profit_cents: int
    false_rejection_rate: float
    suggested_change: str


class EdgeDiscoveryResult(BaseModel):
    missed_profit_cents: int
    biggest_leak: str
    findings: List[EdgeFinding]
    summary: str



class ParamRecommendation(BaseModel):
    param_name: str
    current_value: str
    suggested_value: str
    rationale: str
    expected_pnl_impact_cents: int
    confidence: Literal["high", "medium", "low"]
    downside_risk: str


class ParamOptimizerResult(BaseModel):
    recommendations: List[ParamRecommendation]
    summary: str


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def calculate_fee(count: int, price_cents: int, is_taker: bool) -> int:
    """Fee in cents. Kalshi charges $0 on maker fills."""
    if not is_taker:
        return 0
    return math.ceil(0.07 * count * price_cents * (100 - price_cents) / 100)


def calculate_taker_fee(count: int, price_cents: int) -> int:
    return calculate_fee(count, price_cents, is_taker=True)


def calculate_maker_fee(count: int, price_cents: int) -> int:
    return calculate_fee(count, price_cents, is_taker=False)


def wilson_ci(wins: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score 95% confidence interval for win rate."""
    if total == 0:
        return (0.0, 1.0)
    p = wins / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


def brier_score(predicted_probs: List[float], outcomes: List[int]) -> float:
    """Brier score: mean squared error of probability predictions."""
    if not predicted_probs:
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(predicted_probs, outcomes)) / len(
        predicted_probs
    )


_FB_KEY_BAD = str.maketrans(
    {".": "_", "$": "_", "#": "_", "[": "(", "]": ")", "/": "|"}
)


def sanitize_keys(obj: object) -> object:
    """Recursively sanitize dict keys for Firebase."""
    if isinstance(obj, dict):
        return {
            str(k).translate(_FB_KEY_BAD): sanitize_keys(v) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [sanitize_keys(v) for v in obj]
    return obj


def write_journal(entry: dict, path: str = ANALYST_JOURNAL) -> None:
    """Append entry to JSONL journal."""
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open state.db read-only with WAL compatibility."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _price_bucket(price_cents: Optional[int]) -> str:
    """Bucket a price in cents into a label. Lower-bucket floor tracks
    CURRENT_CONFIG["MIN_ENTRY_PRICE"] so the label stays correct as the bot
    floor moves (was 80, now 75 for ETH 75c+ live tier)."""
    if price_cents is None:
        return "unknown"
    if price_cents < 85:
        return f"{CURRENT_CONFIG['MIN_ENTRY_PRICE']}-84"
    if price_cents < 90:
        return "85-89"
    if price_cents < 95:
        return "90-94"
    return "95-99"


# ---------------------------------------------------------------------------
# Pre-computation layer
# ---------------------------------------------------------------------------


def compute_calibration_stats(rows: List[sqlite3.Row]) -> dict:
    """
    Pre-compute calibration statistics from settled evaluated_opportunities.
    Groups by asset, price bucket, and product_type.
    """
    groups: Dict[str, dict] = {}  # key -> {wins, total, preds, outcomes}

    overall_preds: List[float] = []
    overall_outcomes: List[int] = []
    per_asset: Dict[str, dict] = {}
    per_product: Dict[str, dict] = {}

    for row in rows:
        asset = row["asset"]
        price = row["market_price"]
        cal_prob = row["calibrated_prob"]
        result = row["market_result"]
        product = row["product_type"] or "15m"

        if cal_prob is None or result is None:
            continue

        outcome = 1 if result == "yes" else 0
        bucket = _price_bucket(price)

        overall_preds.append(cal_prob)
        overall_outcomes.append(outcome)

        # Per-asset
        if asset not in per_asset:
            per_asset[asset] = {"preds": [], "outcomes": []}
        per_asset[asset]["preds"].append(cal_prob)
        per_asset[asset]["outcomes"].append(outcome)

        # Per-product
        if product not in per_product:
            per_product[product] = {"preds": [], "outcomes": []}
        per_product[product]["preds"].append(cal_prob)
        per_product[product]["outcomes"].append(outcome)

        # Per group (asset x bucket)
        key = f"{asset}_{bucket}"
        if key not in groups:
            groups[key] = {
                "asset": asset,
                "bucket": bucket,
                "wins": 0,
                "total": 0,
                "sum_pred": 0.0,
                "preds": [],
                "outcomes": [],
            }
        g = groups[key]
        g["wins"] += outcome
        g["total"] += 1
        g["sum_pred"] += cal_prob
        g["preds"].append(cal_prob)
        g["outcomes"].append(outcome)

    # Build tables
    group_table = []
    for key, g in sorted(groups.items()):
        n = g["total"]
        wr = g["wins"] / n if n else 0
        avg_pred = g["sum_pred"] / n if n else 0
        ci_lo, ci_hi = wilson_ci(g["wins"], n)
        bs = brier_score(g["preds"], g["outcomes"])
        group_table.append(
            {
                "asset": g["asset"],
                "bucket": g["bucket"],
                "count": n,
                "win_rate": round(wr, 4),
                "avg_predicted_prob": round(avg_pred, 4),
                "calibration_gap": round(wr - avg_pred, 4),
                "wilson_ci_lower": round(ci_lo, 4),
                "wilson_ci_upper": round(ci_hi, 4),
                "brier_score": round(bs, 6),
            }
        )

    overall_bs = brier_score(overall_preds, overall_outcomes)
    asset_brier = {
        a: round(brier_score(d["preds"], d["outcomes"]), 6)
        for a, d in per_asset.items()
    }
    product_brier = {
        p: round(brier_score(d["preds"], d["outcomes"]), 6)
        for p, d in per_product.items()
    }

    return {
        "overall_brier": round(overall_bs, 6),
        "per_asset_brier": asset_brier,
        "per_product_brier": product_brier,
        "group_table": group_table,
        "total_settled": len(overall_preds),
    }


def compute_edge_stats(rows: List[sqlite3.Row]) -> dict:
    """
    Pre-compute edge discovery statistics from settled evaluated_opportunities.
    Groups by filter_stage, computes missed winners and counterfactual PnL.
    """
    stages: Dict[str, dict] = {}

    for row in rows:
        stage = row["filter_stage"]
        result = row["market_result"]
        price = row["market_price"]
        cal_prob = row["calibrated_prob"]

        if result is None:
            continue

        if stage not in stages:
            stages[stage] = {
                "total": 0,
                "missed_winners": 0,
                "missed_profit_cents": 0,
            }

        s = stages[stage]
        s["total"] += 1

        won = result == "yes"
        if won and stage not in ("candidate", "observation_trade"):
            s["missed_winners"] += 1
            # Counterfactual profit: (100 - price) - maker_fee for 1 contract
            if (
                price is not None
                and CURRENT_CONFIG["MIN_ENTRY_PRICE"]
                <= price
                <= CURRENT_CONFIG["MAX_ENTRY_PRICE"]
            ):
                profit = (100 - price) - calculate_maker_fee(1, price)
                s["missed_profit_cents"] += profit

    stage_table = []
    for stage, s in sorted(stages.items()):
        frr = s["missed_winners"] / s["total"] if s["total"] else 0
        stage_table.append(
            {
                "filter_stage": stage,
                "total_rejected": s["total"],
                "missed_winners": s["missed_winners"],
                "missed_profit_cents": s["missed_profit_cents"],
                "false_rejection_rate": round(frr, 4),
            }
        )

    total_missed = sum(s["missed_profit_cents"] for s in stages.values())

    # Counterfactual: what if edge thresholds were halved?
    # Current price-dependent: 97c→1.0%, 95c→0.75%, 93c→0.5%, 91c→0.2%, 89c→0.25%, <89c→0.25%
    # Mirrors bot.constants.MIN_EDGE_BY_PRICE; pinned by tests/test_analyst_current_config_sync.py.
    _EDGE_SCHEDULE = [(97, 0.01), (95, 0.0075), (93, 0.005), (91, 0.002), (89, 0.0025), (0, 0.0025)]

    def _get_min_edge(price_cents):
        for threshold, edge in _EDGE_SCHEDULE:
            if price_cents >= threshold:
                return edge
        return 0.0025

    edge_counterfactual = {"description": "edge thresholds halved"}
    recaptured = 0
    recaptured_losses = 0
    for row in rows:
        if row["filter_stage"] != "insufficient_edge":
            continue
        if row["market_result"] is None:
            continue
        fee_edge = row["fee_adjusted_edge"]
        price = row["market_price"]
        if (
            fee_edge is not None
            and price is not None
            and CURRENT_CONFIG["MIN_ENTRY_PRICE"] <= price <= CURRENT_CONFIG["MAX_ENTRY_PRICE"]
        ):
            half_threshold = _get_min_edge(price) / 2.0
            if fee_edge >= half_threshold:
                if row["market_result"] == "yes":
                    recaptured += (100 - price) - calculate_maker_fee(1, price)
                else:
                    recaptured_losses += price + calculate_maker_fee(1, price)
    edge_counterfactual["recaptured_profit_cents"] = recaptured
    edge_counterfactual["recaptured_losses_cents"] = recaptured_losses
    edge_counterfactual["net_cents"] = recaptured - recaptured_losses

    return {
        "stage_table": stage_table,
        "total_missed_profit_cents": total_missed,
        "edge_counterfactual": edge_counterfactual,
    }


def compute_loss_context(loss_row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    """Pre-compute context for a single loss analysis."""
    ticker = loss_row["ticker"]
    event_ticker = loss_row["event_ticker"]
    asset = loss_row["asset"]

    # Full loss details
    loss = dict(loss_row)

    # Last 10 settled trades for comparison
    recent = conn.execute(
        "SELECT * FROM settled_trades ORDER BY settled_at DESC LIMIT 10"
    ).fetchall()
    recent_trades = [dict(r) for r in recent]

    # Same-event evaluated opportunities
    same_event = conn.execute(
        "SELECT * FROM evaluated_opportunities WHERE event_ticker = ? "
        "ORDER BY evaluation_time",
        (event_ticker,),
    ).fetchall()
    same_event_opps = [dict(r) for r in same_event]

    # Calibration bucket stats for this trade's probability range
    price = loss_row["entry_price_cents"]
    bucket = _price_bucket(price)
    bucket_rows = conn.execute(
        "SELECT calibrated_prob, market_result FROM evaluated_opportunities "
        "WHERE status = 'settled' AND market_result IS NOT NULL "
        "AND market_price >= ? AND market_price < ?",
        (
            int(bucket.split("-")[0]),
            int(bucket.split("-")[1]) + 1 if "-" in bucket else 100,
        ),
    ).fetchall()
    bucket_wins = sum(1 for r in bucket_rows if r["market_result"] == "yes")
    bucket_total = len(bucket_rows)
    bucket_ci = wilson_ci(bucket_wins, bucket_total)

    # All previous losses
    all_losses = conn.execute(
        "SELECT * FROM settled_trades WHERE pnl_cents < 0 ORDER BY settled_at"
    ).fetchall()
    previous_losses = [dict(r) for r in all_losses]

    return {
        "loss": loss,
        "recent_trades": recent_trades,
        "same_event_opportunities": same_event_opps[:20],  # cap for token budget
        "bucket_stats": {
            "bucket": bucket,
            "wins": bucket_wins,
            "total": bucket_total,
            "win_rate": round(bucket_wins / bucket_total, 4) if bucket_total else None,
            "wilson_ci": [round(bucket_ci[0], 4), round(bucket_ci[1], 4)],
        },
        "previous_losses": previous_losses,
    }


def _format_table(headers: List[str], rows: List[list]) -> str:
    """Format a list of rows as a markdown table."""
    if not rows:
        return "(no data)\n"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    hdr = "| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"
    sep = "|-" + "-|-".join("-" * w for w in widths) + "-|"
    body = "\n".join(
        "| "
        + " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))
        + " |"
        for row in rows
    )
    return f"{hdr}\n{sep}\n{body}\n"


def _cal_stats_to_markdown(stats: dict) -> str:
    """Convert calibration stats dict to markdown for LLM consumption."""
    lines = [
        f"## Overall Brier Score: {stats['overall_brier']}",
        f"## Total Settled Evaluations: {stats['total_settled']}",
        "",
        "## Per-Asset Brier Scores",
    ]
    for asset, bs in sorted(stats["per_asset_brier"].items()):
        lines.append(f"- **{asset}**: {bs}")

    lines.append("")
    lines.append("## Per-Product-Type Brier Scores")
    for prod, bs in sorted(stats["per_product_brier"].items()):
        lines.append(f"- **{prod}**: {bs}")

    lines.append("")
    lines.append("## Calibration by Asset x Price Bucket")
    headers = [
        "Asset",
        "Bucket",
        "Count",
        "WinRate",
        "AvgPred",
        "CalGap",
        "CI_Lo",
        "CI_Hi",
        "Brier",
    ]
    rows = []
    for g in stats["group_table"]:
        rows.append(
            [
                g["asset"],
                g["bucket"],
                g["count"],
                g["win_rate"],
                g["avg_predicted_prob"],
                g["calibration_gap"],
                g["wilson_ci_lower"],
                g["wilson_ci_upper"],
                g["brier_score"],
            ]
        )
    lines.append(_format_table(headers, rows))
    return "\n".join(lines)


def _edge_stats_to_markdown(stats: dict) -> str:
    """Convert edge stats dict to markdown for LLM consumption."""
    lines = [
        f"## Total Missed Profit: {stats['total_missed_profit_cents']}c",
        "",
        "## Rejection by Filter Stage",
    ]
    headers = ["Stage", "Total", "MissedWins", "MissedProfit(c)", "FalseRejRate"]
    rows = []
    for s in stats["stage_table"]:
        rows.append(
            [
                s["filter_stage"],
                s["total_rejected"],
                s["missed_winners"],
                s["missed_profit_cents"],
                s["false_rejection_rate"],
            ]
        )
    lines.append(_format_table(headers, rows))

    ec = stats["edge_counterfactual"]
    lines.append(f"## Edge Counterfactual: {ec.get('description', 'halved thresholds')}")
    lines.append(f"- Recaptured profit: {ec['recaptured_profit_cents']}c")
    lines.append(f"- Recaptured losses: {ec['recaptured_losses_cents']}c")
    lines.append(f"- **Net: {ec['net_cents']}c**")
    return "\n".join(lines)


def _loss_context_to_markdown(ctx: dict) -> str:
    """Convert loss context to markdown for LLM consumption."""
    loss = ctx["loss"]
    lines = [
        "## Loss Details",
        f"- **Ticker**: {loss.get('ticker')}",
        f"- **Asset**: {loss.get('asset')}",
        f"- **Entry Price**: {loss.get('entry_price_cents')}c",
        f"- **Contracts**: {loss.get('count')}",
        f"- **PnL**: {loss.get('pnl_cents')}c",
        f"- **Revenue**: {loss.get('revenue_cents')}c",
        f"- **Fee**: {loss.get('fee_cents')}c",
        f"- **Side**: {loss.get('side')}",
        f"- **Strategy**: {loss.get('strategy')}",
        f"- **Seconds to Close**: {loss.get('seconds_to_close')}",
        f"- **Fill Latency**: {loss.get('fill_latency_seconds')}s",
        f"- **Vol Regime**: {loss.get('vol_regime')}",
        f"- **Calibrated Prob**: {loss.get('calibrated_prob')}",
        f"- **Edge**: {loss.get('edge')}",
        f"- **Settled At**: {loss.get('settled_at')}",
        "",
        "## Bucket Stats (same price range)",
        f"- Bucket: {ctx['bucket_stats']['bucket']}",
        f"- Win Rate: {ctx['bucket_stats']['win_rate']} ({ctx['bucket_stats']['wins']}/{ctx['bucket_stats']['total']})",
        f"- Wilson 95% CI: [{ctx['bucket_stats']['wilson_ci'][0]}, {ctx['bucket_stats']['wilson_ci'][1]}]",
        "",
    ]

    # Previous losses
    lines.append(f"## All Previous Losses ({len(ctx['previous_losses'])} total)")
    for pl in ctx["previous_losses"]:
        lines.append(
            f"- {pl.get('ticker')}: {pl.get('asset')} {pl.get('entry_price_cents')}c "
            f"PnL={pl.get('pnl_cents')}c strategy={pl.get('strategy')} "
            f"seconds_to_close={pl.get('seconds_to_close')}"
        )

    # Same event opportunities (truncated)
    lines.append("")
    lines.append(
        f"## Same-Event Opportunities ({len(ctx['same_event_opportunities'])} shown)"
    )
    for opp in ctx["same_event_opportunities"][:10]:
        lines.append(
            f"- {opp.get('ticker')}: stage={opp.get('filter_stage')} "
            f"asset={opp.get('asset')} price={opp.get('market_price')}c "
            f"cal_prob={opp.get('calibrated_prob')} edge={opp.get('edge')}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompts (cached via cache_control)
# ---------------------------------------------------------------------------

LOSS_POSTMORTEM_SYSTEM = """You are an expert quantitative analyst reviewing a loss from a cryptocurrency prediction market trading bot on Kalshi.

The bot trades above/below markets on BTC, ETH, SOL, and XRP. It buys YES contracts when it believes the probability of the asset finishing above a threshold is higher than the market price implies. A "loss" means the asset finished below the threshold and the YES contract expired worthless.

Your job is to analyze the loss and identify the root cause. All statistics have been pre-computed for you — do NOT perform any calculations yourself. Use the data as presented.

Root cause categories (ranked by frequency):
1. **volatility_underestimate** — realized vol exceeded model estimate, probability was too high
2. **calibration_overconfidence** — model systematically overestimates win probability in this bucket
3. **tail_event** — rare large move, fundamentally unpredictable
4. **timing_issue** — entry timing was suboptimal (too early, too late, wrong seconds_to_close)
5. **edge_too_thin** — edge was technically positive but too small to overcome variance
6. **market_microstructure** — fill price, slippage, or execution issue

Important rules:
- Do NOT suggest raising MIN_ENTRY_PRICE without data showing losses are concentrated at specific price levels
- At 92.7% WR, ~7 losses per 96 trades is EXPECTED — do not treat every loss as a crisis
- Do NOT perform any arithmetic or calculations — all stats are pre-computed
- Rate confidence: HIGH (clear evidence in data), MEDIUM (probable given patterns), LOW (speculative)
- Look for PATTERNS across previous losses, not just this individual loss
- Be specific and actionable in suggested_action"""

CALIBRATION_AUDIT_SYSTEM = """You are a calibration analyst reviewing the prediction accuracy of a cryptocurrency trading bot on Kalshi.

**Brier Score**: Mean squared error of probability predictions. 0.0 = perfect, 0.25 = random coin flip. Lower is better.
**Calibration Gap**: Actual win rate minus predicted probability. Positive = underconfident (good for a trading bot). Negative = overconfident (dangerous).
**Wilson CI**: 95% confidence interval on the true win rate. Wide intervals mean insufficient data — do not act on wide CIs.

Your job is to find systematic biases in the bot's probability predictions. All statistics are pre-computed — do NOT calculate anything yourself.

Look for:
- Systematic over/underconfidence by asset, price bucket, or product type (15m vs hourly)
- Calibration gaps that are consistently in one direction
- Assets that are significantly worse-calibrated than others
- Price buckets where the bot is dangerously overconfident

Rules:
- Only flag findings with estimated PnL impact > 200c ($2.00)
- Explicitly call out when confidence intervals are too wide to draw conclusions
- If overall calibration is good, say so — do not invent problems
- Do NOT perform any calculations — all stats are pre-computed for you"""

EDGE_DISCOVERY_SYSTEM = """You are analyzing rejected trading opportunities to find profitable trades the bot is missing.

The bot's filter pipeline stages (in order):
1. **low_probability** — calibrated prob too low to trade
2. **price_out_of_range** — best ask outside entry price bounds
3. **insufficient_edge** — net edge after fees below minimum threshold
4. **candidate** / **observation_trade** — passed all filters

A "missed winner" is a rejected opportunity that would have won (settled YES). The false rejection rate is missed_winners / total_rejected.

All statistics are pre-computed — do NOT calculate anything yourself.

Focus on:
- Filter stages with high false rejection rates AND material missed profit
- Edge counterfactual: are the price-dependent edge thresholds (0.2%-1.0%) optimal?
- Whether price_out_of_range rejections are actually blocking winners at specific price levels

Rules:
- Only recommend changes with net positive counterfactual PnL
- A high false rejection rate at price_out_of_range is expected (most markets close in-the-money)
- Weight missed profit against the risk of additional losses
- Be specific: "lower MIN_EDGE to X%" not "consider relaxing filters\""""

PARAM_OPTIMIZER_SYSTEM = """You are a parameter optimization advisor for a live cryptocurrency trading bot on Kalshi.

CRITICAL CONTEXT: This bot is trading REAL MONEY. The current configuration is LIVE and PROFITABLE. The burden of proof is on any proposed change.

Current configuration:
{config_block}

Rules:
- Every recommendation MUST cite the pre-computed counterfactual PnL data
- If counterfactual shows < 200c ($2) improvement, recommend NO CHANGE
- Never recommend changing more than 1 parameter at a time
- Always state the downside risk explicitly
- "No change recommended" is a perfectly valid and often correct output
- Do NOT perform any calculations — all data is pre-computed
- Consider that parameters interact: changing one may invalidate assumptions of others"""


# ---------------------------------------------------------------------------
# Analyst class
# ---------------------------------------------------------------------------


class Analyst:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._db_path = db_path
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        self._tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self._analyzed_losses = self._load_analyzed_losses()

    def _send_telegram(self, message: str) -> None:
        """Send analyst findings to Telegram."""
        if not self._tg_token or not self._tg_chat:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._tg_token}/sendMessage",
                json={
                    "chat_id": self._tg_chat,
                    "text": message[:4096],
                    "parse_mode": "Markdown",
                },
                timeout=5,
            )
        except Exception as e:
            log.warning("Telegram send failed: %s", e)

    def _load_analyzed_losses(self) -> set:
        """Load set of already-analyzed loss tickers from journal."""
        analyzed = set()
        journal_path = Path(ANALYST_JOURNAL)
        if not journal_path.exists():
            return analyzed
        for line in journal_path.read_text().splitlines():
            try:
                entry = json.loads(line)
                if entry.get("agent") == "loss_postmortem":
                    ticker = entry.get("ticker")
                    if ticker:
                        analyzed.add(ticker)
            except (json.JSONDecodeError, KeyError):
                continue
        return analyzed

    def _push_firebase(self, path: str, data: dict) -> None:
        """No-op: Firebase removed. Analyst results logged to journal only."""
        pass

    def _call_llm(
        self,
        model: str,
        system: str,
        user_msg: str,
        output_type: type,
        agent_name: str,
    ) -> Optional[BaseModel]:
        """Call Claude API with structured output. Retry once on validation failure."""
        for attempt in range(2):
            try:
                message = self._client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=[
                        {
                            "type": "text",
                            "text": system,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": user_msg}],
                )
                # Extract text content
                text = ""
                for block in message.content:
                    if block.type == "text":
                        text = block.text
                        break

                # Try to parse JSON from the response
                # Look for JSON in code blocks first
                json_str = text
                if "```json" in text:
                    start = text.index("```json") + 7
                    end = text.index("```", start)
                    json_str = text[start:end].strip()
                elif "```" in text:
                    start = text.index("```") + 3
                    end = text.index("```", start)
                    json_str = text[start:end].strip()

                # Try to find JSON object boundaries
                if json_str.strip().startswith("{"):
                    pass  # already looks like JSON
                elif "{" in json_str:
                    json_str = json_str[json_str.index("{") :]

                parsed = json.loads(json_str)
                result = output_type.model_validate(parsed)
                return result

            except (json.JSONDecodeError, Exception) as e:
                if attempt == 0:
                    log.warning(
                        "%s: parse failed (attempt %d), retrying: %s",
                        agent_name,
                        attempt + 1,
                        e,
                    )
                    # Append error to user message for retry
                    user_msg += (
                        f"\n\nIMPORTANT: Your previous response failed to parse. "
                        f"Error: {e}\n"
                        f"You MUST respond with ONLY a valid JSON object matching "
                        f"this schema: {output_type.model_json_schema()}"
                    )
                else:
                    log.error(
                        "%s: parse failed after 2 attempts: %s",
                        agent_name,
                        e,
                        exc_info=True,
                    )
                    return None
        return None

    # -------------------------------------------------------------------
    # Data readiness check
    # -------------------------------------------------------------------

    def check_data_readiness(self) -> dict:
        """Check how much settled data is available for each agent."""
        conn = _open_db(self._db_path)
        try:
            settled = conn.execute(
                "SELECT COUNT(*) FROM evaluated_opportunities "
                "WHERE status='settled' AND market_result IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()

        readiness = {
            "settled_evals": settled,
            "calibration_ready": settled >= MIN_SETTLED_FOR_CALIBRATION,
            "calibration_needed": MIN_SETTLED_FOR_CALIBRATION,
            "edge_ready": settled >= MIN_SETTLED_FOR_EDGE_DISCOVERY,
            "edge_needed": MIN_SETTLED_FOR_EDGE_DISCOVERY,
            "param_ready": settled >= MIN_SETTLED_FOR_PARAM_OPTIMIZER,
            "param_needed": MIN_SETTLED_FOR_PARAM_OPTIMIZER,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._push_firebase("data_readiness", readiness)
        return readiness

    # -------------------------------------------------------------------
    # Agent 1: Loss Post-Mortem
    # -------------------------------------------------------------------

    def run_loss_postmortem(self) -> List[dict]:
        """Analyze any new losses not yet in the journal."""
        conn = _open_db(self._db_path)
        try:
            losses = conn.execute(
                "SELECT * FROM settled_trades WHERE pnl_cents < 0 ORDER BY settled_at"
            ).fetchall()

            results = []
            for loss_row in losses:
                ticker = loss_row["ticker"]
                if ticker in self._analyzed_losses:
                    continue

                log.info("Analyzing loss: %s (%s)", ticker, loss_row["asset"])
                ctx = compute_loss_context(loss_row, conn)
                md = _loss_context_to_markdown(ctx)

                user_msg = (
                    f"Analyze this trading loss. Respond with ONLY a JSON object "
                    f"matching this schema: {LossAnalysis.model_json_schema()}\n\n{md}"
                )

                analysis = self._call_llm(
                    model=SONNET_MODEL,
                    system=LOSS_POSTMORTEM_SYSTEM,
                    user_msg=user_msg,
                    output_type=LossAnalysis,
                    agent_name="loss_postmortem",
                )

                if analysis is not None:
                    result = {
                        "agent": "loss_postmortem",
                        "ticker": ticker,
                        "asset": loss_row["asset"],
                        "pnl_cents": loss_row["pnl_cents"],
                        "analysis": analysis.model_dump(),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    write_journal(result)
                    self._analyzed_losses.add(ticker)
                    results.append(result)

                    # Push to Firebase
                    self._push_firebase(
                        f"loss_postmortems/{ticker.replace('-', '_')}",
                        {
                            "asset": loss_row["asset"],
                            "pnl_cents": loss_row["pnl_cents"],
                            "root_cause": analysis.root_cause,
                            "confidence": analysis.confidence,
                            "analysis": analysis.analysis[:500],
                            "preventable": analysis.preventable,
                            "suggested_action": analysis.suggested_action[:300],
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    log.info(
                        "Loss %s analyzed: %s (%s confidence)",
                        ticker,
                        analysis.root_cause,
                        analysis.confidence,
                    )
                    self._send_telegram(
                        f"\U0001f50d *Loss Postmortem: {ticker}*\n"
                        f"Asset: {loss_row['asset']} | PnL: ${loss_row['pnl_cents']/100:+.2f}\n"
                        f"Root cause: *{analysis.root_cause}* ({analysis.confidence})\n"
                        f"{analysis.analysis[:300]}\n"
                        f"Preventable: {analysis.preventable}\n"
                        f"Action: {analysis.suggested_action[:200]}"
                    )
                else:
                    results.append(
                        {
                            "agent": "loss_postmortem",
                            "ticker": ticker,
                            "status": "parse_failed",
                        }
                    )
        finally:
            conn.close()

        if not results:
            log.info("No new losses to analyze")
        return results

    # -------------------------------------------------------------------
    # Agent 2: Calibration Audit
    # -------------------------------------------------------------------

    def run_calibration_audit(self) -> dict:
        """Run daily calibration audit on settled evaluated opportunities."""
        readiness = self.check_data_readiness()
        if not readiness["calibration_ready"]:
            result = {
                "status": "insufficient_data",
                "settled_count": readiness["settled_evals"],
                "needed": MIN_SETTLED_FOR_CALIBRATION,
            }
            self._push_firebase("calibration_audit", result)
            log.info(
                "Calibration audit: insufficient data (%d/%d)",
                readiness["settled_evals"],
                MIN_SETTLED_FOR_CALIBRATION,
            )
            return result

        conn = _open_db(self._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM evaluated_opportunities "
                "WHERE status='settled' AND market_result IS NOT NULL "
                "AND filter_stage IN ('candidate', 'observation_trade', "
                "'insufficient_edge', 'price_out_of_range', "
                "'hourly_observation', 'spx_observation', 'weather_observation') "
                "AND calibrated_prob IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

        stats = compute_calibration_stats(rows)
        md = _cal_stats_to_markdown(stats)

        # Add product_type breakdown to markdown
        pt_brier = stats.get("per_product_brier", {})
        if len(pt_brier) > 1:
            md += "\n\n## WARNING: Multiple product types in data\n"
            md += "Analyze calibration SEPARATELY per product type. "
            md += "15M calibration does NOT transfer to hourly.\n"
            for pt, bs in sorted(pt_brier.items()):
                md += f"- **{pt}**: Brier={bs}\n"

        user_msg = (
            f"Review these calibration statistics and identify any systematic biases. "
            f"Respond with ONLY a JSON object matching this schema: "
            f"{CalibrationAuditResult.model_json_schema()}\n\n{md}"
        )

        audit = self._call_llm(
            model=SONNET_MODEL,
            system=CALIBRATION_AUDIT_SYSTEM,
            user_msg=user_msg,
            output_type=CalibrationAuditResult,
            agent_name="calibration_audit",
        )

        if audit is not None:
            result = {
                "agent": "calibration_audit",
                "analysis": audit.model_dump(),
                "pre_computed_stats": stats,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            write_journal(result)
            self._push_firebase(
                "calibration_audit",
                {
                    "overall_brier": audit.overall_brier,
                    "per_asset_brier": audit.per_asset_brier,
                    "findings_count": len(audit.findings),
                    "summary": audit.summary[:500],
                    "findings": [
                        {
                            "bias_type": f.bias_type,
                            "affected_asset": f.affected_asset,
                            "impact_cents": f.estimated_pnl_impact_cents,
                            "confidence": f.confidence,
                        }
                        for f in audit.findings
                    ],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            log.info(
                "Calibration audit complete: Brier=%.4f, %d findings",
                audit.overall_brier,
                len(audit.findings),
            )
            return result

        return {"status": "error", "error": "LLM parse failed"}

    # -------------------------------------------------------------------
    # Agent 3: Edge Discovery
    # -------------------------------------------------------------------

    def run_edge_discovery(self) -> dict:
        """Find profitable trades the bot is missing."""
        readiness = self.check_data_readiness()
        if not readiness["edge_ready"]:
            result = {
                "status": "insufficient_data",
                "settled_count": readiness["settled_evals"],
                "needed": MIN_SETTLED_FOR_EDGE_DISCOVERY,
            }
            self._push_firebase("edge_discovery", result)
            log.info(
                "Edge discovery: insufficient data (%d/%d)",
                readiness["settled_evals"],
                MIN_SETTLED_FOR_EDGE_DISCOVERY,
            )
            return result

        conn = _open_db(self._db_path)
        try:
            # Filter to crypto 15M only — hourly/SPX/weather have different edge profiles
            rows = conn.execute(
                "SELECT * FROM evaluated_opportunities "
                "WHERE status='settled' AND market_result IS NOT NULL "
                "AND (product_type IS NULL OR product_type = '15m')"
            ).fetchall()
        finally:
            conn.close()

        stats = compute_edge_stats(rows)
        md = _edge_stats_to_markdown(stats)

        user_msg = (
            f"Analyze these rejection statistics and identify missed profitable trades. "
            f"Respond with ONLY a JSON object matching this schema: "
            f"{EdgeDiscoveryResult.model_json_schema()}\n\n{md}"
        )

        discovery = self._call_llm(
            model=HAIKU_MODEL,
            system=EDGE_DISCOVERY_SYSTEM,
            user_msg=user_msg,
            output_type=EdgeDiscoveryResult,
            agent_name="edge_discovery",
        )

        if discovery is not None:
            result = {
                "agent": "edge_discovery",
                "analysis": discovery.model_dump(),
                "pre_computed_stats": stats,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            write_journal(result)
            self._push_firebase(
                "edge_discovery",
                {
                    "missed_profit_cents": discovery.missed_profit_cents,
                    "biggest_leak": discovery.biggest_leak[:200],
                    "findings_count": len(discovery.findings),
                    "summary": discovery.summary[:500],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            log.info(
                "Edge discovery complete: %dc missed, %d findings",
                discovery.missed_profit_cents,
                len(discovery.findings),
            )
            return result

        return {"status": "error", "error": "LLM parse failed"}

    # -------------------------------------------------------------------
    # Agent 4: Param Optimizer
    # -------------------------------------------------------------------

    def run_param_optimizer(
        self,
        calibration_result: Optional[dict] = None,
        edge_result: Optional[dict] = None,
    ) -> dict:
        """Recommend parameter changes based on calibration + edge findings."""
        readiness = self.check_data_readiness()
        if not readiness["param_ready"]:
            result = {
                "status": "insufficient_data",
                "settled_count": readiness["settled_evals"],
                "needed": MIN_SETTLED_FOR_PARAM_OPTIMIZER,
            }
            self._push_firebase("param_recommendations", result)
            log.info(
                "Param optimizer: insufficient data (%d/%d)",
                readiness["settled_evals"],
                MIN_SETTLED_FOR_PARAM_OPTIMIZER,
            )
            return result

        # Build config block for system prompt
        config_lines = [f"- {k} = {v}" for k, v in CURRENT_CONFIG.items()]
        config_block = "\n".join(config_lines)
        system = PARAM_OPTIMIZER_SYSTEM.format(config_block=config_block)

        # Build user message from other agents' outputs
        sections = []

        if calibration_result and calibration_result.get("analysis"):
            cal = calibration_result["analysis"]
            sections.append("## Calibration Audit Results")
            sections.append(f"Overall Brier: {cal.get('overall_brier')}")
            sections.append(f"Summary: {cal.get('summary', 'N/A')}")
            for f in cal.get("findings", []):
                sections.append(
                    f"- [{f.get('confidence')}] {f.get('bias_type')}: "
                    f"{f.get('description', '')} (impact: {f.get('estimated_pnl_impact_cents')}c)"
                )
        else:
            sections.append(
                "## Calibration Audit: Not available (insufficient data or not run)"
            )

        if edge_result and edge_result.get("analysis"):
            edge = edge_result["analysis"]
            sections.append("")
            sections.append("## Edge Discovery Results")
            sections.append(f"Total missed profit: {edge.get('missed_profit_cents')}c")
            sections.append(f"Biggest leak: {edge.get('biggest_leak', 'N/A')}")
            sections.append(f"Summary: {edge.get('summary', 'N/A')}")
            for f in edge.get("findings", []):
                sections.append(
                    f"- {f.get('filter_name')}: {f.get('missed_winners')} missed, "
                    f"{f.get('missed_profit_cents')}c profit (FRR: {f.get('false_rejection_rate')})"
                )
            ec = edge_result.get("pre_computed_stats", {}).get(
                "edge_counterfactual", {}
            )
            if ec:
                sections.append(
                    f"\nEdge counterfactual (thresholds halved): net {ec.get('net_cents', '?')}c"
                )
        else:
            sections.append(
                "\n## Edge Discovery: Not available (insufficient data or not run)"
            )

        # Add settled trade summary
        conn = _open_db(self._db_path)
        try:
            trades = conn.execute(
                "SELECT asset, COUNT(*) as cnt, "
                "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(pnl_cents - COALESCE(fee_cents, 0)) as total_pnl, "
                "AVG(entry_price_cents) as avg_price "
                "FROM settled_trades GROUP BY asset"
            ).fetchall()
        finally:
            conn.close()

        sections.append("\n## Settled Trade Summary (by asset)")
        for t in trades:
            sections.append(
                f"- {t['asset']}: {t['wins']}/{t['cnt']} wins, "
                f"PnL={t['total_pnl']}c, avg_price={t['avg_price']:.1f}c"
            )

        user_content = "\n".join(sections)
        user_msg = (
            f"Based on this analysis data, recommend parameter changes (or no changes). "
            f"Respond with ONLY a JSON object matching this schema: "
            f"{ParamOptimizerResult.model_json_schema()}\n\n{user_content}"
        )

        optimizer = self._call_llm(
            model=HAIKU_MODEL,
            system=system,
            user_msg=user_msg,
            output_type=ParamOptimizerResult,
            agent_name="param_optimizer",
        )

        if optimizer is not None:
            result = {
                "agent": "param_optimizer",
                "analysis": optimizer.model_dump(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            write_journal(result)
            self._push_firebase(
                "param_recommendations",
                {
                    "recommendation_count": len(optimizer.recommendations),
                    "summary": optimizer.summary[:500],
                    "recommendations": [
                        {
                            "param": r.param_name,
                            "current": r.current_value,
                            "suggested": r.suggested_value,
                            "impact_cents": r.expected_pnl_impact_cents,
                            "confidence": r.confidence,
                        }
                        for r in optimizer.recommendations
                    ],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            log.info(
                "Param optimizer: %d recommendations", len(optimizer.recommendations)
            )
            return result

        return {"status": "error", "error": "LLM parse failed"}

    # -------------------------------------------------------------------
    # Orchestrators
    # -------------------------------------------------------------------

    def run_all(self) -> dict:
        """Run all agents once."""
        results = {}

        agents = [
            ("loss_postmortem", self.run_loss_postmortem),
            ("calibration_audit", self.run_calibration_audit),
            ("edge_discovery", self.run_edge_discovery),
        ]

        for name, fn in agents:
            try:
                results[name] = fn()
            except Exception as e:
                log.error("Agent %s failed: %s", name, e, exc_info=True)
                results[name] = {"status": "error", "error": str(e)}

        # Param optimizer consumes calibration + edge results
        try:
            results["param_optimizer"] = self.run_param_optimizer(
                calibration_result=results.get("calibration_audit"),
                edge_result=results.get("edge_discovery"),
            )
        except Exception as e:
            log.error("Agent param_optimizer failed: %s", e, exc_info=True)
            results["param_optimizer"] = {"status": "error", "error": str(e)}

        return results

    def run_daily(self) -> dict:
        """Run daily agents: calibration, edge discovery, param optimizer."""
        results = {}

        for name, fn in [
            ("calibration_audit", self.run_calibration_audit),
            ("edge_discovery", self.run_edge_discovery),
        ]:
            try:
                results[name] = fn()
            except Exception as e:
                log.error("Agent %s failed: %s", name, e, exc_info=True)
                results[name] = {"status": "error", "error": str(e)}

        try:
            results["param_optimizer"] = self.run_param_optimizer(
                calibration_result=results.get("calibration_audit"),
                edge_result=results.get("edge_discovery"),
            )
        except Exception as e:
            log.error("Agent param_optimizer failed: %s", e, exc_info=True)
            results["param_optimizer"] = {"status": "error", "error": str(e)}

        # Telegram daily summary
        try:
            lines = ["\U0001f4ca *Daily Analyst Report*\n"]
            for agent_name, r in results.items():
                status = r.get("status", "ok") if isinstance(r, dict) else "ok"
                if status == "error":
                    lines.append(f"\u274c {agent_name}: error")
                elif status in ("skipped", "insufficient_data"):
                    lines.append(f"\u23ed {agent_name}: insufficient data ({r.get('settled_count', '?')}/{r.get('needed', '?')})")
                else:
                    lines.append(f"\u2705 {agent_name}: complete")
                    # Extract key findings
                    if agent_name == "calibration_audit" and isinstance(r, dict):
                        analysis = r.get("analysis", {})
                        brier = analysis.get("overall_brier")
                        findings = analysis.get("findings", [])
                        if brier is not None:
                            lines.append(f"   Brier: {brier:.4f} | {len(findings)} finding(s)")
                        summary = analysis.get("summary", "")
                        if summary:
                            lines.append(f"   {summary[:200]}")
                    elif agent_name == "param_optimizer" and isinstance(r, dict):
                        analysis = r.get("analysis", {})
                        recs = analysis.get("recommendations", [])
                        if recs:
                            for rec in recs[:3]:
                                if isinstance(rec, dict):
                                    lines.append(f"   \u2022 {rec.get('param_name', '')}: {rec.get('rationale', '')[:100]}")
            self._send_telegram("\n".join(lines))
        except Exception as e:
            log.warning("Daily Telegram summary failed: %s", e)

        return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Analyst for Kalshi Crypto Trading Bot"
    )
    parser.add_argument(
        "--loss-postmortem",
        action="store_true",
        help="Run loss post-mortem on new losses",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Run daily agents (calibration + edge + param optimizer)",
    )
    parser.add_argument(
        "--all", action="store_true", help="Run all agents once (default)"
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to state.db")
    args = parser.parse_args()

    # Default to --all if no specific flag
    if not any([args.loss_postmortem, args.daily, args.all]):
        args.all = True

    # Verify DB exists
    if not Path(args.db).exists():
        log.error("Database not found: %s", args.db)
        sys.exit(1)

    # Verify API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY not set in environment")
        sys.exit(1)

    analyst = Analyst(db_path=args.db)

    if args.loss_postmortem:
        results = analyst.run_loss_postmortem()
        log.info("Loss postmortem: %d analyses", len(results))

    if args.daily:
        results = analyst.run_daily()
        for name, r in results.items():
            status = r.get("status", "complete")
            log.info("  %s: %s", name, status)

    if args.all:
        results = analyst.run_all()
        for name, r in results.items():
            if isinstance(r, list):
                log.info("  %s: %d items", name, len(r))
            else:
                status = r.get("status", "complete")
                log.info("  %s: %s", name, status)


if __name__ == "__main__":
    main()
