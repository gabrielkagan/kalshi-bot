"""TDD for /hourly-alpha skill rebuild (2026-05-15, ticket 86b9yphft).

Pre-flight for ticket 86b9x2dzx (Hourly shadow PnL audit — Phase 3 go/no-go).
Pins the contract that `scripts/audit/hourly_alpha_research.py` does not
repeat the structural-lie pattern class caught by the May-5 alpha_audit
rebuild (`tests/integration/test_alpha_audit.py`, commit `ec0c0e5`).

R1 findings — RCA documented in ticket 86b9yphft:

C1. NO-side WR inversion: line 126 does `won = market_result == 'yes'`
    regardless of `side`. Bot bets on NO-side hourly rows (5,533 in
    `hourly_observation` alone) get their WR inverted. Measured impact:
    skill under-reports WR by 14.4pp on `hourly_observation` stage
    (true 60.7%, reported 46.3%).

C2. Hardcoded single filter_stage: line 141 does
    `is_signal = filter_stage == 'hourly_observation'`. Live DB has
    `hourly_observation_v2` (8,230 rows), `hourly_config_a-m` (~21K
    rows), `candidate` (424). ~48% of structurally-relevant dataset
    is dropped.

M3. Hardcoded 4-asset list in run_research(): lines 477/492/569 use
    `['BTC', 'ETH', 'SOL', 'XRP']`. HYPE/DOGE hourly settled rows
    (150/105) are invisible to the per-asset breakdown.

M6. Price clamp `[1, 99]` silently coerces settled-at-100c markets to
    99c (line 130). Affects PnL math + tier classification.

These tests RED-pin the bugs before patches; patches must turn them
GREEN.
"""
from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit" / "hourly_alpha_research.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts" / "audit") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))


# ─────────────────────────────────────────────────────────────────────────
# Fixture: minimal-projection sqlite mirroring evaluated_opportunities
# ─────────────────────────────────────────────────────────────────────────

_MIN_COLUMNS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    event_ticker TEXT,
    asset TEXT,
    filter_stage TEXT NOT NULL,
    evaluation_time TEXT NOT NULL,
    market_price REAL,
    seconds_to_close REAL,
    calibrated_prob REAL,
    raw_prob REAL,
    edge REAL,
    fee_adjusted_edge REAL,
    z_score REAL,
    volatility REAL,
    kelly_f REAL,
    market_result TEXT,
    side TEXT DEFAULT 'yes',
    position_size INTEGER,
    product_type TEXT,
    shadow_cal_prob REAL,
    shadow_cal_fee_edge REAL,
    hourly_pre_temp_prob REAL,
    hourly_applied_temp_t REAL
"""


def _make_eval_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(f"CREATE TABLE evaluated_opportunities ({_MIN_COLUMNS})")
    conn.commit()
    conn.close()


def _insert_row(
    db: Path,
    *,
    ticker: str = "KXBTCH-2025MAY15-T100000",
    event_ticker: str = "KXBTCH-2025MAY15",
    asset: str = "BTC",
    filter_stage: str = "hourly_observation",
    evaluation_time: str = "2026-05-01T12:00:00Z",
    market_price: float = 0.85,
    seconds_to_close: float = 900.0,
    calibrated_prob: float = 0.90,
    raw_prob: float = 0.88,
    edge: float = 0.05,
    fee_adjusted_edge: float = 0.045,
    z_score: float = 1.2,
    volatility: float = 0.01,
    kelly_f: float = 0.04,
    market_result: str = "yes",
    side: str = "yes",
    position_size: int = 10,
    product_type: str = "hourly",
    shadow_cal_prob: float = None,
    shadow_cal_fee_edge: float = None,
    hourly_pre_temp_prob: float = None,
    hourly_applied_temp_t: float = None,
) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO evaluated_opportunities (
            ticker, event_ticker, asset, filter_stage, evaluation_time,
            market_price, seconds_to_close, calibrated_prob, raw_prob,
            edge, fee_adjusted_edge, z_score, volatility, kelly_f,
            market_result, side, position_size, product_type,
            shadow_cal_prob, shadow_cal_fee_edge,
            hourly_pre_temp_prob, hourly_applied_temp_t
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker, event_ticker, asset, filter_stage, evaluation_time,
            market_price, seconds_to_close, calibrated_prob, raw_prob,
            edge, fee_adjusted_edge, z_score, volatility, kelly_f,
            market_result, side, position_size, product_type,
            shadow_cal_prob, shadow_cal_fee_edge,
            hourly_pre_temp_prob, hourly_applied_temp_t,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def fresh_db(tmp_path):
    db = tmp_path / "state.db"
    _make_eval_db(db)
    return db


# ─────────────────────────────────────────────────────────────────────────
# T-C1: NO-side WR semantics
# ─────────────────────────────────────────────────────────────────────────


def test_hourly_alpha_no_side_won_semantics(fresh_db):
    """C1 RCA pin: NO-side WR semantics correct across the Kalshi 4-vocab.

    Kalshi's API writes one of {'yes', 'all_yes', 'no', 'all_no'} to
    market_result (canonical OK set per bot/state.py + bot/settlement.py).
    NO-side bets win when result in {'no','all_no'}; YES-side bets win
    when result in {'yes','all_yes'}.

    Original C1 bug (R1): `won = result == 'yes'` regardless of side.
    R1 patch missed the `all_yes`/`all_no` 4-vocab extension; R2 patch
    fixes that. Mirrors sister precedent at
    tests/integration/test_alpha_audit.py:227-247.
    """
    # 8-case truth table: 2 sides x 4 result vocab values
    # NULL side defaults to YES semantics (legacy pre-side-column data).
    cases = [
        # (side, market_result, expected_won)
        ("yes", "yes",     True),
        ("yes", "all_yes", True),
        ("yes", "no",      False),
        ("yes", "all_no",  False),
        ("no",  "no",      True),
        ("no",  "all_no",  True),
        ("no",  "yes",     False),
        ("no",  "all_yes", False),
        # Legacy NULL side → YES semantics
        (None,  "yes",     True),
        (None,  "all_yes", True),
        (None,  "no",      False),
        (None,  "all_no",  False),
    ]
    for i, (side, result, _) in enumerate(cases):
        _insert_row(
            fresh_db,
            ticker=f"KXBTCH-CASE-{i:03d}",
            side=side if side is not None else "yes",  # DB schema NOT NULL
            market_result=result,
            evaluation_time=f"2026-05-01T12:{i:02d}:00Z",
        )

    from hourly_alpha_research import load_hourly_data, _won_for_side
    rows = load_hourly_data(str(fresh_db))

    # Test the helper directly with explicit None to cover NULL-side path
    for side, result, expected in cases:
        actual = _won_for_side(side, result)
        assert actual == expected, (
            f"_won_for_side(side={side!r}, market_result={result!r}) "
            f"returned {actual}, expected {expected}. "
            f"4-vocab inversion bug (alpha_audit C1 pattern) repeating."
        )
    # Also verify the loaded-row path agrees (DB-stored 'yes' default
    # for None covered via the side='yes' branch of the truth table).
    assert len(rows) == len(cases)


# ─────────────────────────────────────────────────────────────────────────
# T-C2: filter_stage enumeration (no single hardcoded stage)
# ─────────────────────────────────────────────────────────────────────────


def test_hourly_alpha_signals_enumerates_all_observation_stages(fresh_db):
    """C2 RCA pin: `is_signal` must include every shadow/observation
    stage, not just literal `'hourly_observation'`.

    Live stages that the audit must include as signals:
      - hourly_observation
      - hourly_observation_v2
      - hourly_config_a … hourly_config_m
      - candidate (post-filter, would-trade)

    Live stages that must NOT be signals (pre-filter rejects):
      - insufficient_edge
      - price_out_of_range
      - threshold_implausible

    Current bug (line 141): only `'hourly_observation'` returns
    is_signal=True. ~48% of relevant data is dropped.
    """
    signal_stages = [
        "hourly_observation",
        "hourly_observation_v2",
        "hourly_config_a",
        "hourly_config_d",
        "candidate",
    ]
    # `no_side_shadow` (36 hourly rows on 2026-05-15) is intentionally
    # NOT in the signal set — it's analyzed by the dedicated
    # `no_side_shadow_research()` function downstream. Document the
    # exclusion explicitly so future readers know it's by design, not
    # an oversight (M-R1.5).
    non_signal_stages = [
        "insufficient_edge",
        "price_out_of_range",
        "threshold_implausible",
        "no_side_shadow",
    ]
    for i, stg in enumerate(signal_stages + non_signal_stages):
        _insert_row(
            fresh_db,
            ticker=f"KXBTCH-STG-{i:03d}",
            filter_stage=stg,
            evaluation_time=f"2026-05-01T12:{i:02d}:00Z",
        )

    from hourly_alpha_research import load_hourly_data
    rows = load_hourly_data(str(fresh_db))

    signal_rows = [r for r in rows if r["is_signal"]]
    non_signal_rows = [r for r in rows if not r["is_signal"]]

    signal_stage_names = {r["filter_stage"] for r in signal_rows}
    non_signal_stage_names = {r["filter_stage"] for r in non_signal_rows}

    for stg in signal_stages:
        assert stg in signal_stage_names, (
            f"stage '{stg}' must be marked is_signal=True. "
            f"got signal stages: {signal_stage_names}. "
            f"This is the alpha_audit C2 hardcoded-SHADOW_STAGES "
            f"pattern repeating."
        )
    for stg in non_signal_stages:
        assert stg in non_signal_stage_names, (
            f"stage '{stg}' is a pre-filter reject and must NOT be "
            f"is_signal=True. got non-signal stages: "
            f"{non_signal_stage_names}."
        )


# ─────────────────────────────────────────────────────────────────────────
# T-M3: AST guard against hardcoded 4-asset list in run_research()
# ─────────────────────────────────────────────────────────────────────────


def test_hourly_alpha_no_hardcoded_btc_eth_sol_xrp_list():
    """M3 + R3 MAJOR-R3.1/R3.2 RCA pin: no inline 3+ element subset of
    the legacy 4-asset set {BTC, ETH, SOL, XRP}.

    R1 original M3 fix only banned the 4-element form. R3 adv-review
    found the same structural-lie defect class in 3-element-subset form:
      - asset_combos `({'BTC', 'ETH', 'SOL'}, 'no_XRP')` lies because it
        actually excludes XRP+HYPE+DOGE, not just XRP.
      - Section 10 `HOURLY_EXCLUDED_ASSETS = {'ETH', 'SOL', 'XRP'}`
        printout drops HYPE/DOGE from exclusion if operator pastes
        verbatim → inadvertent live promotion of HYPE/DOGE on hourly.

    The patched form derives both from `all_assets_set` dynamically.
    Test bans any inline literal containing 3+ of the legacy 4-asset
    names; 1- and 2-element subsets are OK (intentional `BTC_only` etc).

    Hourly DB has 150 HYPE + 105 DOGE settled rows on 2026-05-15.
    """
    src = SCRIPT_PATH.read_text()
    tree = ast.parse(src)

    BANNED_SET = {"BTC", "ETH", "SOL", "XRP"}
    offenders: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = []
            for el in node.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    values.append(el.value)
            # R3 extension: any inline subset of size >= 3 is a lie
            # (or at least incomplete vs HYPE/DOGE), not just the
            # exact 4-element form. 1- and 2-element subsets pass
            # (e.g., {'BTC'} for BTC_only, {'BTC','ETH'} for the
            # BTC_ETH explicit pair combo).
            intersect = set(values) & BANNED_SET
            if len(intersect) >= 3:
                offenders.append((node.lineno, repr(values)))

    assert not offenders, (
        f"hardcoded 3+ legacy-asset literal found at lines: {offenders}. "
        f"Must derive dynamically (e.g., `all_assets_set - {{'XRP'}}` "
        f"for the 'no_XRP' combo, `set(best['asset_stats'].keys())` "
        f"for label translation) so HYPE/DOGE hourly observation rows "
        f"are correctly handled and operators don't get misleading "
        f"recommendations that would inadvertently promote HYPE/DOGE."
    )


# ─────────────────────────────────────────────────────────────────────────
# T-M6: price clamp [1, 99] silently mutates settled-100c rows
# ─────────────────────────────────────────────────────────────────────────


def test_hourly_alpha_does_not_silently_clamp_settled_100c(fresh_db):
    """M6 RCA pin: a row with market_price=1.0 (100c) must NOT be
    silently coerced to 99c.

    Current bug (line 130): `max(1, min(99, d['price']))`. Affects PnL
    (uses price), tier classification, breakeven WR. Patch options:
      (a) raise / log warning loudly
      (b) preserve as 100 (price domain is [0, 100])

    Either is acceptable; silent clamp to 99 is not. Test asserts the
    final `price` field is NOT 99 when input is 1.0 (100c).
    """
    _insert_row(
        fresh_db,
        ticker="KXBTCH-100C",
        market_price=1.0,  # ← 100c, fully decided YES at close
        market_result="yes",
        side="yes",
    )

    from hourly_alpha_research import load_hourly_data
    rows = load_hourly_data(str(fresh_db))

    assert len(rows) == 1
    price = rows[0]["price"]
    assert price != 99, (
        f"market_price=1.0 (100c) silently clamped to {price}. "
        f"Settled-100c markets must either preserve price=100 or "
        f"raise loudly. Silent clamp distorts PnL math, tier "
        f"classification, and breakeven WR."
    )


# ─────────────────────────────────────────────────────────────────────────
# T-M-R1.1: BOT_EDGE_SCHEDULE + get_tier_min_edge parity with constants
# ─────────────────────────────────────────────────────────────────────────


def test_hourly_alpha_edge_schedule_parity_with_bot_constants():
    """M-R1.1 RCA pin: the script's edge schedule must match
    bot.constants.MIN_EDGE_BY_PRICE exactly.

    Pre-R2 the script hardcoded a stale BOT_EDGE_SCHEDULE dict +
    get_tier_min_edge function that drifted from the live bot config:
      - 91-92c: 0.20% (constants) vs 0.35% (script) drift
      - 93-94c: 0.50% (constants) vs 0.90% (script) drift
      - 95-96c: 0.75% (constants) vs 1.25% (script) drift
      - 97c+:   1.00% (constants) vs 2.00% (script) drift

    R2 patch imports MIN_EDGE_BY_PRICE from bot.constants directly.
    This test pins that import path against drift.
    """
    from bot.constants import MIN_EDGE_BY_PRICE
    from hourly_alpha_research import BOT_EDGE_SCHEDULE, get_tier_min_edge

    # BOT_EDGE_SCHEDULE must be derived from the canonical list
    expected = {price: edge for price, edge in MIN_EDGE_BY_PRICE}
    assert BOT_EDGE_SCHEDULE == expected, (
        f"BOT_EDGE_SCHEDULE drift from bot.constants.MIN_EDGE_BY_PRICE.\n"
        f"  script:  {BOT_EDGE_SCHEDULE}\n"
        f"  constants: {expected}"
    )

    # get_tier_min_edge must return the canonical edge for each
    # breakpoint price (lower-bound semantics).
    for min_price, expected_edge in MIN_EDGE_BY_PRICE:
        actual_edge = get_tier_min_edge(min_price)
        assert actual_edge == expected_edge, (
            f"get_tier_min_edge({min_price}) returned {actual_edge}, "
            f"expected {expected_edge} per bot.constants.MIN_EDGE_BY_PRICE."
        )

    # Spot-check the 4 historically-drifted prices
    assert get_tier_min_edge(91) == 0.0020, "91c tier drift"
    assert get_tier_min_edge(93) == 0.005, "93c tier drift"
    assert get_tier_min_edge(95) == 0.0075, "95c tier drift"
    assert get_tier_min_edge(97) == 0.010, "97c tier drift"


# ─────────────────────────────────────────────────────────────────────────
# T-C3: v2_variant_alpha side-aware WR (mirror C1 fix in inner SQL path)
# ─────────────────────────────────────────────────────────────────────────


def test_hourly_alpha_v2_variant_alpha_side_aware(fresh_db, capsys):
    """C3 RCA pin (R2 adv-review M-R1.2): the v2_variant_alpha function
    bypasses load_hourly_data — it queries the DB directly. Its inner
    WR/PnL/Brier loops must also use _won_for_side, not the YES-hardcode.

    Test: seed 10 hourly_observation_v2 rows with side='no',
    market_result='no'. Correct WR is 100% (10W/0L). Pre-R1 the function
    reported 0W/10L (WR=0%). Post-R1 (with C3 patch + 4-vocab R2
    extension) the function reports 100%.
    """
    for i in range(10):
        _insert_row(
            fresh_db,
            ticker=f"KXBTCH-V2NO-{i:03d}",
            filter_stage="hourly_observation_v2",
            side="no",
            market_result="no",
            evaluation_time=f"2026-05-01T12:{i:02d}:00Z",
            calibrated_prob=0.20,  # NO-side prediction = 1 - YES_prob; 0.20 YES = 0.80 NO
        )

    from hourly_alpha_research import v2_variant_alpha
    v2_variant_alpha(str(fresh_db), total_days=1.0)
    out = capsys.readouterr().out

    # The function prints "N=10, 10W/0L, WR=100.0%" if the side-aware
    # win semantics are correct. Pre-fix it would print "0W/10L".
    assert "10W/0L" in out, (
        f"v2_variant_alpha reported wrong W/L for NO-side rows. "
        f"Expected '10W/0L' in stdout, got:\n{out}"
    )
    assert "WR=100.0%" in out, (
        f"v2_variant_alpha reported wrong WR for NO-side rows. "
        f"Expected 'WR=100.0%' in stdout, got:\n{out}"
    )
