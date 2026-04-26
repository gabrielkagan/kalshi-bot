#!/usr/bin/env python3
"""Query state.db on the VPS and output whitepaper stats as JSON.

OUTPUT FIELDS:

Core (settled_trades = real LIVE money outcomes; the bot left observation mode in Feb 2026):
  - live_pnl_cents:           SUM(pnl_cents) across all settled_trades.
  - live_settled / live_wins / live_losses
  - settled_by_strategy_group: {strategy_group: {n, wins, pnl_cents, mean_entry_price_cents}}
  - unknown_strategy_groups:  list of strategy_group values not in KNOWN_STRATEGY_GROUPS;
                              empty if all known. Also emitted as stderr warning.

Funnel (evaluated_opportunities):
  - total_evaluated, filter_breakdown, filter_breakdown_by_product
  - total_live_candidates:    count of EO rows whose filter_stage means "would have entered live".
                              Excludes shadow stages (anything with `_shadow` suffix or
                              T2-Z2 which is intentionally shadowed).
  - assets_tracked, observation_period

Shadow strategies:
  - shadow_counterfactual_pnl_by_stage: {filter_stage: {n, pnl_cents, mean_pnl_cents}}.
                              From evaluated_opportunities.counterfactual_pnl. This is the
                              actual "observation P&L" axis — settled_trades has no
                              observation rows, only filled live trades.

Calibration:
  - brier:  {overall: float | None, by_product: {pt: float}}.
            SIDE-AWARE: for a NO-side row, model_p_of_win = 1 - raw_prob.
            Computed from evaluated_opportunities (raw_prob + market_result both populated
            for live candidates — settled_trades does NOT have raw_prob).

Regime cuts (block of {live_pnl_cents, live_settled, live_wins, brier_overall}):
  - since_apr11: post-loss-burst-cooldown (commit 4075655 fix lands 2026-04-11T20:43:27Z)
  - since_apr23: post-WS-schema-fix (commit 0ddcaf8 lands 2026-04-23T23:46:07Z)

Backwards-compat (kept so the workflow keeps rendering old templates without crashing,
but `observation_pnl` LABEL was the lie — drop the placeholder from build_whitepaper.py):
  - total_settled, total_wins, total_trades (unchanged)
  - observation_pnl: alias for live_pnl_cents (was historically misnamed)
  - win_rate_by_price (unchanged buckets)
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "state.db"
)


# --- Classification constants ----------------------------------------------

# Every distinct strategy_group ever observed in settled_trades. All are LIVE money —
# settled_trades has no observation axis (the bot left OBSERVATION_MODE in Feb 2026).
# Source: SELECT DISTINCT strategy_group FROM settled_trades on prod state.db, plus
# memory/config_changes_apr.md for context.
#
# IMPORTANT: weather_no_live and hourly_no_live are LIVE despite the historical naming
# (the "_no_live" suffix dates from when the side was being staged; both shipped real
# money — weather NO since 2026-04-11, hourly NO until kill on 2026-04-18).
KNOWN_STRATEGY_GROUPS = frozenset({
    "main",
    "decided",
    "weekend_discount",
    "overnight_discount",
    "low_price_near_expiry",
    "terminal_momentum",
    "terminal_momentum_95",
    "terminal_momentum_96",
    "terminal_momentum_97",
    "terminal_momentum_98",
    "terminal_momentum_99",
    "weather_no_live",
    "hourly_no_live",
})

# Live but economically distinct from Kelly/risk-fraction-sized strategies. Excluded
# from the headline live PnL number so templates can present a Kelly-comparable figure.
# - weather_no_live: 1ct verification mode (real money, but not Kelly-sized)
# - hourly_no_live:  kill-switched 2026-04-18; historical trades preserved
LIVE_BUT_NON_HEADLINE_GROUPS = frozenset({"weather_no_live", "hourly_no_live"})

# filter_stage values that mean "this opportunity would have entered live trading".
# Used to compute total_live_candidates (vs the legacy total_trades which only counted
# the canonical 'candidate' stage and undercounted by ~6,800 rows on real data).
#
# IMPORTANT: decided_contract_t2_z2 is INTENTIONALLY SHADOWED per
# memory/project_t2_z2_apr22_rejection.md (Apr 22 promotion rejected after -$313/47-trade
# review). Do NOT add it back here without revisiting that decision.
LIVE_CANDIDATE_STAGES = frozenset({
    "candidate",
    "decided_contract_t1",
    "decided_contract_t1b",
    "decided_contract_t2",
    "decided_contract_t2_z25",
    # NOT decided_contract_t2_z2 — intentionally shadowed
    "weekend_discount",
    "overnight_discount",
    "terminal_momentum",
    "low_price_near_expiry",
    # NOT observation_trade — OBSERVATION_MODE rows that did NOT actually trade
})

# Regime cutoffs — pinned to actual deploy commit timestamps (UTC).
# Loss-burst cooldown + weather NO live: 48a7f5a shipped 2026-04-11T11:24:45Z but had
# critical bugs; fix 4075655 lands 2026-04-11T20:43:27Z and is the effective regime start.
REGIME_APR11 = "2026-04-11T20:43:27Z"
# WS schema fix (yes_dollars_fp / delta_fp): 0ddcaf8 at 2026-04-23T23:46:07Z.
REGIME_APR23 = "2026-04-23T23:46:07Z"


# --- DB connection ---------------------------------------------------------

def _connect(db_path):
    """Open state.db with WAL + busy_timeout per scripts/CLAUDE.md."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# --- Helpers ----------------------------------------------------------------

def _classify_settled(rows):
    """Return aggregate live PnL stats + list of any unknown strategy_groups encountered.

    Buckets pnl into wins (>0), losses (<0), breakevens (==0). Headline excludes
    LIVE_BUT_NON_HEADLINE_GROUPS for use in the Kelly-comparable summary number.
    """
    pnl = wins = losses = breakevens = 0
    headline_pnl = 0
    n = headline_n = 0
    unknown = set()
    for r in rows:
        sg = r["strategy_group"] if "strategy_group" in r.keys() else None
        sg = sg or "main"  # defensive — schema has NOT NULL DEFAULT 'main'
        if sg not in KNOWN_STRATEGY_GROUPS:
            unknown.add(sg)
        p = r["pnl_cents"] or 0
        pnl += p
        n += 1
        if p > 0:
            wins += 1
        elif p < 0:
            losses += 1
        else:
            breakevens += 1
        if sg not in LIVE_BUT_NON_HEADLINE_GROUPS:
            headline_pnl += p
            headline_n += 1
    return {
        "live_pnl_cents": pnl,
        "live_settled": n,
        "live_wins": wins,
        "live_losses": losses,
        "live_breakevens": breakevens,
        "live_pnl_headline_cents": headline_pnl,
        "live_settled_headline": headline_n,
        "unknown_strategy_groups": sorted(unknown),
    }


def _brier_side_aware(rows):
    """Side-aware Brier from evaluated_opportunities rows.

    raw_prob in evaluated_opportunities is the model's P(YES). For a YES-side trade the
    model's probability of OUR bet winning IS raw_prob; for a NO-side trade it's 1-raw_prob.
    Outcome is 1 if market_result == side (we won), else 0.
    """
    n = 0
    total = 0.0
    for r in rows:
        keys = r.keys()
        rp = r["raw_prob"] if "raw_prob" in keys else None
        mr = r["market_result"] if "market_result" in keys else None
        side = (r["side"] if "side" in keys else None) or "yes"
        if rp is None or mr is None:
            continue
        p_win = rp if side == "yes" else (1.0 - rp)
        outcome = 1.0 if mr == side else 0.0
        total += (p_win - outcome) ** 2
        n += 1
    if n == 0:
        return None
    return total / n


def _settled_by_strategy_group(conn, since=None):
    sql = (
        "SELECT strategy_group, COUNT(*) AS n, "
        "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) AS wins, "
        "COALESCE(SUM(pnl_cents), 0) AS pnl_cents, "
        "AVG(entry_price_cents) AS mean_entry_price_cents "
        "FROM settled_trades"
    )
    params = ()
    if since:
        sql += " WHERE settled_at >= ?"
        params = (since,)
    sql += " GROUP BY strategy_group"
    rows = conn.execute(sql, params).fetchall()
    return {
        (r["strategy_group"] or "main"): {
            "n": r["n"],
            "wins": r["wins"] or 0,
            "pnl_cents": r["pnl_cents"] or 0,
            "mean_entry_price_cents": (
                round(r["mean_entry_price_cents"], 2)
                if r["mean_entry_price_cents"] is not None else None
            ),
        }
        for r in rows
    }


def _filter_breakdown_by_product(conn):
    rows = conn.execute(
        "SELECT product_type, filter_stage, COUNT(*) AS n "
        "FROM evaluated_opportunities WHERE product_type IS NOT NULL "
        "GROUP BY product_type, filter_stage"
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["product_type"], {})[r["filter_stage"]] = r["n"]
    return out


def _shadow_counterfactual_by_stage(conn):
    """Per-shadow-stage hypothetical PnL from evaluated_opportunities.counterfactual_pnl.

    Includes any filter_stage that has at least one row with non-null counterfactual_pnl
    AND is not a live-candidate stage. This is the real "observation P&L" — what these
    shadow strategies WOULD have made if promoted to live.
    """
    rows = conn.execute(
        "SELECT filter_stage, COUNT(*) AS n, "
        "COALESCE(SUM(counterfactual_pnl), 0) AS pnl_cents, "
        "AVG(counterfactual_pnl) AS mean_pnl_cents "
        "FROM evaluated_opportunities "
        "WHERE counterfactual_pnl IS NOT NULL "
        "GROUP BY filter_stage"
    ).fetchall()
    out = {}
    for r in rows:
        stage = r["filter_stage"]
        if stage in LIVE_CANDIDATE_STAGES:
            # Live candidate — counterfactual_pnl here is the ACTUAL pnl, already in settled_trades
            continue
        out[stage] = {
            "n": r["n"],
            "pnl_cents": r["pnl_cents"] or 0,
            "mean_pnl_cents": (
                round(r["mean_pnl_cents"], 2) if r["mean_pnl_cents"] is not None else None
            ),
        }
    return out


def _settled_rows(conn, since=None):
    """Projection used by _classify_settled. Note: settled_trades has NO raw_prob column."""
    sql = "SELECT strategy_group, product_type, pnl_cents FROM settled_trades"
    params = ()
    if since:
        sql += " WHERE settled_at >= ?"
        params = (since,)
    return conn.execute(sql, params).fetchall()


def _candidate_eo_rows(conn, since=None):
    """EO rows where Brier can be computed (raw_prob + market_result both filled).

    Restricts to live-candidate stages — these are the rows where the model's
    prediction was made and the outcome was observed. Includes BOTH filled and
    unfilled candidates (the model can be evaluated whether or not the bot got a fill).
    For filled-only Brier see _filled_eo_rows.
    """
    placeholders = ",".join("?" * len(LIVE_CANDIDATE_STAGES))
    sql = (
        f"SELECT raw_prob, market_result, side, product_type "
        f"FROM evaluated_opportunities "
        f"WHERE filter_stage IN ({placeholders}) "
        f"AND raw_prob IS NOT NULL AND market_result IS NOT NULL"
    )
    params = list(LIVE_CANDIDATE_STAGES)
    if since:
        sql += " AND evaluation_time >= ?"
        params.append(since)
    return conn.execute(sql, params).fetchall()


def _filled_eo_rows(conn, since=None):
    """EO rows for tickers that produced settled_trades — Brier of the bot's paid decisions.

    Three layers of dedup:
      1. settled_trades has compound PK (ticker, strategy_group); a ticker can have
         multiple settled rows when stacked (main + decided + addon). Dedup to one
         per ticker via DISTINCT.
      2. evaluated_opportunities has many rows per ticker (re-evals at 1s scan cadence).
         Pick the row with MAX(evaluation_time).
      3. If two EO rows share the exact same evaluation_time (1s scan + multiple stages
         firing in the same scan), tiebreak on MAX(id) — id is the autoincrement PK so
         later rows win.
    """
    sorted_stages = sorted(LIVE_CANDIDATE_STAGES)
    placeholders = ",".join("?" * len(sorted_stages))
    settled_filter = "WHERE st.settled_at >= ?" if since else ""
    sql = f"""
        WITH settled_tickers AS (
            SELECT DISTINCT st.ticker
            FROM settled_trades st
            {settled_filter}
        ),
        ranked_eo AS (
            SELECT ticker, raw_prob, market_result, side, product_type, evaluation_time, id
            FROM evaluated_opportunities
            WHERE filter_stage IN ({placeholders})
              AND raw_prob IS NOT NULL AND market_result IS NOT NULL
        ),
        winners AS (
            SELECT ticker, MAX(evaluation_time) AS latest_t,
                   MAX(id) AS tiebreak_id
            FROM ranked_eo
            GROUP BY ticker
        )
        SELECT eo.raw_prob, eo.market_result, eo.side, eo.product_type
        FROM settled_tickers s
        JOIN winners w ON w.ticker = s.ticker
        JOIN ranked_eo eo
          ON eo.ticker = w.ticker
          AND eo.evaluation_time = w.latest_t
          AND eo.id = (
              SELECT MAX(id) FROM ranked_eo r2
              WHERE r2.ticker = w.ticker AND r2.evaluation_time = w.latest_t
          )
    """
    params = []
    if since:
        params.append(since)
    params.extend(sorted_stages)
    return conn.execute(sql, params).fetchall()


def _count_settled_without_matching_eo(conn):
    """How many distinct settled tickers lack a matching EO row with raw_prob+market_result.

    On prod (2026-04-26): 164 of 2,908 settled tickers (5.6%) lack a matching EO,
    typically because they pre-date the EO logging era or were settled out-of-band.
    Reported in JSON so brier_filled.n can be compared against the true filled count.
    """
    sorted_stages = sorted(LIVE_CANDIDATE_STAGES)
    placeholders = ",".join("?" * len(sorted_stages))
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM (
            SELECT DISTINCT st.ticker FROM settled_trades st
            WHERE NOT EXISTS (
                SELECT 1 FROM evaluated_opportunities eo
                WHERE eo.ticker = st.ticker
                  AND eo.filter_stage IN ({placeholders})
                  AND eo.raw_prob IS NOT NULL
                  AND eo.market_result IS NOT NULL
            )
        )
        """,
        sorted_stages,
    ).fetchone()
    return row["n"] if row else 0


def _regime_block(conn, since):
    """Compact stats block for a regime-filtered window."""
    rows = _settled_rows(conn, since=since)
    cls = _classify_settled(rows)
    eo_rows = _candidate_eo_rows(conn, since=since)
    cls["brier_overall"] = _brier_side_aware(eo_rows)
    return cls


# --- Main -------------------------------------------------------------------

def main():
    if not os.path.exists(DB_PATH):
        print(json.dumps({"error": f"state.db not found at {DB_PATH}"}))
        sys.exit(1)

    conn = _connect(DB_PATH)
    stats = {}

    # --- Legacy fields kept for backwards-compat ---------------------------
    row = conn.execute("SELECT COUNT(*) AS n FROM evaluated_opportunities").fetchone()
    stats["total_evaluated"] = row["n"] if row else 0

    rows = conn.execute(
        "SELECT filter_stage, COUNT(*) AS n FROM evaluated_opportunities GROUP BY filter_stage"
    ).fetchall()
    stats["filter_breakdown"] = {r["filter_stage"]: r["n"] for r in rows}

    row = conn.execute("SELECT COUNT(*) AS n FROM settled_trades").fetchone()
    stats["total_settled"] = row["n"] if row else 0

    row = conn.execute("SELECT COUNT(*) AS n FROM settled_trades WHERE pnl_cents > 0").fetchone()
    stats["total_wins"] = row["n"] if row else 0

    row = conn.execute(
        "SELECT COUNT(*) AS n FROM evaluated_opportunities WHERE filter_stage = 'candidate'"
    ).fetchone()
    stats["total_trades"] = row["n"] if row else 0

    buckets = {"80-84": (80, 84), "85-89": (85, 89), "90-94": (90, 94), "95-99": (95, 99)}
    win_rate_by_price = {}
    for label, (lo, hi) in buckets.items():
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) AS wins "
            "FROM settled_trades WHERE entry_price_cents >= ? AND entry_price_cents <= ?",
            (lo, hi),
        ).fetchone()
        win_rate_by_price[label] = {
            "n": row["n"] if row else 0,
            "wins": row["wins"] if row and row["wins"] else 0,
        }
    stats["win_rate_by_price"] = win_rate_by_price

    rows = conn.execute(
        "SELECT DISTINCT asset FROM evaluated_opportunities WHERE asset IS NOT NULL"
    ).fetchall()
    stats["assets_tracked"] = sorted([r["asset"] for r in rows]) if rows else ["BTC", "ETH", "SOL", "XRP"]

    row = conn.execute(
        "SELECT MIN(evaluation_time) AS first, MAX(evaluation_time) AS last "
        "FROM evaluated_opportunities"
    ).fetchone()
    if row and row["first"] and row["last"]:
        stats["observation_period"] = f"{row['first'][:10]} to {row['last'][:10]}"
    else:
        stats["observation_period"] = "N/A"

    # --- Corrected core fields ---------------------------------------------

    all_settled = _settled_rows(conn)
    cls = _classify_settled(all_settled)
    stats.update(cls)

    # Loud warning if any unknown strategy_group landed (silent miscategorization is
    # exactly the failure mode that produced the original 2026-04-26 doc lie).
    if stats["unknown_strategy_groups"]:
        print(
            f"WARN: unknown strategy_groups in settled_trades: "
            f"{stats['unknown_strategy_groups']}. "
            f"Add them to KNOWN_STRATEGY_GROUPS in {os.path.basename(__file__)}.",
            file=sys.stderr,
        )

    # Backwards-compat: observation_pnl was the historical lie. Field equals live_pnl_cents now.
    stats["observation_pnl"] = stats["live_pnl_cents"]

    stats["filter_breakdown_by_product"] = _filter_breakdown_by_product(conn)

    placeholders = ",".join("?" * len(LIVE_CANDIDATE_STAGES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM evaluated_opportunities WHERE filter_stage IN ({placeholders})",
        tuple(LIVE_CANDIDATE_STAGES),
    ).fetchone()
    stats["total_live_candidates"] = row["n"] if row else 0

    stats["settled_by_strategy_group"] = _settled_by_strategy_group(conn)

    stats["shadow_counterfactual_pnl_by_stage"] = _shadow_counterfactual_by_stage(conn)

    # Brier from EO rows (settled_trades doesn't have raw_prob)
    eo_rows = _candidate_eo_rows(conn)
    overall_brier = _brier_side_aware(eo_rows)
    by_product = {}
    rows = conn.execute(
        f"SELECT DISTINCT product_type FROM evaluated_opportunities "
        f"WHERE product_type IS NOT NULL AND filter_stage IN ({placeholders}) "
        f"AND raw_prob IS NOT NULL AND market_result IS NOT NULL",
        tuple(LIVE_CANDIDATE_STAGES),
    ).fetchall()
    for r in rows:
        pt = r["product_type"]
        pt_rows = conn.execute(
            f"SELECT raw_prob, market_result, side FROM evaluated_opportunities "
            f"WHERE product_type = ? AND filter_stage IN ({placeholders}) "
            f"AND raw_prob IS NOT NULL AND market_result IS NOT NULL",
            (pt,) + tuple(LIVE_CANDIDATE_STAGES),
        ).fetchall()
        b = _brier_side_aware(pt_rows)
        if b is not None:
            by_product[pt] = round(b, 4)
    stats["brier"] = {
        "overall": round(overall_brier, 4) if overall_brier is not None else None,
        "by_product": by_product,
    }

    # Brier of FILLED trades (bot's actual paid decisions) — JOIN settled_trades with
    # the latest matching EO row per ticker. Different from `brier` above which covers
    # all live candidates (filled + unfilled).
    filled_eo = _filled_eo_rows(conn)
    filled_brier = _brier_side_aware(filled_eo)
    filled_by_product = {}
    for r in conn.execute(
        f"SELECT DISTINCT product_type FROM evaluated_opportunities "
        f"WHERE product_type IS NOT NULL AND filter_stage IN ({placeholders})",
        tuple(LIVE_CANDIDATE_STAGES),
    ).fetchall():
        pt = r["product_type"]
        pt_filled = [row for row in filled_eo if row["product_type"] == pt]
        b = _brier_side_aware(pt_filled)
        if b is not None:
            filled_by_product[pt] = round(b, 4)
    stats["brier_filled"] = {
        "overall": round(filled_brier, 4) if filled_brier is not None else None,
        "by_product": filled_by_product,
        "n": len(filled_eo),
    }
    stats["settled_without_matching_eo"] = _count_settled_without_matching_eo(conn)

    stats["since_apr11"] = _regime_block(conn, REGIME_APR11)
    stats["since_apr23"] = _regime_block(conn, REGIME_APR23)

    stats["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats["_classifier_version"] = "v3-2026-04-26"

    conn.close()

    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
