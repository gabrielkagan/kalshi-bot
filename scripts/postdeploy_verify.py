#!/usr/bin/env python3
"""Post-deploy DB-row verification (Tier 2 #4, Apr 24 2026).

Runs ON the VPS ~90s after service restart. For every enabled
feature flag, asserts expected DB rows are actually being created —
closes the class of bug that `systemctl is-active` + `no errors in
logs` can't catch:

  - nested-gate weather-NO-fires-never (Apr 4-11 2026, 0 trades 7 days)
  - STC-shadow-dead-code (98c954d Mar 1 2026, gate checked is None but
    windows had product_type='15m')
  - kill-switch state drifted from expected (env var 0 but flag code
    still fires, etc.)

The existing `.github/workflows/post_deploy_verify.yml` checks the
process is up and commits match. This script adds the next layer:
kill-switch ↔ DB-output parity.

Design:
  - Per-check: (name, strict|warn, guard, query, min_rows, reason)
  - `strict` failures exit 1 (block deploy verify)
  - `warn` failures print a warning annotation but exit 0
  - `guard` is a no-arg callable that returns False to skip the check
    (e.g., "only during US market hours", "only on weekends")
  - Run with --strict-all to escalate warnings to failures

Usage on VPS (called by workflow):
    python3 scripts/postdeploy_verify.py --db /home/botuser/kalshi-bot/state.db
    python3 scripts/postdeploy_verify.py --db state.db --strict-all
    python3 scripts/postdeploy_verify.py --db state.db --dry-run

See kb/concepts/contract-testing.md Tier 2 #4.
"""

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple


# ───────────────── Env-file loader ─────────────────

def load_env(env_path: Path) -> dict:
    """Best-effort .env parser. Handles KEY=VALUE, KEY="VALUE", KEY='VALUE'.
    No shell-expansion. Missing file returns {}."""
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def flag_truthy(value: Optional[str]) -> bool:
    if not value:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


# ───────────────── Bot-source flag reader ─────────────────

def read_bot_constants(bot_py: Path) -> dict:
    """Parse bot.py for module-level flag assignments. Regex-based rather
    than import-based to avoid side effects (WebSocket threads etc.)
    starting on import."""
    import re
    flags = {}
    if not bot_py.exists():
        return flags
    src = bot_py.read_text()
    # KEY = True / False / literal int
    pattern = re.compile(
        r"^(?P<key>[A-Z][A-Z0-9_]+)\s*=\s*(?P<val>True|False|\d+)\s",
        re.MULTILINE,
    )
    for m in pattern.finditer(src):
        key, val = m.group("key"), m.group("val")
        if val == "True":
            flags[key] = True
        elif val == "False":
            flags[key] = False
        else:
            try:
                flags[key] = int(val)
            except ValueError:
                pass
    return flags


# ───────────────── Guard predicates ─────────────────

def always(_now) -> bool:
    return True

def is_weekday_0411_utc(now: datetime) -> bool:
    return now.weekday() < 5 and 4 <= now.hour < 11

def is_weekend(now: datetime) -> bool:
    return now.weekday() >= 5


# ───────────────── Check definition ─────────────────

@dataclass
class Check:
    name: str
    strict: bool
    guard: Callable[[datetime], bool]
    query: str
    params: tuple
    min_rows: int
    reason: str  # Human-readable what-this-catches


def build_checks(bot_flags: dict, env: dict) -> List[Check]:
    """Each enabled feature flag produces one row check. Guard predicates
    skip checks outside their active window (e.g., overnight discount
    only 04-11 UTC weekdays)."""
    checks: List[Check] = []

    # ── 1. 15M scan liveness — ALWAYS STRICT ──
    # Bot must be scanning and writing to evaluated_opportunities. This
    # is the smoke-check: if this fails, nothing downstream works.
    checks.append(Check(
        name="15m_scan_liveness",
        strict=True,
        guard=always,
        query=(
            "SELECT COUNT(*) FROM evaluated_opportunities "
            "WHERE product_type='15m' "
            "AND evaluation_time >= ?"
        ),
        params=("__FIVE_MIN_AGO__",),
        min_rows=10,
        reason="bot is running but not scanning/writing 15M rows",
    ))

    # ── 2. Weather NO candidate ──
    # When WEATHER_NO_SIDE_LIVE=True, expect at least one weather NO row
    # per day. Slow rate (STC≥16h markets are few). Would have caught
    # the Apr 4-11 nested-gate bug in ~1 day instead of 7.
    if bot_flags.get("WEATHER_NO_SIDE_LIVE", False):
        checks.append(Check(
            name="weather_no_side_rows",
            strict=False,  # Warn first, escalate later if flaky
            guard=always,
            query=(
                "SELECT COUNT(*) FROM evaluated_opportunities "
                "WHERE product_type='weather' AND side='no' "
                "AND evaluation_time >= ?"
            ),
            params=("__24H_AGO__",),
            min_rows=1,
            reason="WEATHER_NO_SIDE_LIVE=True but zero NO rows in 24h",
        ))

    # ── 3. Sports shadow liveness ──
    # SPORTS_OBSERVATION_ONLY is hardcoded True. At least some signal
    # rows should appear over 24h even accounting for no-game days.
    if bot_flags.get("SPORTS_OBSERVATION_ONLY", False):
        checks.append(Check(
            name="sports_shadow_rows",
            strict=False,
            guard=always,
            query=(
                "SELECT COUNT(*) FROM sports_shadow_log "
                "WHERE evaluation_time >= ?"
            ),
            params=("__24H_AGO__",),
            min_rows=1,
            reason="sports observation on but zero shadow rows in 24h",
        ))

    # ── 4. Hourly observation liveness ──
    # Hourly shadow runs regardless of HOURLY_LIVE_ENABLED kill switch
    # (observation always records). Expect rows in last hour.
    checks.append(Check(
        name="hourly_observation_rows",
        strict=False,
        guard=always,
        query=(
            "SELECT COUNT(*) FROM evaluated_opportunities "
            "WHERE product_type='hourly' "
            "AND evaluation_time >= ?"
        ),
        params=("__1H_AGO__",),
        min_rows=1,
        reason="zero hourly observation rows in last hour",
    ))

    # ── 5. Hourly LIVE candidates (env-gated) ──
    if flag_truthy(env.get("HOURLY_LIVE_ENABLED")):
        checks.append(Check(
            name="hourly_live_candidates",
            strict=False,
            guard=always,
            query=(
                "SELECT COUNT(*) FROM evaluated_opportunities "
                "WHERE product_type='hourly' AND filter_stage='candidate' "
                "AND evaluation_time >= ?"
            ),
            params=("__1H_AGO__",),
            min_rows=1,
            reason="HOURLY_LIVE_ENABLED=1 but zero live candidates in 1h",
        ))

    # ── 6. Overnight discount (weekday 04-11 UTC only) ──
    if bot_flags.get("OVERNIGHT_DISCOUNT_LIVE", False):
        checks.append(Check(
            name="overnight_discount_rows",
            strict=False,
            guard=is_weekday_0411_utc,
            query=(
                "SELECT COUNT(*) FROM evaluated_opportunities "
                "WHERE filter_stage LIKE '%overnight_discount%' "
                "AND evaluation_time >= ?"
            ),
            params=("__30MIN_AGO__",),
            min_rows=1,
            reason="overnight discount live window but zero rows in 30m",
        ))

    # ── 7. Weekend discount (Sat/Sun only) ──
    if bot_flags.get("WEEKEND_DISCOUNT_LIVE", False):
        checks.append(Check(
            name="weekend_discount_rows",
            strict=False,
            guard=is_weekend,
            query=(
                "SELECT COUNT(*) FROM evaluated_opportunities "
                "WHERE filter_stage LIKE '%weekend_discount%' "
                "AND evaluation_time >= ?"
            ),
            params=("__30MIN_AGO__",),
            min_rows=1,
            reason="weekend discount live window but zero rows in 30m",
        ))

    return checks


# ───────────────── Runner ─────────────────

def _resolve_params(params: tuple, now: datetime) -> tuple:
    """Expand time-ago sentinels in check params."""
    lookup = {
        "__FIVE_MIN_AGO__":  (now - timedelta(minutes=5)).isoformat(),
        "__30MIN_AGO__":     (now - timedelta(minutes=30)).isoformat(),
        "__1H_AGO__":        (now - timedelta(hours=1)).isoformat(),
        "__24H_AGO__":       (now - timedelta(hours=24)).isoformat(),
    }
    return tuple(lookup.get(p, p) for p in params)


def run_checks(db_path: Path, checks: List[Check], *, strict_all: bool,
               dry_run: bool) -> int:
    """Returns process exit code: 0 ok, 1 strict failure."""
    now = datetime.now(timezone.utc)
    failures_strict = 0
    failures_warn = 0
    skipped = 0
    ok = 0

    if dry_run:
        print(f"[dry-run] would query {db_path} with {len(checks)} checks")

    conn: Optional[sqlite3.Connection] = None
    if not dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=10000")

    for chk in checks:
        if not chk.guard(now):
            print(f"  SKIP  {chk.name} (outside active window)")
            skipped += 1
            continue

        params = _resolve_params(chk.params, now)
        if dry_run:
            print(f"  DRY   {chk.name}  params={params}")
            continue

        try:
            row = conn.execute(chk.query, params).fetchone()
            n = int(row[0]) if row else 0
        except sqlite3.Error as e:
            tag = "FAIL" if (chk.strict or strict_all) else "WARN"
            print(f"  {tag}  {chk.name}  sqlite error: {e}")
            if chk.strict or strict_all:
                failures_strict += 1
            else:
                failures_warn += 1
            continue

        strict_now = chk.strict or strict_all
        if n >= chk.min_rows:
            print(f"  OK    {chk.name}  rows={n}  (min={chk.min_rows})")
            ok += 1
        else:
            tag = "FAIL" if strict_now else "WARN"
            print(f"  {tag}  {chk.name}  rows={n}  (min={chk.min_rows})  "
                  f"— {chk.reason}")
            if strict_now:
                failures_strict += 1
            else:
                failures_warn += 1

    if conn:
        conn.close()

    print()
    print(f"  summary: {ok} ok · {skipped} skipped · "
          f"{failures_warn} warn · {failures_strict} strict-fail")

    return 1 if failures_strict else 0


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="state.db", help="path to state.db")
    p.add_argument("--env", default=".env", help="path to .env")
    p.add_argument("--bot-py", default="bot.py", help="path to bot.py")
    p.add_argument("--strict-all", action="store_true",
                   help="treat all warnings as failures")
    p.add_argument("--dry-run", action="store_true",
                   help="list checks without querying DB")
    args = p.parse_args(argv)

    db_path = Path(args.db).resolve()
    env_path = Path(args.env).resolve()
    bot_path = Path(args.bot_py).resolve()

    if not args.dry_run and not db_path.exists():
        print(f"FATAL: state.db not found at {db_path}", file=sys.stderr)
        return 2

    env = load_env(env_path)
    # Also pick up process env (GitHub Actions etc.)
    for k in ("HOURLY_LIVE_ENABLED", "HOURLY_NO_SIDE_LIVE"):
        if k in os.environ:
            env[k] = os.environ[k]
    bot_flags = read_bot_constants(bot_path)

    checks = build_checks(bot_flags, env)

    print(f"=== Post-Deploy DB Row Verification ===")
    print(f"  db={db_path}")
    print(f"  checks={len(checks)}")
    print(f"  strict-all={args.strict_all}  dry-run={args.dry_run}")
    print()

    return run_checks(db_path, checks,
                      strict_all=args.strict_all, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
