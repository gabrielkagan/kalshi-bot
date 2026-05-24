"""F0.5 — Settlement-window gamma falsification test suite.

TDD-first scaffold per `CLAUDE.md` extraction-bit discipline. Lands BEFORE
implementation. All invariants pinned here are RED at scaffold-ship (the
script is a stub raising NotImplementedError); they flip GREEN as the
implementation lands in subsequent commits per TDD-first staging.

Precedent: F0.1 (`tests/research/test_f0_1_stale_quote_falsification.py`,
SHIPPED 2026-05-20, PR #132, VERDICT SURVIVE) — F0.5 mirrors the
failing-assertion-scaffold + per-invariant-test pattern.

Invariants pinned here (per plan-doc § Methodological invariants):

1. Schema invariants — required columns exist in settled_trades + moc +
   evaluated_opportunities. 7-asset universe pinned in-script via
   ASSET_TICKER_PREFIX (15M-specific prefix `KX<ASSET>15M`; the trailing
   hyphen is NOT part of the prefix value per SERIES_TICKERS).
2. No-look-ahead — for a window with close at t_close, the T-Xs state must
   use only rows with observation_time ≤ t_close - X. Settle outcome is the
   label, never a feature.
3. Regime conditioning — vol × day/night = 4 cells per asset (moneyness
   retracted at impl-R1 — Kalshi 15M tickers do not encode strike, see
   the test body of test_cells_partition_into_vol_x_daynight for full
   RCA); per-cell AUC reported separately. Kill verdict is per-cell.
4. AUC bounded [0.5, 1.0] after orientation flip — AUC < 0.5 means the
   classifier predicts the wrong direction; flip and report 1 - raw_AUC.
   Degenerate input (single-class fold) must error rather than silently
   return AUC=0.5.
5. Min sample size per cell — ≥30 windows; below that, label cell
   "insufficient" and exclude from kill/survive computation.
6. Kelly-sizing source — sim PnL imports Kelly from the canonical bot
   helper; flat-1-contract sizing is forbidden (per `CLAUDE.md` no-flat-1
   rule).
7. Settle-buffer guard — window close = settled_at - settle_processing_delay
   (default 5s); T-Xs samples must respect this buffer to avoid label leakage.

Parent plan: kb/decisions/ct-mdp-f0-5-settlement-window-gamma-plan.md
Parent ClickUp: 86ba18zhr
"""

from __future__ import annotations

import pytest

# Plain import — NOT importorskip — so any future import-time error in
# the script (SyntaxError, missing dep, etc.) FAILS rather than silently
# greens the module. Mirrors F0.1 scaffold-R1-M4 precedent.
from scripts.research import f0_5_settlement_window_gamma as gamma


# ----- Schema invariants (Invariant 1) -----------------------------------


def test_settled_trades_required_columns_exposed():
    """settled_trades must expose ticker, asset, market_result, settled_at, product_type."""
    required = {
        "ticker", "asset", "market_result", "settled_at", "product_type",
    }
    cols = getattr(gamma, "SETTLED_TRADES_REQUIRED_COLUMNS", None)
    assert cols is not None, \
        "SETTLED_TRADES_REQUIRED_COLUMNS not yet defined in script (scaffold-pending)"
    assert required.issubset(set(cols)), \
        f"missing settled_trades columns: {required - set(cols)}"


def test_moc_required_columns_exposed():
    """moc must expose ticker, observation_time, yes_bid/ask, no_bid/ask, bid/ask_depth, source, cache_age_ms.

    Mirrors the canonical schema in `bot/snapshots/market_observations_snapshotter.py`
    `_DDL` block: id + ticker + observation_time + yes_bid_cents + yes_ask_cents +
    no_bid_cents + no_ask_cents + bid_depth + ask_depth + source + cache_age_ms.
    `source` is needed to distinguish moc-write provenance (ws_book / ws_orderbook_delta);
    `cache_age_ms` is the freshness signal F0.1's stale-quote analysis depends on.
    """
    required = {
        "ticker", "observation_time",
        "yes_bid_cents", "yes_ask_cents",
        "no_bid_cents", "no_ask_cents",
        "bid_depth", "ask_depth",
        "source", "cache_age_ms",
    }
    cols = getattr(gamma, "MOC_REQUIRED_COLUMNS", None)
    assert cols is not None, "MOC_REQUIRED_COLUMNS not yet defined (scaffold-pending)"
    assert required.issubset(set(cols)), \
        f"missing moc columns: {required - set(cols)}"


def test_eval_opps_spot_columns_exposed():
    """evaluated_opportunities must expose per-asset spot_at_decision columns."""
    required_spot = {
        "btc_spot_at_decision", "eth_spot_at_decision", "sol_spot_at_decision",
        "xrp_spot_at_decision", "hype_spot_at_decision", "doge_spot_at_decision",
        "bnb_spot_at_decision",
    }
    cols = getattr(gamma, "EVAL_OPPS_SPOT_COLUMNS", None)
    assert cols is not None, "EVAL_OPPS_SPOT_COLUMNS not yet defined (scaffold-pending)"
    assert required_spot.issubset(set(cols)), \
        f"missing eval_opps spot columns: {required_spot - set(cols)}"


def test_seven_asset_universe_pinned_with_15m_prefix():
    """7-asset universe pinned in-script with the 15M-specific ticker prefix.

    Mirrors `bot.constants.SERIES_TICKERS` (values are `"KXBTC15M"`,
    `"KXETH15M"`, ..., `"KXBNB15M"`). Per impl-R11-M1 correction: the
    prefix is NOT used to construct a LIKE clause against
    `settled_trades.ticker`. The script's 15M-window discrimination uses
    the `WHERE product_type='15m'` SQL filter (Kalshi's canonical
    product_type enum); a separate per-asset prefix-LIKE would be
    redundant. ASSET_TICKER_PREFIX is used only as (a) the iteration
    key set in `_load_spot_series_by_asset` and (b) the anti-drift pin
    against SERIES_TICKERS. The 15M-specific value naming (`KX<ASSET>15M`,
    distinct from hourly `KX<ASSET>`) is preserved to make the anti-drift
    pin reject hourly-prefix promotions of SERIES_TICKERS in the future —
    F0.5 is a terminal-condition HJB on 15M windows only per umbrella
    plan-doc § Attack #5.
    """
    expected = {"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"}
    prefix_map = getattr(gamma, "ASSET_TICKER_PREFIX", None)
    assert prefix_map is not None, "ASSET_TICKER_PREFIX not yet defined (scaffold-pending)"
    assert set(prefix_map.keys()) == expected
    # 15M-specific prefix enforced — NOT the broader KX<ASSET> form.
    for asset, prefix in prefix_map.items():
        assert prefix.endswith("15M"), \
            f"{asset} prefix {prefix!r} is not 15M-specific (would admit hourly/daily)"


# ----- No-look-ahead (Invariant 2) ---------------------------------------


def test_sample_state_at_t_minus_x_excludes_post_event_rows():
    """For a window closing at t_close, state at T-Xs must use only rows with observation_time ≤ t_close - X."""
    # Synthetic: window close at 10:00:00Z, sample T-60s (state at 09:59:00Z).
    # moc rows at 09:58:00Z (pre), 09:59:30Z (between, post-T-60s), 10:00:30Z (post-close).
    sample_fn = getattr(gamma, "sample_state_at_timestep", None)
    assert sample_fn is not None, "sample_state_at_timestep not yet defined (scaffold-pending)"

    # Synthetic moc rows use the canonical schema columns (yes_bid_cents +
    # yes_ask_cents). mid is derived inline as (yes_bid + yes_ask) / 2 by
    # any downstream consumer that needs it — moc table itself does NOT
    # store mid_cents (see `bot/snapshots/market_observations_snapshotter.py`
    # `_DDL`).
    rows = sample_fn(
        moc_rows=[
            {"observation_time": "2026-05-20T09:58:00Z", "yes_bid_cents": 48, "yes_ask_cents": 52},
            {"observation_time": "2026-05-20T09:59:30Z", "yes_bid_cents": 53, "yes_ask_cents": 57},
            {"observation_time": "2026-05-20T10:00:30Z", "yes_bid_cents": 58, "yes_ask_cents": 62},
        ],
        window_close="2026-05-20T10:00:00Z",
        timestep_s=60,
    )
    # No row strictly after t_close - 60s may appear.
    parse = getattr(gamma, "_parse_iso", None)
    assert parse is not None, "_parse_iso helper not yet defined (scaffold-pending)"
    cutoff = parse("2026-05-20T09:59:00Z")
    for row in rows:
        assert parse(row["observation_time"]) <= cutoff, \
            f"look-ahead violation at T-60s sample: {row['observation_time']}"


def test_settle_outcome_never_used_as_feature():
    """The settle outcome y_w is a LABEL, not a feature. Any feature column derived from data at t > window_close must error."""
    extract_features = getattr(gamma, "extract_state_features", None)
    assert extract_features is not None, \
        "extract_state_features not yet defined (scaffold-pending)"

    # Construct a synthetic "tainted" input where a feature claims to derive
    # from post-close data. The function must reject it.
    with pytest.raises(ValueError, match="look-ahead|post.close|label.leak"):
        extract_features(
            moc_row_at_t_xs={"observation_time": "2026-05-20T09:59:00Z", "yes_bid_cents": 53, "yes_ask_cents": 57},
            window_close="2026-05-20T10:00:00Z",
            timestep_s=60,
            tainted_post_close_row={"observation_time": "2026-05-20T10:00:30Z", "yes_bid_cents": 58, "yes_ask_cents": 62},
        )


# ----- Regime conditioning (Invariant 3) ----------------------------------


def test_cells_partition_into_vol_x_daynight():
    """4 cells per asset = vol(high/low) × day/night.

    Moneyness dimension retracted at impl-R1 (2026-05-21). Plan-doc § Method
    step 4 originally specified 8 cells per asset including a moneyness
    (itm/otm) axis via `|spot - strike| / strike < 0.005`. **Kalshi 15M
    tickers do NOT encode strike** — empirically the trailing `-MM` segment
    on every settled 15M ticker is the close-MINUTE (00/15/30/45), not a
    strike index. Without strike, moneyness cannot be computed from
    settled_trades alone; the cell partition reduces to 4 cells per asset.
    """
    cells_fn = getattr(gamma, "enumerate_cells", None)
    assert cells_fn is not None, "enumerate_cells not yet defined (scaffold-pending)"

    cells = cells_fn()
    assert len(cells) == 4, f"expected 4 cells per asset, got {len(cells)}"
    # Spot-check label shape (vol × day_night).
    labels = {tuple(c) for c in cells}
    assert ("vol_high", "day") in labels
    assert ("vol_low", "night") in labels


# ----- AUC bounded (Invariant 4) ------------------------------------------


def test_auc_below_half_flips_orientation():
    """Raw AUC < 0.5 means classifier predicts wrong direction; report 1 - raw_AUC."""
    compute_auc = getattr(gamma, "compute_auc_with_orientation", None)
    assert compute_auc is not None, \
        "compute_auc_with_orientation not yet defined (scaffold-pending)"

    # Synthetic: classifier perfectly anti-correlated with label → raw AUC = 0.0,
    # flipped AUC = 1.0.
    auc = compute_auc(
        y_true=[0, 0, 0, 1, 1, 1],
        y_score=[0.9, 0.8, 0.7, 0.3, 0.2, 0.1],  # higher score on label=0
    )
    assert 0.5 <= auc <= 1.0, f"AUC {auc} not in [0.5, 1.0] after orientation flip"


def test_degenerate_single_class_fold_errors():
    """Single-class fold (all y=0 or all y=1) makes AUC undefined; must error."""
    compute_auc = getattr(gamma, "compute_auc_with_orientation", None)
    assert compute_auc is not None, \
        "compute_auc_with_orientation not yet defined (scaffold-pending)"

    with pytest.raises(ValueError, match="single.class|degenerate|undefined"):
        compute_auc(
            y_true=[0, 0, 0, 0],
            y_score=[0.1, 0.2, 0.3, 0.4],
        )


# ----- Min sample size per cell (Invariant 5) -----------------------------


def test_cell_with_fewer_than_30_events_labeled_insufficient():
    """Cells with <30 events must be labeled 'insufficient' and excluded from kill/survive."""
    classify = getattr(gamma, "classify_cell_sufficiency", None)
    assert classify is not None, \
        "classify_cell_sufficiency not yet defined (scaffold-pending)"

    assert classify(n_events=29) == "insufficient"
    assert classify(n_events=30) == "sufficient"
    assert classify(n_events=100) == "sufficient"


# ----- Kelly-sizing source (Invariant 6) ----------------------------------


def test_sim_pnl_uses_kelly_sizing_not_flat_one():
    """Sim PnL must use actual Kelly sizing (per CLAUDE.md no-flat-1 rule).

    Failing-assertion contract: a 1-contract input should NOT produce
    pnl = 1 × edge (which would be the flat-1 fingerprint). Real Kelly
    sizing accounts for edge magnitude + bankroll + risk caps.
    """
    sim_fn = getattr(gamma, "simulate_pnl_with_kelly", None)
    assert sim_fn is not None, \
        "simulate_pnl_with_kelly not yet defined (scaffold-pending)"

    # Synthetic single-event input. If the impl reimplements flat-1 inline,
    # the resulting PnL equals (settle_revenue - entry_price) × 1 contract.
    # Real Kelly: bankroll-scaled size > 1 for a high-edge setup.
    pnl = sim_fn(
        events=[{"entry_price_cents": 30, "settle_cents": 100, "edge": 0.40}],
        bankroll_dollars=10000.0,
    )
    flat_one_pnl = (100 - 30)  # = 70 cents (= 1 contract flat)
    assert pnl != flat_one_pnl, \
        f"PnL {pnl} matches flat-1-contract fingerprint — Kelly sizing not engaged"


# ----- Settle-buffer guard (Invariant 7) ----------------------------------


def test_window_close_uses_settle_buffer():
    """window_close = settled_at - settle_processing_delay (≥5s) to avoid label leakage."""
    compute_close = getattr(gamma, "compute_window_close", None)
    assert compute_close is not None, \
        "compute_window_close not yet defined (scaffold-pending)"

    settled_at = "2026-05-20T10:00:05Z"
    close = compute_close(settled_at=settled_at, settle_processing_delay_s=5)
    # Window close should be 5s before the settle print.
    parse = getattr(gamma, "_parse_iso", None)
    assert parse is not None, "_parse_iso helper not yet defined (scaffold-pending)"
    delta = (parse(settled_at) - parse(close)).total_seconds()
    assert delta == 5.0, \
        f"settle-buffer not applied: settled_at - close = {delta}s (expected 5s)"


def test_settle_buffer_below_minimum_rejected():
    """Settle processing delay < 5s is rejected (conservative buffer per plan-doc § RCA Risk-2)."""
    compute_close = getattr(gamma, "compute_window_close", None)
    assert compute_close is not None, \
        "compute_window_close not yet defined (scaffold-pending)"

    with pytest.raises(ValueError, match="settle_processing_delay|buffer"):
        compute_close(
            settled_at="2026-05-20T10:00:05Z",
            settle_processing_delay_s=1,  # too small
        )


# ----- Verdict mapping ----------------------------------------------------


def test_verdict_kill_when_every_cell_below_threshold():
    """No cell with auc_lower ≥ 0.55 → KILL (binary complement of SURVIVE per
    classify_verdict; the umbrella's stricter upper-CI < 0.55 framing is a
    subset of the actual-code KILL definition and is not pinned at the impl —
    see plan-doc § Kill threshold table for the canonical statement)."""
    classify = getattr(gamma, "classify_verdict", None)
    assert classify is not None, "classify_verdict not yet defined (scaffold-pending)"

    cells = [
        {"asset": "BTC", "cell": "vol_high_day", "auc_lower": 0.48, "auc_upper": 0.53},
        {"asset": "ETH", "cell": "vol_low_night", "auc_lower": 0.49, "auc_upper": 0.54},
    ]
    verdict = classify(cells=cells, auc_threshold=0.55)
    assert verdict == "KILL"


def test_verdict_survive_when_one_cell_clears_threshold():
    """At least one cell with auc_lower ≥ 0.55 → SURVIVE."""
    classify = getattr(gamma, "classify_verdict", None)
    assert classify is not None, "classify_verdict not yet defined (scaffold-pending)"

    cells = [
        {"asset": "BTC", "cell": "vol_high_day", "auc_lower": 0.48, "auc_upper": 0.53},
        {"asset": "SOL", "cell": "vol_high_night", "auc_lower": 0.56, "auc_upper": 0.62},  # survivor
    ]
    verdict = classify(cells=cells, auc_threshold=0.55)
    assert verdict == "SURVIVE"


# ----- Full pipeline wiring -----------------------------------------------


def test_main_returns_expected_keys():
    """End-to-end main() emits per-cell AUC + verdict dict shape pinned for downstream consumers.

    Presence-only at scaffold-ship; full-pipeline shape lands at impl-Bit
    (F0.1 chose a heavier scaffold-ship pipeline test; F0.5 defers the
    synthetic-DB fixture to impl). Once `main` exists as a callable, this
    test flips GREEN.
    """
    main_fn = getattr(gamma, "main", None)
    assert main_fn is not None, "main() not yet defined (scaffold-pending)"


def test_cv_uses_forward_chaining_temporal_split():
    """Pipeline-level no-look-ahead pin (Invariant 2 at the CV-fold layer).

    Plan-doc § Method step 5 + § Methodological invariants ¶ k-fold:
    "Use k-fold cross-validation (k=5) for AUC estimation with strict
    temporal ordering of folds (no look-ahead across folds — fold k
    trains on windows with settled_at < fold-k boundary only)."

    The initial impl-R1 ship used `sklearn.model_selection.KFold(shuffle=False)`
    which is NOT forward-chaining (only 1 of 5 folds honors the temporal
    rule). The correct primitive is `TimeSeriesSplit`. This contract
    AST-asserts the script imports + uses `TimeSeriesSplit` so a future
    revert to plain `KFold` fails RED.
    """
    import ast
    import inspect

    src = inspect.getsource(gamma)
    tree = ast.parse(src)

    # Walk imports.
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported_names.add(f"{module}.{alias.name}")

    # The forward-chaining temporal CV primitive must be imported AND
    # actually referenced from main()'s CV-instantiation site. We require
    # BOTH the import + a textual `TimeSeriesSplit(` call to guard against
    # an import without use.
    assert (
        "sklearn.model_selection.TimeSeriesSplit" in imported_names
    ), (
        "F0.5 pipeline must import `TimeSeriesSplit` from sklearn.model_selection "
        "for forward-chaining temporal CV per plan-doc § Method step 5. Plain "
        "`KFold` is NOT forward-chaining (only 1 of n folds honors the temporal "
        "invariant). Imports found: "
        f"{sorted(n for n in imported_names if n.startswith('sklearn.'))}"
    )
    assert "TimeSeriesSplit(" in src, (
        "F0.5 imports TimeSeriesSplit but never instantiates it — guard "
        "against accidental import-only revert."
    )
    # Defense-in-depth: forbid `KFold(` instantiation (the bug we fixed).
    # Allow `KFold` mentions in comments / docstrings (which would not
    # match the `(` instantiation pattern).
    assert "KFold(" not in src, (
        "F0.5 must NOT instantiate plain `KFold` — see impl-R1-C2; use "
        "`TimeSeriesSplit` for forward-chaining temporal CV."
    )
