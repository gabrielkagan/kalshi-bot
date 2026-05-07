"""Post-deploy [6/6] startup-pattern gate.

Bit 2.0.5.3 of repo modularization plan
(kb/decisions/repo-modularization-plan-may05.md). Invoked from
.github/workflows/post_deploy_verify.yml as the last verification
step. Spec correction at
kb/decisions/bit-2.0.5.3-spec-correction-may07.md.

Signal: `bot_startup_log`. The cal_mlp integration writes one row
UNCONDITIONALLY at startup (`scripts/cal_mlp/integration.py:457`),
AFTER WAL verify + cal_mlp constants import, AND whether parity
assertion passes OR fails. So a row in this table proves the bot
reached its init's parity-check stage.

Threshold:
- 0 rows in window: FAIL (Bit 2.1a class — deploy didn't restart bot,
  OR bot crashed BEFORE reaching cal_mlp parity check: ExecStart,
  missing/invalid Kalshi credentials at bot.py:26173, WAL verify, or
  cal_mlp import failed).
- count > max_events (default 2): FAIL (post-init crash loop — bot
  inits, runs briefly, crashes, repeats).
- 1 ≤ count ≤ max_events: OK.

Empirical: 92 startup pairs across 8 days; max 2 events in any 5-min
window historically (Apr 29 + May 3 + May 5 dev-churn iterations).
≥3 (`max_events=2`) has zero historical false-positives.

SQL form: `julianday(ts) > julianday('now', '-N seconds')` —
numerical compare, NOT lex. `bot_startup_log.ts` uses Python's
`datetime.now(timezone.utc).isoformat()` which renders with `+00:00`
offset (NOT `Z` suffix as other bot.py write sites use). julianday()
handles both forms identically.

Why this is a separate script (not inline in the workflow): mirrors
`scripts/postdeploy_verify.py` and `scripts/audit_cron.py`
conventions, and the VPS doesn't have the `sqlite3` CLI installed
(it relies on Python's stdlib `sqlite3` module instead).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys


def query_startup_count(db_path: str, window_seconds: int) -> int:
    """Return the count of `bot_startup_log` rows within the lookback window.

    Uses Python's sqlite3 module (stdlib) — no `sqlite3` CLI required
    on the VPS. Sets `PRAGMA journal_mode=WAL` and
    `PRAGMA busy_timeout=10000` explicitly per scripts/CLAUDE.md
    convention; matches the pattern in `scripts/postdeploy_verify.py`
    and `scripts/audit_cron.py`. Connection-level `timeout=10.0` is
    Python's wrapper retry — distinct from SQLite's internal
    busy_timeout, which interacts with WAL-mode reader/writer
    concurrency. Sister scripts pin both, so this gate does too.

    SQL API surface is intentionally the lowest-common-denominator
    (`SELECT COUNT(*)` + `julianday()` only, no `RETURNING`, no JSON1,
    no `iif()`) so test-runner Python and venv Python don't need
    sqlite3-version parity.
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        cur = conn.execute(
            "SELECT COUNT(*) FROM bot_startup_log "
            "WHERE julianday(ts) > julianday('now', ?)",
            (f"-{int(window_seconds)} seconds",),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Post-deploy startup-pattern gate (Bit 2.0.5.3)."
    )
    p.add_argument(
        "--db",
        required=True,
        help="Path to state.db.",
    )
    p.add_argument(
        "--window-seconds",
        type=int,
        default=300,
        help="Lookback window in seconds (default 300 = 5 min).",
    )
    p.add_argument(
        "--max-events",
        type=int,
        default=2,
        help=(
            "Pass when count is 1..max_events. Empirical max in any "
            "5-min window across 92 historical pairs (last 8 days) "
            "is 2; default 2 makes count >= 3 the crash-loop floor."
        ),
    )
    args = p.parse_args()

    # Defensive validation (R4 MAJOR #2). Both bounds prevent
    # nonsensical unconditional-FAIL gates that masquerade as Bit
    # 2.1a alerts: --window-seconds=0 makes the cutoff equal to 'now'
    # so all rows fail >; --window-seconds=-N makes the cutoff arg
    # f"--N seconds" (double-dash) which julianday() returns NULL on;
    # --max-events=0 means count==0 hits the floor branch and
    # count>=1 hits the ceiling branch (`count > 0` is True), so the
    # gate cannot return OK for any DB state.
    if args.window_seconds < 1:
        p.error(
            f"--window-seconds must be >= 1; got {args.window_seconds}"
        )
    if args.max_events < 1:
        p.error(
            f"--max-events must be >= 1; got {args.max_events} "
            f"(max_events=0 produces an unconditional-FAIL gate)"
        )

    try:
        count = query_startup_count(args.db, args.window_seconds)
    except sqlite3.OperationalError as e:
        # Schema drift, missing table, locked beyond timeout, corrupt DB.
        # NOT misclassified as crash loop (R3 MAJOR #2): surface the
        # actual sqlite3 error to the operator.
        print(
            f"FAIL: sqlite3 query failed: {e}. Investigate state.db "
            f"(locked beyond 10s timeout? schema drift? "
            f"bot_startup_log table missing? state.db corrupt?)",
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # noqa: BLE001 — top-level CI gate, fail loud
        print(f"FAIL: unexpected error: {e!r}", file=sys.stderr)
        return 1

    # All FAIL paths emit to stderr (R4 MINOR #1) so CI log readers
    # see "stderr = anything bad, stdout = healthy/info" uniformly.
    if count == 0:
        # Bit 2.1a class: deploy didn't reach init at all.
        print(
            f"FAIL: bot_startup_log has 0 rows in last "
            f"{args.window_seconds}s — deploy didn't restart bot, "
            f"OR bot crashed before reaching cal_mlp parity check "
            f"(Bit 2.1a class: ExecStart, missing/invalid Kalshi "
            f"credentials at bot.py:26173, WAL verify, or cal_mlp "
            f"import failed)",
            file=sys.stderr,
        )
        return 1
    if count > args.max_events:
        # Post-init crash loop: bot inits, runs briefly, crashes,
        # repeats — bot.py:28053 cascading OR repeated SystemExit(2)
        # from CalMLPParityError.
        print(
            f"FAIL: bot has {count} startup events in last "
            f"{args.window_seconds}s — post-init crash loop "
            f"(threshold: > {args.max_events})",
            file=sys.stderr,
        )
        return 1
    print(
        f"  OK: {count} startup event(s) in last "
        f"{args.window_seconds}s (healthy: 1-{args.max_events})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
