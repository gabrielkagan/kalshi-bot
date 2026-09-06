"""P1a — Settlement-convergence proxy edge: TDD-first test suite.

TDD-first per `CLAUDE.md` extraction-bit discipline + the WWJD/Principle-0
gate in `kb/decisions/settlement-lag-convergence-edge-spike-plan.md`. Lands
BEFORE the analysis trusts any number. The methodology-critical helpers
(partial running settlement average, remaining-move bound / lock decision,
side prediction) are pinned here against HAND-CHECKED fixtures — exactly the
surfaces where backtest conclusions in this repo have flipped (look-ahead,
temporal alignment, endpoint handling). Precedent:
`tests/research/test_f0_5_settlement_window_gamma.py` (sibling spike).

Mechanic pinned at Phase 0 (3 independent sources): Kalshi 15M settles to the
ARITHMETIC MEAN of the 60 per-second CF Benchmarks RTI readings over the final
60s before expiry. ⇒ at decision time T-Xs (X ≤ 60) the portion of the
settlement in [close-60s, T] is already LOCKED; only the remaining X seconds
are unknown.

Parent plan: kb/decisions/settlement-lag-convergence-edge-spike-plan.md
Parent ClickUp: 86ba747mc (Spike P1) under umbrella 86ba747ke
"""

from __future__ import annotations

import pytest

from scripts.research import settlement_convergence_p1a as scp


# ----- Timestamp parsing --------------------------------------------------


def test_parse_iso_handles_z_suffix_and_micros():
    parse = scp._parse_iso
    a = parse("2026-05-20T09:59:00Z")
    b = parse("2026-05-20T09:59:00.500000Z")
    assert (b - a).total_seconds() == 0.5


# ----- 7-asset universe pin (anti-drift, mirrors f0_5) --------------------


def test_crypto_15m_asset_universe_pinned_with_15m_prefix():
    # All 11 Kalshi 15M crypto series the bot knows: the 7 it trades + ADA/BCH
    # (shadow onboarding T1 #156, 2026-05-30) + NEAR/ZEC (shadow T1 2026-09-05,
    # 86bbvdc8y). The platform handles all 11; shadow assets are registered
    # series (markets list when Kalshi schedules windows).
    expected = {"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH", "NEAR", "ZEC"}  # NEAR/ZEC T1 shadow 2026-09-05 (86bbvdc8y)
    prefix_map = getattr(scp, "ASSET_TICKER_PREFIX", None)
    assert prefix_map is not None, "ASSET_TICKER_PREFIX not yet defined (scaffold-pending)"
    assert set(prefix_map.keys()) == expected
    for asset, prefix in prefix_map.items():
        assert prefix.endswith("15M"), \
            f"{asset} prefix {prefix!r} is not 15M-specific (would admit hourly/daily)"


def test_position_price_obs_required_columns_exposed():
    required = {
        "ticker", "asset", "observation_time", "seconds_to_close",
        "spot_price", "threshold", "yes_ask_cents", "yes_bid_cents",
    }
    cols = getattr(scp, "PPO_REQUIRED_COLUMNS", None)
    assert cols is not None, "PPO_REQUIRED_COLUMNS not yet defined (scaffold-pending)"
    assert required.issubset(set(cols)), f"missing ppo columns: {required - set(cols)}"


# ----- Partial running settlement average: NO LOOK-AHEAD ------------------


def test_partial_settlement_average_excludes_post_decision_readings():
    """Window [09:59:00,10:00:00], decision 09:59:45. Eligible @ :00=100, :30=102,
    :45=98 -> mean=100.0. The :55=90 reading is post-decision; including it
    (look-ahead bug) would drag the mean to 97.5 and flip the prediction."""
    fn = getattr(scp, "partial_settlement_average", None)
    assert fn is not None, "partial_settlement_average not yet defined (scaffold-pending)"
    observations = [
        {"observation_time": "2026-05-20T09:59:00Z", "spot_price": 100.0},
        {"observation_time": "2026-05-20T09:59:30Z", "spot_price": 102.0},
        {"observation_time": "2026-05-20T09:59:45Z", "spot_price": 98.0},
        {"observation_time": "2026-05-20T09:59:55Z", "spot_price": 90.0},  # post-decision
    ]
    avg = fn(observations, close_time="2026-05-20T10:00:00Z",
             decision_time="2026-05-20T09:59:45Z", window_seconds=60)
    assert avg == pytest.approx(100.0), f"expected 100.0 (no look-ahead), got {avg}"


def test_partial_settlement_average_excludes_pre_window_readings():
    fn = scp.partial_settlement_average
    observations = [
        {"observation_time": "2026-05-20T09:58:30Z", "spot_price": 50.0},   # pre-window
        {"observation_time": "2026-05-20T09:59:30Z", "spot_price": 100.0},
    ]
    avg = fn(observations, close_time="2026-05-20T10:00:00Z",
             decision_time="2026-05-20T09:59:45Z", window_seconds=60)
    assert avg == pytest.approx(100.0), f"pre-window reading leaked: got {avg}"


def test_partial_settlement_average_none_before_final_minute():
    fn = scp.partial_settlement_average
    observations = [{"observation_time": "2026-05-20T09:58:30Z", "spot_price": 100.0}]
    avg = fn(observations, close_time="2026-05-20T10:00:00Z",
             decision_time="2026-05-20T09:58:30Z", window_seconds=60)
    assert avg is None, f"expected None before final minute, got {avg}"


def test_partial_settlement_average_rejects_decision_after_close():
    fn = scp.partial_settlement_average
    with pytest.raises(ValueError, match="after close|look-ahead|decision"):
        fn([{"observation_time": "2026-05-20T10:00:30Z", "spot_price": 100.0}],
           close_time="2026-05-20T10:00:00Z",
           decision_time="2026-05-20T10:00:30Z", window_seconds=60)


# ----- Side prediction ----------------------------------------------------


def test_predict_side_above_below_and_tie():
    fn = getattr(scp, "predict_side", None)
    assert fn is not None, "predict_side not yet defined (scaffold-pending)"
    assert fn(estimate=100.25, threshold=100.0) == "yes"
    assert fn(estimate=99.0, threshold=100.0) == "no"
    assert fn(estimate=100.0, threshold=100.0) == "no"  # tie settles NO


# ----- Remaining-move bound / lock decision -------------------------------


def test_is_locked_true_when_worst_case_future_cannot_flip():
    fn = getattr(scp, "is_locked", None)
    assert fn is not None, "is_locked not yet defined (scaffold-pending)"
    assert fn(locked_sum=5025.0, n_locked=50, n_total=60,
              future_extreme=98.0, threshold=100.0, side="yes") is True


def test_is_locked_false_when_worst_case_future_flips():
    fn = scp.is_locked
    assert fn(locked_sum=5025.0, n_locked=50, n_total=60,
              future_extreme=97.0, threshold=100.0, side="yes") is False


# ----- The thesis, end-to-end on a hand-checked late-reversal window -------


def test_convergence_beats_instantaneous_on_late_reversal():
    """Window [09:59:00,10:00:00], strike 100. Spot 100.5 for first 50s, reverses
    to 99.0 for last 10s. Settlement=(50*100.5+10*99.0)/60=100.25 -> YES. At T-10s
    instantaneous spot is 99.0 -> NO (wrong); partial average is >100 -> YES (right)."""
    evaluate = getattr(scp, "evaluate_window", None)
    assert evaluate is not None, "evaluate_window not yet defined (scaffold-pending)"
    obs = (
        [{"observation_time": f"2026-05-20T09:59:{s:02d}Z", "spot_price": 100.5} for s in range(0, 50)]
        + [{"observation_time": f"2026-05-20T09:59:{s:02d}Z", "spot_price": 99.0} for s in range(50, 60)]
    )
    res = evaluate(observations=obs, close_time="2026-05-20T10:00:00Z",
                   decision_time="2026-05-20T09:59:50Z", threshold=100.0,
                   actual_result="yes", window_seconds=60)
    assert res["convergence_side"] == "yes"
    assert res["instantaneous_side"] == "no"
    assert res["convergence_correct"] is True
    assert res["instantaneous_correct"] is False


def test_main_callable():
    assert getattr(scp, "main", None) is not None, "main() not yet defined (scaffold-pending)"


# ----- Lock decision (the actual Layer-1 mechanism) -----------------------


def test_lock_decision_locked_when_tight_bound():
    """Steady 100.5 for first 51s, strike 100, decision T-10s. Tight 1.0 bound:
    final_worst=(51*100.5+9*99.5)/60=100.35 > 100 -> LOCKED."""
    fn = getattr(scp, "lock_decision", None)
    assert fn is not None, "lock_decision not yet defined (scaffold-pending)"
    obs = [{"observation_time": f"2026-05-20T09:59:{s:02d}Z", "spot_price": 100.5}
           for s in range(0, 51)]
    assert fn(observations=obs, close_time="2026-05-20T10:00:00Z",
              decision_time="2026-05-20T09:59:50Z", threshold=100.0,
              side="yes", move_bound=1.0, n_total=60) == "locked"


def test_lock_decision_not_locked_when_loose_bound():
    """Same window, wide 10.0 bound: final_worst=(51*100.5+9*90.5)/60=99.0 < 100 -> NOT locked."""
    fn = scp.lock_decision
    obs = [{"observation_time": f"2026-05-20T09:59:{s:02d}Z", "spot_price": 100.5}
           for s in range(0, 51)]
    assert fn(observations=obs, close_time="2026-05-20T10:00:00Z",
              decision_time="2026-05-20T09:59:50Z", threshold=100.0,
              side="yes", move_bound=10.0, n_total=60) == "not_locked"


def test_lock_decision_no_data_before_final_minute():
    fn = scp.lock_decision
    obs = [{"observation_time": "2026-05-20T09:58:30Z", "spot_price": 100.5}]
    assert fn(observations=obs, close_time="2026-05-20T10:00:00Z",
              decision_time="2026-05-20T09:58:30Z", threshold=100.0,
              side="yes", move_bound=1.0, n_total=60) == "no_data"


# ----- Economics primitives (the make-or-break) ---------------------------


def test_kalshi_fee_per_contract_is_tiny_at_extremes_large_in_middle():
    """Kalshi fee = 0.07 * P * (1-P) per contract (large-order limit, cents).
    Near-certain (99c) markets cost ~0.07c; coin-flips (50c) cost ~1.75c."""
    fee = getattr(scp, "kalshi_fee_per_contract_cents", None)
    assert fee is not None, "kalshi_fee_per_contract_cents not yet defined (scaffold-pending)"
    assert fee(99) == pytest.approx(7 * 0.99 * 0.01, abs=1e-6)   # ~0.0693c
    assert fee(50) == pytest.approx(7 * 0.50 * 0.50, abs=1e-6)   # 1.75c
    assert fee(99) < 0.1 < fee(50)


def test_realized_pnl_cents_win_and_loss():
    """Buy YES at price p: win -> (100-p), lose -> (-p)  [gross of fees]."""
    pnl = getattr(scp, "realized_pnl_cents", None)
    assert pnl is not None, "realized_pnl_cents not yet defined (scaffold-pending)"
    assert pnl(price_cents=98, won=True) == pytest.approx(2.0)
    assert pnl(price_cents=98, won=False) == pytest.approx(-98.0)


# ----- Staleness guard (kills the sparse-corpus stale-price artifact) ------


def test_spot_at_or_before_respects_max_staleness():
    """A pre-decision reading older than max_staleness_s must NOT be used —
    otherwise a sparse corpus pairs a stale cheap early price with the final
    outcome (the evalopps +18c artifact)."""
    fn = scp._spot_at_or_before
    obs = [{"observation_time": "2026-05-20T09:58:00Z", "spot_price": 100.0}]  # 120s stale
    assert fn(obs, "2026-05-20T10:00:00Z", max_staleness_s=20) is None
    assert fn(obs, "2026-05-20T10:00:00Z", max_staleness_s=200) == 100.0
    assert fn(obs, "2026-05-20T10:00:00Z") == 100.0  # no guard -> unchanged


def test_yes_ask_at_or_before_respects_max_staleness():
    fn = scp._yes_ask_at_or_before
    obs = [{"observation_time": "2026-05-20T09:58:00Z", "yes_ask_cents": 85}]  # 120s stale
    assert fn(obs, "2026-05-20T10:00:00Z", max_staleness_s=20) is None
    assert fn(obs, "2026-05-20T10:00:00Z", max_staleness_s=200) == 85


# ----- Transactability gate (R1-C1: crossed-book asks are not fillable) ----


def test_is_transactable_quote_rejects_crossed_and_no_upside():
    """R1-C1: a recorded ask BELOW the contemporaneous bid is not a price any
    order could fill at; an ask of 100 has zero upside. Both must be rejected."""
    fn = getattr(scp, "is_transactable_quote", None)
    assert fn is not None, "is_transactable_quote not yet defined (scaffold-pending)"
    assert fn(ask=98, bid=97) is True       # normal 1c spread
    assert fn(ask=98, bid=98) is True        # zero spread, still coherent
    assert fn(ask=91, bid=99) is False       # CROSSED (ask<bid) -> artifact
    assert fn(ask=100, bid=99) is False      # no upside
    assert fn(ask=None, bid=97) is False     # missing ask
    assert fn(ask=98, bid=None) is False     # missing bid (can't check coherence)
    assert fn(ask=0, bid=0) is False         # degenerate


def test_quote_at_or_before_returns_same_row_pair_no_lookahead():
    """R2-M2: the C1 fix's contemporaneity guarantee. The (ask,bid) must come
    from a SINGLE row at-or-before the decision — NOT ask from one row stitched
    to bid from another (which is what made the scalar fields crossable). A
    crossed same-row pair must survive intact so the gate can reject it."""
    fn = getattr(scp, "_quote_at_or_before", None)
    assert fn is not None, "_quote_at_or_before not yet defined (scaffold-pending)"
    obs = [
        {"observation_time": "2026-05-20T09:59:00Z", "yes_ask_cents": 98, "yes_bid_cents": 97},
        {"observation_time": "2026-05-20T09:59:30Z", "yes_ask_cents": 91, "yes_bid_cents": 99},  # crossed, most recent <= decision
        {"observation_time": "2026-05-20T09:59:55Z", "yes_ask_cents": 50, "yes_bid_cents": 40},  # post-decision
    ]
    ask, bid = fn(obs, "2026-05-20T09:59:45Z")
    assert (ask, bid) == (91, 99), "must return the contemporaneous (crossed) pair, post-decision excluded"
    assert scp.is_transactable_quote(ask, bid) is False  # flows through to rejection


def test_quote_at_or_before_does_not_stitch_bid_from_another_row():
    """R3-MN2: the load-bearing same-row property. The newest ask-bearing row
    has NO bid; a STITCHED impl would borrow the bid from the earlier row (95,97
    -> transactable). The correct same-row impl returns (95, None) -> rejected.
    This is the case that actually discriminates same-row from stitched."""
    obs = [
        {"observation_time": "2026-05-20T09:59:00Z", "yes_ask_cents": 98, "yes_bid_cents": 97},
        {"observation_time": "2026-05-20T09:59:40Z", "yes_ask_cents": 95},  # newest ask, NO bid this row
    ]
    ask, bid = scp._quote_at_or_before(obs, "2026-05-20T09:59:45Z")
    assert (ask, bid) == (95, None), "must NOT stitch bid=97 from the 09:59:00 row"
    assert scp.is_transactable_quote(ask, bid) is False


def test_quote_at_or_before_staleness_and_empty_return_none_pair():
    fn = scp._quote_at_or_before
    obs = [{"observation_time": "2026-05-20T09:58:00Z", "yes_ask_cents": 98, "yes_bid_cents": 97}]  # 120s stale
    assert fn(obs, "2026-05-20T10:00:00Z", max_staleness_s=20) == (None, None)
    assert fn(obs, "2026-05-20T10:00:00Z", max_staleness_s=200) == (98, 97)
    assert fn([], "2026-05-20T10:00:00Z") == (None, None)
