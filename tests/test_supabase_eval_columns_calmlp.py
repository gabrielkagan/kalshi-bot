"""Supabase _EVAL_COLUMNS must include all cal_mlp_* fields.

Failure mode (May 1 2026): bot.py was fixed so the candidate / observation_trade /
bleed-cell rows now carry cal_mlp_request_id (commit f7d2f47), enabling the
post-hoc processor to annotate every 15M trade. But supabase_sync._EVAL_COLUMNS
is an explicit whitelist that does NOT include cal_mlp_*, so the dashboard
mirror to Postgres silently drops these annotations — operators can't see them
in the dashboard / public site even though the local DB is 100% annotated.

This test pins the column list to the cal_mlp_* schema, so:
  - if someone adds a new cal_mlp_* field to bot.py without extending the list,
    sync silently drops the new field (caught here),
  - if someone removes one of the 7 fields from the list, dashboard observability
    breaks (caught here).

Pre-flight: the remote `evaluations` table MUST have these columns ALREADY
(applied via supabase migration `add_cal_mlp_columns_to_evaluations` on
2026-05-01). Adding to _EVAL_COLUMNS without the remote columns existing
causes silent HTTP 400s — the failure mode that lost weeks of evaluation
data on 2026-04-04 (see supabase_sync.py:_check_schema_parity docstring).
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


CAL_MLP_FIELDS = {
    "cal_mlp_request_id",
    "cal_mlp_skipped_reason",
    "cal_mlp_p_mean",
    "cal_mlp_p_std",
    "cal_mlp_final_lo",
    "cal_mlp_final_hi",
    "cal_mlp_train_id",
}


def _eval_columns_set() -> set[str]:
    """Parse SupabaseSyncer._EVAL_COLUMNS into a set of bare column names."""
    from supabase_sync import SupabaseSyncer
    raw = SupabaseSyncer._EVAL_COLUMNS
    return {c.strip() for c in raw.split(",") if c.strip()}


def test_eval_columns_includes_all_cal_mlp_fields():
    cols = _eval_columns_set()
    missing = CAL_MLP_FIELDS - cols
    assert not missing, (
        "supabase_sync._EVAL_COLUMNS is missing these cal_mlp_* fields:\n"
        + "\n".join(f"  - {c}" for c in sorted(missing))
        + "\n\nLocal DB has them after commit f7d2f47, but the dashboard mirror "
          "won't pick them up. Add to the comma-separated list at "
          "supabase_sync.py:_EVAL_COLUMNS. The remote evaluations table already "
          "has these columns (migration `add_cal_mlp_columns_to_evaluations`)."
    )


def test_eval_columns_no_duplicates():
    """Defensive: if someone pastes the same field twice in the long string,
    SQL still works but it's a smell. Lock against it."""
    from supabase_sync import SupabaseSyncer
    raw = SupabaseSyncer._EVAL_COLUMNS
    cols = [c.strip() for c in raw.split(",") if c.strip()]
    dups = sorted({c for c in cols if cols.count(c) > 1})
    assert not dups, f"_EVAL_COLUMNS has duplicate column(s): {dups}"


def test_eval_columns_subset_of_local_evaluated_opportunities_schema(tmp_path):
    """Every column in _EVAL_COLUMNS must exist in the local sqlite
    evaluated_opportunities schema, otherwise the SELECT will raise
    OperationalError at runtime and freeze sync. Uses a fresh StateManager
    DB so the schema reflects the current bot.py."""
    import bot
    db_path = str(tmp_path / "state.db")
    state = bot.StateManager(db_path)
    local_cols = {
        r[1] for r in state.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
    }
    eval_cols = _eval_columns_set()
    not_in_local = eval_cols - local_cols
    assert not not_in_local, (
        "_EVAL_COLUMNS references columns that do not exist in the local "
        "sqlite schema — sync will OperationalError at runtime:\n"
        + "\n".join(f"  - {c}" for c in sorted(not_in_local))
    )
