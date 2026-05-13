#!/usr/bin/env python3
"""Weekly bleed report generator — Money Printer Roadmap P1.4.

Runs Mondays at 13:13 UTC via systemd timer (intentionally 6 minutes
AFTER the P1.1 nightly aggregator at 13:07 UTC so the report reads a
post-nightly-commit cohort_attribution_daily row). Writes a markdown
summary to `kb/findings/weekly-bleed-{YYYY-MM-DD}.md` (KB-local per
`kb/CLAUDE.md` — not git-tracked) and emits a Telegram one-liner via
the canonical `bot.notifier._TELEGRAM` singleton.

Five sections per design § Weekly bleed report:
  1. Top 10 bleeders (30d cf_pnl)
  2. Top 10 cal-drift cohorts (|cal_gap_30d| DESC)
  3. New cohorts crossing alert thresholds this week
  4. Cohorts that EXITED alert state this week
  5. Coverage stats

Ticket: ClickUp 86b9x3kn2 (P1.4).
Design: kb/decisions/cohort-measurement-design-may12.md.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot.helpers.cohort_attribution import (  # noqa: E402
    COHORT_PARTITION_STAGES,
    _compute_persistence_days,
    ensure_schema,
)

# ── Constants (single source of truth) ───────────────────────────────────────

REPORT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

BLEED_TOP_N = 10
CAL_DRIFT_TOP_N = 10

# Report is "stale" if the freshest cohort_date row is older than this many
# hours relative to report_date midnight UTC. 36h tolerates one missed
# nightly run; >36h means the cron is broken and the operator must triage.
STALE_HOURS_THRESHOLD = 36

# Week window for sections 3-5: trailing 7d ending at report_date inclusive.
WEEK_WINDOW_DAYS = 7

DEFAULT_DB_PATH = os.environ.get("KALSHI_STATE_DB", str(REPO_ROOT / "state.db"))
DEFAULT_OUTPUT_DIR = REPO_ROOT / "kb" / "findings"

DASHBOARD_URL = "https://gabrielkagan.github.io/kalshi-bot/"
SCHEMA_VERSION = 1


# ── Input validation ─────────────────────────────────────────────────────────


def validate_report_date(s: Any) -> str:
    """Strict YYYY-MM-DD validator — path-injection safe.

    The validated string is interpolated into the output filename
    (`weekly-bleed-{date}.md`) so anything other than digits-and-dashes
    is a security risk. Two-step gate: regex match THEN
    `date.fromisoformat` to catch impossible dates like `2026-13-01`.
    """
    if not isinstance(s, str):
        raise ValueError(f"Invalid report_date {s!r}: expected str")
    if not REPORT_DATE_RE.match(s):
        raise ValueError(
            f"Invalid report_date {s!r}: must match YYYY-MM-DD exactly"
        )
    try:
        _dt.date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"Invalid report_date {s!r}: {e}") from e
    return s


# ── Staleness helper ─────────────────────────────────────────────────────────


def _latest_cohort_date(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(cohort_date) FROM cohort_attribution_daily"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _is_stale(
    latest_cohort_date_iso: Optional[str],
    report_date_iso: str,
    *,
    hours: int = STALE_HOURS_THRESHOLD,
) -> bool:
    """Return True iff the latest cohort_date is older than `hours`
    relative to `report_date` midnight UTC, or the table is empty."""
    if latest_cohort_date_iso is None:
        return True
    report_dt = _dt.datetime.fromisoformat(report_date_iso).replace(
        tzinfo=_dt.timezone.utc
    )
    latest_dt = _dt.datetime.fromisoformat(latest_cohort_date_iso).replace(
        tzinfo=_dt.timezone.utc
    )
    return (report_dt - latest_dt) > _dt.timedelta(hours=hours)


# ── Display formatters ───────────────────────────────────────────────────────


def _price_band_label(band_5c: Any) -> str:
    try:
        n = int(band_5c)
    except (TypeError, ValueError):
        return "?"
    lo = n * 5
    return f"{lo}-{lo + 4}"


def _stc_band_label(band_60s: Any) -> str:
    try:
        n = int(band_60s)
    except (TypeError, ValueError):
        return "?"
    if n >= 11:
        return "660+"
    lo = n * 60
    return f"{lo}-{lo + 60}"


def _fmt_float(v: Any, places: int = 3) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.{places}f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_dollars(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "n/a"


# ── Section builders ─────────────────────────────────────────────────────────


def _build_top_bleeders_section(
    conn: sqlite3.Connection, *, cohort_date: str
) -> str:
    """Section 1: Top N bleeders by cf_pnl_30d ASC (positive excluded)."""
    rows = conn.execute(
        """
        SELECT asset, strategy, price_band_5c, stc_band_60s, cell_block_stage,
               n_30d, wr_30d, wilson95_hi_30d, cf_pnl_30d_dollars
        FROM cohort_attribution_daily
        WHERE cohort_date = ?
          AND cf_pnl_30d_dollars IS NOT NULL
          AND cf_pnl_30d_dollars < 0
        ORDER BY cf_pnl_30d_dollars ASC
        LIMIT ?
        """,
        (cohort_date, BLEED_TOP_N),
    ).fetchall()

    lines = [
        f"## 1. Top {BLEED_TOP_N} bleeders (30d cf_pnl, candidate + cell-block UNION)",
        "",
    ]
    if not rows:
        lines.append("_No bleeding cohorts on this cohort_date._")
        return "\n".join(lines) + "\n"

    lines.append("| asset | strategy | price | stc | stage | n | wr | wilson95_hi | cf_pnl_30d |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r[0]} | {r[1]} | {_price_band_label(r[2])} | "
            f"{_stc_band_label(r[3])} | {r[4]} | {r[5]} | "
            f"{_fmt_float(r[6])} | {_fmt_float(r[7])} | "
            f"{_fmt_dollars(r[8])} |"
        )
    return "\n".join(lines) + "\n"


def _build_cal_drift_section(
    conn: sqlite3.Connection, *, cohort_date: str
) -> str:
    """Section 2: Top N cal-drift by |cal_gap_30d| DESC, with
    persistence_days computed via the canonical P1.1 helper."""
    rows = conn.execute(
        """
        SELECT asset, product_type, strategy, price_band_5c, stc_band_60s,
               cell_block_stage, n_30d, mean_cal_prob_30d, wr_30d, cal_gap_30d
        FROM cohort_attribution_daily
        WHERE cohort_date = ?
          AND cal_gap_30d IS NOT NULL
        ORDER BY ABS(cal_gap_30d) DESC
        LIMIT ?
        """,
        (cohort_date, CAL_DRIFT_TOP_N),
    ).fetchall()

    lines = [
        f"## 2. Top {CAL_DRIFT_TOP_N} cal-drift cohorts (|cal_gap_30d| DESC)",
        "",
    ]
    if not rows:
        lines.append("_No cohorts with cal_gap_30d on this cohort_date._")
        return "\n".join(lines) + "\n"

    lines.append("| asset | strategy | price | stc | stage | n | mean_cal_prob | realized_wr | cal_gap | persistence_days |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        asset, product_type, strategy, pb, sb, stage, n, mean_cp, wr, gap = r
        cohort_key = (asset, product_type, strategy, int(pb), int(sb), stage)
        persistence = _compute_persistence_days(
            conn,
            cohort_date=cohort_date,
            cohort_key=cohort_key,
            abs_threshold=0.05,
        )
        gap_str = (
            f"{'+' if gap >= 0 else ''}{gap:.3f}" if gap is not None else "n/a"
        )
        lines.append(
            f"| {asset} | {strategy} | {_price_band_label(pb)} | "
            f"{_stc_band_label(sb)} | {stage} | {n} | "
            f"{_fmt_float(mean_cp)} | {_fmt_float(wr)} | "
            f"{gap_str} | {persistence} |"
        )
    return "\n".join(lines) + "\n"


def _week_window_bounds(report_date: str) -> Tuple[str, str]:
    """Return (week_start_iso, report_date) — inclusive 7d trailing window."""
    end = _dt.date.fromisoformat(report_date)
    start = end - _dt.timedelta(days=WEEK_WINDOW_DAYS - 1)
    return start.isoformat(), end.isoformat()


def _build_new_alerts_section(
    conn: sqlite3.Connection, *, report_date: str
) -> str:
    """Section 3: Cohorts whose first-fire cohort_date falls within
    [report_date - 6d, report_date]."""
    week_start, week_end = _week_window_bounds(report_date)
    rows = conn.execute(
        """
        WITH first_fires AS (
            SELECT asset, product_type, strategy, price_band_5c,
                   stc_band_60s, cell_block_stage,
                   MIN(cohort_date) AS first_fire_date
            FROM cohort_attribution_daily
            WHERE alert_state LIKE 'firing%'
            GROUP BY asset, product_type, strategy, price_band_5c,
                     stc_band_60s, cell_block_stage
        )
        SELECT ff.asset, ff.strategy, ff.price_band_5c, ff.stc_band_60s,
               ff.cell_block_stage, ff.first_fire_date,
               c.alert_state AS current_state
        FROM first_fires ff
        LEFT JOIN cohort_attribution_daily c
          ON c.asset = ff.asset
         AND c.product_type = ff.product_type
         AND c.strategy = ff.strategy
         AND c.price_band_5c = ff.price_band_5c
         AND c.stc_band_60s = ff.stc_band_60s
         AND c.cell_block_stage = ff.cell_block_stage
         AND c.cohort_date = ?
        WHERE ff.first_fire_date >= ?
          AND ff.first_fire_date <= ?
        ORDER BY ff.first_fire_date ASC
        """,
        (report_date, week_start, week_end),
    ).fetchall()

    lines = [
        f"## 3. New cohorts crossing alert thresholds this week",
        "",
        f"Week window: {week_start} → {week_end} ({WEEK_WINDOW_DAYS} days, inclusive)",
        "",
    ]
    if not rows:
        lines.append("_No new alert fires within this week's window._")
        return "\n".join(lines) + "\n"

    lines.append("| asset | strategy | price | stc | stage | first_fire_date | current_state |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        asset, strategy, pb, sb, stage, first_date, current = r
        lines.append(
            f"| {asset} | {strategy} | {_price_band_label(pb)} | "
            f"{_stc_band_label(sb)} | {stage} | {first_date} | "
            f"{current or 'n/a'} |"
        )
    return "\n".join(lines) + "\n"


def _build_exited_alerts_section(
    conn: sqlite3.Connection, *, report_date: str
) -> str:
    """Section 4: Cohorts where alert_state transitioned to 'quiet' on
    `report_date`, after having fired within the trailing 7d window.
    Reads `last_alert_time` preserved by P1.1's firing→quiet transition.

    Firing-window bounds: [report_date - 7d, report_date - 1d] (strictly
    before report_date, since report_date itself is the quiet row).
    R1 MN1 fix — the prior `[week_start, week_end]` bound dropped the
    boundary case where a cohort fired on `week_start - 1d` and quieted
    on `week_start`: the transition still fell within the week but the
    firing date didn't, so the row was invisible. Aligning the firing
    window with `[report_date - 7d, report_date - 1d]` catches transitions
    that occur ON ANY day within the report week, including the first.
    """
    report_dt = _dt.date.fromisoformat(report_date)
    firing_window_lo = (report_dt - _dt.timedelta(days=WEEK_WINDOW_DAYS)).isoformat()
    firing_window_hi = (report_dt - _dt.timedelta(days=1)).isoformat()
    week_start, week_end = _week_window_bounds(report_date)
    rows = conn.execute(
        """
        WITH last_fires AS (
            SELECT asset, product_type, strategy, price_band_5c,
                   stc_band_60s, cell_block_stage,
                   MAX(cohort_date) AS last_fire_date
            FROM cohort_attribution_daily
            WHERE alert_state LIKE 'firing%'
              AND cohort_date >= ?
              AND cohort_date <= ?
            GROUP BY asset, product_type, strategy, price_band_5c,
                     stc_band_60s, cell_block_stage
        )
        SELECT lf.asset, lf.strategy, lf.price_band_5c, lf.stc_band_60s,
               lf.cell_block_stage, lf.last_fire_date,
               c.last_alert_time
        FROM last_fires lf
        JOIN cohort_attribution_daily c
          ON c.asset = lf.asset
         AND c.product_type = lf.product_type
         AND c.strategy = lf.strategy
         AND c.price_band_5c = lf.price_band_5c
         AND c.stc_band_60s = lf.stc_band_60s
         AND c.cell_block_stage = lf.cell_block_stage
         AND c.cohort_date = ?
        WHERE c.alert_state = 'quiet'
        ORDER BY lf.last_fire_date DESC
        """,
        (firing_window_lo, firing_window_hi, report_date),
    ).fetchall()

    lines = [
        f"## 4. Cohorts that EXITED alert state this week",
        "",
        f"Week window: {week_start} → {week_end} ({WEEK_WINDOW_DAYS} days, inclusive)",
        "",
    ]
    if not rows:
        lines.append("_No cohorts exited alert state within this week's window._")
        return "\n".join(lines) + "\n"

    lines.append("| asset | strategy | price | stc | stage | last_fire_date | last_alert_time |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        asset, strategy, pb, sb, stage, last_fire_date, last_alert_time = r
        lines.append(
            f"| {asset} | {strategy} | {_price_band_label(pb)} | "
            f"{_stc_band_label(sb)} | {stage} | {last_fire_date} | "
            f"{last_alert_time or 'n/a'} |"
        )
    return "\n".join(lines) + "\n"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _build_coverage_stats_section(
    conn: sqlite3.Connection, *, report_date: str
) -> str:
    """Section 5: Raw rowcounts of candidate admits / cell-block admits /
    rejected_opportunities within the report-week window, plus a
    coverage-gap check for missing cohort_dates in the weekly series."""
    week_start, week_end = _week_window_bounds(report_date)
    # ISO timestamp bounds for the time-bucketed admit/reject counts.
    since_iso = f"{week_start}T00:00:00+00:00"
    until_iso = f"{week_end}T23:59:59+00:00"

    bleed_stages = sorted(s for s in COHORT_PARTITION_STAGES if s != "candidate")

    candidate_admits = 0
    cell_block_admits = 0
    rejected_count = 0

    if _table_exists(conn, "evaluated_opportunities"):
        placeholders = ",".join("?" for _ in bleed_stages)
        candidate_admits = conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities "
            "WHERE filter_stage = 'candidate' "
            "  AND evaluation_time >= ? AND evaluation_time <= ?",
            (since_iso, until_iso),
        ).fetchone()[0]
        if bleed_stages:
            cell_block_admits = conn.execute(
                f"SELECT COUNT(*) FROM evaluated_opportunities "
                f"WHERE filter_stage IN ({placeholders}) "
                f"  AND evaluation_time >= ? AND evaluation_time <= ?",
                (*bleed_stages, since_iso, until_iso),
            ).fetchone()[0]

    if _table_exists(conn, "rejected_opportunities"):
        rejected_count = conn.execute(
            "SELECT COUNT(*) FROM rejected_opportunities "
            "WHERE evaluation_time >= ? AND evaluation_time <= ?",
            (since_iso, until_iso),
        ).fetchone()[0]

    # Coverage gap: cohort_date series should be complete across the 7d window.
    present_dates = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT cohort_date FROM cohort_attribution_daily "
            "WHERE cohort_date >= ? AND cohort_date <= ?",
            (week_start, week_end),
        ).fetchall()
    }
    expected = []
    cur = _dt.date.fromisoformat(week_start)
    end = _dt.date.fromisoformat(week_end)
    while cur <= end:
        expected.append(cur.isoformat())
        cur += _dt.timedelta(days=1)
    missing = [d for d in expected if d not in present_dates]

    lines = [
        "## 5. Coverage stats",
        "",
        f"Week window: {week_start} → {week_end} ({WEEK_WINDOW_DAYS} days, inclusive)",
        "",
        f"- Total candidate admits (week): **{candidate_admits}**",
        f"- Total cell-block admits (week): **{cell_block_admits}**",
        f"- Total rejected_opportunities rows (week): **{rejected_count}**",
        f"- Distinct cohort_dates present in window: **{len(present_dates)} / {len(expected)}**",
    ]
    if missing:
        lines.append(f"- **Coverage gaps**: missing cohort_dates: {', '.join(missing)}")
    else:
        lines.append("- Coverage gaps: none")
    return "\n".join(lines) + "\n"


# ── Summary counts (used by Telegram payload) ────────────────────────────────


def _count_firing_on_date(conn: sqlite3.Connection, cohort_date: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM cohort_attribution_daily "
        "WHERE cohort_date = ? AND alert_state LIKE 'firing%'",
        (cohort_date,),
    ).fetchone()
    return int(row[0]) if row else 0


def _count_new_alerts_this_week(
    conn: sqlite3.Connection, *, report_date: str
) -> int:
    week_start, week_end = _week_window_bounds(report_date)
    row = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT asset, product_type, strategy, price_band_5c,
                   stc_band_60s, cell_block_stage,
                   MIN(cohort_date) AS first_fire_date
            FROM cohort_attribution_daily
            WHERE alert_state LIKE 'firing%'
            GROUP BY asset, product_type, strategy, price_band_5c,
                     stc_band_60s, cell_block_stage
            HAVING first_fire_date >= ? AND first_fire_date <= ?
        )
        """,
        (week_start, week_end),
    ).fetchone()
    return int(row[0]) if row else 0


def _count_resolved_this_week(
    conn: sqlite3.Connection, *, report_date: str
) -> int:
    """Mirror of `_build_exited_alerts_section` count for the Telegram
    one-liner. Must use the same firing-window bounds — [report_date -
    7d, report_date - 1d] — so the count in the summary matches what
    the operator actually sees in section 4."""
    report_dt = _dt.date.fromisoformat(report_date)
    firing_window_lo = (report_dt - _dt.timedelta(days=WEEK_WINDOW_DAYS)).isoformat()
    firing_window_hi = (report_dt - _dt.timedelta(days=1)).isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT lf.asset
            FROM (
                SELECT asset, product_type, strategy, price_band_5c,
                       stc_band_60s, cell_block_stage,
                       MAX(cohort_date) AS last_fire_date
                FROM cohort_attribution_daily
                WHERE alert_state LIKE 'firing%'
                  AND cohort_date >= ?
                  AND cohort_date <= ?
                GROUP BY asset, product_type, strategy, price_band_5c,
                         stc_band_60s, cell_block_stage
            ) lf
            JOIN cohort_attribution_daily c
              ON c.asset = lf.asset
             AND c.product_type = lf.product_type
             AND c.strategy = lf.strategy
             AND c.price_band_5c = lf.price_band_5c
             AND c.stc_band_60s = lf.stc_band_60s
             AND c.cell_block_stage = lf.cell_block_stage
             AND c.cohort_date = ?
            WHERE c.alert_state = 'quiet'
        )
        """,
        (firing_window_lo, firing_window_hi, report_date),
    ).fetchone()
    return int(row[0]) if row else 0


# ── Report assembly ──────────────────────────────────────────────────────────


def build_report(
    conn: sqlite3.Connection,
    *,
    report_date: str,
    now: Optional[_dt.datetime] = None,
) -> str:
    """Assemble the full markdown body for `report_date` (YYYY-MM-DD UTC).

    `now` is the wall-clock timestamp stamped in the frontmatter — pinned
    by the cron-driven main() to the actual fire time; tests pass a
    fixed value for byte-stable idempotency.
    """
    validate_report_date(report_date)
    ensure_schema(conn)

    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)

    latest = _latest_cohort_date(conn)
    stale = _is_stale(latest, report_date)

    parts: List[str] = []
    parts.append(f"# Weekly Bleed Report — {report_date}")
    parts.append("")
    parts.append(f"**Generated**: {now.isoformat()}")
    parts.append(f"**Report date**: {report_date}")
    parts.append(f"**Latest cohort_date in DB**: {latest if latest else 'none'}")
    parts.append(f"**Stale**: {stale}")
    parts.append(f"**Schema version**: {SCHEMA_VERSION}")
    parts.append("")
    parts.append(
        "P1.4 weekly summary of `cohort_attribution_daily`. Source design: "
        "`kb/decisions/cohort-measurement-design-may12.md` § Weekly bleed "
        "report. Ticket: ClickUp `86b9x3kn2`."
    )
    parts.append("")
    parts.append(_build_top_bleeders_section(conn, cohort_date=report_date))
    parts.append(_build_cal_drift_section(conn, cohort_date=report_date))
    parts.append(_build_new_alerts_section(conn, report_date=report_date))
    parts.append(_build_exited_alerts_section(conn, report_date=report_date))
    parts.append(_build_coverage_stats_section(conn, report_date=report_date))
    return "\n".join(parts)


def telegram_summary_payload(
    *,
    report_date: str,
    n_firing: int,
    n_new: int,
    n_resolved: int,
    report_path: Optional[Path] = None,
) -> str:
    """One-line Telegram summary per design § Weekly bleed report § Delivery."""
    link = f" {DASHBOARD_URL}" if report_path is None else f" file://{report_path}"
    return (
        f"\U0001f4ca Weekly bleed report ready ({report_date}): "
        f"{n_firing} firing, {n_new} new, {n_resolved} resolved.{link}"
    )


def generate_weekly_report(
    conn: sqlite3.Connection,
    *,
    report_date: str,
    output_dir: Path,
    telegram_send: Optional[Callable[[str], bool]] = None,
    now: Optional[_dt.datetime] = None,
) -> Path:
    """Write the report markdown to `output_dir/weekly-bleed-{date}.md`
    and optionally send the Telegram one-liner. Returns the file path.

    `telegram_send` is dependency-injected so tests can capture the
    payload without standing up the notifier; production main() resolves
    `bot.notifier._TELEGRAM.send` and passes a wrapper.
    """
    validate_report_date(report_date)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    body = build_report(conn, report_date=report_date, now=now)
    path = output_dir / f"weekly-bleed-{report_date}.md"
    path.write_text(body, encoding="utf-8")

    if telegram_send is not None:
        n_firing = _count_firing_on_date(conn, report_date)
        n_new = _count_new_alerts_this_week(conn, report_date=report_date)
        n_resolved = _count_resolved_this_week(conn, report_date=report_date)
        payload = telegram_summary_payload(
            report_date=report_date,
            n_firing=n_firing, n_new=n_new, n_resolved=n_resolved,
            report_path=path,
        )
        try:
            telegram_send(payload)
        except Exception:
            logging.exception("[WEEKLY_BLEED] telegram_send raised; report file is still written")

    return path


# ── Production main() — Telegram resolution + CLI ────────────────────────────


def _resolve_telegram_send() -> Optional[Callable[[str], bool]]:
    """Reach the canonical singleton via the Bit 8.1 path-A++ pattern.

    Returns a callable that wraps `_TELEGRAM.send(...)` with the
    dedup_key for the weekly summary, or None if the singleton is
    unset or disabled (boot ordering / dev env)."""
    try:
        import bot.notifier as _telegram_state
    except Exception:
        logging.exception("[WEEKLY_BLEED] failed to import bot.notifier")
        return None
    notifier = getattr(_telegram_state, "_TELEGRAM", None)
    if notifier is None or not getattr(notifier, "enabled", False):
        return None

    def _send(msg: str) -> bool:
        try:
            notifier.send(msg, dedup_key="weekly_bleed_report")
            return True
        except Exception:
            logging.exception("[WEEKLY_BLEED] notifier.send raised")
            return False

    return _send


def _open_conn(db_path: str) -> sqlite3.Connection:
    """Open with WAL + busy_timeout per scripts/CLAUDE.md."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH,
        help=f"Path to state.db (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--report-date", default=None,
        help="UTC date (YYYY-MM-DD) the report represents. Default: today.",
    )
    parser.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR),
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="Skip the Telegram one-liner (writes the file only).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [WEEKLY_BLEED] %(levelname)s %(message)s",
    )

    report_date = args.report_date or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    try:
        report_date = validate_report_date(report_date)
    except ValueError as e:
        logging.error("invalid --report-date: %s", e)
        return 2

    telegram_send = None if args.no_telegram else _resolve_telegram_send()

    conn = _open_conn(args.db)
    try:
        path = generate_weekly_report(
            conn,
            report_date=report_date,
            output_dir=Path(args.output_dir),
            telegram_send=telegram_send,
        )
        logging.info("wrote weekly bleed report to %s", path)
        return 0
    except Exception:
        logging.exception("weekly bleed report failed")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
