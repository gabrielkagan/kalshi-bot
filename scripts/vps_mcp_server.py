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


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
