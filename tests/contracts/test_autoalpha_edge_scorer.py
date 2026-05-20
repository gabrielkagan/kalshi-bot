"""Autoalpha Phase 1 — edge scorer contract pins (ticket TBD, 2026-05-19).

Pins the promote / demote gate thresholds, the cell-key shape, the report
output format, and the cron-convention exit code. Drift here would silently
shift the autoalpha's recommendations without the operator noticing.

Plan doc: kb/decisions/autoalpha-phase1-scorer-plan.md
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _import_scorer():
    """Module is at scripts/audit/autoalpha_edge_scorer.py - not a package.
    Add scripts/audit to sys.path on demand."""
    repo = Path(__file__).resolve().parents[2]
    audit_dir = repo / "scripts" / "audit"
    if str(audit_dir) not in sys.path:
        sys.path.insert(0, str(audit_dir))
    return importlib.import_module("autoalpha_edge_scorer")


def _make_cell(mod, **overrides):
    """Default-fill a CellMetrics for test setup. Defaults to a passing-promote
    shape at band 18 (90-94c, fee-aware breakeven ~0.95)."""
    defaults = dict(
        asset="BTC", product_type="15m", strategy="MAKER_AGGRESSIVE",
        price_band_5c=18, stc_band_60s=0, cell_block_stage="candidate",
        n_30d=100, n_7d=20,
        wr_30d=0.98, wilson95_lo_30d=0.96,
        cf_pnl_30d_dollars=50.0, cf_pnl_7d_dollars=10.0,
    )
    defaults.update(overrides)
    return mod.CellMetrics(**defaults)


def test_promote_gate_n_30d_threshold():
    """n_30d=49 doesn't promote; n_30d=50 does (when other gates pass)."""
    mod = _import_scorer()
    # Tune so other gates pass
    cell_just_under = _make_cell(mod, n_30d=49, cf_pnl_30d_dollars=49 * 0.30)
    cell_just_at = _make_cell(mod, n_30d=50, cf_pnl_30d_dollars=50 * 0.30)
    assert mod.is_promote_candidate(cell_just_under) is False
    assert mod.is_promote_candidate(cell_just_at) is True


def test_promote_gate_per_trade_threshold():
    """net_per_trade_30d=$0.24 doesn't promote; $0.25 does."""
    mod = _import_scorer()
    cell_just_under = _make_cell(mod, n_30d=100, cf_pnl_30d_dollars=24.0)
    cell_just_at = _make_cell(mod, n_30d=100, cf_pnl_30d_dollars=25.0)
    assert mod.is_promote_candidate(cell_just_under) is False
    assert mod.is_promote_candidate(cell_just_at) is True


def test_promote_gate_wilson_lo_band_aware_fee_adjusted():
    """At band=14 (70-74c), fee-aware breakeven is 0.76 (74c upper + 2c fee).
    wilson_lo=0.75 doesn't promote; 0.77 does. R1-M3 fix: the original
    fee-naive threshold of 0.70 was 6pp too low for band 14."""
    mod = _import_scorer()
    base_kwargs = dict(price_band_5c=14, n_30d=100, cf_pnl_30d_dollars=50.0)
    cell_under = _make_cell(mod, wilson95_lo_30d=0.75, **base_kwargs)
    cell_over = _make_cell(mod, wilson95_lo_30d=0.77, **base_kwargs)
    assert mod.is_promote_candidate(cell_under) is False
    assert mod.is_promote_candidate(cell_over) is True


def test_promote_gate_band_19_essentially_unreachable():
    """At band=19 (95-99c upper=99), fee = 1c → breakeven 1.00. Even WR=1.0
    is not > 1.0 (strict gate), so band 19 promote candidates are
    intentionally impossible. R1-M3: 95-99c is the asymmetry trap; even
    100% WR barely breaks even after fees."""
    mod = _import_scorer()
    base_kwargs = dict(price_band_5c=19, n_30d=100, cf_pnl_30d_dollars=50.0)
    cell_perfect = _make_cell(mod, wilson95_lo_30d=1.0, **base_kwargs)
    assert mod.is_promote_candidate(cell_perfect) is False


def test_promote_gate_unknown_band_blocked():
    """Cells in unknown price bands (outside 14-19) must NOT promote — defends
    against silent acceptance of edge in unmapped tiers."""
    mod = _import_scorer()
    cell_unknown = _make_cell(mod, price_band_5c=20, cf_pnl_30d_dollars=100.0)
    assert mod.is_promote_candidate(cell_unknown) is False
    cell_unknown_low = _make_cell(
        mod, price_band_5c=10, cf_pnl_30d_dollars=100.0)
    assert mod.is_promote_candidate(cell_unknown_low) is False


def test_demote_gate_per_trade_threshold():
    """live cell with $-0.20/trade 7d exactly does NOT demote (strict <);
    $-0.21 does. The strict-less-than is intentional: -$0.20 is the
    threshold value, not the demote line."""
    mod = _import_scorer()
    live = {"MAKER_AGGRESSIVE"}
    cell_at_threshold = _make_cell(mod, n_7d=20, cf_pnl_7d_dollars=20 * -0.20)
    cell_below = _make_cell(mod, n_7d=20, cf_pnl_7d_dollars=20 * -0.21)
    assert mod.is_demote_candidate(cell_at_threshold, live) is False
    assert mod.is_demote_candidate(cell_below, live) is True


def test_demote_gate_n_7d_minimum():
    """Live cell with n_7d=9 doesn't demote (sample too small) even at -1.00/trade."""
    mod = _import_scorer()
    live = {"MAKER_AGGRESSIVE"}
    cell_tiny = _make_cell(mod, n_7d=9, cf_pnl_7d_dollars=9 * -1.00)
    cell_ok = _make_cell(mod, n_7d=10, cf_pnl_7d_dollars=10 * -1.00)
    assert mod.is_demote_candidate(cell_tiny, live) is False
    assert mod.is_demote_candidate(cell_ok, live) is True


def test_demote_gate_unknown_strategy_blocked():
    """Strategy NOT in live_strategies allow-list must not demote — defends
    against demoting cells the bot isn't actually trading."""
    mod = _import_scorer()
    live = {"MAKER_AGGRESSIVE"}
    cell_off_list = _make_cell(
        mod, strategy="UNKNOWN_STRATEGY",
        n_7d=20, cf_pnl_7d_dollars=20 * -1.00)
    assert mod.is_demote_candidate(cell_off_list, live) is False


def test_main_exits_zero_on_empty_cohort(tmp_path):
    """No rows in cohort_attribution_daily -> exit 0, no crash, no report."""
    mod = _import_scorer()
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE cohort_attribution_daily (
            cohort_date TEXT, asset TEXT, product_type TEXT, strategy TEXT,
            price_band_5c INTEGER, stc_band_60s INTEGER,
            cell_block_stage TEXT, n_30d INTEGER, n_7d INTEGER,
            wr_30d REAL, wilson95_lo_30d REAL,
            cf_pnl_30d_dollars REAL, cf_pnl_7d_dollars REAL
        )""")
    # No rows inserted.
    notifier = MagicMock()
    rc = mod.main(conn=conn, report_dir=tmp_path,
                  live_strategies={"X"}, notifier=notifier)
    assert rc == 0
    notifier.send.assert_not_called()


def test_main_writes_report_with_top_n_sections(tmp_path):
    """End-to-end smoke: main() reads cells, writes the markdown report,
    sends Telegram summary with both promote and demote sections."""
    mod = _import_scorer()
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE cohort_attribution_daily (
            cohort_date TEXT, asset TEXT, product_type TEXT, strategy TEXT,
            price_band_5c INTEGER, stc_band_60s INTEGER,
            cell_block_stage TEXT, n_30d INTEGER, n_7d INTEGER,
            wr_30d REAL, wilson95_lo_30d REAL,
            cf_pnl_30d_dollars REAL, cf_pnl_7d_dollars REAL
        )""")
    # 1 strong promote candidate + 1 strong demote candidate.
    # Promote: BTC MAKER_AGGRESSIVE band 14 (70-74c) with wilson_lo=0.95 > fee-aware 0.76
    conn.execute("""INSERT INTO cohort_attribution_daily VALUES
        ('2026-05-19', 'BTC', '15m', 'MAKER_AGGRESSIVE', 14, 0, 'candidate',
         100, 20, 1.0, 0.95, 50.0, 10.0)""")
    # Demote: ETH MAKER_PATIENT bleeding 7d
    conn.execute("""INSERT INTO cohort_attribution_daily VALUES
        ('2026-05-19', 'ETH', '15m', 'MAKER_PATIENT', 18, 0, 'candidate',
         80, 20, 0.85, 0.80, -25.0, -10.0)""")
    conn.commit()

    notifier = MagicMock()
    rc = mod.main(conn=conn, report_dir=tmp_path,
                  live_strategies={"MAKER_AGGRESSIVE", "MAKER_PATIENT"},
                  notifier=notifier)
    assert rc == 0
    report_path = tmp_path / "autoalpha-recommendations-2026-05-19.md"
    assert report_path.is_file()
    content = report_path.read_text()
    assert "promote candidates" in content
    assert "demote candidates" in content
    # R1-M2 fix: cell_key uses `/` separator, not `|`
    assert "BTC/15m/MAKER_AGGRESSIVE" in content
    assert "ETH/15m/MAKER_PATIENT" in content
    notifier.send.assert_called_once()
    _, kwargs = notifier.send.call_args
    assert kwargs.get("dedup_key") == "autoalpha_recommendations_2026-05-19"


def test_cell_key_includes_product_type():
    """R1-M1: cell_key must include product_type so 15m + 1h rows on the
    same (asset, strategy, price, stc, stage) don't collapse."""
    mod = _import_scorer()
    cell_15m = _make_cell(mod, product_type="15m")
    cell_1h = _make_cell(mod, product_type="1h")
    assert cell_15m.cell_key != cell_1h.cell_key, (
        f"cell_keys must differ when product_type differs: "
        f"15m={cell_15m.cell_key!r}, 1h={cell_1h.cell_key!r}"
    )
    assert "15m" in cell_15m.cell_key
    assert "1h" in cell_1h.cell_key


def test_cell_key_uses_slash_separator_not_pipe():
    """R1-M2: cell_key must use `/` not `|`. `|` is the markdown table
    column delimiter — using it inside cell content breaks table rendering."""
    mod = _import_scorer()
    cell = _make_cell(mod)
    assert "|" not in cell.cell_key, (
        f"cell_key must not contain `|` (breaks markdown tables); "
        f"got {cell.cell_key!r}"
    )
    assert "/" in cell.cell_key


def test_promote_gate_excludes_bleed_stage_cells():
    """R1-M7: cells with cell_block_stage != 'candidate' are already
    cell-block-rejected by the bot. Promoting them is an operator-trap
    (sizing change has no effect because cell-block continues to reject
    the trade). Must be filtered from promote candidates."""
    mod = _import_scorer()
    cell_candidate = _make_cell(mod, cell_block_stage="candidate")
    cell_bleed = _make_cell(
        mod, cell_block_stage="96C_SOL_XRP_STC_DANGER_BAND")
    assert mod.is_promote_candidate(cell_candidate) is True
    assert mod.is_promote_candidate(cell_bleed) is False


def test_main_empty_live_strategies_disables_demote(tmp_path):
    """R1-M5: empty live_strategies (set or env var unset) must DISABLE
    demote-mode entirely, not fall back to 'every observed strategy'.
    Otherwise shadow-only strategies (XRP_15M_SHADOW, sports_shadow_log
    rows) get spammed as demote candidates."""
    mod = _import_scorer()
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE cohort_attribution_daily (
            cohort_date TEXT, asset TEXT, product_type TEXT, strategy TEXT,
            price_band_5c INTEGER, stc_band_60s INTEGER,
            cell_block_stage TEXT, n_30d INTEGER, n_7d INTEGER,
            wr_30d REAL, wilson95_lo_30d REAL,
            cf_pnl_30d_dollars REAL, cf_pnl_7d_dollars REAL
        )""")
    # 1 bleeding cell that WOULD demote if live_strategies were populated
    conn.execute("""INSERT INTO cohort_attribution_daily VALUES
        ('2026-05-19', 'BTC', '15m', 'MAKER_PATIENT', 18, 0, 'candidate',
         80, 20, 0.85, 0.80, -25.0, -10.0)""")
    conn.commit()
    notifier = MagicMock()
    rc = mod.main(conn=conn, report_dir=tmp_path,
                  live_strategies=set(),  # EMPTY — disables demote
                  notifier=notifier)
    assert rc == 0
    report = (tmp_path / "autoalpha-recommendations-2026-05-19.md").read_text()
    assert "(no live cells meet demote criteria today)" in report, (
        "With empty live_strategies, demote section must be empty (placeholder row). "
        "Pre-R1-M5 fix this would have listed the BTC MAKER_PATIENT cell as a demote candidate."
    )


def test_sys_path_bootstrap_at_module_top():
    """AST guard mirroring test_monitor_watchdog_runnable.py: bootstrap must
    exist + use parents[2] depth + precede ALL from-bot.* imports anywhere
    in the module (R1-N1 fix: also walks lazy imports inside FunctionDef
    bodies). Defends against the cron ModuleNotFoundError class from
    `feedback_monitor_the_monitor`."""
    import ast
    src = Path(__file__).resolve().parents[2] / "scripts" / "audit" / "autoalpha_edge_scorer.py"
    text = src.read_text()
    tree = ast.parse(text)
    has_bootstrap = False
    bootstrap_line = -1
    for node in tree.body:
        if isinstance(node, ast.If):
            for child in node.body:
                if (isinstance(child, ast.Expr)
                        and isinstance(child.value, ast.Call)
                        and isinstance(child.value.func, ast.Attribute)
                        and child.value.func.attr in ("insert", "append")):
                    has_bootstrap = True
                    bootstrap_line = child.lineno
    assert has_bootstrap, (
        "autoalpha_edge_scorer.py must include a `sys.path.insert(0, ...)` "
        "bootstrap. Without it, cron's invocation flow crashes at "
        "`from bot.notifier import TelegramNotifier`."
    )
    assert "parents[2]" in text, (
        "bootstrap must compute REPO_ROOT via Path(__file__).resolve().parents[2] "
        "for scripts/audit/<file>.py depth"
    )
    # R1-N1: assert bootstrap precedes every `from bot.*` import, including
    # lazy imports inside function bodies. A future edit that adds a
    # module-top `from bot.notifier import ...` ABOVE the bootstrap would
    # pass the bootstrap-exists check but crash at runtime.
    bot_import_linenos = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            if mod_name == "bot" or mod_name.startswith("bot."):
                bot_import_linenos.append(node.lineno)
    for ln in bot_import_linenos:
        assert ln > bootstrap_line, (
            f"`from bot.*` import at line {ln} must come AFTER the sys.path "
            f"bootstrap at line {bootstrap_line}. Lazy import order matters "
            f"because the bootstrap runs at module-load while the lazy "
            f"import runs at function-call — but if the lazy import line is "
            f"physically above the bootstrap, a future refactor that hoists "
            f"the import to module-top would silently break it."
        )
