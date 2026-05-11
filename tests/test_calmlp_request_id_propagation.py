"""cal_mlp request_id propagation — every-trade annotation guarantee.

Bug (May 1 2026): trade-endpoint inserts (filter_stage='candidate',
'observation_trade', and the bleed-cell blocks) used explicit named kwargs
that omitted cal_mlp_request_id. The id was generated at scan-tick into
_shadow_diag but dropped at insert time, so the post-hoc processor's
`WHERE cal_mlp_request_id IS NOT NULL` query never matched the rows that
became real trades. Result: 0% annotation on candidate (n=227 in 41h),
0% on TM98/SOL_TAKER bleed-cell blocks (n=18).

These tests lock in the propagation contract so every trade-endpoint
INSERT must either:
  (a) splat **_shadow_diag (which carries cal_mlp_request_id), or
  (b) pass cal_mlp_request_id= explicitly as a kwarg.

Stages BEFORE the cal_mlp annotate hook (price_out_of_range,
floor_raise_shadow, *_price_shadow*, silent_*) cannot be annotated by
design — _shadow_diag has no request_id at those rejection points. They
are pre-trade rejections, not trades, so they are out of scope.
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys

import pytest
import bot.state  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# Stages that represent a real trade entering the book OR a candidate that
# was selected and then strategy-blocked (still a "trade" from the
# calibrator's perspective — it had a valid raw_prob and would have been
# placed). Adding a new trade-endpoint stage? Add it here so the
# propagation invariant is enforced.
TRADE_ENDPOINT_STAGES = {
    "candidate",
    "observation_trade",
    "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_85_89C_2_5MIN_BLEED",
    "SOL_BLEED_V2_88_93C_2_5MIN",
    "96C_SOL_XRP_STC_DANGER_BAND",  # HPSB — original 96¢ bleed gate
}

CAL_MLP_FIELDS = {
    "cal_mlp_request_id",
    "cal_mlp_skipped_reason",
    "cal_mlp_p_mean",
    "cal_mlp_p_std",
    "cal_mlp_final_lo",
    "cal_mlp_final_hi",
    "cal_mlp_train_id",
}


def _load_bot_ast():
    # Bit 9.1 (2026-05-10): include bot/executor.py for OrderExecutor content

    bot_path = os.path.join(PROJECT_ROOT, "bot/_impl.py")

    executor_path = os.path.join(PROJECT_ROOT, "bot/executor.py")
    sources = []
    if os.path.exists(bot_path):
        with open(bot_path) as f:
            sources.append(f.read())
    if os.path.isfile(executor_path):
        with open(executor_path) as f:
            sources.append(f.read())
    return ast.parse("\n".join(sources), filename="bot/_impl.py+executor.py")


def _stage_value(call: ast.Call) -> str | None:
    """Return the literal filter_stage value at this insert_evaluated_opportunity
    call, or None if it's a non-literal (variable) we can't resolve.

    Signature: insert_evaluated_opportunity(ticker, event_ticker, asset, filter_stage, ...)
    filter_stage is the 4th positional or kwarg `filter_stage=`.
    """
    # Positional 4th arg (index 3)
    if len(call.args) >= 4:
        node = call.args[3]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        # Allow Name resolution for the bleed-cell constants by name match
        if isinstance(node, ast.Name):
            return node.id
    # Or keyword
    for kw in call.keywords:
        if kw.arg == "filter_stage":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            if isinstance(kw.value, ast.Name):
                return kw.value.id
    return None


# Map known constant NAMES (used positionally) to their literal stage strings.
# Keep in sync with bot/_impl.py.
CONSTANT_NAME_TO_STAGE = {
    "TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE": "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE": "SOL_TAKER_85_89C_2_5MIN_BLEED",
    "SOL_BLEED_V2_BLOCK_FILTER_STAGE": "SOL_BLEED_V2_88_93C_2_5MIN",
    "HIGH_PRICE_STC_BLOCK_FILTER_STAGE": "96C_SOL_XRP_STC_DANGER_BAND",
    "_bleed_stage": "__BLEED_LOOP__",  # dynamic loop-bound; treated as endpoint
}


def _normalize_stage(raw: str | None) -> str | None:
    if raw is None:
        return None
    if raw in CONSTANT_NAME_TO_STAGE:
        return CONSTANT_NAME_TO_STAGE[raw]
    return raw


def _propagates_request_id(call: ast.Call) -> bool:
    """True if this call passes cal_mlp_request_id either explicitly or via
    a **_shadow_diag splat."""
    for kw in call.keywords:
        # Explicit kwarg
        if kw.arg == "cal_mlp_request_id":
            return True
        # Splat: kw.arg is None for **mapping
        if kw.arg is None and isinstance(kw.value, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)):
            # Match common patterns: **_shadow_diag, **candidate.get("_shadow_diag", {}),
            # **item.get("_shadow_diag", {})
            src = ast.unparse(kw.value)
            if "_shadow_diag" in src:
                return True
    return False


def _is_insert_eval_opp(call: ast.Call) -> bool:
    """Match `*.insert_evaluated_opportunity(...)` regardless of receiver."""
    fn = call.func
    if isinstance(fn, ast.Attribute) and fn.attr == "insert_evaluated_opportunity":
        return True
    return False


def _collect_endpoint_inserts():
    """Yield (lineno, stage) for each insert_evaluated_opportunity call whose
    filter_stage is in TRADE_ENDPOINT_STAGES. Includes the dynamic bleed
    loop, which writes whichever bleed_stage the loop iteration carries."""
    tree = _load_bot_ast()
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_insert_eval_opp(node):
            raw_stage = _stage_value(node)
            stage = _normalize_stage(raw_stage)
            if stage in TRADE_ENDPOINT_STAGES or stage == "__BLEED_LOOP__":
                sites.append((node.lineno, stage, node))
    return sites


# ---------------------------------------------------------------------------
# AST: every trade-endpoint insert MUST propagate cal_mlp_request_id
# ---------------------------------------------------------------------------

def test_every_trade_endpoint_insert_propagates_cal_mlp_request_id():
    sites = _collect_endpoint_inserts()
    assert sites, (
        "No trade-endpoint insert_evaluated_opportunity sites found in bot/_impl.py — "
        "the AST scanner is broken or all sites were renamed"
    )
    missing = []
    for lineno, stage, call in sites:
        if not _propagates_request_id(call):
            missing.append((lineno, stage))
    assert not missing, (
        "These trade-endpoint insert_evaluated_opportunity sites drop "
        "cal_mlp_request_id (post-hoc processor will never annotate the rows):\n"
        + "\n".join(f"  bot/_impl.py:{ln}  filter_stage={st}" for ln, st in missing)
        + "\n\nFix: add `cal_mlp_request_id=<source>.get('cal_mlp_request_id'),` "
          "(plus the other cal_mlp_* fields) or splat **_shadow_diag in the call."
    )


def test_endpoint_stage_set_matches_known_stages():
    """Sanity: if someone adds a new trade-endpoint stage in bot/_impl.py without
    extending TRADE_ENDPOINT_STAGES, the propagation guarantee is silently
    lost. This isn't fully detectable from AST alone; we lock the constant
    names referenced here and expect the human to add new entries when
    introducing new endpoint stages.

    If this test fails because the bleed-cell constants were renamed,
    update CONSTANT_NAME_TO_STAGE + TRADE_ENDPOINT_STAGES together."""
    # Bit 3.1: FILTER_STAGE definitions live in bot/constants.py post-move.
    # The endpoint INSERT sites that USE these constants stay in bot/_impl.py
    # (other tests in this file scan _impl.py for those usages).
    bot_path = os.path.join(PROJECT_ROOT, "bot/constants.py")
    src = ""
    if os.path.exists(bot_path):
        with open(bot_path) as f:
            src = f.read()
    assert 'TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE = "TM98_97_98C_2_5MIN_BLEED"' in src
    assert 'SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE = "SOL_TAKER_85_89C_2_5MIN_BLEED"' in src
    assert 'SOL_BLEED_V2_BLOCK_FILTER_STAGE = "SOL_BLEED_V2_88_93C_2_5MIN"' in src
    assert 'HIGH_PRICE_STC_BLOCK_FILTER_STAGE = "96C_SOL_XRP_STC_DANGER_BAND"' in src


# ---------------------------------------------------------------------------
# Integration: real StateManager round-trip — cal_mlp_request_id survives
# ---------------------------------------------------------------------------

def test_candidate_insert_persists_cal_mlp_request_id(tmp_path):
    """End-to-end: the actual signature accepts cal_mlp_request_id and the
    INSERT stores it. This catches regressions where the signature is fine
    but a column rename / SQL omission breaks persistence."""
    # Import lazily so module-level CALMLP env / threading guards don't fire
    # for unrelated tests.
    import bot
    import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)

    db_path = str(tmp_path / "state.db")
    state = bot.state.StateManager(db_path)

    state.insert_evaluated_opportunity(
        "KXBTC15M-TEST-00", "EVT-TEST", "BTC",
        "candidate",
        spot_price=70000.0, threshold=70100.0, volatility=0.5,
        market_price=98, seconds_to_close=120.0,
        calibrated_prob=0.96, edge=0.02,
        raw_prob=0.95,
        product_type="15m",
        cal_mlp_request_id="test-uuid-abc123",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT cal_mlp_request_id, filter_stage FROM evaluated_opportunities "
        "WHERE ticker='KXBTC15M-TEST-00'"
    ).fetchone()
    conn.close()
    assert row is not None, "candidate row was not inserted"
    assert row["filter_stage"] == "candidate"
    assert row["cal_mlp_request_id"] == "test-uuid-abc123", (
        "cal_mlp_request_id was dropped by insert_evaluated_opportunity"
    )


def test_candidate_insert_persists_cal_mlp_skipped_reason(tmp_path):
    """When cal_mlp annotation skips (env=0 / raw_prob=None / no_predictor),
    _shadow_diag carries cal_mlp_skipped_reason but no request_id. The skip
    reason MUST also persist on the trade row, otherwise the post-hoc
    processor sees a row with NULL request_id AND NULL skip — looks
    permanently unannotated."""
    import bot
    import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)

    db_path = str(tmp_path / "state.db")
    state = bot.state.StateManager(db_path)

    state.insert_evaluated_opportunity(
        "KXSOL15M-TEST-00", "EVT-TEST-2", "SOL",
        "candidate",
        spot_price=160.0, threshold=160.5, volatility=0.4,
        market_price=89, seconds_to_close=200.0,
        calibrated_prob=0.93, edge=0.04,
        raw_prob=None,  # would have produced raw_prob_null skip
        product_type="15m",
        cal_mlp_skipped_reason="raw_prob_null",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT cal_mlp_skipped_reason FROM evaluated_opportunities "
        "WHERE ticker='KXSOL15M-TEST-00'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["cal_mlp_skipped_reason"] == "raw_prob_null"
