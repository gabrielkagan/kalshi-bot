#!/usr/bin/env python3
"""Calibrator feature health monitor (P3, R-p7-deploy-r11).

Queries non-null rates of cal_mlp v1/v2/v3 features on training-eligible
15M rows over a rolling 7-day window. Alerts via Telegram if any feature
drops below threshold.

Background: kb/concepts/calibrator-data-hygiene-apr29.md.

The Apr 29 audit found:
  - v2 cohort (Apr 19 features): 88-92% non-null aggregate, due to 5m
    spot history buffer being empty for ~5 min after every WS reconnect.
  - v3 cohort (Apr 23 features): 91-100% non-null, same reconnect pattern.

This monitor catches when feature populating regresses BEFORE the May 19
v2 K=1 train. Without it, we'd discover a silent NULL spike at train time
and wait 3+ weeks for cleaner data.

Usage:
    python3 scripts/calibrator_feature_health.py --db /path/to/state.db
    python3 scripts/calibrator_feature_health.py --db ... --telegram
    python3 scripts/calibrator_feature_health.py --db ... --verbose

Cron: every 6 hours
    0 */6 * * * cd ~/kalshi-bot-repo && python3 scripts/calibrator_feature_health.py \\
        --db state.db --telegram >> logs/calmlp_feature_health.log 2>&1

Exit codes:
    0 = all features healthy
    1 = at least one feature below threshold
    2 = DB error / table missing
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional


# Threshold contract: features that should be ≥99% non-null on training-eligible
# rows. The post-reconnect 5m-momentum class gets a relaxed 95% threshold
# because the WS-reconnect-recovery fix is being staged separately (P1).
HIGH_THRESHOLD = 99.0  # default for clean features
RELAXED_THRESHOLD = 95.0  # for features known to dip post-reconnect

# Per-cohort activation dates. The monitor measures non-null rate from the
# LATER of (rolling-window-start, cohort_activation). Without this, a 7-day
# window crossing a feature's introduction date would spuriously alert on
# pre-existence NULLs (the column literally didn't exist).
#
# Use the FIRST FULL DAY after the rollout — the introduction day itself is
# typically a partial-rollout (deploy mid-day → some scans had old code),
# producing a long tail of legitimate NULLs on the introduction date.
COHORT_ACTIVATION_DATE = {
    'v1': '2026-02-23',  # Feb 22 was rollout
    'v2': '2026-04-20',  # Apr 19 was rollout
    'v3': '2026-04-24',  # Apr 23 was rollout
}

# v1 features (live since Feb 22 — should be 100% non-null on rolling 7d).
V1_FEATURES = [
    'market_price',
    'seconds_to_close',
    'spot_distance_to_strike_sigma',
    'prob_breakeven_gap',
    'hour_of_day_utc',  # source for hour_sin/cos derivation
]

# v2 cohort (Apr 19+).
V2_FEATURES = [
    ('window_max_buf_pct', HIGH_THRESHOLD),
    ('window_min_buf_pct', HIGH_THRESHOLD),
    ('btc_realized_vol_15m', HIGH_THRESHOLD),
    ('minutes_above_strike', HIGH_THRESHOLD),
    ('spot_momentum_60s_bps', HIGH_THRESHOLD),
    # 5-minute momentum features depend on a 5-min spot history buffer that
    # empties on WS reconnect. Until the buffer-persistence fix lands (P1),
    # accept ≥95% non-null.
    ('spot_momentum_5m_bps', RELAXED_THRESHOLD),
    ('spot_realized_range_15m_bps', HIGH_THRESHOLD),
    ('btc_spot_change_5m_bps', RELAXED_THRESHOLD),
]

# v3 cohort (Apr 23+).
V3_FEATURES = [
    ('yes_spread_cents', HIGH_THRESHOLD),
    ('spot_coinbase_kraken_gap_bps', HIGH_THRESHOLD),
    # flow_velocity also dips post-reconnect (same WS data path).
    ('kalshi_flow_depth_velocity', RELAXED_THRESHOLD),
]


# Training-eligibility filter, mirroring scripts/cal_mlp/extract_data.py
# _classify_drop. Aligns this monitor with what training will actually see.
ELIGIBLE_FILTER = """
    product_type = '15m'
    AND ticker NOT LIKE 'SPORTS-%'
    AND market_price IS NOT NULL
    AND market_price > 0
    AND market_price >= 75   -- GLOBAL_MIN_ENTRY_PRICE for include_sub_floor
    AND raw_prob IS NOT NULL
    AND market_result IN ('yes','all_yes','no','all_no')
    AND settled_time IS NOT NULL
    AND side IN ('yes','no')
    AND seconds_to_close IS NOT NULL
"""


def send_telegram(msg: str) -> bool:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token or not chat_id:
        print('[calmlp_health] No Telegram config; skipping alert.', file=sys.stderr)
        return False
    try:
        import urllib.parse
        import urllib.request
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        data = urllib.parse.urlencode({
            'chat_id': chat_id,
            'text': msg,
            'parse_mode': 'Markdown',
        }).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f'[calmlp_health] Telegram send failed: {e}', file=sys.stderr)
        return False


def _build_valid_columns(conn: sqlite3.Connection) -> set:
    """Return the set of column names actually present in
    evaluated_opportunities. Used to whitelist any feature name
    before SQL-interpolating it (defense against future CLI flags
    or features.py introspection wiring)."""
    return {row[1] for row in conn.execute(
        'PRAGMA table_info(evaluated_opportunities)'
    ).fetchall()}


def query_non_null_pct(
    conn: sqlite3.Connection,
    column: str,
    since_iso: str,
    valid_columns: set,
) -> tuple[int, float]:
    """Return (n_eligible, pct_non_null) for the given column over the
    rolling window. Returns (0, 100.0) when there are no eligible rows
    (treated as healthy — calling code distinguishes via n).

    R-p7-deploy-r11 R2: column is validated against valid_columns
    before SQL-interpolation to prevent injection / silent typos.
    """
    if column not in valid_columns:
        raise ValueError(
            f"feature column {column!r} not present in evaluated_opportunities "
            f"(typo? schema drift? rename without updating monitor?)"
        )
    cur = conn.execute(
        f"""SELECT
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN {column} IS NOT NULL THEN 1 ELSE 0 END)
                  / NULLIF(COUNT(*), 0), 2) AS pct
        FROM evaluated_opportunities
        WHERE evaluation_time >= ?
          AND {ELIGIBLE_FILTER}""",
        (since_iso,),
    )
    row = cur.fetchone()
    n = int(row[0]) if row and row[0] is not None else 0
    pct = float(row[1]) if row and row[1] is not None else 100.0
    return n, pct


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='state.db')
    ap.add_argument('--days', type=int, default=7,
                    help='rolling window size (default 7)')
    ap.add_argument('--telegram', action='store_true',
                    help='alert via Telegram on any feature failing threshold')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f'[calmlp_health] DB not found: {args.db}', file=sys.stderr)
        return 2

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    # R-p7-deploy-r11 R2: bot.py writes evaluation_time with microsecond
    # precision (`%Y-%m-%dT%H:%M:%S.%fZ`). Match that format so lexical
    # `evaluation_time >= since_iso` doesn't exclude rows whose second
    # equals the cutoff second (DB has `.123456Z`, `.` < `Z` lexically).
    since_iso = since.strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    try:
        conn = sqlite3.connect(args.db)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=10000')
    except sqlite3.OperationalError as e:
        print(f'[calmlp_health] DB open failed: {e}', file=sys.stderr)
        return 2

    # Confirm evaluated_opportunities exists.
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='evaluated_opportunities'"
    ).fetchone()
    if not row or row[0] == 0:
        print('[calmlp_health] evaluated_opportunities table missing', file=sys.stderr)
        return 2

    # Whitelist of feature column names actually in the schema. Any feature
    # not present raises ValueError — catches typos/rename drift early.
    valid_columns = _build_valid_columns(conn)

    # Sample the eligible row count first — if zero, no point reporting per-feature.
    # We bypass the column-name validator here intentionally (the literal `1`
    # is constant SQL, not a column name) by using a separate COUNT path.
    cur = conn.execute(
        f"SELECT COUNT(*) FROM evaluated_opportunities "
        f"WHERE evaluation_time >= ? AND {ELIGIBLE_FILTER}",
        (since_iso,),
    )
    n = int(cur.fetchone()[0] or 0)
    if n == 0:
        msg = (
            f'[calmlp_health] No training-eligible rows in last {args.days}d. '
            f'Either bot is down or the eligibility filter is broken.'
        )
        print(msg, file=sys.stderr)
        if args.telegram:
            send_telegram(f'cal\\_mlp health: {msg}')
        return 1

    # `pct` is float OR the literal string 'SCHEMA_DRIFT' (R4 sentinel for
    # missing column). Body-build branches on the type.
    failures: list[tuple[str, str, "float | str", float, int]] = []
    healthy_count = 0
    cohort_iter = (
        [('v1', f, HIGH_THRESHOLD) for f in V1_FEATURES]
        + [('v2', f, t) for f, t in V2_FEATURES]
        + [('v3', f, t) for f, t in V3_FEATURES]
    )
    for cohort, feature, threshold in cohort_iter:
        # Effective window start: max(rolling_window_start, cohort_activation)
        # to avoid spuriously measuring pre-existence NULLs.
        activation = COHORT_ACTIVATION_DATE.get(cohort, since_iso)
        effective_since = max(since_iso, activation + 'T00:00:00Z')
        try:
            n_seen, pct = query_non_null_pct(
                conn, feature, effective_since, valid_columns,
            )
        except ValueError as e:
            # R-p7-deploy-r11 R3-H2: schema drift — feature column in
            # V1/V2/V3 lists but missing from evaluated_opportunities.
            # Surface as an explicit failure entry so the existing alert
            # path (Telegram + exit 1/3) fires. R4 (MED): use string
            # sentinel `'SCHEMA_DRIFT'` instead of magic-number tuple
            # so a real feature with 0% non-null on threshold 100% can't
            # collide with the schema-drift signal.
            print(f'[FAIL] [{cohort}] {feature}: SCHEMA_DRIFT ({e})')
            failures.append((cohort, feature, 'SCHEMA_DRIFT', 100.0, 0))
            continue
        if n_seen == 0:
            # Cohort hasn't accumulated any rows in window yet — neither pass nor fail.
            if args.verbose:
                print(f'[SKIP] [{cohort}] {feature}: no eligible rows since {effective_since}')
            continue
        status = 'OK' if pct >= threshold else 'FAIL'
        if status == 'OK':
            healthy_count += 1
        else:
            failures.append((cohort, feature, pct, threshold, n_seen))
        if args.verbose or status == 'FAIL':
            print(f'[{status}] [{cohort}] {feature}: {pct}% '
                  f'(threshold {threshold}%, n={n_seen})')

    if args.verbose:
        print(f'\nSummary: {healthy_count}/{len(cohort_iter)} features healthy. '
              f'Window: {args.days}d ending {datetime.now(timezone.utc).isoformat()}')

    if failures:
        body_lines = [
            f'*cal_mlp feature-health alert* ({args.days}d window)',
            f'Eligible rows: {n}',
            '',
        ]
        for cohort, feature, pct, threshold, _n_seen in failures:
            # R4 (MED): explicit string sentinel for schema-drift, no
            # magic-number coincidence with a 0%-non-null real feature.
            if pct == 'SCHEMA_DRIFT':
                body_lines.append(
                    f'• [{cohort}] `{feature}`: SCHEMA DRIFT (column missing)'
                )
            else:
                body_lines.append(
                    f'• [{cohort}] `{feature}`: {pct}% < {threshold}%'
                )
        body_lines.append('')
        body_lines.append(
            'Action: investigate WS reconnect cycle and feature backfill.'
        )
        msg = '\n'.join(body_lines)
        print(msg, file=sys.stderr)
        # R-p7-deploy-r11 R2: distinguish "alert sent" vs "alert failed
        # to send" via exit code. Without this, a Telegram outage silently
        # eats the alert (cron typically `>>log 2>&1` swallows stderr).
        # Exit 3 = had failures AND couldn't notify -> alarmable separately.
        if args.telegram:
            telegram_ok = send_telegram(msg)
            if not telegram_ok:
                print('[calmlp_health] CRITICAL: failures detected AND telegram '
                      'send failed; check TELEGRAM_BOT_TOKEN/CHAT_ID env',
                      file=sys.stderr)
                return 3
        return 1

    if args.verbose:
        print('[calmlp_health] All features healthy.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
