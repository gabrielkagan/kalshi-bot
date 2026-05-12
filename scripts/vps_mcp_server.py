#!/usr/bin/env python3
"""MCP Server for Kalshi Bot VPS — exposes state.db queries and log access."""

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

DB_PATH = os.environ.get(
    "STATE_DB_PATH", os.path.expanduser("~/kalshi-bot-repo/state.db")
)
REPO_PATH = os.environ.get(
    "BOT_REPO_PATH", os.path.expanduser("~/kalshi-bot-repo")
)
SERVICE_NAME = "kalshi-bot"
SUBPROCESS_TIMEOUT = 10

# SQL statements that are NOT allowed (read-only enforcement)
_WRITE_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|REINDEX|VACUUM)\b",
    re.IGNORECASE,
)

# Bit 13.5: explicit allow-list for `get_journal_tail`. Mirrors the journal
# basenames declared in `bot/constants.py` (SCAN_JOURNAL, TRADE_JOURNAL, etc.)
# — kept as a literal here so the MCP surface stays independent of bot/
# import-time side effects on the VPS. Adding a journal = one-line edit.
_JOURNAL_ALLOWLIST = frozenset({
    "scan_journal.jsonl",
    "trade_journal.jsonl",
    "settlement_journal.jsonl",
    "order_journal.jsonl",
    "rejection_journal.jsonl",
    "opportunity_journal.jsonl",
    "execution_journal.jsonl",
    "performance_journal.jsonl",
    "fill_model_journal.jsonl",
    "raw_api_journal.jsonl",
})

# Bit 13.5: known pre-existing error signatures that `get_error_summary`
# suppresses (count separately, don't dominate the bucket list).
# Source: kb/decisions/bit-10.4-shipped-may12.md catalog +
# 86b9wgetr pre-existing surfaces.
_ERROR_SUPPRESS_PATTERNS = (
    "KXUELGAME",       # sports event 400s (pre-existing)
    "SPX-price-stale", # SPX engine occasional stale-price warn
    "sports-events",   # sports API transient 400
)

server = Server("kalshi-vps")


def _get_db_connection() -> sqlite3.Connection:
    """Create a read-only DB connection with safety pragmas."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def _run_cmd(cmd: list[str], timeout: int = SUBPROCESS_TIMEOUT) -> str:
    """Run a subprocess command and return stdout. Raises on failure."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        return f"[exit {result.returncode}] {result.stderr.strip() or result.stdout.strip()}"
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="query_db",
            description=(
                "Run a read-only SQL query against state.db. "
                "Returns results as a list of dicts. "
                "INSERT/UPDATE/DELETE/DROP/ALTER/CREATE are rejected."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SQL query to execute (SELECT/PRAGMA only).",
                    },
                    "params": {
                        "type": "array",
                        "items": {},
                        "description": "Optional positional parameters for the query.",
                        "default": [],
                    },
                },
                "required": ["sql"],
            },
        ),
        Tool(
            name="get_recent_logs",
            description=(
                "Fetch recent journalctl logs for the kalshi-bot service. "
                "Returns log text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "description": "Max number of log lines to return.",
                        "default": 50,
                    },
                    "since_minutes": {
                        "type": "integer",
                        "description": "Only show logs from the last N minutes.",
                        "default": 5,
                    },
                    "grep_pattern": {
                        "type": "string",
                        "description": "Optional grep pattern to filter log lines.",
                        "default": None,
                    },
                },
            },
        ),
        Tool(
            name="get_bot_status",
            description=(
                "Get bot health overview: systemd state, git commit, uptime, "
                "last 10 log lines, and current balance from DB."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_data_health",
            description=(
                "Quick data health check: eval counts per product_type in last 30min, "
                "NULL rates on key columns, latest timestamps per system, "
                "and error patterns in recent logs."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="checkpoint_wal",
            description=(
                "Run PRAGMA wal_checkpoint(PASSIVE) on state.db. "
                "Returns the checkpoint result."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ------------------------------------------------------------------
        # Bit 13.5 — new canned tools (all READ-ONLY).
        # ------------------------------------------------------------------
        Tool(
            name="get_settled_outcomes",
            description=(
                "Recent settled_trades rows with NET PnL (gross − fees) and "
                "W/L breakdown. Read-only. Filters by asset (optional) and "
                "settlement window. NET PnL respects SUM(pnl_cents − "
                "COALESCE(fee_cents, 0)) per scripts/CLAUDE.md."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "asset": {
                        "type": "string",
                        "description": "Asset symbol filter (BTC/ETH/SOL/XRP/HYPE/DOGE). Omit for all.",
                        "default": None,
                    },
                    "since_minutes": {
                        "type": "integer",
                        "description": "Only include trades settled in the last N minutes.",
                        "default": 60,
                        "minimum": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows returned (newest first).",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
            },
        ),
        Tool(
            name="get_active_window_summary",
            description=(
                "DB-only post-deploy-verify snapshot: latest evaluation_time "
                "per product_type, plus NULL counts on raw_prob and "
                "calibrated_prob across the last 30 min. No live Kalshi API "
                "call. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_journal_tail",
            description=(
                "Read the last N lines of an allow-listed *.jsonl journal "
                "file under the bot repo. Allow-list is hardcoded in the "
                "server source — no arbitrary path injection. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_basename": {
                        "type": "string",
                        "description": (
                            "Basename of the journal file "
                            "(e.g. 'scan_journal.jsonl'). Must be in the "
                            "_JOURNAL_ALLOWLIST set; path separators rejected."
                        ),
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of trailing lines to return.",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 1000,
                    },
                },
                "required": ["file_basename"],
            },
        ),
        Tool(
            name="get_error_summary",
            description=(
                "Count ERROR/CRITICAL/Traceback lines from journalctl in the "
                "last N minutes, bucketed by signature (last 120 chars after "
                "the level token). Suppresses known pre-existing surfaces "
                "(KXUELGAME 400, SPX-price-stale, sports-events transient "
                "400) — see _ERROR_SUPPRESS_PATTERNS. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since_minutes": {
                        "type": "integer",
                        "description": "Window in minutes (default 60).",
                        "default": 60,
                        "minimum": 1,
                    },
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "query_db":
            return await _tool_query_db(arguments)
        elif name == "get_recent_logs":
            return await _tool_get_recent_logs(arguments)
        elif name == "get_bot_status":
            return await _tool_get_bot_status(arguments)
        elif name == "get_data_health":
            return await _tool_get_data_health(arguments)
        elif name == "checkpoint_wal":
            return await _tool_checkpoint_wal(arguments)
        elif name == "get_settled_outcomes":
            return await _tool_get_settled_outcomes(arguments)
        elif name == "get_active_window_summary":
            return await _tool_get_active_window_summary(arguments)
        elif name == "get_journal_tail":
            return await _tool_get_journal_tail(arguments)
        elif name == "get_error_summary":
            return await _tool_get_error_summary(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {type(exc).__name__}: {exc}")]


async def _tool_query_db(args: dict) -> list[TextContent]:
    sql = args.get("sql", "").strip()
    params = args.get("params", []) or []

    if not sql:
        return [TextContent(type="text", text="Error: empty SQL query.")]

    # Read-only enforcement — reject write statements
    if _WRITE_PATTERN.search(sql):
        return [TextContent(
            type="text",
            text="Error: write operations are not allowed. Only SELECT and PRAGMA queries are permitted.",
        )]

    conn = _get_db_connection()
    try:
        cursor = conn.execute(sql, params)
        if cursor.description is None:
            # PRAGMA or statement with no result set
            return [TextContent(type="text", text="Query executed (no rows returned).")]
        rows = [dict(row) for row in cursor.fetchall()]
        return [TextContent(type="text", text=json.dumps(rows, default=str, indent=2))]
    finally:
        conn.close()


async def _tool_get_recent_logs(args: dict) -> list[TextContent]:
    lines = args.get("lines", 50)
    since_minutes = args.get("since_minutes", 5)
    grep_pattern = args.get("grep_pattern")

    cmd = [
        "journalctl",
        "-u", SERVICE_NAME,
        "--no-pager",
        f"--since={since_minutes} min ago",
        "-n", str(lines),
    ]

    if grep_pattern:
        cmd.extend(["-g", grep_pattern])

    output = _run_cmd(cmd)
    return [TextContent(type="text", text=output or "(no log output)")]


async def _tool_get_bot_status(args: dict) -> list[TextContent]:
    status = {}

    # systemd active state
    try:
        status["systemd_state"] = _run_cmd(
            ["systemctl", "is-active", SERVICE_NAME]
        )
    except Exception as e:
        status["systemd_state"] = f"error: {e}"

    # latest commit
    try:
        status["latest_commit"] = _run_cmd(
            ["git", "-C", REPO_PATH, "log", "--oneline", "-1"]
        )
    except Exception as e:
        status["latest_commit"] = f"error: {e}"

    # uptime
    try:
        raw = _run_cmd(
            ["systemctl", "show", SERVICE_NAME, "--property=ActiveEnterTimestamp"]
        )
        # e.g. ActiveEnterTimestamp=Mon 2026-03-07 12:00:00 UTC
        if "=" in raw:
            ts_str = raw.split("=", 1)[1].strip()
            status["started_at"] = ts_str
            status["uptime_note"] = "Compare started_at to current time for uptime."
        else:
            status["uptime_raw"] = raw
    except Exception as e:
        status["uptime"] = f"error: {e}"

    # last 10 log lines
    try:
        status["last_10_logs"] = _run_cmd(
            ["journalctl", "-u", SERVICE_NAME, "--no-pager", "-n", "10"]
        )
    except Exception as e:
        status["last_10_logs"] = f"error: {e}"

    # current balance from DB
    try:
        conn = _get_db_connection()
        try:
            row = conn.execute(
                "SELECT balance FROM balance_history ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            status["current_balance"] = dict(row) if row else "no balance_history table or no rows"
        except sqlite3.OperationalError:
            # Table might not exist; try evaluated_opportunities for latest balance reference
            status["current_balance"] = "balance_history table not found"
        finally:
            conn.close()
    except Exception as e:
        status["current_balance"] = f"error: {e}"

    return [TextContent(type="text", text=json.dumps(status, default=str, indent=2))]


async def _tool_get_data_health(args: dict) -> list[TextContent]:
    health = {}

    conn = _get_db_connection()
    try:
        # 1. Eval counts in last 30 min per product_type
        try:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(product_type, 'unknown') AS product_type,
                    filter_stage,
                    COUNT(*) AS cnt
                FROM evaluated_opportunities
                WHERE evaluation_time >= datetime('now', '-30 minutes')
                GROUP BY product_type, filter_stage
                ORDER BY product_type, filter_stage
                """
            ).fetchall()
            health["evals_last_30min"] = [dict(r) for r in rows]
        except Exception as e:
            health["evals_last_30min"] = f"error: {e}"

        # 2. NULL rates on key shadow/diagnostic columns
        try:
            key_cols = ["raw_prob", "egarch_sigma", "rk_sigma", "calibrated_prob"]
            null_rates = {}
            for col in key_cols:
                try:
                    row = conn.execute(
                        f"""
                        SELECT
                            COUNT(*) AS total,
                            SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS nulls
                        FROM evaluated_opportunities
                        WHERE evaluation_time >= datetime('now', '-60 minutes')
                        """
                    ).fetchone()
                    if row and row["total"] > 0:
                        null_rates[col] = {
                            "total": row["total"],
                            "nulls": row["nulls"],
                            "null_pct": round(100 * row["nulls"] / row["total"], 1),
                        }
                    else:
                        null_rates[col] = "no data in last 60 min"
                except sqlite3.OperationalError:
                    null_rates[col] = "column not found"
            health["null_rates_last_60min"] = null_rates
        except Exception as e:
            health["null_rates_last_60min"] = f"error: {e}"

        # 3. Latest timestamp per product_type
        try:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(product_type, 'unknown') AS product_type,
                    MAX(evaluation_time) AS latest_ts
                FROM evaluated_opportunities
                GROUP BY product_type
                ORDER BY latest_ts DESC
                """
            ).fetchall()
            health["latest_timestamps"] = [dict(r) for r in rows]
        except Exception as e:
            health["latest_timestamps"] = f"error: {e}"

        # 4. Settled trade counts by product_type (last 24h)
        try:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(product_type, '15m') AS product_type,
                    COUNT(*) AS cnt,
                    SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_cents <= 0 THEN 1 ELSE 0 END) AS losses
                FROM settled_trades
                WHERE settled_at >= datetime('now', '-24 hours')
                GROUP BY product_type
                """
            ).fetchall()
            health["settled_last_24h"] = [dict(r) for r in rows]
        except Exception as e:
            health["settled_last_24h"] = f"error: {e}"

    finally:
        conn.close()

    # 5. Error patterns in recent logs
    try:
        log_output = _run_cmd(
            ["journalctl", "-u", SERVICE_NAME, "--no-pager",
             "--since=30 min ago", "-n", "500"]
        )
        # Count lines with ERROR/WARNING/Exception/Traceback
        error_lines = []
        for line in log_output.split("\n"):
            if any(kw in line for kw in ["ERROR", "Exception", "Traceback", "CRITICAL"]):
                error_lines.append(line.strip())
        health["error_lines_last_30min"] = len(error_lines)
        # Show up to 20 unique error snippets
        if error_lines:
            seen = []
            for line in error_lines:
                short = line[-120:]  # last 120 chars as dedup key
                if short not in seen:
                    seen.append(short)
                if len(seen) >= 20:
                    break
            health["sample_errors"] = seen
    except Exception as e:
        health["recent_errors"] = f"error: {e}"

    return [TextContent(type="text", text=json.dumps(health, default=str, indent=2))]


async def _tool_checkpoint_wal(args: dict) -> list[TextContent]:
    conn = _get_db_connection()
    try:
        result = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return [TextContent(
            type="text",
            text=json.dumps({
                "status": result[0] if result else None,
                "pages_wallog": result[1] if result and len(result) > 1 else None,
                "pages_checkpointed": result[2] if result and len(result) > 2 else None,
            }, indent=2),
        )]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bit 13.5 — new tool helpers (all READ-ONLY)
# ---------------------------------------------------------------------------


async def _tool_get_settled_outcomes(args: dict) -> list[TextContent]:
    """Recent settled trades w/ NET PnL (gross − fees) and W/L breakdown.

    Read-only? Yes. Pure SELECTs against `settled_trades`.

    Args:
        asset (str | None): asset symbol filter (BTC/ETH/SOL/XRP/HYPE/DOGE).
        since_minutes (int): trade-settlement window in minutes (default 60).
        limit (int): max rows to return (default 20, max 500).

    Returns:
        TextContent with JSON:
            {
              "summary": {"n": int, "wins": int, "losses": int,
                          "net_pnl_cents": int, "gross_pnl_cents": int,
                          "fees_cents": int, "asset_filter": str | None,
                          "since_minutes": int},
              "rows": [<row dict>, ...]   # newest-first, capped at `limit`
            }
    """
    asset = args.get("asset")
    since_minutes = int(args.get("since_minutes") or 60)
    if since_minutes < 1:
        since_minutes = 1
    limit = int(args.get("limit") or 20)
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    # NOTE: SQLite has no minutes-arithmetic param shape, so we pass a
    # `-N minutes` modifier string as a positional parameter. The modifier
    # value is server-controlled (built from an int), so no injection.
    modifier = f"-{since_minutes} minutes"

    sql_base = (
        "SELECT ticker, asset, side, count, entry_price_cents, "
        "revenue_cents, fee_cents, pnl_cents, settled_at, product_type "
        "FROM settled_trades "
        "WHERE settled_at >= datetime('now', ?)"
    )
    params: list = [modifier]
    if asset:
        sql_base += " AND asset = ?"
        params.append(asset)
    sql_base += " ORDER BY settled_at DESC LIMIT ?"
    params.append(limit)

    conn = _get_db_connection()
    try:
        rows = [dict(r) for r in conn.execute(sql_base, params).fetchall()]
    finally:
        conn.close()

    # Aggregate NET PnL = pnl_cents - COALESCE(fee_cents, 0). Win/loss
    # classification matches `_tool_get_data_health`'s convention:
    #   net > 0 → win, net <= 0 → loss (ties are losses). Keep this in
    # lock-step so dashboards / audits show the same totals.
    n = len(rows)
    wins = 0
    losses = 0
    gross = 0
    fees = 0
    for r in rows:
        pnl = r.get("pnl_cents") or 0
        fee = r.get("fee_cents") or 0
        net = pnl - fee
        gross += pnl
        fees += fee
        if net > 0:
            wins += 1
        else:
            losses += 1
    summary = {
        "n": n,
        "wins": wins,
        "losses": losses,
        "net_pnl_cents": gross - fees,
        "gross_pnl_cents": gross,
        "fees_cents": fees,
        "asset_filter": asset,
        "since_minutes": since_minutes,
    }
    return [TextContent(
        type="text",
        text=json.dumps({"summary": summary, "rows": rows}, default=str, indent=2),
    )]


async def _tool_get_active_window_summary(args: dict) -> list[TextContent]:
    """Latest evaluation_time per product_type + NULL counts on key prob cols.

    Read-only? Yes. Pure SELECTs against `evaluated_opportunities`.

    Args:
        (none)

    Returns:
        TextContent with JSON:
            {
              "latest_per_product_type": [{"product_type": str,
                                           "latest_ts": str,
                                           "row_count_last_30min": int}, ...],
              "null_counts_last_30min": {
                  "raw_prob":        {"total": int, "nulls": int, "null_pct": float},
                  "calibrated_prob": {"total": int, "nulls": int, "null_pct": float},
              }
            }

    Use case: post-deploy-verify pattern from bit-12.1-fu1 — after
    pushing, confirm the bot is writing raw_prob/calibrated_prob (NULL
    rate ~0%) and that 15M / hourly / spx are all evaluating.
    """
    conn = _get_db_connection()
    try:
        latest_rows = conn.execute(
            """
            SELECT
                COALESCE(product_type, 'unknown') AS product_type,
                MAX(evaluation_time) AS latest_ts,
                SUM(CASE
                    WHEN evaluation_time >= datetime('now', '-30 minutes')
                    THEN 1 ELSE 0 END
                ) AS row_count_last_30min
            FROM evaluated_opportunities
            GROUP BY product_type
            ORDER BY latest_ts DESC
            """
        ).fetchall()
        latest = [dict(r) for r in latest_rows]

        # NULL rates on raw_prob and calibrated_prob in the last 30 min.
        # Compute both columns in ONE static SQL — no f-string, no
        # dynamic column-name injection. The pattern mirrors the
        # production `_tool_get_data_health` shape but tightens it to
        # static SQL since this Bit's tests guard f-string SQL.
        null_counts: dict = {}
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN raw_prob IS NULL THEN 1 ELSE 0 END) AS raw_nulls,
                    SUM(CASE WHEN calibrated_prob IS NULL THEN 1 ELSE 0 END) AS cal_nulls
                FROM evaluated_opportunities
                WHERE evaluation_time >= datetime('now', '-30 minutes')
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            null_counts = {"error": f"schema mismatch: {exc}"}
            row = None

        if row is not None:
            total = (row["total"] if row else 0) or 0
            raw_nulls = (row["raw_nulls"] if row else 0) or 0
            cal_nulls = (row["cal_nulls"] if row else 0) or 0
            null_counts = {
                "raw_prob": {
                    "total": total,
                    "nulls": raw_nulls,
                    "null_pct": round(100 * raw_nulls / total, 1) if total else None,
                },
                "calibrated_prob": {
                    "total": total,
                    "nulls": cal_nulls,
                    "null_pct": round(100 * cal_nulls / total, 1) if total else None,
                },
            }
    finally:
        conn.close()

    payload = {
        "latest_per_product_type": latest,
        "null_counts_last_30min": null_counts,
    }
    return [TextContent(
        type="text",
        text=json.dumps(payload, default=str, indent=2),
    )]


async def _tool_get_journal_tail(args: dict) -> list[TextContent]:
    """Tail an allow-listed *.jsonl journal file under the bot repo.

    Read-only? Yes. File read only, no writes.

    Args:
        file_basename (str): journal basename (e.g. 'scan_journal.jsonl').
            MUST be in `_JOURNAL_ALLOWLIST`; path separators / traversal
            sequences are rejected.
        lines (int): number of trailing lines to return (default 50).

    Returns:
        TextContent with raw text of the tail (one line per record). On
        rejection or missing file, returns a one-line error message
        (still as TextContent, never raises).

    Security:
        - Basename must be in `_JOURNAL_ALLOWLIST` (hardcoded literal).
        - `os.sep`, `/`, `\\`, `..`, and any non-allow-listed name return
          a structured error.
        - Final path is `os.path.join(REPO_PATH, basename)` and is
          verified to resolve INSIDE REPO_PATH via os.path.commonpath.

    Note:
        `raw_api_journal.jsonl` contains FULL Kalshi API response bodies
        for forensic work (settlements, orders, fills). Auth tokens are
        in HTTP headers and NEVER serialized into the response body, so
        tailing this journal does not expose credentials. The journal
        already lives on disk readable by anyone with SSH to the VPS;
        MCP just promotes that access pattern. If you wouldn't paste
        the line into a Telegram message, don't paste it into chat.
    """
    basename = args.get("file_basename", "")
    lines = int(args.get("lines") or 50)
    if lines < 1:
        lines = 1
    if lines > 1000:
        lines = 1000

    if not isinstance(basename, str) or not basename:
        return [TextContent(type="text", text="Error: file_basename required")]

    # Reject any traversal / separator chars BEFORE consulting the allow-list.
    if any(ch in basename for ch in ("/", "\\", "..", "\0")):
        return [TextContent(
            type="text",
            text=f"Error: file_basename rejected (path separators not allowed): {basename!r}",
        )]

    if basename not in _JOURNAL_ALLOWLIST:
        return [TextContent(
            type="text",
            text=(
                f"Error: file_basename not in allow-list: {basename!r}. "
                f"Allowed: {sorted(_JOURNAL_ALLOWLIST)}"
            ),
        )]

    file_path = os.path.join(REPO_PATH, basename)
    # Defense in depth — confirm the resolved path stays under REPO_PATH.
    try:
        repo_real = os.path.realpath(REPO_PATH)
        file_real = os.path.realpath(file_path)
        if os.path.commonpath([repo_real, file_real]) != repo_real:
            return [TextContent(
                type="text",
                text="Error: resolved file escapes REPO_PATH (path traversal blocked)",
            )]
    except (ValueError, OSError) as exc:
        return [TextContent(
            type="text",
            text=f"Error: path resolution failed: {exc}",
        )]

    if not os.path.isfile(file_real):
        return [TextContent(
            type="text",
            text=f"Error: journal file not found: {basename}",
        )]

    # Use `tail` via list-form subprocess (no shell=True) to avoid loading
    # large journals into memory. Fall back to in-process if tail unavailable.
    try:
        out = _run_cmd(["tail", "-n", str(lines), file_real])
        return [TextContent(type="text", text=out or "(empty)")]
    except FileNotFoundError:
        try:
            with open(file_real, "r", encoding="utf-8", errors="replace") as fh:
                buf = fh.readlines()
            tail = "".join(buf[-lines:])
            return [TextContent(type="text", text=tail or "(empty)")]
        except OSError as exc:
            return [TextContent(type="text", text=f"Error reading journal: {exc}")]


def _bucket_signature(line: str) -> str:
    """Reduce a log line to a stable bucket key (last 120 chars, stripped)."""
    s = line.strip()
    return s[-120:] if len(s) > 120 else s


async def _tool_get_error_summary(args: dict) -> list[TextContent]:
    """Bucket ERROR/CRITICAL/Traceback lines from recent journalctl output.

    Read-only? Yes. journalctl is invoked read-only via list-form subprocess.

    Args:
        since_minutes (int): window in minutes (default 60).

    Returns:
        TextContent with JSON:
            {
              "total_error_lines": int,
              "suppressed_count": int,
              "buckets": [{"signature": str, "count": int}, ...]  # desc by count
            }

    Suppression: any line containing a substring from
    `_ERROR_SUPPRESS_PATTERNS` is counted in `suppressed_count` but
    EXCLUDED from the bucket list. The suppress list captures known
    pre-existing surfaces so emergent regressions stay visible.
    """
    since_minutes = int(args.get("since_minutes") or 60)
    if since_minutes < 1:
        since_minutes = 1

    cmd = [
        "journalctl",
        "-u", SERVICE_NAME,
        "--no-pager",
        f"--since={since_minutes} min ago",
        "-n", "5000",
    ]
    log_output = _run_cmd(cmd, timeout=SUBPROCESS_TIMEOUT)

    error_kws = ("ERROR", "CRITICAL", "Exception", "Traceback")
    buckets: dict = {}
    suppressed = 0
    total = 0
    for raw_line in (log_output or "").split("\n"):
        if not raw_line:
            continue
        if not any(kw in raw_line for kw in error_kws):
            continue
        total += 1
        if any(pat in raw_line for pat in _ERROR_SUPPRESS_PATTERNS):
            suppressed += 1
            continue
        sig = _bucket_signature(raw_line)
        buckets[sig] = buckets.get(sig, 0) + 1

    bucket_list = sorted(
        ({"signature": k, "count": v} for k, v in buckets.items()),
        key=lambda d: d["count"],
        reverse=True,
    )
    payload = {
        "since_minutes": since_minutes,
        "total_error_lines": total,
        "suppressed_count": suppressed,
        "suppress_patterns": list(_ERROR_SUPPRESS_PATTERNS),
        "buckets": bucket_list[:30],  # cap at 30 distinct signatures
    }
    return [TextContent(
        type="text",
        text=json.dumps(payload, default=str, indent=2),
    )]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
