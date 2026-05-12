#!/usr/bin/env python3
"""Data health monitor for Kalshi bot.

Checks data freshness, NULL rates, shadow variant progress, NO-side flow,
and DB integrity across all systems. Optionally sends Telegram alerts for
critical issues.

Usage:
    python3 scripts/data_health_monitor.py --db /path/to/state.db
    python3 scripts/data_health_monitor.py --db /path/to/state.db --telegram
    python3 scripts/data_health_monitor.py --db /path/to/state.db --verbose

Designed to run as a cron job every 30 minutes on VPS.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Telegram config ──────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(msg: str) -> bool:
    """Send Telegram message. Returns True on success."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[data_health] No Telegram config, skipping alert.")
        return False
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        print(f"[data_health] Telegram send failed: {e}")
        return False


# ── Severity levels ──────────────────────────────────────────────

CRITICAL = "CRITICAL"
WARNING = "WARNING"
INFO = "INFO"
OK = "OK"


def severity_icon(severity: str) -> str:
    if severity == CRITICAL:
        return "[CRIT]"
    elif severity == WARNING:
        return "[WARN]"
    elif severity == INFO:
        return "[INFO]"
    return "[ OK ]"


# ── Helper: table existence check ────────────────────────────────

def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] > 0


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(c[1] == column for c in cols)
    except Exception:
        return False


# ── Market hours check ───────────────────────────────────────────

def is_crypto_market_hours(now_utc: datetime) -> bool:
    """Crypto 15M markets run ~14:00-22:00 UTC on weekdays."""
    hour = now_utc.hour
    weekday = now_utc.weekday()  # 0=Mon, 6=Sun
    # Crypto markets also run on weekends, but check reasonable hours
    return 14 <= hour < 22


def is_spx_market_hours(now_utc: datetime) -> bool:
    """SPX markets run during US market hours, weekdays only."""
    hour = now_utc.hour
    weekday = now_utc.weekday()
    if weekday >= 5:  # Sat/Sun
        return False
    return 14 <= hour < 21  # ~9am-4pm ET


# ── Health checks ────────────────────────────────────────────────

def check_stale_data(conn: sqlite3.Connection, now_utc: datetime) -> list:
    """Check for stale evaluated_opportunities data per product_type."""
    results = []

    if not table_exists(conn, "evaluated_opportunities"):
        results.append((CRITICAL, "evaluated_opportunities table missing"))
        return results

    has_product_type = column_exists(conn, "evaluated_opportunities", "product_type")

    # Define product types and their market-hours check
    product_types = {
        "15m": ("15M", is_crypto_market_hours),
        "hourly": ("Hourly", is_crypto_market_hours),
        "spx_hourly": ("SPX Hourly", is_spx_market_hours),
        "weather": ("Weather", lambda _: True),  # Weather runs anytime
    }

    if has_product_type:
        for pt, (label, hours_check) in product_types.items():
            if not hours_check(now_utc):
                results.append((INFO, f"{label}: Outside market hours, skipping freshness check"))
                continue

            row = conn.execute(
                "SELECT MAX(evaluation_time) FROM evaluated_opportunities "
                "WHERE product_type = ?",
                (pt,),
            ).fetchone()

            last_time_str = row[0] if row else None
            if not last_time_str:
                results.append((INFO, f"{label}: No data found for product_type='{pt}'"))
                continue

            try:
                last_time = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                age = now_utc - last_time
                age_min = age.total_seconds() / 60

                if age_min > 120:
                    last_fmt = last_time.strftime("%H:%M UTC")
                    results.append((CRITICAL, f"{label}: No evals in {age_min:.0f}min (last: {last_fmt})"))
                elif age_min > 30:
                    last_fmt = last_time.strftime("%H:%M UTC")
                    results.append((WARNING, f"{label}: No evals in {age_min:.0f}min (last: {last_fmt})"))
                else:
                    results.append((OK, f"{label}: Last eval {age_min:.0f}min ago"))
            except (ValueError, TypeError):
                results.append((WARNING, f"{label}: Could not parse last eval time: {last_time_str}"))
    else:
        # Fallback: no product_type column, check all evals
        row = conn.execute("SELECT MAX(evaluation_time) FROM evaluated_opportunities").fetchone()
        last_time_str = row[0] if row else None
        if not last_time_str:
            results.append((WARNING, "No evaluated_opportunities data found"))
        else:
            try:
                last_time = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                age_min = (now_utc - last_time).total_seconds() / 60
                if age_min > 120:
                    results.append((CRITICAL, f"All evals: No data in {age_min:.0f}min"))
                elif age_min > 30:
                    results.append((WARNING, f"All evals: No data in {age_min:.0f}min"))
                else:
                    results.append((OK, f"All evals: Last eval {age_min:.0f}min ago"))
            except (ValueError, TypeError):
                results.append((WARNING, f"Could not parse eval time: {last_time_str}"))

    return results


# Bit 11.1b (Sprint 11, 2026-05-11) — per-column NULL-rate eligibility.
# Pre-Bit-11.1b, check_null_rates() computed `null_count / total` against
# the FULL per-product-type population. This produced false-positive WARN
# / CRIT for conditionally-written columns:
#   - position_size / entry_price: only written for sizing-eligible
#     filter_stages. Rejection-pre-sizing rows (~30 distinct filter_stages
#     across `price_out_of_range`, `floor_raise_shadow`, the dc_shadow_*
#     family, `silent_loss_cooldown`, `tm96_calmlp_gate_blocked`,
#     `weather_timing_restricted`, etc.) correctly have NULL — they were
#     rejected BEFORE the sizing engine ran. Counting them in the
#     denominator inflated the NULL rate to ~38% even though sizing-
#     eligible rows had 0% NULL.
#   - egarch_sigma / egarch_blend_sigma: crypto-only features. Weather +
#     sports product_types correctly have 100% NULL by design.
#   - volatility: not written for sports (different model family).
# Per Bit 11.1b RCA (kb/findings/skill-audit-may11-bit-11.1b.md), the
# fix is two-pronged:
#   1. PRODUCT_TYPE_SKIP — for a given column, skip the check entirely
#      when the current product_type doesn't write that column.
#   2. SIZING_ELIGIBLE_ONLY — for position_size + entry_price, restrict
#      the denominator + numerator to the CANDIDATE filter_stage only.
#      The R1 adversarial review caught that any deny-list shape would
#      drift as new rejection stages get added (~30 exist on 7d window,
#      growing); the allow-list shape with `{candidate}` is the minimal
#      robust set. Trade-off: shadow-stage writer bugs are not caught by
#      this signal (acceptable — `candidate` is the canonical actual-
#      trade path; downstream signals catch shadow regressions).
# Columns NOT in either map are checked unconditionally against the
# full per-product-type population (always-write: calibrated_prob,
# edge, market_price, seconds_to_close, spot_price, raw_prob).
# Contract pin: tests/integration/test_data_health_null_rate_eligibility.py.
PRODUCT_TYPE_SKIP = {
    "egarch_sigma": ("sports", "weather"),
    "egarch_blend_sigma": ("sports", "weather"),
    "volatility": ("sports",),
}
# R2 adversarial fix 2026-05-11: removed `spx_hourly` from egarch skip
# tuples — VPS 7d evidence shows spx_hourly writes egarch_sigma +
# egarch_blend_sigma at 0% NULL (n=726). Skipping it would mask a real
# writer-path regression. spx_hourly uses HAR-RV as PRIMARY (per
# bot/CLAUDE.md), but ALSO writes egarch as a secondary signal.
SIZING_ELIGIBLE_ONLY = {"position_size", "entry_price"}
# R1 adversarial fix 2026-05-11: switched from deny-list to allow-list.
# Deny-list shape (excluding price_out_of_range / floor_raise_shadow /
# eth_low_floor_shadow / insufficient_edge) missed ~30 other rejection-
# stage families (`dc_shadow_*`, `*_timing_restricted`, `silent_*`,
# `tm96_*_blocked`, `*_asset_excluded`, `*_edge_cap`, ...) that drift in
# as the bot adds new filters. Allow-list = "definitely-sized" — only
# `candidate` for now (the canonical actual-trade filter_stage).
SIZING_ELIGIBLE_FILTER_STAGES = frozenset({"candidate"})
SIZING_ELIGIBLE_FILTER_STAGE_PREDICATE = (
    "filter_stage IN ('"
    + "', '".join(sorted(SIZING_ELIGIBLE_FILTER_STAGES))
    + "')"
)


def check_null_rates(conn: sqlite3.Connection, now_utc: datetime) -> list:
    """Check NULL rates for key columns per product_type in last 24h.

    Bit 11.1b (2026-05-11): per-column eligibility filters applied —
    see PRODUCT_TYPE_SKIP + SIZING_ELIGIBLE_ONLY constants above and
    RCA in kb/findings/skill-audit-may11-bit-11.1b.md."""
    results = []

    if not table_exists(conn, "evaluated_opportunities"):
        return results

    cutoff = (now_utc - timedelta(hours=24)).isoformat()
    has_product_type = column_exists(conn, "evaluated_opportunities", "product_type")

    # Key columns to monitor per product_type
    base_columns = ["calibrated_prob", "edge", "market_price", "seconds_to_close"]
    crypto_columns = base_columns + ["volatility", "spot_price"]
    # Only check columns that actually exist
    all_check_columns = set(crypto_columns + [
        "raw_prob", "egarch_sigma", "egarch_blend_sigma",
        "entry_price", "position_size",
    ])
    existing_columns = []
    for col in all_check_columns:
        if column_exists(conn, "evaluated_opportunities", col):
            existing_columns.append(col)

    if not existing_columns:
        results.append((INFO, "NULL rates: No monitored columns found"))
        return results

    # Bit 11.1b R1 fix 2026-05-11: data-driven product_types via DISTINCT
    # query — the prior hardcoded `["15m", "hourly", "spx_hourly",
    # "weather"]` silently dropped `sports` + `dip_addon_shadow` (which
    # exist on prod). Now any product_type with rows in the 24h window
    # gets checked. Empty result → fall back to `[None]` (un-typed mode).
    if has_product_type:
        rows = conn.execute(
            "SELECT DISTINCT product_type FROM evaluated_opportunities "
            "WHERE evaluation_time > ? AND product_type IS NOT NULL "
            "ORDER BY product_type",
            (cutoff,),
        ).fetchall()
        product_types = [r[0] for r in rows] or [None]
    else:
        product_types = [None]
    NULL_THRESHOLD = 0.20  # Flag if >20% NULL

    for pt in product_types:
        if has_product_type and pt:
            where = "WHERE product_type = ? AND evaluation_time > ?"
            params = (pt, cutoff)
            label = {"15m": "15M", "hourly": "Hourly", "spx_hourly": "SPX", "weather": "Wx", "sports": "Sports"}.get(pt, pt)
        else:
            where = "WHERE evaluation_time > ?"
            params = (cutoff,)
            label = "All"

        # Get total count
        total_row = conn.execute(
            f"SELECT COUNT(*) FROM evaluated_opportunities {where}", params
        ).fetchone()
        total = total_row[0] if total_row else 0

        if total == 0:
            continue  # No data to check

        flagged = []
        for col in sorted(existing_columns):
            # Bit 11.1b eligibility — skip columns the current product_type
            # doesn't write at all (crypto-only features on weather/sports).
            if pt in PRODUCT_TYPE_SKIP.get(col, ()):
                continue

            # Bit 11.1b eligibility — scope sizing-conditional columns to
            # sizing-eligible filter_stages for both numerator AND
            # denominator. Otherwise rejection-pre-sizing rows inflate
            # the NULL rate.
            if col in SIZING_ELIGIBLE_ONLY:
                scoped_where = f"{where} AND {SIZING_ELIGIBLE_FILTER_STAGE_PREDICATE}"
                scoped_total_row = conn.execute(
                    f"SELECT COUNT(*) FROM evaluated_opportunities {scoped_where}",
                    params,
                ).fetchone()
                col_total = scoped_total_row[0] if scoped_total_row else 0
                if col_total == 0:
                    continue
                null_row = conn.execute(
                    f"SELECT COUNT(*) FROM evaluated_opportunities {scoped_where} "
                    f"AND {col} IS NULL",
                    params,
                ).fetchone()
            else:
                col_total = total
                null_row = conn.execute(
                    f"SELECT COUNT(*) FROM evaluated_opportunities {where} "
                    f"AND {col} IS NULL",
                    params,
                ).fetchone()
            null_count = null_row[0] if null_row else 0
            null_rate = null_count / col_total if col_total > 0 else 0

            if null_rate > NULL_THRESHOLD:
                flagged.append(f"{col} {null_rate:.0%} NULL")

        if flagged:
            severity = CRITICAL if any("raw_prob" in f or "calibrated_prob" in f for f in flagged) else WARNING
            results.append((severity, f"{label}: {', '.join(flagged)} (n={total}, 24h)"))
        else:
            results.append((OK, f"{label}: All key columns <{NULL_THRESHOLD:.0%} NULL (n={total}, 24h)"))

    return results


def check_shadow_variants(conn: sqlite3.Connection) -> list:
    """Check fifteenm_shadow_signals row counts per approach."""
    results = []

    if not table_exists(conn, "fifteenm_shadow_signals"):
        results.append((INFO, "Shadow: fifteenm_shadow_signals table not found"))
        return results

    has_approach = column_exists(conn, "fifteenm_shadow_signals", "approach")
    if not has_approach:
        results.append((INFO, "Shadow: No 'approach' column"))
        return results

    TARGET_N = 200  # Minimum rows needed for LightGBM training

    rows = conn.execute(
        "SELECT approach, COUNT(*) as cnt, "
        "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled "
        "FROM fifteenm_shadow_signals GROUP BY approach"
    ).fetchall()

    if not rows:
        results.append((WARNING, "Shadow: No data in fifteenm_shadow_signals"))
        return results

    for approach, total, settled in rows:
        settled = settled or 0
        pct = (settled / TARGET_N * 100) if TARGET_N > 0 else 0
        if settled >= TARGET_N:
            results.append((OK, f"Shadow {approach}: {settled}/{TARGET_N} settled ({pct:.0f}%) - READY"))
        else:
            remaining = TARGET_N - settled
            results.append((INFO, f"Shadow {approach}: {settled}/{TARGET_N} settled ({pct:.0f}%), {remaining} remaining"))

    return results


def check_no_side_flow(conn: sqlite3.Connection, now_utc: datetime) -> list:
    """Check NO-side evaluated_opportunities in last 6h."""
    results = []

    if not table_exists(conn, "evaluated_opportunities"):
        return results

    has_side = column_exists(conn, "evaluated_opportunities", "side")
    if not has_side:
        results.append((INFO, "NO-side: 'side' column not found"))
        return results

    cutoff = (now_utc - timedelta(hours=6)).isoformat()

    row = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities "
        "WHERE side = 'no' AND evaluation_time > ?",
        (cutoff,),
    ).fetchone()
    no_count = row[0] if row else 0

    if no_count == 0:
        # Check if it's market hours — only warn during active trading
        if is_crypto_market_hours(now_utc):
            results.append((WARNING, f"NO-side: 0 evals in last 6h (expected some during market hours)"))
        else:
            results.append((INFO, "NO-side: 0 evals in last 6h (outside market hours)"))
    else:
        results.append((OK, f"NO-side: {no_count} evals in last 6h"))

    return results


def check_db_integrity(conn: sqlite3.Connection) -> list:
    """Check settled_trades vs evaluated_opportunities consistency."""
    results = []

    has_settled = table_exists(conn, "settled_trades")
    has_evals = table_exists(conn, "evaluated_opportunities")

    if not has_settled or not has_evals:
        if not has_settled:
            results.append((WARNING, "DB integrity: settled_trades table missing"))
        if not has_evals:
            results.append((WARNING, "DB integrity: evaluated_opportunities table missing"))
        return results

    # Count settled trades
    settled_count = conn.execute("SELECT COUNT(*) FROM settled_trades").fetchone()[0]

    # Count candidate-stage evals (these become actual trades)
    candidate_count = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities WHERE filter_stage = 'candidate'"
    ).fetchone()[0]

    # Settled trades should roughly match candidate evals
    # Some candidates may not fill, so settled <= candidates
    if settled_count > 0 and candidate_count == 0:
        results.append((CRITICAL, f"DB integrity: {settled_count} settled trades but 0 candidate evals"))
    elif settled_count > candidate_count * 1.5 and settled_count > 10:
        results.append((WARNING,
                        f"DB integrity: {settled_count} settled > {candidate_count} candidates (unusual)"))
    else:
        results.append((OK, f"DB integrity: {settled_count} settled, {candidate_count} candidates"))

    # Check for orphaned settled trades (ticker not in evaluated_opportunities)
    has_side = column_exists(conn, "evaluated_opportunities", "side")
    orphan_row = conn.execute(
        "SELECT COUNT(*) FROM settled_trades st "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM evaluated_opportunities eo WHERE eo.ticker = st.ticker"
        ")"
    ).fetchone()
    orphans = orphan_row[0] if orphan_row else 0
    if orphans > 5:
        results.append((WARNING, f"DB integrity: {orphans} settled trades with no matching eval"))
    elif orphans > 0:
        results.append((INFO, f"DB integrity: {orphans} orphaned settled trades"))

    return results


def check_quiet_market(conn: sqlite3.Connection, now_utc: datetime, db_path: str) -> list:
    """Check if 15M candidates are flowing. Distinguish closed market from broken bot."""
    results = []

    if not table_exists(conn, "evaluated_opportunities"):
        return results

    if not is_crypto_market_hours(now_utc):
        results.append((INFO, "Quiet market: Outside crypto market hours, skipping"))
        return results

    cutoff = (now_utc - timedelta(hours=4)).isoformat()

    # Count 15M candidates in last 4h
    has_product_type = column_exists(conn, "evaluated_opportunities", "product_type")
    if has_product_type:
        cand_row = conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities "
            "WHERE filter_stage = 'candidate' AND product_type = '15m' AND evaluation_time > ?",
            (cutoff,),
        ).fetchone()
    else:
        cand_row = conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities "
            "WHERE filter_stage = 'candidate' AND evaluation_time > ?",
            (cutoff,),
        ).fetchone()
    candidates = cand_row[0] if cand_row else 0

    if candidates > 0:
        results.append((OK, f"Quiet market: {candidates} 15M candidates in last 4h"))
        return results

    # Zero candidates — check if ANY evals (including rejections) happened
    if has_product_type:
        any_row = conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities "
            "WHERE product_type = '15m' AND evaluation_time > ?",
            (cutoff,),
        ).fetchone()
    else:
        any_row = conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities WHERE evaluation_time > ?",
            (cutoff,),
        ).fetchone()
    any_evals = any_row[0] if any_row else 0

    # Also check scan_journal for scan activity
    scan_journal_path = Path(db_path).parent / "scan_journal.jsonl"
    scan_active = False
    if scan_journal_path.exists():
        try:
            # Check last few lines of scan journal for recent activity
            with open(scan_journal_path, "rb") as f:
                # Seek to last 10KB
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 10240))
                tail = f.read().decode("utf-8", errors="replace")
                lines = tail.strip().split("\n")
                if lines:
                    last_line = lines[-1]
                    try:
                        entry = json.loads(last_line)
                        ts_str = entry.get("ts", "")
                        if ts_str:
                            scan_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if scan_ts.tzinfo is None:
                                scan_ts = scan_ts.replace(tzinfo=timezone.utc)
                            if (now_utc - scan_ts).total_seconds() < 14400:  # 4h
                                scan_active = True
                    except (json.JSONDecodeError, ValueError):
                        pass
        except Exception:
            pass

    if any_evals > 0:
        results.append((INFO,
                        f"Quiet market: 0 candidates but {any_evals} evals in 4h "
                        "(markets may be filtered out)"))
    elif scan_active:
        results.append((WARNING,
                        "Quiet market: 0 evals in 4h but scan journal active "
                        "(bot running, no markets passing filters)"))
    else:
        results.append((CRITICAL,
                        "Quiet market: 0 evals and no scan activity in 4h "
                        "(bot may be down or markets closed)"))

    return results


# ── Main ─────────────────────────────────────────────────────────

def run_checks(db_path: str, verbose: bool = False) -> tuple:
    """Run all health checks. Returns (all_results, critical_results)."""
    now_utc = datetime.now(timezone.utc)

    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception as e:
        return ([(CRITICAL, f"Cannot open DB: {e}")], [(CRITICAL, f"Cannot open DB: {e}")])

    all_results = []

    # Run all checks
    checks = [
        ("STALE DATA", check_stale_data(conn, now_utc)),
        ("NULL RATES", check_null_rates(conn, now_utc)),
        ("SHADOW VARIANTS", check_shadow_variants(conn)),
        ("NO-SIDE FLOW", check_no_side_flow(conn, now_utc)),
        ("DB INTEGRITY", check_db_integrity(conn)),
        ("QUIET MARKET", check_quiet_market(conn, now_utc, db_path)),
    ]

    conn.close()

    critical_results = []
    for section, results in checks:
        for severity, msg in results:
            all_results.append((section, severity, msg))
            if severity == CRITICAL:
                critical_results.append((section, severity, msg))

    return all_results, critical_results


def format_summary(all_results: list, verbose: bool) -> str:
    """Format results as a readable summary table."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"  DATA HEALTH MONITOR  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)

    current_section = None
    for section, severity, msg in all_results:
        if not verbose and severity == OK:
            continue

        if section != current_section:
            lines.append("")
            lines.append(f"--- {section} ---")
            current_section = section

        lines.append(f"  {severity_icon(severity)} {msg}")

    # Summary
    crit_count = sum(1 for _, s, _ in all_results if s == CRITICAL)
    warn_count = sum(1 for _, s, _ in all_results if s == WARNING)
    ok_count = sum(1 for _, s, _ in all_results if s == OK)

    lines.append("")
    lines.append("-" * 60)
    lines.append(f"  Summary: {crit_count} critical, {warn_count} warnings, {ok_count} ok")
    lines.append("=" * 60)

    return "\n".join(lines)


def format_telegram_alert(critical_results: list) -> str:
    """Format critical alerts for Telegram."""
    lines = ["*DATA HEALTH ALERT*"]
    for section, severity, msg in critical_results:
        lines.append(f"- {msg}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Data health monitor for Kalshi bot")
    parser.add_argument("--db", default="state.db", help="Path to state.db (default: state.db)")
    parser.add_argument("--telegram", action="store_true", help="Send Telegram alerts for critical issues")
    parser.add_argument("--verbose", action="store_true", help="Show all checks including OK status")
    args = parser.parse_args()

    db_path = args.db
    if not Path(db_path).exists():
        print(f"[data_health] DB not found: {db_path}")
        sys.exit(1)

    all_results, critical_results = run_checks(db_path, verbose=args.verbose)

    # Always print summary to stdout
    print(format_summary(all_results, args.verbose))

    # Send Telegram for critical issues
    if args.telegram and critical_results:
        alert_msg = format_telegram_alert(critical_results)
        sent = send_telegram(alert_msg)
        if sent:
            print(f"\n[data_health] Telegram alert sent ({len(critical_results)} critical issues)")
        else:
            print(f"\n[data_health] Telegram alert FAILED")

    # Exit code: 2 for critical, 1 for warnings, 0 for clean
    if critical_results:
        sys.exit(2)
    elif any(s == WARNING for _, s, _ in all_results):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
