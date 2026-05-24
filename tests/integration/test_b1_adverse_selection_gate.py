"""B1 composite adverse-selection gate — behavioral pins on production scenarios.

[ClickUp 86ba1zdwm, umbrella 86ba1zcd3, 2026-05-21]

Pins two independent gates that ship as Bit B1:

Gate A — orderbook-prior adverse-selection block (asset-agnostic, entry ≥ 90c)
    disagree = calibrated_prob - (100 - no_ask_cents) / 100
    conv_ge2 = Σ over yes_asks levels (depth × (100 - price)) where (100 - price) ≥ 2
    BLOCK if disagree > 0.05 AND conv_ge2 > 500

Gate B — HYPE high-price buf gate (HYPE-only, entry ≥ 98c)
    BLOCK if asset == "HYPE" AND entry ≥ 98 AND bot_buf_pct < 0.75

Helper home: `bot/helpers/adverse_selection.py` (to be created in impl pass).
The helper exposes two pure functions:

    check_orderbook_prior_gate(calibrated_prob, no_ask_cents, yes_asks) -> Optional[str]
    check_hype_high_price_buf_gate(asset, entry_price_cents, bot_buf_pct) -> Optional[str]

Both return the `filter_stage` literal when the gate fires, else None.

Production scenarios pinned here (from kb/decisions/b1-orderbook-prior-gate-plan.md):

  HYPE 99c 2026-05-21 12:30 UTC  KXHYPE15M-26MAY210830-30  -$50.49 → Gate B blocks
  HYPE 98c 2026-05-18 09:30 UTC  KXHYPE15M-26MAY180530-30  -$56.84 → Gate B blocks (1 of 2 stacked rows)
  DOGE 97c 2026-05-14 21:15 UTC  KXDOGE15M-26MAY141715-15  -$48.49 → Gate A blocks
  SOL  90c 2026-05-10 16:15 UTC  KXSOL15M-26MAY101615-15   -$176.39 → Gate A blocks
  SOL  92c 2026-05-09 11:45 UTC  KXSOL15M-26MAY091145-45   -$127.88 → Gate A blocks

Negative scenarios (must NOT block):
  SOL  99c clean winner — both gates pass
  HYPE 97c (entry < 98 threshold for Gate B) — Gate B does NOT fire
  HYPE 99c with healthy buf=1.50% — Gate B does NOT fire
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Heavy-dep stubs so `from bot.constants import ...` resolves cleanly under test.
_HEAVY = (
    "websockets", "websocket", "requests",
    "cryptography", "cryptography.hazmat",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.asymmetric.padding",
)
for _m in _HEAVY:
    sys.modules.setdefault(_m, MagicMock())


# ── Gate A: orderbook-prior ───────────────────────────────────────────────────

def test_gate_a_blocks_doge_97c_2026_05_14_production_scenario():
    """KXDOGE15M-26MAY141715-15 -$48.49.
    At decision time: cal_p=0.970, no_ask=9c, orderbook had wide NO bid depth
    (conv_ge2 = 3066c per R0 sim).
    Gate A must fire (disagree=0.060 > 0.05; conv=3066 > 500; entry=97 >= 90)."""
    from bot.helpers.adverse_selection import check_orderbook_prior_gate
    yes_asks = [
        (91, 39),   # no_bid=9, depth=39 -> contributes 9*39 = 351 (no_bid >= 2)
        (92, 100),  # no_bid=8 -> 800
        (93, 150),  # no_bid=7 -> 1050
        (94, 100),  # no_bid=6 -> 600
        (98, 33),   # no_bid=2 -> 66
        (99, 117),  # no_bid=1 -> BELOW threshold, excluded
        (100, 200), # no_bid=0 -> excluded
    ]
    result = check_orderbook_prior_gate(
        calibrated_prob=0.970, no_ask_cents=9, yes_asks=yes_asks,
        entry_price_cents=97,
    )
    assert result == "orderbook_prior_block", (
        f"Gate A should fire on DOGE 97c production scenario, got {result!r}"
    )


def test_gate_a_does_not_fire_below_min_entry_cents():
    """R0 sim only measured entry >= 90c. Gate A must NOT fire on sub-90c
    entries (e.g., SOL MIN_ENTRY_PRICE=86c, DC t2 high-edge low-price entries).
    Even with all other conditions met, entry=89 must pass."""
    from bot.helpers.adverse_selection import check_orderbook_prior_gate
    yes_asks = [(95, 1000)]  # conv_ge2 = 5000 (way above threshold)
    # cal_p=0.97, no_ask=10 -> disagree=0.07 (passes)
    # But entry=89 < 90 floor
    result = check_orderbook_prior_gate(
        calibrated_prob=0.97, no_ask_cents=10, yes_asks=yes_asks,
        entry_price_cents=89,
    )
    assert result is None, (
        f"Gate A must NOT fire below 90c entry (R0 sim out-of-scope), got {result!r}"
    )


def test_gate_a_boundary_entry_cents_at_90c_fires():
    """At entry exactly 90c (boundary, >= floor), Gate A should fire when
    other conditions met."""
    from bot.helpers.adverse_selection import check_orderbook_prior_gate
    yes_asks = [(95, 1000)]  # conv_ge2 = 5000
    result = check_orderbook_prior_gate(
        calibrated_prob=0.97, no_ask_cents=10, yes_asks=yes_asks,
        entry_price_cents=90,
    )
    assert result == "orderbook_prior_block", (
        f"Gate A must fire at entry=90c (boundary, inclusive floor), got {result!r}"
    )


def test_gate_a_does_not_fire_when_disagree_too_small():
    """At disagree = 0.05 (boundary, strict >), gate should NOT fire
    even with very high conviction."""
    from bot.helpers.adverse_selection import check_orderbook_prior_gate
    yes_asks = [(95, 1000)]
    # cal_p=0.95, no_ask=10 -> market_p_floor=0.90, disagree=0.05 (NOT > 0.05)
    result = check_orderbook_prior_gate(
        calibrated_prob=0.95, no_ask_cents=10, yes_asks=yes_asks,
        entry_price_cents=99,
    )
    assert result is None, (
        f"Gate A should NOT fire when disagree=0.05 (strict boundary), got {result!r}"
    )


def test_gate_a_does_not_fire_when_conviction_too_low():
    """At disagree > 0.05 but conv_ge2 <= 500, gate should NOT fire."""
    from bot.helpers.adverse_selection import check_orderbook_prior_gate
    yes_asks = [(98, 200)]  # no_bid=2, depth=200 -> conv = 400 (<= 500)
    result = check_orderbook_prior_gate(
        calibrated_prob=0.97, no_ask_cents=10, yes_asks=yes_asks,
        entry_price_cents=99,
    )
    assert result is None, (
        f"Gate A should NOT fire when conv_ge2=400 <= 500, got {result!r}"
    )


def test_gate_a_excludes_no_bid_below_min_price():
    """conv_ge2 must exclude NO bids at price < 2 (pure liquidity-makers)."""
    from bot.helpers.adverse_selection import check_orderbook_prior_gate
    yes_asks = [(99, 1000), (100, 1000)]
    result = check_orderbook_prior_gate(
        calibrated_prob=0.97, no_ask_cents=10, yes_asks=yes_asks,
        entry_price_cents=99,
    )
    assert result is None, (
        f"Gate A must exclude 0-1c liquidity-only bids, got {result!r}"
    )


def test_gate_a_sol_90c_2026_05_10_production_scenario():
    """KXSOL15M-26MAY101615-15 -$176.39 (largest 14d loss).
    cal_p=0.888, no_ask=26c (market priced 26% NO probability), wide NO depth.
    disagree = 0.888 - 0.74 = 0.148 > 0.05; conv = 14785¢ from R0 sim > 500.
    Entry=90 (boundary). Gate A must fire."""
    from bot.helpers.adverse_selection import check_orderbook_prior_gate
    # Synthetic NO bid book matching the 14785¢ R0-sim conviction.
    yes_asks = [(80, 700), (78, 500), (75, 800)]
    # conv_ge2 = 20*700 + 22*500 + 25*800 = 14000+11000+20000 = 45000c (>> 500)
    result = check_orderbook_prior_gate(
        calibrated_prob=0.888, no_ask_cents=26, yes_asks=yes_asks,
        entry_price_cents=90,
    )
    assert result == "orderbook_prior_block", (
        f"Gate A should fire on SOL 90c production scenario, got {result!r}"
    )


def test_gate_a_sol_92c_2026_05_09_production_scenario():
    """KXSOL15M-26MAY091145-45 -$127.88.
    cal_p=0.915, no_ask=20c, conv=51186 per R0 sim, entry=92. Gate A must fire."""
    from bot.helpers.adverse_selection import check_orderbook_prior_gate
    yes_asks = [(85, 800), (83, 1200), (82, 1500)]
    # conv = 15*800 + 17*1200 + 18*1500 = 12000+20400+27000 = 59400 (>> 500)
    result = check_orderbook_prior_gate(
        calibrated_prob=0.915, no_ask_cents=20, yes_asks=yes_asks,
        entry_price_cents=92,
    )
    assert result == "orderbook_prior_block", (
        f"Gate A should fire on SOL 92c production scenario, got {result!r}"
    )


# ── Gate B: HYPE high-price buf ───────────────────────────────────────────────

def test_gate_b_blocks_hype_99c_2026_05_21_production_scenario():
    """KXHYPE15M-26MAY210830-30 (2026-05-21 12:30 UTC, -$50.49 loss).
    At decision time:
      asset=HYPE, entry=99c, spot=57.35, threshold=57.0701
      bot_buf_pct = (57.35 - 57.0701) / 57.0701 * 100 = 0.490%
    Gate B must fire (entry ≥ 98 AND buf < 0.75)."""
    from bot.helpers.adverse_selection import check_hype_high_price_buf_gate
    result = check_hype_high_price_buf_gate(
        asset="HYPE", entry_price_cents=99, bot_buf_pct=0.490,
    )
    assert result == "hype_high_price_buf_block", (
        f"Gate B should fire on HYPE 99c production scenario, got {result!r}"
    )


def test_gate_b_blocks_hype_98c_2026_05_18_production_scenario():
    """KXHYPE15M-26MAY180530-30 (2026-05-18 09:30 UTC, -$56.84 per row, $117 total).
    At decision time:
      asset=HYPE, entry=98c, spot=45.71, threshold=45.5378
      bot_buf_pct = (45.71 - 45.5378) / 45.5378 * 100 = 0.378%
    Gate B must fire."""
    from bot.helpers.adverse_selection import check_hype_high_price_buf_gate
    result = check_hype_high_price_buf_gate(
        asset="HYPE", entry_price_cents=98, bot_buf_pct=0.378,
    )
    assert result == "hype_high_price_buf_block", (
        f"Gate B should fire on HYPE 98c production scenario, got {result!r}"
    )


def test_gate_b_does_not_fire_on_non_hype_assets():
    """Gate B is HYPE-only. SOL/BTC/XRP/DOGE/ETH/BNB at thin buf must pass."""
    from bot.helpers.adverse_selection import check_hype_high_price_buf_gate
    for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
        result = check_hype_high_price_buf_gate(
            asset=asset, entry_price_cents=99, bot_buf_pct=0.10,
        )
        assert result is None, (
            f"Gate B must be HYPE-only; fired on {asset} at 99c buf=0.10%"
        )


def test_gate_b_does_not_fire_below_entry_threshold():
    """Gate B requires entry ≥ 98c. HYPE at 95-97c must pass even at thin buf."""
    from bot.helpers.adverse_selection import check_hype_high_price_buf_gate
    for price in (95, 96, 97):
        result = check_hype_high_price_buf_gate(
            asset="HYPE", entry_price_cents=price, bot_buf_pct=0.10,
        )
        assert result is None, (
            f"Gate B must require entry >= 98; fired on HYPE at {price}c"
        )


def test_gate_b_does_not_fire_when_buf_above_threshold():
    """Gate B requires buf < 0.75%. HYPE 99c with buf=0.80% must pass."""
    from bot.helpers.adverse_selection import check_hype_high_price_buf_gate
    result = check_hype_high_price_buf_gate(
        asset="HYPE", entry_price_cents=99, bot_buf_pct=0.80,
    )
    assert result is None, (
        f"Gate B must NOT fire when buf=0.80% >= 0.75% threshold; got {result!r}"
    )


def test_gate_b_boundary_buf_exactly_at_threshold():
    """At buf = exactly 0.75%, gate should NOT fire (strict less-than)."""
    from bot.helpers.adverse_selection import check_hype_high_price_buf_gate
    result = check_hype_high_price_buf_gate(
        asset="HYPE", entry_price_cents=98, bot_buf_pct=0.75,
    )
    assert result is None, (
        f"Gate B strict less-than: buf=0.75% must NOT fire, got {result!r}"
    )


# ── Schema chain: filter_stage literals must be registered ────────────────────

def test_filter_stage_literals_registered_in_cohort_partition_stages():
    """Per cell-block discipline in bot/CLAUDE.md, every new filter_stage emitted
    by scanner must be added to `bot.helpers.cohort_attribution.COHORT_PARTITION_STAGES`,
    otherwise audit scripts filtering `filter_stage='candidate'` under-count."""
    from bot.helpers.cohort_attribution import COHORT_PARTITION_STAGES
    assert "orderbook_prior_block" in COHORT_PARTITION_STAGES, (
        "Gate A filter_stage 'orderbook_prior_block' missing from COHORT_PARTITION_STAGES"
    )
    assert "hype_high_price_buf_block" in COHORT_PARTITION_STAGES, (
        "Gate B filter_stage 'hype_high_price_buf_block' missing from COHORT_PARTITION_STAGES"
    )


# ── Constants ─────────────────────────────────────────────────────────────────

def test_b1_constants_locked_values():
    """B1 R0 (2026-05-21) locked thresholds per kb/decisions/b1-orderbook-prior-gate-plan.md.
    Values are data-justified — do not retune without a new R0 sim."""
    from bot import constants as C

    # Gate A
    assert C.ORDERBOOK_PRIOR_GATE_ENABLED is True
    assert C.ORDERBOOK_PRIOR_GATE_MIN_ENTRY_CENTS == 90
    assert C.ORDERBOOK_PRIOR_GATE_MIN_DISAGREE == 0.05
    assert C.ORDERBOOK_PRIOR_GATE_MIN_CONVICTION_CENTS == 500
    assert C.ORDERBOOK_PRIOR_GATE_MIN_NO_BID_PRICE == 2
    assert C.ORDERBOOK_PRIOR_GATE_FILTER_STAGE == "orderbook_prior_block"

    # Gate B
    assert C.HYPE_HIGH_PRICE_BUF_GATE_ENABLED is True
    assert C.HYPE_HIGH_PRICE_BUF_GATE_MIN_ENTRY_CENTS == 98
    assert C.HYPE_HIGH_PRICE_BUF_GATE_MIN_BUF_PCT == 0.75
    assert C.HYPE_HIGH_PRICE_BUF_GATE_FILTER_STAGE == "hype_high_price_buf_block"


# ── Orchestrator + ob_data extractor ──────────────────────────────────────────

def test_extract_no_ask_and_yes_asks_from_live_orderbook():
    """ob_data = {"yes": [...], "no": [...]} (Kalshi WS BID books).
    no_ask = 100 - best_yes_bid; yes_asks = inverted no_bid book."""
    from bot.helpers.adverse_selection import extract_no_ask_and_yes_asks
    ob_data = {
        "yes": [[90, 23], [89, 2], [88, 8]],   # best YES bid = 90
        "no": [[1, 39], [2, 71], [4, 104]],    # NO bids at 1,2,4
    }
    no_ask, yes_asks = extract_no_ask_and_yes_asks(ob_data)
    assert no_ask == 10, f"no_ask should be 100-90=10, got {no_ask}"
    # Sorted by yes_ask price ascending (per inversion: no_bid=1→yes_ask=99 etc.)
    sorted_asks = sorted(yes_asks)
    assert (96, 104) in sorted_asks  # no_bid=4, depth=104 → yes_ask=96
    assert (98, 71) in sorted_asks   # no_bid=2 → yes_ask=98
    assert (99, 39) in sorted_asks   # no_bid=1 → yes_ask=99


def test_extract_handles_missing_or_malformed_ob_data():
    from bot.helpers.adverse_selection import extract_no_ask_and_yes_asks
    assert extract_no_ask_and_yes_asks(None) == (None, [])
    assert extract_no_ask_and_yes_asks({}) == (None, [])
    assert extract_no_ask_and_yes_asks({"yes": [], "no": []}) == (None, [])
    # Garbage entries silently dropped
    no_ask, yes_asks = extract_no_ask_and_yes_asks({
        "yes": [["bad"], [None, 5], [True, 10], [50, 5]],
        "no": [[10, 3]],
    })
    assert no_ask == 50  # only [50, 5] survives parsing
    assert (90, 3) in yes_asks  # no_bid=10 → yes_ask=90


def test_orchestrator_fires_gate_b_first_for_hype():
    """When asset==HYPE + entry>=98 + buf<0.75%, Gate B fires regardless of
    orderbook state. Gate B short-circuits before Gate A is evaluated."""
    from bot.helpers.adverse_selection import check_15m_entry_gates
    # ob_data deliberately empty — would NOT trigger Gate A; only Gate B should fire
    result = check_15m_entry_gates(
        asset="HYPE", entry_price_cents=99, calibrated_prob=0.97,
        bot_buf_pct=0.490, ob_data=None,
    )
    assert result == "hype_high_price_buf_block"


def test_orchestrator_fires_gate_a_when_gate_b_passes():
    """When Gate B does NOT fire (non-HYPE asset), orchestrator evaluates Gate A."""
    from bot.helpers.adverse_selection import check_15m_entry_gates
    # Synthetic ob_data: NO bid book has strong conviction at non-trivial prices
    ob_data = {
        "yes": [[90, 23]],                   # best yes_bid=90 → no_ask=10
        "no": [[3, 80], [4, 150], [5, 100]], # conv_ge2 = 3*80+4*150+5*100 = 1340 >> 500
    }
    # cal_p=0.97, no_ask=10 → disagree=0.07 > 0.05 ✓
    result = check_15m_entry_gates(
        asset="DOGE", entry_price_cents=97, calibrated_prob=0.97,
        bot_buf_pct=0.50, ob_data=ob_data,
    )
    assert result == "orderbook_prior_block"


def test_orchestrator_passes_when_both_gates_clean():
    """Clean SOL 99c winner: HYPE-only Gate B doesn't apply; orderbook is thin/clean."""
    from bot.helpers.adverse_selection import check_15m_entry_gates
    ob_data = {
        "yes": [[99, 50], [98, 100]],   # tight book at top
        "no": [[1, 50]],                # only liquidity-maker bid at 1c, conv_ge2=0
    }
    result = check_15m_entry_gates(
        asset="SOL", entry_price_cents=99, calibrated_prob=0.97,
        bot_buf_pct=0.30, ob_data=ob_data,
    )
    assert result is None


# ── Kill-switch semantics: helpers are PURE would-block predicates ────────────

def test_helpers_do_not_check_enable_flag(monkeypatch):
    """B1 R1 fix-up (per adv-review R1 finding C1): helpers are pure would-block
    predicates and DO NOT short-circuit on *_GATE_ENABLED. The kill-switch is
    scanner-side so shadow rows continue to log during rollback for counterfactual
    measurement. Mirrors the TM96 cal_mlp gate R-p7-deploy-r10 precedent."""
    import bot.constants as C
    from bot.helpers import adverse_selection

    # Disable both gates at the constants module.
    monkeypatch.setattr(C, "ORDERBOOK_PRIOR_GATE_ENABLED", False)
    monkeypatch.setattr(C, "HYPE_HIGH_PRICE_BUF_GATE_ENABLED", False)

    # Gate A would still fire — helper ignores enable flag.
    result_a = adverse_selection.check_orderbook_prior_gate(
        calibrated_prob=0.97, no_ask_cents=10, yes_asks=[(95, 1000)],
        entry_price_cents=99,
    )
    assert result_a == "orderbook_prior_block", (
        "helper must return would-block verdict regardless of *_ENABLED — "
        "scanner gates trade-block on the flag; shadow rows MUST still log"
    )

    # Gate B would still fire too.
    result_b = adverse_selection.check_hype_high_price_buf_gate(
        asset="HYPE", entry_price_cents=99, bot_buf_pct=0.490,
    )
    assert result_b == "hype_high_price_buf_block", (
        "Gate B helper must also ignore *_ENABLED — shadow-log invariant"
    )
