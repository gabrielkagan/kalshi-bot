"""Sprint B Bit B.1a: rejected_opportunities feature enrichment.

ClickUp ticket: 86b9vfzjp. Shipped 2026-05-12.

Adds 7 training-data columns to `rejected_opportunities` so a future
gate-policy learner can be trained on rejected rows (with proper
features, not just the 9-col base schema):

  sigma_winsorize REAL        — winsorized spot_distance_to_strike_sigma,
                                clipped to ±SIGMA_WINSOR_ABS_CAP (=25.0)
                                via canonical helper to preserve
                                train/serve invariant (the cal_mlp
                                lock-step surface).
  hour_sin REAL, hour_cos REAL — analytical 24h cyclic embedding,
                                identical formula to cal_mlp
                                extract_data.py / post_hoc_processor.py /
                                integration.py.
  prob_breakeven_gap REAL     — calibrated_prob - market_price/100.
                                None when either is missing (e.g.
                                price_out_of_range_early before
                                probability computation).
  vol_regime TEXT             — 'normal' / 'elevated' from vol_est at
                                rejection time. NULL when rejection
                                fires before vol_est is built.
  data_provenance TEXT        — 'live_ws' for live-bot inserts (mirrors
                                evaluated_opportunities.data_provenance
                                Sprint A.2 / commit f26a611).
  orderbook_levels_json TEXT  — top-N YES ladder JSON via
                                _get_fresh_ob_ladder (10s freshness
                                gate, stale → NULL). NULL on
                                no_orderbook rejections (correct).

Population happens centrally in `insert_rejection()` via the same
auto-fill pattern `insert_evaluated_opportunity` uses (Tier 4 time
features + Tier 5 derived features + cache-backed orderbook ladder).

The 4 pre-gate rejection sites in `bot/scanner/__init__.py` covered:
  - price_out_of_range_early (line ~1850)
  - low_probability_15m       (line ~2058)
  - no_orderbook              (line ~2135)
  - no_best_ask               (line ~2205)

Lock-step rule (bot/CLAUDE.md): hour_sin/hour_cos/sigma_winsorize/
prob_breakeven_gap derivations MUST match the cal_mlp lock-step
surface. This test pins numeric equivalence.

See kb/decisions/sprint-b-bit-1a-shipped-may12.md.
"""

import ast
import json
import math
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


# Authoritative list of new columns added by Sprint B Bit B.1a.
SPRINT_B_BIT_1A_NEW_COLUMNS = [
    ("sigma_winsorize", "REAL"),
    ("hour_sin", "REAL"),
    ("hour_cos", "REAL"),
    ("prob_breakeven_gap", "REAL"),
    ("vol_regime", "TEXT"),
    ("data_provenance", "TEXT"),
    ("orderbook_levels_json", "TEXT"),
]


# ──────────────────────────────────────────────────────────────────────
# Schema layer
# ──────────────────────────────────────────────────────────────────────


class TestSchemaMigration:
    """All 7 new columns present on rejected_opportunities after init."""

    def test_all_new_columns_present(self):
        import bot.state
        sm = bot.state.StateManager(":memory:")
        cols = {
            r["name"] for r in
            sm.conn.execute("PRAGMA table_info(rejected_opportunities)").fetchall()
        }
        missing = [name for (name, _) in SPRINT_B_BIT_1A_NEW_COLUMNS if name not in cols]
        assert not missing, (
            f"Sprint B Bit B.1a columns missing from rejected_opportunities: {missing}. "
            f"Add via ALTER TABLE ADD COLUMN in StateManager._create_tables migration loop "
            f"(bot/state.py:812-836)."
        )

    def test_column_types_match(self):
        import bot.state
        sm = bot.state.StateManager(":memory:")
        col_types = {
            r["name"]: r["type"].upper() for r in
            sm.conn.execute("PRAGMA table_info(rejected_opportunities)").fetchall()
        }
        for (name, sql_type) in SPRINT_B_BIT_1A_NEW_COLUMNS:
            assert col_types.get(name) == sql_type.upper(), (
                f"{name} declared as {col_types.get(name)!r}, expected {sql_type!r}"
            )

    def test_migration_is_idempotent(self, tmp_path):
        """Re-running StateManager init on an existing DB must not raise."""
        import bot.state
        db_path = tmp_path / "state.db"
        sm1 = bot.state.StateManager(str(db_path))
        sm1.conn.close()
        # Re-open — ALTER block must short-circuit on existing cols.
        sm2 = bot.state.StateManager(str(db_path))
        cols = {
            r["name"] for r in
            sm2.conn.execute("PRAGMA table_info(rejected_opportunities)").fetchall()
        }
        for (name, _) in SPRINT_B_BIT_1A_NEW_COLUMNS:
            assert name in cols


# ──────────────────────────────────────────────────────────────────────
# Population layer — insert_rejection auto-fills the new cols.
# ──────────────────────────────────────────────────────────────────────


def _make_state(tmp_path):
    import bot.state
    return bot.state.StateManager(str(tmp_path / "state.db"))


def _fetch_row(sm, ticker):
    return dict(sm.conn.execute(
        "SELECT * FROM rejected_opportunities WHERE ticker=?", (ticker,)
    ).fetchone())


class TestInsertRejectionEnrichment:
    """insert_rejection() must populate the 7 new cols from its inputs."""

    def test_full_context_rejection_populates_all_cols(self, tmp_path):
        """Simulating a `low_probability_15m`-shaped call (full context:
        cal_prob, market_price, spot, threshold, vol, stc, vol_regime)."""
        sm = _make_state(tmp_path)
        sm.insert_rejection(
            "BTCD-25MAY12-T100000", "BTCD-25MAY12",
            "BTC", "low_probability_15m",
            None,                  # z_score
            100050.0,              # spot_price
            100000.0,              # threshold
            0.001,                 # volatility (blended_rv)
            85,                    # market_price (cents)
            300.0,                 # seconds_to_close
            0.45,                  # calibrated_prob
            raw_prob=0.40,
            product_type="15m",
            vol_regime="normal",
            data_provenance="live_ws",
        )
        row = _fetch_row(sm, "BTCD-25MAY12-T100000")

        # prob_breakeven_gap = cal_prob - market_price/100 = 0.45 - 0.85
        assert row["prob_breakeven_gap"] is not None
        assert abs(row["prob_breakeven_gap"] - (-0.40)) < 1e-9

        # sigma_winsorize: signed σ buffer ≈ +0.5/100k / (0.001*sqrt(60)*100) ≈ small +
        # Not None — and within ±SIGMA_WINSOR_ABS_CAP.
        assert row["sigma_winsorize"] is not None
        assert -25.0 <= row["sigma_winsorize"] <= 25.0

        # hour_sin/hour_cos analytical from rejection_time hour.
        assert row["hour_sin"] is not None
        assert row["hour_cos"] is not None
        assert -1.0 <= row["hour_sin"] <= 1.0
        assert -1.0 <= row["hour_cos"] <= 1.0

        # vol_regime + data_provenance pass-through.
        assert row["vol_regime"] == "normal"
        assert row["data_provenance"] == "live_ws"

    def test_no_orderbook_rejection_orderbook_levels_json_nullable(self, tmp_path):
        """no_orderbook rejection fires when orderbook cache is empty;
        _get_fresh_ob_ladder returns None → orderbook_levels_json IS NULL.
        That is CORRECT (honest NULL). The other 6 enrichment cols still
        populate from inputs."""
        sm = _make_state(tmp_path)
        # _scan_ob_cache is empty by default.
        sm.insert_rejection(
            "ETHD-25MAY12-T2500", "ETHD-25MAY12",
            "ETH", "no_orderbook",
            None,
            2510.0, 2500.0, 0.002, None, 240.0, 0.50,
            raw_prob=0.48,
            product_type="15m",
            vol_regime="elevated",
        )
        row = _fetch_row(sm, "ETHD-25MAY12-T2500")
        assert row["orderbook_levels_json"] is None
        assert row["vol_regime"] == "elevated"
        assert row["hour_sin"] is not None
        assert row["hour_cos"] is not None
        # prob_breakeven_gap is None — market_price was None.
        assert row["prob_breakeven_gap"] is None
        # sigma_winsorize derivable from spot/threshold/vol/stc (all non-None).
        assert row["sigma_winsorize"] is not None

    def test_orderbook_levels_json_populated_from_cache(self, tmp_path):
        """When _scan_ob_cache has a FRESH ticker entry,
        insert_rejection populates orderbook_levels_json from it (same
        path as insert_evaluated_opportunity)."""
        import time
        sm = _make_state(tmp_path)
        ladder_json = json.dumps({"yes_bids": [[80, 100]], "yes_asks": [[81, 100]]})
        sm._scan_ob_cache["SOLD-25MAY12-T200"] = (time.monotonic(), ladder_json)
        sm.insert_rejection(
            "SOLD-25MAY12-T200", "SOLD-25MAY12",
            "SOL", "no_best_ask",
            None,
            201.0, 200.0, 0.005, None, 180.0, 0.40,
        )
        row = _fetch_row(sm, "SOLD-25MAY12-T200")
        assert row["orderbook_levels_json"] == ladder_json

    def test_price_out_of_range_early_partial_context_honest_nulls(self, tmp_path):
        """price_out_of_range_early fires for SPX/hourly/weather BEFORE
        calibrated_prob is computed. Sigma_winsorize derivable from
        spot/threshold/vol/stc; prob_breakeven_gap MUST be None (honest)."""
        sm = _make_state(tmp_path)
        sm.insert_rejection(
            "SPXD-25MAY12-12pm-T5000", "SPXD-25MAY12-12pm",
            "SPX", "price_out_of_range_early",
            None,
            4950.0, 5000.0, 0.0015, 5, 1800.0, None,   # cal_prob=None
            product_type="spx_hourly",
        )
        row = _fetch_row(sm, "SPXD-25MAY12-12pm-T5000")
        assert row["prob_breakeven_gap"] is None
        # vol_regime not passed → None (honest, not synthesized).
        assert row["vol_regime"] is None
        # data_provenance defaults to 'live_ws' for live-bot inserts.
        assert row["data_provenance"] == "live_ws"
        # hour_sin / hour_cos still computable from rejection_time.
        assert row["hour_sin"] is not None
        assert row["hour_cos"] is not None


# ──────────────────────────────────────────────────────────────────────
# Numeric equivalence — bot.helpers MUST match cal_mlp's lock-step surface.
# ──────────────────────────────────────────────────────────────────────


class TestCalMLPLockStepEquivalence:
    """Numeric outputs from bot.helpers MUST match cal_mlp/{extract_data,
    post_hoc_processor, integration, features, sim_pnl}. Any divergence
    is train/serve skew (model trained on one distribution, served from
    another — see kb/failures/ws-cache-drift-... and bot/CLAUDE.md
    "cal_mlp feature transforms (lock-step)")."""

    def test_apply_sigma_winsor_matches_cal_mlp_features(self):
        """bot.helpers.derived_features.apply_sigma_winsor must clip
        identically to scripts/cal_mlp/features.apply_sigma_winsor."""
        from bot.helpers.derived_features import apply_sigma_winsor as bot_winsor
        # Import the cal_mlp canonical via sys.path.
        cal_mlp_dir = os.path.join(PROJECT_ROOT, "scripts", "cal_mlp")
        if cal_mlp_dir not in sys.path:
            sys.path.insert(0, cal_mlp_dir)
        from features import apply_sigma_winsor as cal_winsor, SIGMA_WINSOR_ABS_CAP

        # SIGMA_WINSOR_ABS_CAP=25.0 — exercise both sides + None passthrough.
        for v in (None, 0.0, 1.5, -3.2, 24.9, 25.0, 25.1, 100.0, -25.0,
                  -50.0, -3337.0, 1e-9):
            assert bot_winsor(v) == cal_winsor(v), (
                f"divergence at v={v}: bot={bot_winsor(v)} cal_mlp={cal_winsor(v)}"
            )
        # NaN passthrough (NaN > x and NaN < x are both False).
        nan = float("nan")
        # both must return NaN-shaped output
        bv, cv = bot_winsor(nan), cal_winsor(nan)
        assert math.isnan(bv) and math.isnan(cv)
        # Constant matches.
        from bot.helpers.derived_features import SIGMA_WINSOR_ABS_CAP as bot_cap
        assert bot_cap == SIGMA_WINSOR_ABS_CAP == 25.0

    def test_hour_sin_cos_matches_cal_mlp_integration_formula(self):
        """For any hour_of_day_utc in 0..23, the bot.helpers hour_sin/cos
        derivation must equal cal_mlp/integration.py:1430-1431 (which
        is the runtime-serve canonical formula)."""
        from bot.helpers.derived_features import compute_hour_sin_cos
        for hour in range(0, 24):
            sin, cos = compute_hour_sin_cos(hour)
            expected_sin = math.sin(2.0 * math.pi * float(hour) / 24.0)
            expected_cos = math.cos(2.0 * math.pi * float(hour) / 24.0)
            assert abs(sin - expected_sin) < 1e-12, f"hour={hour} sin diverges"
            assert abs(cos - expected_cos) < 1e-12, f"hour={hour} cos diverges"
        # None passthrough.
        assert compute_hour_sin_cos(None) == (None, None)


# ──────────────────────────────────────────────────────────────────────
# AST guards — rejection sites must NOT inline duplicate formulas.
# (Lock-step rule prevention.)
# ──────────────────────────────────────────────────────────────────────


SCANNER_PATH = os.path.join(PROJECT_ROOT, "bot", "scanner", "__init__.py")
STATE_PATH = os.path.join(PROJECT_ROOT, "bot", "state.py")


def _read_ast(path):
    with open(path) as f:
        return ast.parse(f.read())


class TestNoInlineLockStepFormulaDuplication:
    """No inline `math.sin(2 * math.pi * ... / 24)` or `if x > 25: x = 25`
    pattern in bot/scanner/__init__.py — must call the helper."""

    def test_scanner_does_not_inline_hour_sin_cos_formula(self):
        """A literal `2 * math.pi * hour / 24.0` outside the canonical
        helpers is a lock-step bug waiting to happen — model trained on
        cal_mlp's formula could diverge if scanner inlines its own."""
        with open(SCANNER_PATH) as f:
            src = f.read()
        # Look for the smoking gun: math.pi / 24 in scanner module.
        # The canonical helpers live in bot.helpers.derived_features; the
        # scanner itself must not duplicate the formula.
        for line_no, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "math.pi" in line and ("/ 24" in line or "/24" in line):
                pytest.fail(
                    f"bot/scanner/__init__.py:{line_no} inlines an "
                    f"hour_sin/cos-shaped formula: {line.strip()!r}. Use "
                    f"bot.helpers.derived_features.compute_hour_sin_cos instead."
                )

    def test_state_module_does_not_inline_sigma_winsorize_cap(self):
        """No bare `25.0` magic-number compare in bot/state.py for sigma —
        must read SIGMA_WINSOR_ABS_CAP from canonical or call apply_sigma_winsor."""
        with open(STATE_PATH) as f:
            src = f.read()
        for line_no, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Allow references to the named constant; block literal-25 cap-compares.
            if (("sigma" in line.lower() or "winsor" in line.lower())
                    and "25.0" in line
                    and "SIGMA_WINSOR_ABS_CAP" not in line
                    and "apply_sigma_winsor" not in line):
                pytest.fail(
                    f"bot/state.py:{line_no} appears to inline the "
                    f"SIGMA_WINSOR_ABS_CAP literal 25.0: {line.strip()!r}. "
                    f"Use bot.helpers.derived_features.apply_sigma_winsor instead."
                )


# ──────────────────────────────────────────────────────────────────────
# Planted-defect / drift detector — assert that the 4 scanner rejection
# sites pass at least the keyword arguments the enriched-row contract
# requires (vol_regime when vol_est is in scope; data_provenance default
# via state.py).
# ──────────────────────────────────────────────────────────────────────


class TestScannerCallsAtFourSites:
    """The 4 pre-gate rejection insert_rejection() sites in
    bot/scanner/__init__.py must thread vol_regime= for the 3 sites
    where vol_est is in scope (low_probability_15m, no_orderbook,
    no_best_ask) so the new vol_regime col is populated."""

    def _all_insert_rejection_calls(self):
        tree = _read_ast(SCANNER_PATH)
        calls = []
        for sub in ast.walk(tree):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if (isinstance(f, ast.Attribute)
                    and f.attr == "insert_rejection"
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "_state"):
                calls.append(sub)
        return calls

    def _reason_of(self, call):
        # 4th positional arg = rejection_reason
        if len(call.args) >= 4 and isinstance(call.args[3], ast.Constant):
            return call.args[3].value
        return None

    @pytest.mark.parametrize("reason", [
        "low_probability_15m", "no_orderbook", "no_best_ask",
    ])
    def test_post_vol_est_rejection_sites_pass_vol_regime(self, reason):
        """3 sites that fire AFTER vol_est is computed must pass
        vol_regime= so the new column is non-NULL for those rejections.
        Without this kwarg, vol_regime defaults to None at the StateManager
        layer → the training signal is broken."""
        calls = [c for c in self._all_insert_rejection_calls()
                 if self._reason_of(c) == reason]
        assert calls, f"no insert_rejection call for {reason!r} found"
        for c in calls:
            kwargs = {kw.arg for kw in c.keywords}
            assert "vol_regime" in kwargs, (
                f"insert_rejection(... {reason!r} ...) in bot/scanner/__init__.py "
                f"does not pass vol_regime= — the new training-data column would "
                f"be NULL for these rejections. Add `vol_regime=vol_est['regime']` "
                f"to the kwargs (Sprint B Bit B.1a)."
            )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-x", "-v"])
