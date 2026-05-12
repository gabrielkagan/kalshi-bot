"""Spike 86b9wxr6p (2026-05-12) — STC structural clustering pipeline pins.

Parent: 86b9wwqn9 (admit-SHAP) + outcome-SHAP follow-up. Model B in the
outcome-SHAP doc surfaced `seconds_to_close` mean |SHAP| 5.96 — the
dominant non-gate predictor of W/L. This spike answers the actionable
question: are there specific (asset × STC band) cells where the
counterfactual Kelly PnL is materially negative AND not already covered
by an existing cell-block stage?

The analysis script `scripts/audit/stc_structural_clustering.py`:

- Partitions admits (filter_stage='candidate') into STC bands.
- Computes per-(asset × band): n_admit, n_yes, n_no, WR, Wilson95.
- Maps each (asset × STC band × price-band) combo against the 4 existing
  cell-block predicates (`should_block_high_price_stc_candidate`,
  `should_block_tm98_highprice_bleed_candidate`,
  `should_block_sol_taker_lowprice_bleed_candidate`,
  `should_block_sol_bleed_v2_candidate`).
- Uses `counterfactual_pnl` (already Kelly-sized at decision time via
  the bot's actual sizer; pinned by the parent spike's leakage doc as a
  settlement-time col — kept here because the spike's job IS counterfactual
  PnL and we're NOT building a predictive model that would be biased by it).
- Wilson95 lower/upper computed from `statsmodels` if available, else
  inline (preferred — fewer dependencies).

These pins protect against drift if the script is rerun later. Mirrors
the sibling `test_wave1_feature_importance.py` structure.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "audit" / "stc_structural_clustering.py"
sys.path.insert(0, str(REPO_ROOT))


def _load_script_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("stc_cluster_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_script_exists():
    assert SCRIPT.exists(), f"Spike script missing at {SCRIPT}"


def test_universe_pinned_to_15m_crypto():
    """cal_mlp + the 4 cell-block predicates only fire on 15M crypto;
    a future maintainer must not silently widen to hourly/SPX/weather/sports.
    """
    mod = _load_script_module()
    assert set(mod.ASSETS) == {"BTC", "ETH", "SOL", "XRP"}
    assert mod.PRODUCT_TYPE == "15m"


def test_admit_filter_stage_is_candidate_only():
    """The analysis uses filter_stage='candidate' (the bot's would-have-
    traded rows) for counterfactual PnL.

    Cell-block stages (96C_SOL_XRP_STC_DANGER_BAND, TM98_*, SOL_TAKER_*,
    SOL_BLEED_V2_*) are DROPPED — they reflect candidates the bot already
    gated; including them would double-count cells the gate is ALREADY
    catching. The audit's job is to find STC bands WITHOUT existing
    coverage; mixing in already-blocked rows distorts that.
    """
    mod = _load_script_module()
    assert mod.CANDIDATE_FILTER_STAGE == "candidate"


def test_stc_band_partition_breakpoints_pinned():
    """STC band breakpoints are 60s increments from 0 to 600s plus a
    600+ tail. Pinned so the empirical histogram + Wilson-CI tables stay
    comparable across reruns.
    """
    mod = _load_script_module()
    assert mod.STC_BAND_EDGES == (0, 60, 120, 180, 240, 300, 360, 420, 480, 540, 600)


def test_stc_band_partitioner_assigns_correct_band():
    """0.0 → '0-60', 59.9 → '0-60', 60.0 → '60-120', 599.9 → '540-600',
    600.0 → '600+', None → None (honest-NULL passthrough).
    """
    mod = _load_script_module()
    assert mod.assign_stc_band(0.0) == "0-60"
    assert mod.assign_stc_band(59.9) == "0-60"
    assert mod.assign_stc_band(60.0) == "60-120"
    assert mod.assign_stc_band(120.0) == "120-180"
    assert mod.assign_stc_band(300.0) == "300-360"
    assert mod.assign_stc_band(599.9) == "540-600"
    assert mod.assign_stc_band(600.0) == "600+"
    assert mod.assign_stc_band(900.0) == "600+"
    assert mod.assign_stc_band(None) is None


def test_wilson_ci_known_values():
    """Wilson score CI for (k=10, n=100, alpha=0.05) should give
    approximately [0.0552, 0.1734] per textbook references. Use 1e-3
    tolerance to allow inline vs statsmodels-route equivalence.
    """
    mod = _load_script_module()
    lo, hi = mod.wilson_ci(k=10, n=100, alpha=0.05)
    assert lo == pytest.approx(0.0552, abs=1e-3)
    assert hi == pytest.approx(0.1734, abs=1e-3)


def test_wilson_ci_edge_cases():
    """n=0 → (0.0, 1.0) — no data, max uncertainty.
    k=n → upper bound = 1.0 + tiny epsilon (continuity).
    k=0 → lower bound = 0.0.
    """
    mod = _load_script_module()
    lo, hi = mod.wilson_ci(k=0, n=0, alpha=0.05)
    assert lo == 0.0
    assert hi == 1.0
    lo, hi = mod.wilson_ci(k=10, n=10, alpha=0.05)
    assert hi == pytest.approx(1.0, abs=1e-3)
    lo, hi = mod.wilson_ci(k=0, n=10, alpha=0.05)
    assert lo == pytest.approx(0.0, abs=1e-3)


def test_cell_block_coverage_classifier_delegates_to_canonical_helpers():
    """The classifier must delegate to the actual bot/helpers/cell_blocks.py
    predicates — NEVER reimplement the (asset, side, price, STC, strategy)
    conditions inline. Otherwise it drifts if the helpers change.
    """
    from bot.helpers import cell_blocks

    mod = _load_script_module()
    # The 4 canonical predicates are the source of truth.
    assert mod.HIGH_PRICE_STC_BLOCK is cell_blocks.should_block_high_price_stc_candidate
    assert mod.TM98_HIGHPRICE_BLEED_BLOCK is cell_blocks.should_block_tm98_highprice_bleed_candidate
    assert mod.SOL_TAKER_LOWPRICE_BLEED_BLOCK is cell_blocks.should_block_sol_taker_lowprice_bleed_candidate
    assert mod.SOL_BLEED_V2_BLOCK is cell_blocks.should_block_sol_bleed_v2_candidate


def test_cell_block_coverage_classifier_returns_named_stage_or_none():
    """For a row matching SOL × TAKER_NOW × 92¢ × 180s, the classifier must
    return the matching cell-block stage name. For a row matching no gate,
    return None.

    The classifier evaluates ALL 4 gates with enabled=True (we're asking
    "would this row be gated if the operator enabled the block" not "is
    it gated right now"). The operator-side env flag is orthogonal.
    """
    mod = _load_script_module()
    # SOL × 92¢ × 180s × TAKER_NOW → covered by SOL_BLEED_V2 (88-93¢ band, 121-300s)
    covered = mod.classify_cell_block_coverage(
        asset="SOL", side="yes", entry_price_cents=92,
        seconds_to_close=180.0, strategy="TAKER_NOW")
    assert covered == "SOL_BLEED_V2"

    # SOL × 92¢ × 400s × TAKER_NOW → NOT covered (STC band 121-300s only)
    not_covered = mod.classify_cell_block_coverage(
        asset="SOL", side="yes", entry_price_cents=92,
        seconds_to_close=400.0, strategy="TAKER_NOW")
    assert not_covered is None

    # XRP × 96¢ × 200s × MAKER_PATIENT → covered by HIGH_PRICE_STC_BLOCK.
    # HPSB is strategy-aware: only fires for the 4 bleeder strategies
    # (decided_t2, decided_t2_z2, decided_t2_z25, MAKER_PATIENT) per
    # HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES in bot/constants.py.
    covered = mod.classify_cell_block_coverage(
        asset="XRP", side="yes", entry_price_cents=96,
        seconds_to_close=200.0, strategy="MAKER_PATIENT")
    assert covered == "HIGH_PRICE_STC_BLOCK"

    # XRP × 96¢ × 200s × TAKER_NOW → NOT covered. TAKER_NOW is NOT a HPSB
    # bleeder (intentional — TAKER_NOW is profitable inside the 96¢ cell).
    not_covered = mod.classify_cell_block_coverage(
        asset="XRP", side="yes", entry_price_cents=96,
        seconds_to_close=200.0, strategy="TAKER_NOW")
    assert not_covered is None


def test_counterfactual_pnl_uses_actual_kelly_position_size_not_flat_one():
    """The analysis MUST NOT compute counterfactual PnL as flat-1-contract.
    Per CLAUDE.md "Sim PnL and counterfactuals use actual Kelly sizing."

    The script reads `counterfactual_pnl` from the DB (already Kelly-sized
    at decision time by the bot's sizer). The wrapper helper must not
    fall back to a flat-1 calculation when position_size is populated.
    """
    mod = _load_script_module()
    # row with explicit non-1 position_size → wrapper returns the DB value
    row = {
        "counterfactual_pnl": -3500,  # cents
        "position_size": 35,
        "market_price": 90,
        "market_result": "no",
    }
    assert mod.row_counterfactual_cents(row) == -3500


def test_counterfactual_pnl_handles_null_db_value_via_derivation():
    """If counterfactual_pnl is NULL but position_size + market_price +
    market_result are populated, derive: yes_win = pos × (100 - mp), no_loss = -pos × mp.

    Honest-NULL passthrough if any input is missing.
    """
    mod = _load_script_module()
    # NULL cf, derive from other cols (40 contracts × +10c win = +400c)
    row_win = {
        "counterfactual_pnl": None, "position_size": 40,
        "market_price": 90, "market_result": "yes"}
    assert mod.row_counterfactual_cents(row_win) == 400

    # NULL cf, derive loss (40 × -90c = -3600c)
    row_loss = {
        "counterfactual_pnl": None, "position_size": 40,
        "market_price": 90, "market_result": "no"}
    assert mod.row_counterfactual_cents(row_loss) == -3600

    # NULL position_size → None (honest-NULL)
    row_null = {
        "counterfactual_pnl": None, "position_size": None,
        "market_price": 90, "market_result": "no"}
    assert mod.row_counterfactual_cents(row_null) is None


def test_loader_drops_settlement_leakage_columns(tmp_path):
    """The loader must drop the same settlement-time leakage cols as the
    outcome-SHAP loader — minus `counterfactual_pnl` which IS the
    quantity being summed here.

    final_spot_price / knockout_time_relative / max_excursion_from_strike /
    time_above_strike_seconds / time_below_strike_seconds / settled_time /
    order_outcome / minutes_above_strike are still dropped — they're
    settlement-derived and not used in band-partitioning.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
            asset TEXT NOT NULL, filter_stage TEXT NOT NULL,
            evaluation_time TEXT NOT NULL, market_result TEXT,
            market_price INTEGER, seconds_to_close REAL,
            position_size INTEGER, strategy TEXT, side TEXT DEFAULT 'yes',
            counterfactual_pnl INTEGER, product_type TEXT,
            final_spot_price REAL, knockout_time_relative REAL,
            max_excursion_from_strike REAL, time_above_strike_seconds REAL,
            time_below_strike_seconds REAL, settled_time TEXT,
            order_outcome TEXT, minutes_above_strike REAL
        );
    """)
    conn.execute(
        "INSERT INTO evaluated_opportunities (ticker,asset,filter_stage,evaluation_time,"
        " market_result,market_price,seconds_to_close,position_size,strategy,side,"
        " counterfactual_pnl,product_type,final_spot_price,knockout_time_relative,"
        " max_excursion_from_strike,time_above_strike_seconds,time_below_strike_seconds,"
        " settled_time,order_outcome,minutes_above_strike) VALUES "
        "('E1','BTC','candidate','2026-04-01T10:00:00Z','yes',93,200,30,'TAKER_NOW','yes',"
        " 210,'15m',105.0,400.0,3.0,800.0,100.0,'2026-04-01T10:15:00Z','filled_taker',13.3)")
    conn.commit()
    conn.close()

    mod = _load_script_module()
    df = mod.load_admit_corpus(db)
    assert len(df) == 1
    # cf_pnl IS kept — it's the target quantity for the audit.
    assert "counterfactual_pnl" in df.columns
    # The other settlement-time cols are dropped.
    for leak in ("final_spot_price", "knockout_time_relative",
                 "max_excursion_from_strike", "time_above_strike_seconds",
                 "time_below_strike_seconds", "settled_time",
                 "order_outcome", "minutes_above_strike"):
        assert leak not in df.columns, f"leakage col {leak!r} not dropped"


def test_loader_filters_universe_and_admits_only(tmp_path):
    """Loader pins: only filter_stage='candidate', only 15M crypto,
    only market_result IN ('yes','no'). NULL/'push'/other dropped.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
            asset TEXT NOT NULL, filter_stage TEXT NOT NULL,
            evaluation_time TEXT NOT NULL, market_result TEXT,
            market_price INTEGER, seconds_to_close REAL,
            position_size INTEGER, strategy TEXT, side TEXT DEFAULT 'yes',
            counterfactual_pnl INTEGER, product_type TEXT
        );
    """)
    conn.executescript("""
        INSERT INTO evaluated_opportunities (ticker,asset,filter_stage,evaluation_time,market_result,market_price,seconds_to_close,position_size,strategy,side,counterfactual_pnl,product_type) VALUES
          ('E1','BTC','candidate','2026-04-01T10:00:00Z','yes',90,200,30,'TAKER_NOW','yes',300,'15m'),
          ('E2','BTC','candidate','2026-04-02T10:00:00Z','no',90,200,30,'TAKER_NOW','yes',-2700,'15m'),
          ('E3','BTC','candidate','2026-04-03T10:00:00Z',NULL,90,200,30,'TAKER_NOW','yes',NULL,'15m'),
          ('E4','BTC','candidate','2026-04-04T10:00:00Z','push',90,200,30,'TAKER_NOW','yes',NULL,'15m'),
          ('E5','BTC','TM98_97_98C_2_5MIN_BLEED','2026-04-05T10:00:00Z','yes',98,200,30,'terminal_momentum_98','yes',60,'15m'),
          ('E6','BTC','insufficient_edge','2026-04-06T10:00:00Z','yes',90,200,30,'TAKER_NOW','yes',300,'15m'),
          ('E7','SPX','candidate','2026-04-07T10:00:00Z','yes',60,200,30,'TAKER_NOW','yes',1200,'spx'),
          ('E8','BTC','candidate','2026-04-08T10:00:00Z','yes',90,200,30,'TAKER_NOW','yes',300,'hourly');
    """)
    conn.commit()
    conn.close()

    mod = _load_script_module()
    df = mod.load_admit_corpus(db)
    # Only E1 (yes) + E2 (no) survive — E3/E4 NULL/push dropped, E5 cell-block
    # stage dropped, E6 wrong filter_stage, E7 wrong asset, E8 wrong product_type.
    assert len(df) == 2
    assert set(df["ticker"]) == {"E1", "E2"}


def test_per_band_table_has_required_columns():
    """Output schema: (asset, stc_band, n_admit, n_yes, n_no, wr, wilson_lo,
    wilson_hi, cf_pnl_cents, cf_pnl_30d_cents, covered_by, qualifies_for_gate).

    Pinned so a future column rename in the analysis body breaks the test,
    not the consumer.
    """
    import pandas as pd
    mod = _load_script_module()
    df = pd.DataFrame({
        "asset": ["BTC"] * 4,
        "seconds_to_close": [50.0, 70.0, 130.0, 200.0],
        "market_result": ["yes", "yes", "no", "yes"],
        "market_price": [90, 91, 92, 93],
        "position_size": [30, 30, 30, 30],
        "counterfactual_pnl": [300, 270, -2760, 210],
        "strategy": ["TAKER_NOW"] * 4,
        "side": ["yes"] * 4,
    })
    # Time window: 4 rows over 1 day → 30d normalization factor large.
    table = mod.build_per_band_table(df, window_days=1.0)
    required = {"asset", "stc_band", "n_admit", "n_yes", "n_no", "wr",
                "wilson_lo", "wilson_hi", "cf_pnl_cents", "cf_pnl_30d_cents",
                "covered_by", "qualifies_for_gate"}
    missing = required - set(table.columns)
    assert not missing, f"per-band table missing cols: {missing}"


def test_actionable_gate_trigger_criteria():
    """Gate-trigger criteria per the spike brief:
    (a) n ≥ 50
    (b) Wilson95 UPPER < 0.92    (definitively below the 95.3% global WR)
    (c) NOT already gated by any existing cell-block stage
    (d) abs(counterfactual_pnl_30d) > $50  (50_00 cents)

    A combo qualifies for gating iff ALL 4 criteria hold. The test pins
    each criterion individually + a passing case to prevent silent
    relaxation of any single condition.
    """
    mod = _load_script_module()
    # Passing case
    assert mod.qualifies_for_gate_trigger(n_admit=100, wilson_hi=0.85,
                                          cf_pnl_30d_cents=-8000,
                                          covered_by=None) is True
    # (a) too few admits
    assert mod.qualifies_for_gate_trigger(n_admit=49, wilson_hi=0.85,
                                          cf_pnl_30d_cents=-8000,
                                          covered_by=None) is False
    # (b) Wilson95 upper at threshold (>= 0.92 means cannot rule out
    # being within healthy-WR range)
    assert mod.qualifies_for_gate_trigger(n_admit=100, wilson_hi=0.92,
                                          cf_pnl_30d_cents=-8000,
                                          covered_by=None) is False
    # (c) already gated
    assert mod.qualifies_for_gate_trigger(n_admit=100, wilson_hi=0.85,
                                          cf_pnl_30d_cents=-8000,
                                          covered_by="SOL_BLEED_V2") is False
    # (d) below $50 save threshold (abs < 5000c)
    assert mod.qualifies_for_gate_trigger(n_admit=100, wilson_hi=0.85,
                                          cf_pnl_30d_cents=-4999,
                                          covered_by=None) is False


def test_window_days_normalization_is_proportional():
    """cf_pnl_30d_cents = cf_pnl_cents * (30 / window_days).
    Pin against drift toward arbitrary annualization factors.
    """
    import pandas as pd
    mod = _load_script_module()
    df = pd.DataFrame({
        "asset": ["BTC"] * 2,
        "seconds_to_close": [50.0, 130.0],
        "market_result": ["yes", "no"],
        "market_price": [90, 92],
        "position_size": [30, 30],
        "counterfactual_pnl": [300, -2760],
        "strategy": ["TAKER_NOW"] * 2,
        "side": ["yes"] * 2,
    })
    # 73d window → 30d factor 30/73 ≈ 0.411
    table = mod.build_per_band_table(df, window_days=73.0)
    for _, row in table.iterrows():
        if row["cf_pnl_cents"]:
            ratio = row["cf_pnl_30d_cents"] / row["cf_pnl_cents"]
            assert ratio == pytest.approx(30.0 / 73.0, rel=1e-6)


def test_strategy_majority_used_for_cell_block_coverage():
    """When a band has mixed strategies, classify_cell_block_coverage_band
    determines coverage based on the MAJORITY strategy in the band — and
    only flags 'covered' if the majority strategy IS in the gate's blocker
    set. Mixed bands with no majority cell-block bleeder are reported as
    "partial" (string) so the recommendation memo can call them out
    explicitly rather than silently labeling them covered/uncovered.
    """
    mod = _load_script_module()
    # All TAKER_NOW × SOL 92¢ 200s → SOL_BLEED_V2 covers (majority strategy
    # is a bleeder for that gate)
    band_rows = [
        {"asset": "SOL", "side": "yes", "market_price": 92, "seconds_to_close": 200.0,
         "strategy": "TAKER_NOW"} for _ in range(60)
    ] + [
        {"asset": "SOL", "side": "yes", "market_price": 92, "seconds_to_close": 200.0,
         "strategy": "MAKER_AGGRESSIVE"} for _ in range(10)
    ]
    cov = mod.classify_band_cell_block_coverage(band_rows)
    assert cov == "SOL_BLEED_V2"
    # Same band but MAJORITY MAKER_AGGRESSIVE → partial (TAKER_NOW minority is gated;
    # MAKER_AGGRESSIVE majority is NOT in SOL_BLEED_V2 strategies)
    band_rows = [
        {"asset": "SOL", "side": "yes", "market_price": 92, "seconds_to_close": 200.0,
         "strategy": "MAKER_AGGRESSIVE"} for _ in range(60)
    ] + [
        {"asset": "SOL", "side": "yes", "market_price": 92, "seconds_to_close": 200.0,
         "strategy": "TAKER_NOW"} for _ in range(10)
    ]
    cov = mod.classify_band_cell_block_coverage(band_rows)
    assert cov == "partial"
