#!/usr/bin/env python3
"""
Daily Research Reporter — 3x daily performance reports for the Kalshi trading bot.

Runs via cron every 30 minutes with --cron flag (auto-detects report times in ET).
Compiles trading reports from state.db and auditor findings, sends to Telegram.
No external AI APIs. Read-only access to state.db.

Usage:
    python3 researcher.py                    # Auto-detect report type from current ET time
    python3 researcher.py --cron             # Cron mode: run if it's report time, else exit
    python3 researcher.py --verbose          # Print report to stdout
    python3 researcher.py --type morning     # Force a specific report type
    python3 researcher.py --test-telegram    # Send a test message

Report schedule (ET):
    7:30am  — Morning Briefing (overnight recap, 12h window)
    12:30pm — Midday Update (morning activity, 5h window)
    7:30pm  — Evening Wrap (afternoon + daily summary, 7h window)

Adding new sections:
    1. Write a function: def section_something(ctx) -> str
    2. Add it to the appropriate SECTIONS list below
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("researcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DB_PATH = SCRIPT_DIR / "state.db"
AUDITOR_DB_PATH = SCRIPT_DIR / "auditor_state.db"
RESEARCHER_DB_PATH = SCRIPT_DIR / "researcher_state.db"
ENV_PATH = SCRIPT_DIR / ".env"

# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# ---------------------------------------------------------------------------
# Report schedule: (hour, minute) in ET
# ---------------------------------------------------------------------------
REPORT_SCHEDULE = {
    "morning": (7, 30),
    "midday": (12, 30),
    "evening": (19, 30),
}
CRON_TOLERANCE_MINUTES = 5  # How close to target time cron must be

# ---------------------------------------------------------------------------
# .env parser (matches auditor.py)
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key not in os.environ:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Telegram (matches auditor.py)
# ---------------------------------------------------------------------------

def _escape_md(text: str) -> str:
    """Escape underscores for Telegram Markdown so they display literally."""
    return text.replace("_", "\\_")


def send_telegram(message: str, token: str, chat_id: str) -> bool:
    if not token or not chat_id:
        log.warning("Telegram credentials not configured")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Escape underscores so Telegram doesn't eat them as italic
    escaped = _escape_md(message)
    # Try with Markdown first, fall back to plain text on parse errors
    for attempt_msg in (escaped, message):
        body = {
            "chat_id": chat_id,
            "text": attempt_msg[:4096],
            "parse_mode": "Markdown",
        }
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 400 and attempt_msg == escaped:
                log.info("Escaped Markdown failed, retrying with original")
                continue
            log.warning("Telegram send failed: %s", e)
            return False
        except Exception as e:
            log.warning("Telegram send failed: %s", e)
            return False
    return False


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_state_db() -> sqlite3.Connection | None:
    if not STATE_DB_PATH.exists():
        log.error("state.db not found at %s", STATE_DB_PATH)
        return None
    conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def get_auditor_db() -> sqlite3.Connection | None:
    if not AUDITOR_DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{AUDITOR_DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def get_researcher_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(RESEARCHER_DB_PATH), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_history (
            report_time TEXT,
            report_type TEXT,
            period_start TEXT,
            period_end TEXT,
            trades_covered INTEGER,
            alerts_covered INTEGER
        )
    """)
    conn.commit()
    return conn


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT count(*) as cnt FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row["cnt"] > 0


def has_column(db: sqlite3.Connection, table: str, col: str) -> bool:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def safe_div(a, b, default=0.0):
    return a / b if b else default


# ---------------------------------------------------------------------------
# Report context — shared state passed to all section functions
# ---------------------------------------------------------------------------

class ReportContext:
    def __init__(
        self,
        report_type: str,
        period_start: datetime,
        period_end: datetime,
        state_db: sqlite3.Connection,
        auditor_db: sqlite3.Connection | None,
        verbose: bool,
    ):
        self.report_type = report_type
        self.period_start = period_start
        self.period_end = period_end
        self.db = state_db
        self.auditor_db = auditor_db
        self.verbose = verbose
        # ISO strings for SQL queries (UTC)
        self.start_iso = period_start.astimezone(UTC).isoformat()
        self.end_iso = period_end.astimezone(UTC).isoformat()
        # ET display strings
        self.start_et = period_start.astimezone(ET).strftime("%I:%M%p").lstrip("0")
        self.end_et = period_end.astimezone(ET).strftime("%I:%M%p").lstrip("0")
        self.date_str = period_end.astimezone(ET).strftime("%b %d, %Y")

    @property
    def is_morning(self) -> bool:
        return self.report_type == "morning"

    @property
    def is_evening(self) -> bool:
        return self.report_type == "evening"


# ---------------------------------------------------------------------------
# Section 1: Header
# ---------------------------------------------------------------------------

def section_header(ctx: ReportContext) -> str:
    titles = {
        "morning": "Morning Briefing",
        "midday": "Midday Update",
        "evening": "Evening Wrap",
    }
    title = titles.get(ctx.report_type, "Report")

    # Daily PnL (today in ET)
    today_et = ctx.period_end.astimezone(ET).date()
    today_start_utc = datetime(today_et.year, today_et.month, today_et.day,
                               tzinfo=ET).astimezone(UTC).isoformat()
    daily_pnl = 0
    if table_exists(ctx.db, "settled_trades"):
        row = ctx.db.execute(
            "SELECT coalesce(sum(pnl_cents), 0) as pnl FROM settled_trades "
            "WHERE settled_at >= ?", (today_start_utc,)
        ).fetchone()
        daily_pnl = row["pnl"]

    # Weekly PnL
    week_start_utc = (ctx.period_end.astimezone(UTC) - timedelta(days=7)).isoformat()
    weekly_pnl = 0
    if table_exists(ctx.db, "settled_trades"):
        row = ctx.db.execute(
            "SELECT coalesce(sum(pnl_cents), 0) as pnl FROM settled_trades "
            "WHERE settled_at >= ?", (week_start_utc,)
        ).fetchone()
        weekly_pnl = row["pnl"]

    # Bankroll — latest available_balance_cents from evaluated_opportunities
    bankroll_str = "N/A"
    if table_exists(ctx.db, "evaluated_opportunities") and has_column(ctx.db, "evaluated_opportunities", "available_balance_cents"):
        row = ctx.db.execute(
            "SELECT available_balance_cents FROM evaluated_opportunities "
            "WHERE available_balance_cents IS NOT NULL "
            "ORDER BY evaluation_time DESC LIMIT 1"
        ).fetchone()
        if row and row["available_balance_cents"]:
            bankroll_str = f"${row['available_balance_cents'] / 100:.2f}"

    def fmt_pnl(cents):
        sign = "+" if cents >= 0 else ""
        return f"{sign}${cents / 100:.2f}"

    return (
        f"📊 *{title}*\n"
        f"{ctx.date_str} | {ctx.start_et} → {ctx.end_et} ET\n"
        f"Bankroll: {bankroll_str} | Daily PnL: {fmt_pnl(daily_pnl)} | Weekly PnL: {fmt_pnl(weekly_pnl)}"
    )


# ---------------------------------------------------------------------------
# Section 2: Trade Activity
# ---------------------------------------------------------------------------

def section_trade_activity(ctx: ReportContext) -> str:
    if not table_exists(ctx.db, "settled_trades"):
        return ""

    rows = ctx.db.execute(
        "SELECT ticker, asset, side, pnl_cents, entry_price_cents, "
        "product_type, escalation_type, edge "
        "FROM settled_trades WHERE settled_at >= ? AND settled_at <= ?",
        (ctx.start_iso, ctx.end_iso),
    ).fetchall()

    # Evaluated opportunities count
    eval_count = 0
    entered_count = 0
    rejected_count = 0
    if table_exists(ctx.db, "evaluated_opportunities"):
        r = ctx.db.execute(
            "SELECT count(*) as cnt FROM evaluated_opportunities "
            "WHERE evaluation_time >= ? AND evaluation_time <= ?",
            (ctx.start_iso, ctx.end_iso),
        ).fetchone()
        eval_count = r["cnt"]
        r = ctx.db.execute(
            "SELECT count(*) as cnt FROM evaluated_opportunities "
            "WHERE evaluation_time >= ? AND evaluation_time <= ? "
            "AND filter_stage IN ('candidate', 'observation_trade')",
            (ctx.start_iso, ctx.end_iso),
        ).fetchone()
        entered_count = r["cnt"]
        rejected_count = eval_count - entered_count

    if not rows and eval_count == 0:
        return ""

    lines = []
    if not rows:
        lines.append("📈 *Trades Since Last Report:* 0")
        lines.append("No trades settled this period.")
        lines.append(f"\nEvaluated: {eval_count} | Entered: {entered_count} | Rejected: {rejected_count}")
        return "\n".join(lines)

    # Group by product_type
    by_pt = {}
    for r in rows:
        pt = r["product_type"] or "15m"
        by_pt.setdefault(pt, []).append(r)

    total_pnl = sum(r["pnl_cents"] for r in rows)
    total_wins = sum(1 for r in rows if r["pnl_cents"] > 0)
    total_losses = len(rows) - total_wins

    lines.append(f"📈 *Trades Since Last Report:* {len(rows)} ({total_wins}W/{total_losses}L, {_fmt_pnl(total_pnl)})")

    # 15M breakdown
    if "15m" in by_pt:
        trades_15m = by_pt["15m"]
        w = sum(1 for t in trades_15m if t["pnl_cents"] > 0)
        l = len(trades_15m) - w
        pnl = sum(t["pnl_cents"] for t in trades_15m)
        lines.append(f"\n*15M:* {w}W/{l}L ({_fmt_pnl(pnl)})")

        # By asset
        by_asset = {}
        for t in trades_15m:
            a = t["asset"] or "?"
            by_asset.setdefault(a, []).append(t)
        asset_parts = []
        for a in sorted(by_asset.keys()):
            aw = sum(1 for t in by_asset[a] if t["pnl_cents"] > 0)
            al = len(by_asset[a]) - aw
            asset_parts.append(f"{a}: {aw}W/{al}L")
        if asset_parts:
            lines.append("  " + " | ".join(asset_parts))

    # Other product types
    for pt in ["hourly", "spx_hourly", "weather", "sports"]:
        if pt in by_pt:
            trades = by_pt[pt]
            w = sum(1 for t in trades if t["pnl_cents"] > 0)
            l = len(trades) - w
            pnl = sum(t["pnl_cents"] for t in trades)
            label = pt.replace("_", " ").title()
            lines.append(f"*{label}:* {w}W/{l}L ({_fmt_pnl(pnl)})")
        else:
            obs_labels = {
                "hourly": "Hourly: Observation only",
                "spx_hourly": "SPX: Observation only",
                "weather": "Weather: Observation only",
                "sports": "Sports: Observation only",
            }
            # Only mention if there are observation entries
            if table_exists(ctx.db, "evaluated_opportunities"):
                obs = ctx.db.execute(
                    "SELECT count(*) as cnt FROM evaluated_opportunities "
                    "WHERE evaluation_time >= ? AND evaluation_time <= ? AND product_type = ?",
                    (ctx.start_iso, ctx.end_iso, pt),
                ).fetchone()["cnt"]
                if obs > 0:
                    lines.append(f"*{obs_labels.get(pt, pt)}* ({obs} obs)")

    # Side distribution
    yes_count = sum(1 for r in rows if r["side"] == "yes")
    no_count = sum(1 for r in rows if r["side"] == "no")
    if yes_count + no_count > 0:
        lines.append(f"\nSides: {yes_count} YES / {no_count} NO")

    # Avg entry, avg edge
    entries = [r["entry_price_cents"] for r in rows if r["entry_price_cents"]]
    edges = [r["edge"] for r in rows if r["edge"] is not None]
    parts = []
    if entries:
        parts.append(f"Avg entry: {sum(entries)/len(entries):.0f}c")
    if edges:
        parts.append(f"Avg edge: {sum(edges)/len(edges):.1%}")
    if parts:
        lines.append(" | ".join(parts))

    # Maker vs taker
    if has_column(ctx.db, "settled_trades", "escalation_type"):
        maker = sum(1 for r in rows if r["escalation_type"] and "maker" in str(r["escalation_type"]).lower())
        taker = sum(1 for r in rows if r["escalation_type"] and "taker" in str(r["escalation_type"]).lower())
        if maker + taker > 0:
            lines.append(f"Maker: {maker} | Taker: {taker}")

    lines.append(f"\nEvaluated: {eval_count} | Entered: {entered_count} | Rejected: {rejected_count}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section 3: Loss Detail
# ---------------------------------------------------------------------------

def section_loss_detail(ctx: ReportContext) -> str:
    if not table_exists(ctx.db, "settled_trades"):
        return ""

    losses = ctx.db.execute(
        "SELECT ticker, asset, side, pnl_cents, entry_price_cents, "
        "count as contracts, market_result, product_type, seconds_to_close "
        "FROM settled_trades WHERE settled_at >= ? AND settled_at <= ? AND pnl_cents < 0",
        (ctx.start_iso, ctx.end_iso),
    ).fetchall()

    if not losses:
        return ""

    lines = [f"❌ *Losses This Period:* {len(losses)}"]
    for loss in losses:
        ticker = loss["ticker"]
        asset = loss["asset"] or "?"
        side = loss["side"] or "yes"
        pnl = loss["pnl_cents"]
        entry = loss["entry_price_cents"]
        contracts = loss["contracts"] or 1
        result = loss["market_result"] or "?"
        stc = loss["seconds_to_close"]

        lines.append(f"\n`{ticker}` {asset} {side.upper()}")
        lines.append(f"  Entry: {entry}c | Result: {result} | PnL: {_fmt_pnl(pnl)}")

        # Try to get model prediction from evaluated_opportunities
        if table_exists(ctx.db, "evaluated_opportunities"):
            eo = ctx.db.execute(
                "SELECT calibrated_prob, edge, market_price FROM evaluated_opportunities "
                "WHERE ticker = ? AND filter_stage IN ('candidate', 'observation_trade') LIMIT 1",
                (ticker,),
            ).fetchone()
            if eo:
                prob_str = f"{eo['calibrated_prob']:.0%}" if eo["calibrated_prob"] else "?"
                edge_str = f"{eo['edge']:.1%}" if eo["edge"] is not None else "?"
                mkt = eo["market_price"] or "?"
                lines.append(f"  Model: {prob_str} | Market: {mkt}c | Edge: {edge_str}")

        stc_str = f"{stc:.0f}s" if stc is not None else "?"
        lines.append(f"  STC: {stc_str} | Contracts: {contracts}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section 4: Auditor Alerts
# ---------------------------------------------------------------------------

def section_auditor_alerts(ctx: ReportContext) -> str:
    if ctx.auditor_db is None:
        return ""
    if not table_exists(ctx.auditor_db, "alert_history"):
        return ""

    alerts = ctx.auditor_db.execute(
        "SELECT alert_type, alert_key, last_alerted, times_seen, first_seen "
        "FROM alert_history WHERE last_alerted >= ? AND resolved = 0 "
        "ORDER BY last_alerted DESC",
        (ctx.start_iso,),
    ).fetchall()

    if not alerts:
        return ""

    lines = [f"🔍 *Auditor Alerts ({len(alerts)} since last report):*"]
    for a in alerts[:10]:  # Cap at 10 to avoid huge messages
        first = _fmt_time_et(a["first_seen"])
        lines.append(
            f"\n[{a['alert_type']}] {a['alert_key']}\n"
            f"  First seen: {first} | Occurrences: {a['times_seen']}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section 5: Model Calibration Snapshot
# ---------------------------------------------------------------------------

def section_calibration(ctx: ReportContext) -> str:
    # Only include in morning/evening, or midday if enough new trades
    if not table_exists(ctx.db, "settled_trades") or not table_exists(ctx.db, "evaluated_opportunities"):
        return ""

    cutoff_7d = (ctx.period_end.astimezone(UTC) - timedelta(days=7)).isoformat()

    rows = ctx.db.execute(
        "SELECT st.ticker, st.pnl_cents, eo.calibrated_prob, st.entry_price_cents "
        "FROM settled_trades st "
        "JOIN evaluated_opportunities eo ON st.ticker = eo.ticker "
        "WHERE st.settled_at >= ? AND st.product_type = '15m' "
        "AND eo.filter_stage IN ('candidate', 'observation_trade') "
        "AND eo.calibrated_prob IS NOT NULL",
        (cutoff_7d,),
    ).fetchall()

    if len(rows) < 10:
        return ""

    # Skip for midday unless significant new data
    if ctx.report_type == "midday" and len(rows) < 30:
        return ""

    avg_pred = sum(r["calibrated_prob"] for r in rows) / len(rows)
    wins = sum(1 for r in rows if r["pnl_cents"] > 0)
    actual_wr = wins / len(rows)
    gap = avg_pred - actual_wr

    lines = [f"🎯 *Calibration (7 days, n={len(rows)})*"]
    lines.append(
        f"Predicted avg: {avg_pred:.1%} | Actual WR: {actual_wr:.1%} | "
        f"Gap: {gap * 100:+.1f}pp"
    )

    # By price zone
    zones = [
        ("86-90c", 86, 90),
        ("90-94c", 90, 94),
        ("94-99c", 94, 99),
    ]
    zone_parts = []
    for label, lo, hi in zones:
        zone_rows = [r for r in rows if r["entry_price_cents"] and lo <= r["entry_price_cents"] <= hi]
        if len(zone_rows) >= 3:
            zp = sum(r["calibrated_prob"] for r in zone_rows) / len(zone_rows)
            zw = sum(1 for r in zone_rows if r["pnl_cents"] > 0)
            zwr = zw / len(zone_rows)
            zone_parts.append(f"  {label}: pred {zp:.0%} / actual {zwr:.0%} (n={len(zone_rows)})")

    if zone_parts:
        lines.append("By price zone:")
        lines.extend(zone_parts)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section 6: Shadow Strategy Status
# ---------------------------------------------------------------------------

def section_shadow_status(ctx: ReportContext) -> str:
    parts = []

    # 15M shadows (fifteenm_shadow_signals)
    if table_exists(ctx.db, "fifteenm_shadow_signals"):
        for approach, prob_col, pnl_col in [
            ("A1 RecalibratedEGARCH", "a1_final_prob", "a1_pnl_cents"),
            ("A2 LightGBM", "a2_calibrated_prob", "a2_pnl_cents"),
            ("A3 EGARCH Gate (10%)", "a3_gate_prob", "a3_pnl_gate10_cents"),
        ]:
            shadow = _shadow_stats(ctx.db, "fifteenm_shadow_signals", prob_col, pnl_col, ctx.start_iso)
            if shadow:
                parts.append(_format_shadow(approach, shadow))

    # Hourly alt shadows (hourly_alt_shadow_signals)
    if table_exists(ctx.db, "hourly_alt_shadow_signals"):
        for strategy_val, label in [
            ("harrv_shadow", "Hourly HAR-RV"),
            ("mm_shadow", "Hourly MM Sim"),
        ]:
            row = ctx.db.execute(
                "SELECT count(*) as total, "
                "sum(CASE WHEN status='settled' AND shadow_pnl_cents != 0 AND shadow_pnl_cents > 0 THEN 1 ELSE 0 END) as wins, "
                "sum(CASE WHEN status='settled' AND shadow_pnl_cents != 0 THEN 1 ELSE 0 END) as traded, "
                "sum(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
                "sum(CASE WHEN status='settled' AND shadow_pnl_cents != 0 THEN shadow_pnl_cents ELSE 0 END) as pnl, "
                "sum(CASE WHEN evaluation_time >= ? THEN 1 ELSE 0 END) as new_obs "
                "FROM hourly_alt_shadow_signals WHERE strategy = ?",
                (ctx.start_iso, strategy_val),
            ).fetchone()
            if row and row["total"] > 0:
                n = row["total"]
                settled = row["settled"] or 0
                traded = row["traded"] or 0
                wins = row["wins"] or 0
                pnl = row["pnl"] or 0
                new_obs = row["new_obs"] or 0
                status = _shadow_status_label(n, wins, traded, pnl)
                wr_str = f"{safe_div(wins, traded):.0%}" if traded > 0 else "N/A"
                parts.append(
                    f"*{label}:* {n} obs (+{new_obs} new)\n"
                    f"  Traded: {traded}/{settled} settled | WR: {wr_str} | "
                    f"PnL: {_fmt_pnl(pnl)}\n"
                    f"  Status: {status}"
                )

    # Shadow filter stages from evaluated_opportunities
    if table_exists(ctx.db, "evaluated_opportunities"):
        shadow_stages = ctx.db.execute(
            "SELECT filter_stage, count(*) as total, "
            "sum(CASE WHEN status='settled' AND market_result='yes' THEN 1 ELSE 0 END) as yes_wins, "
            "sum(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
            "sum(CASE WHEN status='settled' THEN counterfactual_pnl ELSE 0 END) as cf_pnl, "
            "sum(CASE WHEN evaluation_time >= ? THEN 1 ELSE 0 END) as new_obs "
            "FROM evaluated_opportunities "
            "WHERE filter_stage LIKE '%shadow%' "
            "GROUP BY filter_stage ORDER BY total DESC LIMIT 8",
            (ctx.start_iso,),
        ).fetchall()
        for s in shadow_stages:
            if s["total"] > 0:
                n = s["total"]
                settled = s["settled"] or 0
                new_obs = s["new_obs"] or 0
                cf_pnl = s["cf_pnl"] or 0
                parts.append(
                    f"*{s['filter_stage']}:* {n} obs (+{new_obs} new)\n"
                    f"  Settled: {settled} | CF PnL: {_fmt_pnl(cf_pnl)}"
                )

    if not parts:
        return ""

    return "🧪 *Shadow Variants*\n\n" + "\n\n".join(parts)


def _shadow_stats(db, table, prob_col, pnl_col, period_start_iso):
    """Get shadow stats for a fifteenm_shadow approach.

    pnl_col=0 means the strategy would NOT have traded (gates not passed).
    Only count rows with pnl != 0 as 'traded' for WR calculation.
    """
    if not has_column(db, table, prob_col):
        return None
    row = db.execute(
        f"SELECT count(*) as total, "
        f"sum(CASE WHEN status='settled' AND {pnl_col} != 0 AND {pnl_col} > 0 THEN 1 ELSE 0 END) as wins, "
        f"sum(CASE WHEN status='settled' AND {pnl_col} != 0 THEN 1 ELSE 0 END) as traded, "
        f"sum(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled, "
        f"sum(CASE WHEN status='settled' AND {pnl_col} != 0 THEN {pnl_col} ELSE 0 END) as pnl, "
        f"sum(CASE WHEN evaluation_time >= ? THEN 1 ELSE 0 END) as new_obs "
        f"FROM {table} WHERE {prob_col} IS NOT NULL",
        (period_start_iso,),
    ).fetchone()
    if not row or row["total"] == 0:
        return None
    return dict(row)


def _format_shadow(label, stats):
    n = stats["total"]
    settled = stats["settled"] or 0
    traded = stats.get("traded", settled) or 0
    wins = stats["wins"] or 0
    pnl = stats["pnl"] or 0
    new_obs = stats["new_obs"] or 0
    status = _shadow_status_label(n, wins, traded, pnl)
    wr_str = f"{safe_div(wins, traded):.0%}" if traded > 0 else "N/A"
    return (
        f"*{label}:* {n} obs (+{new_obs} new)\n"
        f"  Traded: {traded}/{settled} settled | WR: {wr_str} | "
        f"PnL: {_fmt_pnl(pnl)}\n"
        f"  Status: {status}"
    )


def _shadow_status_label(n, wins, traded, pnl_cents):
    """traded = rows where strategy would have entered (pnl != 0)."""
    if traded < 10:
        return "Collecting data"
    wr = safe_div(wins, traded)
    if traded >= 50 and wr > 0.80 and pnl_cents > 0:
        return "Ready for review ✅"
    if wr > 0.75 and pnl_cents > 0:
        return "Showing promise"
    if wr < 0.65 or pnl_cents < 0:
        return "Underperforming"
    return "Monitoring"


# ---------------------------------------------------------------------------
# Section 7: Distribution Analysis (morning + evening only)
# ---------------------------------------------------------------------------

def section_distribution(ctx: ReportContext) -> str:
    if ctx.report_type == "midday":
        return ""
    if not table_exists(ctx.db, "evaluated_opportunities"):
        return ""

    cutoff_24h = (ctx.period_end.astimezone(UTC) - timedelta(hours=24)).isoformat()

    # By asset
    asset_rows = ctx.db.execute(
        "SELECT asset, count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND product_type = '15m' AND asset IS NOT NULL "
        "GROUP BY asset ORDER BY cnt DESC",
        (cutoff_24h,),
    ).fetchall()
    total_assets = sum(r["cnt"] for r in asset_rows)

    if total_assets == 0:
        return ""

    lines = ["📊 *Distributions (last 24h)*"]

    asset_parts = []
    for r in asset_rows:
        pct = r["cnt"] / total_assets * 100
        asset_parts.append(f"{r['asset']} {pct:.0f}%")
    if asset_parts:
        lines.append("By asset: " + " | ".join(asset_parts))

    # By STC zone
    if has_column(ctx.db, "evaluated_opportunities", "seconds_to_close"):
        stc_zones = [
            ("0-300s", 0, 300),
            ("300-500s", 300, 500),
            ("500-900s", 500, 900),
        ]
        stc_parts = []
        for label, lo, hi in stc_zones:
            cnt = ctx.db.execute(
                "SELECT count(*) as cnt FROM evaluated_opportunities "
                "WHERE evaluation_time >= ? AND product_type = '15m' "
                "AND seconds_to_close >= ? AND seconds_to_close < ?",
                (cutoff_24h, lo, hi),
            ).fetchone()["cnt"]
            if total_assets > 0:
                stc_parts.append(f"{label}: {cnt / total_assets * 100:.0f}%")
        if stc_parts:
            lines.append("By STC: " + " | ".join(stc_parts))

    # Top rejection reasons
    rejection_rows = ctx.db.execute(
        "SELECT filter_stage, count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND product_type = '15m' "
        "AND filter_stage NOT IN ('candidate', 'observation_trade') "
        "GROUP BY filter_stage ORDER BY cnt DESC LIMIT 5",
        (cutoff_24h,),
    ).fetchall()
    if rejection_rows:
        total_rej = sum(r["cnt"] for r in rejection_rows)
        lines.append("\nTop rejection reasons:")
        for i, r in enumerate(rejection_rows[:5], 1):
            pct = r["cnt"] / total_rej * 100 if total_rej else 0
            lines.append(f"  {i}. {r['filter_stage']}: {r['cnt']} ({pct:.0f}%)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section 8: Comparison to Baseline (evening only)
# ---------------------------------------------------------------------------

def section_baseline_comparison(ctx: ReportContext) -> str:
    if not ctx.is_evening:
        return ""
    if not table_exists(ctx.db, "settled_trades"):
        return ""

    today_et = ctx.period_end.astimezone(ET).date()
    today_start_utc = datetime(today_et.year, today_et.month, today_et.day,
                               tzinfo=ET).astimezone(UTC).isoformat()
    week_start_utc = (ctx.period_end.astimezone(UTC) - timedelta(days=7)).isoformat()

    # Today's stats
    today_rows = ctx.db.execute(
        "SELECT pnl_cents, entry_price_cents, edge FROM settled_trades "
        "WHERE settled_at >= ? AND product_type = '15m'",
        (today_start_utc,),
    ).fetchall()

    # 7-day stats
    week_rows = ctx.db.execute(
        "SELECT pnl_cents, entry_price_cents, edge FROM settled_trades "
        "WHERE settled_at >= ? AND product_type = '15m'",
        (week_start_utc,),
    ).fetchall()

    if len(today_rows) == 0 or len(week_rows) < 7:
        return ""

    def calc_stats(rows):
        n = len(rows)
        wins = sum(1 for r in rows if r["pnl_cents"] > 0)
        wr = wins / n if n else 0
        pnl = sum(r["pnl_cents"] for r in rows)
        avg_entry = sum(r["entry_price_cents"] for r in rows if r["entry_price_cents"]) / max(1, sum(1 for r in rows if r["entry_price_cents"]))
        edges = [r["edge"] for r in rows if r["edge"] is not None]
        avg_edge = sum(edges) / len(edges) if edges else 0
        return n, wr, pnl, avg_entry, avg_edge

    t_n, t_wr, t_pnl, t_entry, t_edge = calc_stats(today_rows)
    w_n, w_wr, w_pnl, w_entry, w_edge = calc_stats(week_rows)
    # 7d daily average
    w_n_avg = w_n / 7
    w_pnl_avg = w_pnl / 7

    def flag(today_val, avg_val):
        if avg_val == 0:
            return ""
        deviation = abs(today_val - avg_val) / abs(avg_val)
        return " ⚠️" if deviation > 0.20 else ""

    lines = ["📉 *Today vs 7-Day Average*"]
    lines.append("```")
    lines.append(f"{'':14s} {'Today':>8s}  {'7d Avg':>8s}")
    lines.append(f"{'Trades':14s} {t_n:>8d}  {w_n_avg:>8.1f}{flag(t_n, w_n_avg)}")
    lines.append(f"{'Win Rate':14s} {t_wr:>7.0%}   {w_wr:>7.0%}{flag(t_wr, w_wr)}")
    lines.append(f"{'PnL':14s} {_fmt_pnl(t_pnl):>8s}  {_fmt_pnl(w_pnl_avg):>8s}{flag(t_pnl, w_pnl_avg)}")
    lines.append(f"{'Avg Edge':14s} {t_edge:>7.1%}   {w_edge:>7.1%}{flag(t_edge, w_edge)}")
    lines.append(f"{'Avg Entry':14s} {t_entry:>7.0f}c  {w_entry:>7.0f}c{flag(t_entry, w_entry)}")
    lines.append("```")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section 9: Opportunity Pipeline
# ---------------------------------------------------------------------------

def section_pipeline(ctx: ReportContext) -> str:
    if not table_exists(ctx.db, "evaluated_opportunities"):
        return ""

    total = ctx.db.execute(
        "SELECT count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND evaluation_time <= ?",
        (ctx.start_iso, ctx.end_iso),
    ).fetchone()["cnt"]

    if total == 0:
        return ""

    entered = ctx.db.execute(
        "SELECT count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND evaluation_time <= ? "
        "AND filter_stage IN ('candidate', 'observation_trade')",
        (ctx.start_iso, ctx.end_iso),
    ).fetchone()["cnt"]

    # Top skip reasons
    skips = ctx.db.execute(
        "SELECT filter_stage, count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND evaluation_time <= ? "
        "AND filter_stage NOT IN ('candidate', 'observation_trade') "
        "GROUP BY filter_stage ORDER BY cnt DESC LIMIT 5",
        (ctx.start_iso, ctx.end_iso),
    ).fetchall()

    passed_pct = entered / total * 100 if total else 0
    lines = [f"🔎 *Pipeline (this period)*"]
    lines.append(f"Opportunities scanned: ~{total}")
    lines.append(f"Passed filters: {entered} ({passed_pct:.1f}%)")

    if skips:
        skip_parts = [f"{s['filter_stage']}: {s['cnt']}" for s in skips[:4]]
        lines.append("Top skip reasons: " + " | ".join(skip_parts))

    return "\n".join(lines)


def section_post_blr_regime(ctx: ReportContext) -> str:
    """Post-BLR passthrough regime monitoring (deployed Mar 29 2026)."""
    if not table_exists(ctx.db, "settled_trades"):
        return ""

    # Today's 15M trades by asset
    trades = ctx.db.execute(
        "SELECT asset, COUNT(*) as n, "
        "SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as w, "
        "SUM(pnl_cents - COALESCE(fee_cents, 0)) as pnl "
        "FROM settled_trades "
        "WHERE product_type='15m' AND settled_at >= ? AND settled_at <= ? "
        "GROUP BY asset ORDER BY n DESC",
        (ctx.start_iso, ctx.end_iso),
    ).fetchall()

    if not trades:
        return ""

    total_n = sum(r["n"] for r in trades)
    total_w = sum(r["w"] for r in trades)
    total_pnl = sum(r["pnl"] for r in trades)

    lines = ["\U0001f9ea *Post-BLR Regime*"]
    asset_parts = []
    for r in trades:
        wr = r["w"] / r["n"] * 100 if r["n"] else 0
        asset_parts.append(f"{r['asset']}:{r['n']}t/{wr:.0f}%/${r['pnl']/100:+.0f}")
    lines.append(" | ".join(asset_parts))
    lines.append(f"Total: {total_n}t, {total_w}W/{total_n-total_w}L, ${total_pnl/100:+.2f}")

    # SOL empty-book maker fallback stats (if available)
    if table_exists(ctx.db, "evaluated_opportunities"):
        sol_eb = ctx.db.execute(
            "SELECT filter_stage, COUNT(*) as n FROM evaluated_opportunities "
            "WHERE asset='SOL' AND product_type='15m' "
            "AND evaluation_time >= ? AND evaluation_time <= ? "
            "AND filter_stage LIKE 'sol_empty_book%' "
            "GROUP BY filter_stage",
            (ctx.start_iso, ctx.end_iso),
        ).fetchall()
        if sol_eb:
            eb_parts = [f"{r['filter_stage'].replace('sol_empty_book_', '')}={r['n']}" for r in sol_eb]
            lines.append(f"SOL empty-book: {' '.join(eb_parts)}")

    return "\n".join(lines)


def section_spx_pipeline(ctx: ReportContext) -> str:
    """SPX HAR-RV shadow pipeline health — signals, RV quality, Finnhub status."""
    if not table_exists(ctx.db, "spx_harrv_shadow_signals"):
        return ""

    row = ctx.db.execute(
        "SELECT COUNT(*) as n, "
        "SUM(CASE WHEN rv_1h IS NOT NULL AND rv_1h > 0 THEN 1 ELSE 0 END) as nonzero_rv, "
        "MAX(evaluation_time) as latest "
        "FROM spx_harrv_shadow_signals "
        "WHERE evaluation_time >= ? AND evaluation_time <= ?",
        (ctx.start_iso, ctx.end_iso),
    ).fetchone()

    n = row["n"]
    if n == 0:
        return ""

    nonzero = row["nonzero_rv"]
    latest = row["latest"]

    # Settled stats
    settled = ctx.db.execute(
        "SELECT COUNT(*) as n, "
        "SUM(CASE WHEN market_result='yes' AND shadow_pnl_cents > 0 THEN 1 "
        "     WHEN market_result='no' AND shadow_pnl_cents > 0 THEN 1 ELSE 0 END) as w, "
        "SUM(shadow_pnl_cents) as pnl "
        "FROM spx_harrv_shadow_signals "
        "WHERE status='settled' AND settled_time >= ? AND settled_time <= ?",
        (ctx.start_iso, ctx.end_iso),
    ).fetchone()

    lines = ["\U0001f4ca *SPX HAR-RV Pipeline*"]
    lines.append(f"Signals: {n} (RV>0: {nonzero}/{n})")
    lines.append(f"Latest: {latest or 'none'}")

    if settled["n"] and settled["n"] > 0:
        sn = settled["n"]
        sw = settled["w"] or 0
        spnl = settled["pnl"] or 0
        wr = sw / sn * 100 if sn else 0
        lines.append(f"Settled: {sn}t, {sw}W/{sn-sw}L ({wr:.0f}%), ${spnl/100:+.2f}")

    if nonzero == 0 and n > 0:
        lines.append("⚠️ ALL signals have zero RV — Finnhub feed may be stale")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section lists per report type
# ---------------------------------------------------------------------------

# Future: Add Opus API analysis section (Phase 3)
# Future: Auto-fix proposal generation
# Future: Shadow variant auto-creation reporting

MORNING_SECTIONS = [
    section_header,
    section_trade_activity,
    section_loss_detail,
    section_auditor_alerts,
    section_calibration,
    section_shadow_status,
    section_distribution,
    section_pipeline,
]

MIDDAY_SECTIONS = [
    section_header,
    section_trade_activity,
    section_loss_detail,
    section_auditor_alerts,
    section_shadow_status,
    section_pipeline,
]

EVENING_SECTIONS = [
    section_header,
    section_trade_activity,
    section_loss_detail,
    section_auditor_alerts,
    section_calibration,
    section_shadow_status,
    section_distribution,
    section_baseline_comparison,
    section_pipeline,
    section_post_blr_regime,
    section_spx_pipeline,
]

REPORT_SECTIONS = {
    "morning": MORNING_SECTIONS,
    "midday": MIDDAY_SECTIONS,
    "evening": EVENING_SECTIONS,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_pnl(cents) -> str:
    if cents is None:
        return "$0.00"
    sign = "+" if cents >= 0 else ""
    return f"{sign}${cents / 100:.2f}"


def _fmt_time_et(iso_str) -> str:
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ET).strftime("%b %d %I:%M%p ET")
    except Exception:
        return iso_str[:16]


# ---------------------------------------------------------------------------
# Report period calculation
# ---------------------------------------------------------------------------

def get_period(report_type: str, researcher_db: sqlite3.Connection, now: datetime) -> tuple[datetime, datetime]:
    """Determine the period (start, end) for this report."""
    period_end = now

    # Check last report time
    last = researcher_db.execute(
        "SELECT period_end FROM report_history ORDER BY report_time DESC LIMIT 1"
    ).fetchone()

    if last and last["period_end"]:
        try:
            period_start = datetime.fromisoformat(last["period_end"])
            if period_start.tzinfo is None:
                period_start = period_start.replace(tzinfo=UTC)
            # Sanity: don't go back more than 24h
            min_start = now - timedelta(hours=24)
            if period_start < min_start:
                period_start = min_start
            return period_start, period_end
        except Exception:
            pass

    # Default: look back 12 hours
    return now - timedelta(hours=12), period_end


def determine_report_type(now_et: datetime) -> str | None:
    """Given current ET time, determine which report to run (or None if not time)."""
    hour, minute = now_et.hour, now_et.minute
    for rtype, (target_h, target_m) in REPORT_SCHEDULE.items():
        diff = abs((hour * 60 + minute) - (target_h * 60 + target_m))
        if diff <= CRON_TOLERANCE_MINUTES:
            return rtype
    return None


def record_report(researcher_db: sqlite3.Connection, report_type: str,
                  period_start: datetime, period_end: datetime,
                  trades: int, alerts: int) -> None:
    researcher_db.execute(
        "INSERT INTO report_history (report_time, report_type, period_start, period_end, "
        "trades_covered, alerts_covered) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now(UTC).isoformat(), report_type,
         period_start.isoformat(), period_end.isoformat(),
         trades, alerts),
    )
    researcher_db.commit()


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------

def build_report(report_type: str, verbose: bool = False, force: bool = False) -> int:
    """Build and send a report. Returns number of Telegram messages sent."""
    load_dotenv(ENV_PATH)
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")

    state_db = get_state_db()
    if state_db is None:
        msg = "📊 *Research Report*\n\nNo data available — state.db not found."
        if verbose:
            print(msg)
        send_telegram(msg, tg_token, tg_chat)
        return 1

    auditor_db = get_auditor_db()
    researcher_db = get_researcher_db()

    now = datetime.now(UTC)
    period_start, period_end = get_period(report_type, researcher_db, now)

    ctx = ReportContext(
        report_type=report_type,
        period_start=period_start,
        period_end=period_end,
        state_db=state_db,
        auditor_db=auditor_db,
        verbose=verbose,
    )

    # Build sections
    sections = REPORT_SECTIONS.get(report_type, MORNING_SECTIONS)
    rendered = []
    for section_fn in sections:
        try:
            text = section_fn(ctx)
            if text:
                rendered.append(text)
        except Exception as e:
            log.warning("Section %s failed: %s", section_fn.__name__, e, exc_info=True)
            if verbose:
                print(f"  ERROR in {section_fn.__name__}: {e}")

    if not rendered:
        rendered = ["📊 *Report*\n\nNo data to report for this period."]

    # Print to stdout if verbose
    if verbose:
        full = "\n\n".join(rendered)
        print(full)
        print(f"\n{'='*60}")
        print(f"Report type: {report_type}")
        print(f"Period: {ctx.start_et} → {ctx.end_et} ET")
        print(f"Sections: {len(rendered)}")
        print(f"Total chars: {len(full)}")

    # Split into Telegram messages at section boundaries (4096 char limit)
    messages = _split_for_telegram(rendered)

    sent = 0
    for msg in messages:
        if send_telegram(msg, tg_token, tg_chat):
            sent += 1
            time.sleep(0.5)

    # Record this report
    trades_count = 0
    if table_exists(state_db, "settled_trades"):
        trades_count = state_db.execute(
            "SELECT count(*) as cnt FROM settled_trades WHERE settled_at >= ? AND settled_at <= ?",
            (ctx.start_iso, ctx.end_iso),
        ).fetchone()["cnt"]
    record_report(researcher_db, report_type, period_start, period_end, trades_count, 0)

    # Cleanup
    state_db.close()
    if auditor_db:
        auditor_db.close()
    researcher_db.close()

    if verbose:
        print(f"Sent {sent} Telegram messages")

    return sent


def _split_for_telegram(sections: list[str], max_len: int = 4096) -> list[str]:
    """Split rendered sections into messages that fit Telegram's limit."""
    messages = []
    current = ""
    for section in sections:
        candidate = (current + "\n\n" + section) if current else section
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                messages.append(current)
            # If a single section exceeds max_len, truncate it
            if len(section) > max_len:
                messages.append(section[:max_len - 20] + "\n\n_(truncated)_")
            else:
                current = section
    if current:
        messages.append(current)
    return messages if messages else ["📊 No data to report."]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Kalshi Bot Daily Research Reporter")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print report to stdout")
    parser.add_argument("--type", choices=["morning", "midday", "evening"],
                        help="Force a specific report type")
    parser.add_argument("--cron", action="store_true",
                        help="Cron mode: run only if it's report time, else exit silently")
    parser.add_argument("--test-telegram", action="store_true",
                        help="Send a test Telegram message")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)

    if args.test_telegram:
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        ok = send_telegram(
            "📊 *RESEARCHER TEST*\n\nTelegram integration is working.",
            tg_token, tg_chat,
        )
        print(f"Telegram test: {'SUCCESS' if ok else 'FAILED'}")
        return

    # Determine report type
    now_et = datetime.now(ET)

    if args.type:
        report_type = args.type
    elif args.cron:
        report_type = determine_report_type(now_et)
        if report_type is None:
            # Not time for a report — exit silently
            return
        log.info("Cron triggered %s report at %s ET", report_type,
                 now_et.strftime("%H:%M"))
    else:
        # Manual run — pick the most appropriate report type
        report_type = determine_report_type(now_et)
        if report_type is None:
            # Default to morning if run manually outside schedule
            report_type = "morning"

    build_report(report_type, verbose=args.verbose)


if __name__ == "__main__":
    main()
