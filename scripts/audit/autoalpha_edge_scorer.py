#!/usr/bin/env python3
"""Autoalpha Phase 1 — edge scorer + nightly recommendation report.

Reads `cohort_attribution_daily` (latest cohort_date), applies promote /
demote gates, writes a markdown report to `kb/findings/`, Telegram-alerts
with top 5 promote + top 5 demote candidates.

Phase 1 is OBSERVATION-ONLY: produces a report. Does NOT touch live
trading behavior. The report is the operator's input for manual
sizing/kill decisions for the first 2-4 weeks while the autoalpha proves
itself. Phase 2 will add a live_cell_registry table; Phase 3 will wire
the scanner/executor to consult it; Phase 4 will add auto-apply.

Plan doc: kb/decisions/autoalpha-phase1-scorer-plan.md

Operator install (manual, post-merge):

    # In `crontab -e` (botuser):
    30 13 * * * cd ~/kalshi-bot-repo && . venv/bin/activate && set -a && . ~/.env && set +a && python3 scripts/audit/autoalpha_edge_scorer.py >> ~/autoalpha.log 2>&1

(Runs at 13:30 UTC — 23 minutes after cohort_attribution_nightly.py
populates the day's data at 13:07 UTC per scripts/CLAUDE.md.)

Env reads:
    STATE_DB_PATH (optional, defaults to `<repo_root>/state.db` resolved
        from the script's own location via Path(__file__).resolve().parents[2])
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional, no-op if missing)
    AUTOALPHA_REPORT_DIR (optional, defaults to kb/findings/)
    AUTOALPHA_LIVE_STRATEGIES (optional comma-separated allow-list for
        demote eligibility. R1-M5 fix: if unset, demote-mode is DISABLED
        entirely (no demote recommendations) to avoid Telegram-spamming
        on shadow-only cells. NO bot-wide `LIVE_STRATEGIES` constant in
        `bot.constants` today; the closest existing thing is
        `bot.constants.TM_LIVE_STRATEGIES` which is TM-family-scoped.
        Phase 2 will replace this env var with a `live_cell_registry`
        table that generalizes across all strategies.)

Exit code: ALWAYS 0 (cron health-script convention).
"""
from __future__ import annotations

import dataclasses
import math
import os
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional

# Bootstrap repo root onto sys.path BEFORE any `from bot.*` reference.
# Cron's invocation flow does NOT auto-add the repo root to sys.path —
# only the script's parent dir is added by Python's script-invocation rule.
# Without this bootstrap the lazy `from bot.notifier import TelegramNotifier`
# inside `main()` crashes with `ModuleNotFoundError: No module named 'bot'`.
# Same pattern as scripts/ops/collector_health_monitor.py + monitor_watchdog.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Fee-aware breakeven WR per price band (5c-wide). Uses the canonical
# formula from scripts/audit/weather_shadow_audit.py:69-71:
#   breakeven_wr = (price + taker_fee) / 100
#   taker_fee    = ceil(0.07 * 100 * p * (1-p))
# where p = price/100 (the implied probability). Evaluated at the band's
# upper bound (most-conservative within the band — promotes only when
# edge exceeds the worst-case price in the band).
#
# At band 14 (70-74c, upper=74): fee = ceil(0.07*100*0.74*0.26) = 2c → WR > 0.76
# At band 18 (90-94c, upper=94): fee = ceil(0.07*100*0.94*0.06) = 1c → WR > 0.95
# At band 19 (95-99c, upper=99): fee = ceil(0.07*100*0.99*0.01) = 1c → WR > 1.00 (never promotes — intentional;
#   95-99c is the asymmetry trap; even 100% WR barely breaks even net of fees)
def _breakeven_wr_for_band(price_band_5c: int) -> Optional[float]:
    """Return the fee-aware breakeven WR for a 5c-wide price band.

    Uses the band's upper bound (most conservative). Returns None for
    bands outside the supported 14-19 range (70-99c).
    """
    if not (14 <= price_band_5c <= 19):
        return None
    upper_cents = price_band_5c * 5 + 4  # band 14 → 74, band 18 → 94, etc.
    p = upper_cents / 100.0
    taker_fee = math.ceil(0.07 * 100 * p * (1 - p))
    return (upper_cents + taker_fee) / 100.0

# Promote gate thresholds
PROMOTE_MIN_N_30D = 50
PROMOTE_MIN_NET_PER_TRADE_30D = 0.25   # dollars
PROMOTE_MIN_NET_PER_TRADE_7D = 0.10    # dollars

# Demote gate thresholds
DEMOTE_MAX_NET_PER_TRADE_7D = -0.20    # dollars; below this = demote
DEMOTE_MIN_N_7D = 10                   # avoid demote on tiny sample

# Report controls
REPORT_TOP_N = 5  # top N promote + top N demote in Telegram alert


@dataclasses.dataclass(frozen=True)
class CellMetrics:
    """One row of cohort_attribution_daily, post-gate-evaluation.

    R1-M1: includes `product_type` to match the canonical
    cohort_attribution_daily PK (7 dims: cohort_date + asset + product_type
    + strategy + price_band_5c + stc_band_60s + cell_block_stage). Without
    product_type, post-hourly-promotion two rows with otherwise identical
    dims (one 15m, one 1h) collapse into the same cell_key and the report
    silently mixes them.
    """
    asset: str
    product_type: str
    strategy: str
    price_band_5c: int
    stc_band_60s: int
    cell_block_stage: str
    n_30d: int
    n_7d: int
    wr_30d: Optional[float]
    wilson95_lo_30d: Optional[float]
    cf_pnl_30d_dollars: Optional[float]
    cf_pnl_7d_dollars: Optional[float]

    @property
    def cell_key(self) -> str:
        """Cell key with `/` separator (R1-M2 fix: `|` broke markdown table rendering)."""
        return (f"{self.asset}/{self.product_type}/{self.strategy}/"
                f"band{self.price_band_5c}/stc{self.stc_band_60s}/"
                f"{self.cell_block_stage}")

    @property
    def net_per_trade_30d(self) -> Optional[float]:
        if not self.n_30d or self.cf_pnl_30d_dollars is None:
            return None
        return self.cf_pnl_30d_dollars / self.n_30d

    @property
    def net_per_trade_7d(self) -> Optional[float]:
        if not self.n_7d or self.cf_pnl_7d_dollars is None:
            return None
        return self.cf_pnl_7d_dollars / self.n_7d


def is_promote_candidate(c: CellMetrics) -> bool:
    """Apply promote gates. Returns True if cell meets ALL criteria.

    R1-M7: cells with `cell_block_stage != 'candidate'` are already
    cell-block-rejected by the bot. Promoting them is an operator-trap
    (the sizing change has no effect because the cell-block continues
    to reject the trade). Filter them out of promote candidates.
    """
    if c.cell_block_stage != "candidate":
        return False
    if c.n_30d < PROMOTE_MIN_N_30D:
        return False
    p30 = c.net_per_trade_30d
    if p30 is None or p30 < PROMOTE_MIN_NET_PER_TRADE_30D:
        return False
    p7 = c.net_per_trade_7d
    if p7 is None or p7 < PROMOTE_MIN_NET_PER_TRADE_7D:
        return False
    breakeven = _breakeven_wr_for_band(c.price_band_5c)
    if breakeven is None or c.wilson95_lo_30d is None:
        return False
    if c.wilson95_lo_30d <= breakeven:
        return False
    return True


def is_demote_candidate(c: CellMetrics, live_strategies: set[str]) -> bool:
    """Apply demote gates. Returns True if a currently-LIVE cell meets
    demote criteria."""
    if c.strategy not in live_strategies:
        return False
    if c.n_7d < DEMOTE_MIN_N_7D:
        return False
    p7 = c.net_per_trade_7d
    if p7 is None or p7 >= DEMOTE_MAX_NET_PER_TRADE_7D:
        return False
    return True


def load_latest_cohort(conn: sqlite3.Connection) -> List[CellMetrics]:
    """Pull cohort_attribution_daily rows for the latest cohort_date.

    R1-M1: SELECT now includes `product_type` so 15m/1h/etc rows on the
    same (asset, strategy, price, stc, stage) don't collapse into one
    cell_key.
    """
    rows = conn.execute("""
        SELECT asset, product_type, strategy, price_band_5c, stc_band_60s,
               cell_block_stage,
               n_30d, n_7d, wr_30d, wilson95_lo_30d,
               cf_pnl_30d_dollars, cf_pnl_7d_dollars
        FROM cohort_attribution_daily
        WHERE cohort_date = (SELECT MAX(cohort_date)
                             FROM cohort_attribution_daily)
    """).fetchall()
    return [CellMetrics(*r) for r in rows]


def render_report(promotes: List[CellMetrics], demotes: List[CellMetrics],
                  cohort_date: str) -> str:
    """Build the markdown report content."""
    lines = [
        f"# Autoalpha recommendations — {cohort_date}",
        "",
        "Phase 1: observation-only. Gates per `kb/decisions/autoalpha-phase1-scorer-plan.md`.",
        "",
        f"## Top {REPORT_TOP_N} promote candidates",
        "",
        "| Rank | Cell | n_30d | $/trade 30d | $/trade 7d | wilson_lo |",
        "|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(promotes[:REPORT_TOP_N], 1):
        lines.append(
            f"| {i} | {c.cell_key} | {c.n_30d} | "
            f"${c.net_per_trade_30d:.2f} | ${c.net_per_trade_7d:.2f} | "
            f"{c.wilson95_lo_30d:.3f} |"
        )
    if not promotes:
        lines.append("| — | (no cells meet promote criteria today) | | | | |")
    lines += ["", f"## Top {REPORT_TOP_N} demote candidates", "",
              "| Rank | Cell | n_7d | $/trade 7d | $/trade 30d |",
              "|---|---|---|---|---|"]
    for i, c in enumerate(demotes[:REPORT_TOP_N], 1):
        lines.append(
            f"| {i} | {c.cell_key} | {c.n_7d} | "
            f"${c.net_per_trade_7d:.2f} | ${c.net_per_trade_30d or 0:.2f} |"
        )
    if not demotes:
        lines.append("| — | (no live cells meet demote criteria today) | | | |")
    lines += ["",
              "## Totals",
              f"- Promote candidates: {len(promotes)}",
              f"- Demote candidates: {len(demotes)}",
              ""]
    return "\n".join(lines)


def build_telegram_summary(promotes: List[CellMetrics],
                           demotes: List[CellMetrics],
                           cohort_date: str) -> str:
    """One-message Telegram summary."""
    parts = [f"*Autoalpha report {cohort_date}*"]
    parts.append(f"Promote candidates: {len(promotes)}")
    for c in promotes[:REPORT_TOP_N]:
        parts.append(
            f"+ {c.cell_key}: ${c.net_per_trade_30d:.2f}/trade (n={c.n_30d})"
        )
    parts.append(f"Demote candidates: {len(demotes)}")
    for c in demotes[:REPORT_TOP_N]:
        parts.append(
            f"- {c.cell_key}: ${c.net_per_trade_7d:.2f}/trade (n_7d={c.n_7d})"
        )
    return "\n".join(parts)


def main(conn: Optional[sqlite3.Connection] = None,
         report_dir: Optional[Path] = None,
         live_strategies: Optional[set[str]] = None,
         notifier: Optional[object] = None) -> int:
    """Generate the recommendation report + Telegram summary. Test seams
    on all inputs."""
    if conn is None:
        db_path = os.environ.get(
            "STATE_DB_PATH",
            str(_REPO_ROOT / "state.db"),
        )
        conn = sqlite3.connect(db_path)
    if report_dir is None:
        report_dir = Path(os.environ.get(
            "AUTOALPHA_REPORT_DIR",
            str(_REPO_ROOT / "kb" / "findings"),
        ))
    if live_strategies is None:
        env_allow = os.environ.get("AUTOALPHA_LIVE_STRATEGIES", "")
        if env_allow:
            live_strategies = {s.strip() for s in env_allow.split(",") if s.strip()}
        else:
            # R1-M5: empty default disables demote-eligibility rather than
            # treating every observed strategy as live. The bot has many
            # shadow-only strategies (XRP_15M_SHADOW, V2 variants, sports
            # shadow, etc.); demoting them is a no-op but pollutes the
            # report and Telegram alert with cells the bot isn't actually
            # trading. Operator must explicitly set AUTOALPHA_LIVE_STRATEGIES
            # to enable demote recommendations. Phase 2 replaces with
            # registry-table lookup.
            live_strategies = set()
            print(
                "[autoalpha] AUTOALPHA_LIVE_STRATEGIES unset → "
                "demote-mode disabled (set the env var to a comma-separated "
                "list of live strategies to enable)",
                flush=True,
            )
    if notifier is None:
        from bot.notifier import TelegramNotifier  # noqa: PLC0415 — lazy intentional
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    cells = load_latest_cohort(conn)
    if not cells:
        print("[autoalpha] no rows in cohort_attribution_daily — skipping",
              flush=True)
        return 0

    # R1-M5: NO fallback to "every observed strategy". Empty live_strategies
    # = demote-mode disabled. Operator opts in via env var.
    promotes = [c for c in cells if is_promote_candidate(c)]
    demotes = [c for c in cells if is_demote_candidate(c, live_strategies)]

    # Sort: best promote first (highest $/trade 30d), worst demote first
    # (most negative $/trade 7d).
    promotes.sort(key=lambda c: -(c.net_per_trade_30d or 0))
    demotes.sort(key=lambda c: (c.net_per_trade_7d or 0))

    cohort_date = conn.execute(
        "SELECT MAX(cohort_date) FROM cohort_attribution_daily"
    ).fetchone()[0]
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"autoalpha-recommendations-{cohort_date}.md"
    report_path.write_text(render_report(promotes, demotes, cohort_date))

    telegram_summary = build_telegram_summary(promotes, demotes, cohort_date)
    notifier.send(
        telegram_summary,
        silent=False,
        dedup_key=f"autoalpha_recommendations_{cohort_date}",
    )

    print(
        f"[autoalpha] cohort_date={cohort_date} "
        f"cells={len(cells)} promote={len(promotes)} demote={len(demotes)} "
        f"report={report_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
