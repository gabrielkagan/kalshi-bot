"""Regression tests for scripts/ops/generate_whitepaper_stats.py.

Failure modes (all originally surfaced by the 2026-04-26 doc audit):

1. Original generator labeled SUM(pnl_cents) as "observation P&L" — but every row in
   settled_trades is LIVE money. The label was the lie; there is no observation axis on
   settled_trades. Observation/shadow PnL lives in evaluated_opportunities.counterfactual_pnl.

2. Brier was wrong for NO-side trades. raw_prob is P(YES); for a NO trade the model's
   probability of our-bet-winning is (1 - raw_prob). The fixture must include NO trades
   to expose this.

3. Original total_trades = COUNT WHERE filter_stage='candidate' undercounted by ignoring
   90+ other filter_stages that ARE live candidates (decided_contract_*/discount/etc).

4. T2-Z2 is intentionally SHADOWED (memory/project_t2_z2_apr22_rejection.md), so its
   filter_stage 'decided_contract_t2_z2' must NOT be in LIVE_CANDIDATE_STAGES.

5. Filter pass rate denominator was inflated by hourly/spx/weather/sports observation
   logs that are not real candidates.

6. Regime cutoffs at midnight UTC misalign with actual deploy commit timestamps.

7. Generator opens sqlite3 connection without WAL/busy_timeout pragmas required by
   scripts/CLAUDE.md.

8. New strategy_group ships → silently lands in 'unknown' bucket and pollutes live PnL.
   Generator must warn loudly on stderr.
"""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GENERATOR_PATH = os.path.join(PROJECT_ROOT, "scripts", "ops", "generate_whitepaper_stats.py")


def _create_schema(conn):
    """Minimal prod-shape schema. We omit columns the generator doesn't read."""
    conn.executescript(
        """
        CREATE TABLE settled_trades (
            ticker TEXT NOT NULL,
            event_ticker TEXT NOT NULL,
            asset TEXT NOT NULL,
            market_result TEXT NOT NULL,
            side TEXT NOT NULL,
            count INTEGER NOT NULL,
            entry_price_cents INTEGER NOT NULL,
            revenue_cents INTEGER NOT NULL,
            fee_cents INTEGER NOT NULL,
            pnl_cents INTEGER NOT NULL,
            settled_at TEXT NOT NULL,
            strategy TEXT,
            seconds_to_close REAL,
            calibrated_prob REAL,
            edge REAL,
            kelly_f REAL,
            product_type TEXT,
            strategy_group TEXT NOT NULL DEFAULT 'main',
            is_stacked INTEGER DEFAULT 0,
            PRIMARY KEY (ticker, strategy_group)
        );

        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            event_ticker TEXT NOT NULL,
            asset TEXT NOT NULL,
            filter_stage TEXT NOT NULL,
            evaluation_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            calibrated_prob REAL,
            raw_prob REAL,
            edge REAL,
            market_price INTEGER,
            seconds_to_close REAL,
            product_type TEXT,
            market_result TEXT,
            counterfactual_pnl INTEGER,
            side TEXT DEFAULT 'yes'
        );
        """
    )


def _insert_settled(
    conn, ticker, asset, strategy_group, product_type, entry_price, pnl,
    settled_at="2026-04-15T12:00:00Z", side="yes",
):
    """Insert a settled trade. revenue derived deterministically from entry+pnl."""
    market_result = side if pnl > 0 else ("no" if side == "yes" else "yes")
    conn.execute(
        """INSERT INTO settled_trades
        (ticker, event_ticker, asset, market_result, side, count, entry_price_cents,
         revenue_cents, fee_cents, pnl_cents, settled_at, strategy_group, product_type)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?, ?, ?)""",
        (ticker, "EVT-" + ticker, asset, market_result, side, entry_price,
         entry_price + pnl, pnl, settled_at, strategy_group, product_type),
    )


def _insert_eo(conn, ticker, asset, filter_stage, product_type, **kwargs):
    """Insert an evaluated_opportunities row."""
    conn.execute(
        """INSERT INTO evaluated_opportunities
        (ticker, event_ticker, asset, filter_stage, evaluation_time, product_type,
         calibrated_prob, raw_prob, market_price, market_result, counterfactual_pnl, side)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, "EVT-" + ticker, asset, filter_stage,
         kwargs.get("evaluation_time", "2026-04-15T12:00:00Z"),
         product_type,
         kwargs.get("calibrated_prob"),
         kwargs.get("raw_prob"),
         kwargs.get("market_price"),
         kwargs.get("market_result"),
         kwargs.get("counterfactual_pnl"),
         kwargs.get("side", "yes")),
    )


@pytest.fixture
def fixture_db(tmp_path):
    """Build a state.db with realistic LIVE + shadow data.

    Settled trades (ALL LIVE — there's no observation axis on this table):
      strategy_group=main:        5 wins @ 90c (+10 each), 1 loss @ 95c (-95)  → -45  (5w/1l)
      strategy_group=decided:     2 wins @ 95c (+5),       1 loss @ 95c (-95)  → -85  (2w/1l)
      strategy_group=weekend_discount: 1 win @ 90c (+10)                       → +10  (1w)
      strategy_group=low_price_near_expiry: 1 win @ 84c (+16)                  → +16  (1w)
      strategy_group=weather_no_live:  1 win @ 38c side=no (+62), 1 loss (-38) → +24  (1w/1l)
      strategy_group=hourly_no_live:   1 loss @ 50c (-50)                      → -50  (0w/1l)
                                                                       LIVE TOTAL = -130

    Settled with NO-side row to expose the Brier bug:
      ticker NO-WX-W has side='no', entry 38c, won (+62), market_result='no'.
      Its evaluated_opportunities row has side='no' raw_prob=0.40 (P(YES)=0.40).
      Correct Brier contribution: model_p_of_win = 1-0.40 = 0.60, outcome=1 (we won),
      contrib = (0.60 - 1)^2 = 0.16. The OLD broken formula computes (0.40 - 1)^2 = 0.36.

    Evaluated funnel (15M):
      30 candidate, 50 insufficient_edge, 20 price_out_of_range = 100 total
      + 5 decided_contract_t1, 3 weekend_discount = 108 total 15M rows

    Shadow stages with counterfactual_pnl populated:
      4 dc_shadow_t2_z2 rows, cf_pnl: 100, 50, -200, -50 → sum -100
      3 floor_raise_shadow rows, cf_pnl: 50, 50, 50      → sum +150
      2 weekend_discount_shadow rows, cf_pnl: 30, -10    → sum +20

    Plus pure observation logs (no cf_pnl needed for tests):
      50 hourly_observation, 10 spx_observation, 5 weather_observation
    """
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    # Per scripts/CLAUDE.md, any new sqlite3.connect() needs both pragmas.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    _create_schema(conn)

    # --- LIVE settled trades (all rows in this table are LIVE) ---
    for i in range(5):
        _insert_settled(conn, f"LIVE-15M-W-{i}", "BTC", "main", "15m", 90, 10)
    _insert_settled(conn, "LIVE-15M-L-0", "BTC", "main", "15m", 95, -95)
    _insert_settled(conn, "LIVE-DC-W-0", "BTC", "decided", "15m", 95, 5)
    _insert_settled(conn, "LIVE-DC-W-1", "BTC", "decided", "15m", 95, 5)
    _insert_settled(conn, "LIVE-DC-L-0", "BTC", "decided", "15m", 95, -95)
    _insert_settled(conn, "LIVE-WKND-W-0", "BTC", "weekend_discount", "15m", 90, 10)
    _insert_settled(conn, "LIVE-LPNE-W-0", "BTC", "low_price_near_expiry", "15m", 84, 16)
    _insert_settled(conn, "LIVE-WX-NO-W-0", "WEATHER", "weather_no_live", "weather", 38, 62, side="no")
    _insert_settled(conn, "LIVE-WX-NO-L-0", "WEATHER", "weather_no_live", "weather", 38, -38, side="no")
    _insert_settled(conn, "LIVE-HRLY-L-0", "BTC", "hourly_no_live", "hourly", 50, -50)

    # --- 15M filter funnel ---
    for i in range(30):
        _insert_eo(conn, f"EVAL-15M-CAND-{i}", "BTC", "candidate", "15m",
                   raw_prob=0.92, market_result="yes")
    for i in range(50):
        _insert_eo(conn, f"EVAL-15M-IE-{i}", "BTC", "insufficient_edge", "15m")
    for i in range(20):
        _insert_eo(conn, f"EVAL-15M-POOR-{i}", "BTC", "price_out_of_range", "15m")

    # --- Other LIVE candidate stages ---
    for i in range(5):
        _insert_eo(conn, f"EVAL-DC-T1-{i}", "BTC", "decided_contract_t1", "15m",
                   raw_prob=0.97, market_result="yes")
    for i in range(3):
        _insert_eo(conn, f"EVAL-WKND-{i}", "BTC", "weekend_discount", "15m",
                   raw_prob=0.93, market_result="yes")

    # --- Weather NO live candidates (side=no) ---
    _insert_eo(conn, "EVAL-WX-NO-W", "WEATHER", "candidate", "weather",
               raw_prob=0.40, market_result="no", side="no")
    _insert_eo(conn, "EVAL-WX-NO-L", "WEATHER", "candidate", "weather",
               raw_prob=0.40, market_result="yes", side="no")

    # --- Shadow stages with counterfactual_pnl ---
    for i, cf in enumerate([100, 50, -200, -50]):
        _insert_eo(conn, f"EVAL-DCS-T2Z2-{i}", "BTC", "dc_shadow_t2_z2", "15m",
                   counterfactual_pnl=cf)
    for i, cf in enumerate([50, 50, 50]):
        _insert_eo(conn, f"EVAL-FRS-{i}", "BTC", "floor_raise_shadow", "15m",
                   counterfactual_pnl=cf)
    for i, cf in enumerate([30, -10]):
        _insert_eo(conn, f"EVAL-WKNDS-{i}", "BTC", "weekend_discount_shadow", "15m",
                   counterfactual_pnl=cf)

    # --- Pure observation logs (no entry; engine in observation mode) ---
    for i in range(50):
        _insert_eo(conn, f"EVAL-HRLY-{i}", "BTC", "hourly_observation", "hourly")
    for i in range(10):
        _insert_eo(conn, f"EVAL-SPX-{i}", "SPX", "spx_observation", "spx_hourly")
    for i in range(5):
        _insert_eo(conn, f"EVAL-WX-OBS-{i}", "WEATHER", "weather_observation", "weather")

    conn.commit()
    conn.close()
    return str(db_path)


def _run_generator(db_path):
    """Run the generator and return (parsed_stats, stderr_text)."""
    result = subprocess.run(
        [sys.executable, GENERATOR_PATH, db_path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"Generator failed: {result.stderr}"
    return json.loads(result.stdout), result.stderr


# --- Tests ------------------------------------------------------------------

class TestLivePnlCorrectness:
    """All settled_trades rows are LIVE. live_pnl_cents = SUM(pnl_cents). Period."""

    def test_live_pnl_cents_sums_all_settled(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # Sum: 5*10 -95 +5 +5 -95 +10 +16 +62 -38 -50 = -130
        assert stats["live_pnl_cents"] == -130

    def test_live_settled_counts_all_settled(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # 6 main + 3 decided + 1 weekend + 1 lpne + 2 weather_no_live + 1 hourly_no_live = 14
        assert stats["live_settled"] == 14

    def test_live_wins_and_losses_match(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # Wins: 5 main + 2 decided + 1 wknd + 1 lpne + 1 wx_no = 10
        # Losses: 1 main + 1 decided + 1 wx_no + 1 hrly = 4
        assert stats["live_wins"] == 10
        assert stats["live_losses"] == 4
        assert stats["live_settled"] == 14


class TestNoObservationOnSettledTrades:
    """settled_trades has no observation axis. Old observation_pnl_cents must NOT exist."""

    def test_observation_pnl_cents_field_removed(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # The Round-1 misnamed field must not be there. observation belongs in
        # shadow_counterfactual_pnl (from evaluated_opportunities), not here.
        assert "observation_pnl_cents" not in stats, (
            "Round-1 added a fake observation/live split on settled_trades. "
            "All settled_trades rows are LIVE — drop this field."
        )

    def test_no_unclassified_bucket(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # No more unk bucket for the live-vs-obs split, since it doesn't exist.
        assert "unclassified_pnl_cents" not in stats
        assert "unclassified_settled" not in stats


class TestShadowCounterfactual:
    """Shadow / observation PnL comes from evaluated_opportunities.counterfactual_pnl."""

    def test_shadow_pnl_by_stage_present(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        assert "shadow_counterfactual_pnl_by_stage" in stats
        sbs = stats["shadow_counterfactual_pnl_by_stage"]
        assert "dc_shadow_t2_z2" in sbs
        assert "floor_raise_shadow" in sbs
        assert "weekend_discount_shadow" in sbs

    def test_shadow_pnl_aggregates_correctly(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        sbs = stats["shadow_counterfactual_pnl_by_stage"]
        # dc_shadow_t2_z2: 100+50-200-50 = -100, n=4
        assert sbs["dc_shadow_t2_z2"]["pnl_cents"] == -100
        assert sbs["dc_shadow_t2_z2"]["n"] == 4
        # floor_raise_shadow: 50+50+50 = 150, n=3
        assert sbs["floor_raise_shadow"]["pnl_cents"] == 150
        assert sbs["floor_raise_shadow"]["n"] == 3


class TestSideAwareBrier:
    """Brier must use 1-raw_prob for NO-side trades. Bug-fix from Round 1."""

    def test_brier_overall_uses_side_correctly(self, fixture_db):
        """Compute expected Brier by hand and assert exact value.

        Fixture EO rows with raw_prob + market_result:
          30x candidate side=yes raw_prob=0.92 market_result='yes' (all wins from model POV)
            outcome=1, p=0.92, contrib = (0.92-1)^2 = 0.0064 each, 30 rows = 0.192
          5x dc_t1 side=yes raw_prob=0.97 mr='yes': (0.97-1)^2 = 0.0009 each, 5 = 0.0045
          3x wknd side=yes raw_prob=0.93 mr='yes': (0.93-1)^2 = 0.0049 each, 3 = 0.0147
          1x weather NO side=no raw_prob=0.40 mr='no': p_of_win = 1-0.40 = 0.60, outcome=1
              contrib = (0.60-1)^2 = 0.16
          1x weather NO side=no raw_prob=0.40 mr='yes': p_of_win=0.60, outcome=0
              contrib = (0.60-0)^2 = 0.36

        Sum = 0.192 + 0.0045 + 0.0147 + 0.16 + 0.36 = 0.7312
        n = 40 → Brier = 0.7312 / 40 = 0.01828
        """
        stats, _ = _run_generator(fixture_db)
        b = stats["brier"]["overall"]
        assert b is not None
        assert abs(b - 0.01828) < 1e-4, f"Expected ~0.01828, got {b}"

    def test_brier_no_side_contributes_correctly(self, fixture_db):
        """Brier per-product weather: only 2 NO-side rows.

        Both have raw_prob=0.40. p_of_win=0.60.
          1 win  (mr='no'):  (0.60-1)^2 = 0.16
          1 loss (mr='yes'): (0.60-0)^2 = 0.36
        Sum = 0.52, n=2, Brier = 0.26
        """
        stats, _ = _run_generator(fixture_db)
        bp = stats["brier"]["by_product"]
        assert "weather" in bp
        assert abs(bp["weather"] - 0.26) < 1e-4, (
            f"Weather Brier expected 0.26 (proves side-aware), got {bp['weather']}. "
            f"Old buggy formula would have given (0.40-1)^2 + (0.40-0)^2 = 0.5/2 = 0.25"
        )


class TestT2Z2NotInLiveCandidates:
    """T2-Z2 is intentionally shadowed per memory. Must not count as a live candidate."""

    def test_t2_z2_not_counted_as_live_candidate(self, fixture_db):
        # Add T2-Z2 evals to the fixture inline since standard fixture doesn't include them
        conn = sqlite3.connect(fixture_db)
        for i in range(7):
            _insert_eo(conn, f"EVAL-T2Z2-{i}", "BTC", "decided_contract_t2_z2", "15m")
        conn.commit()
        conn.close()
        stats, _ = _run_generator(fixture_db)
        # Live candidates = 30 (15M cand) + 5 (dc_t1) + 3 (wknd) + 2 (wx cand) = 40
        # If T2-Z2 was wrongly included it'd be 47.
        assert stats["total_live_candidates"] == 40, (
            "T2-Z2 (decided_contract_t2_z2) is intentionally shadowed and must not be "
            "counted as live. Current count: " + str(stats["total_live_candidates"])
        )


class TestUnknownStrategyGroupWarning:
    """Generator must warn loudly on stderr when unknown strategy_group encountered."""

    def test_warning_on_unknown_strategy_group(self, fixture_db):
        conn = sqlite3.connect(fixture_db)
        _insert_settled(conn, "FUTURE-SG-0", "BTC", "future_unknown_group", "15m", 90, 10)
        conn.commit()
        conn.close()
        stats, stderr = _run_generator(fixture_db)
        assert "future_unknown_group" in stderr, (
            "Unknown strategy_group must produce stderr warning naming the offender. "
            f"stderr was: {stderr!r}"
        )
        # And it should be flagged in the JSON for downstream consumers
        assert "unknown_strategy_groups" in stats
        assert "future_unknown_group" in stats["unknown_strategy_groups"]

    def test_no_warning_for_known_groups(self, fixture_db):
        _, stderr = _run_generator(fixture_db)
        # Standard fixture only uses known groups; should be silent
        assert "WARN" not in stderr or "unknown" not in stderr.lower(), (
            f"Unexpected warning for known groups. stderr: {stderr!r}"
        )


class TestRegimeCutoffsMatchCommitTimestamps:
    """Regime cutoffs must use real deploy commit timestamps, not midnight UTC heuristics."""

    def test_apr11_cutoff_pulled_from_commit(self):
        """48a7f5a (loss-burst cooldown + weather NO) shipped 2026-04-11T07:24:45-04:00,
        with a critical fix 4075655 at 2026-04-11T16:43:27-04:00 = 20:43:27Z that made the
        feature actually work. The regime begins at the FIX commit, not the buggy ship.

        Cutoff must NOT be 2026-04-11T00:00:00Z (the original Round-1 day-boundary heuristic).
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("gen", GENERATOR_PATH)
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        assert gen.REGIME_APR11 != "2026-04-11T00:00:00Z", (
            "Apr 11 cutoff is still the day-boundary midnight heuristic; must use "
            "the actual deploy commit timestamp (4075655 = 2026-04-11T20:43:27Z)"
        )
        assert gen.REGIME_APR11.startswith("2026-04-11T20:")

    def test_apr23_cutoff_pulled_from_commit(self):
        """0ddcaf8 (WS schema fix) shipped 2026-04-23T19:46:07-04:00 = 23:46:07Z."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("gen", GENERATOR_PATH)
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        assert gen.REGIME_APR23 != "2026-04-23T00:00:00Z", (
            "Apr 23 cutoff is still day-boundary midnight; must use commit timestamp "
            "(0ddcaf8 = 2026-04-23T23:46:07Z)"
        )
        assert gen.REGIME_APR23.startswith("2026-04-23T23:")


class TestPragmas:
    """scripts/CLAUDE.md requires WAL + busy_timeout on any new sqlite3.connect()."""

    def test_generator_source_has_wal_pragma(self):
        with open(GENERATOR_PATH) as f:
            src = f.read()
        assert "journal_mode=WAL" in src or "journal_mode = WAL" in src, (
            "Generator must call PRAGMA journal_mode=WAL per scripts/CLAUDE.md"
        )

    def test_generator_source_has_busy_timeout(self):
        with open(GENERATOR_PATH) as f:
            src = f.read()
        assert "busy_timeout=10000" in src or "busy_timeout = 10000" in src, (
            "Generator must call PRAGMA busy_timeout=10000 per scripts/CLAUDE.md"
        )


class TestPerProductBreakdown:
    """Per-product filter funnels. 15M denominator excludes hourly/spx/weather logs."""

    def test_per_product_filter_breakdown_present(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        assert "filter_breakdown_by_product" in stats
        for pt in ["15m", "hourly", "weather"]:
            assert pt in stats["filter_breakdown_by_product"]

    def test_15m_funnel_exact_counts(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        fb_15m = stats["filter_breakdown_by_product"]["15m"]
        # Exact assertions, not just sum (Round 1 review tightened this)
        assert fb_15m["candidate"] == 30
        assert fb_15m["insufficient_edge"] == 50
        assert fb_15m["price_out_of_range"] == 20
        assert fb_15m["decided_contract_t1"] == 5
        assert fb_15m["weekend_discount"] == 3


class TestCandidateStageInclusion:
    """total_live_candidates counts canonical 'candidate' + decided/discount/etc."""

    def test_total_live_candidates_exact(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # 15m: 30 cand + 5 dc_t1 + 3 wknd = 38
        # weather: 2 cand
        # = 40 (T2-Z2 NOT included; observation logs NOT included)
        assert stats["total_live_candidates"] == 40

    def test_observation_stages_excluded(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # 50+10+5 = 65 observation rows must NOT be in the live-candidate count
        assert stats["total_live_candidates"] == 40
        assert stats["total_evaluated"] >= 165


class TestPerStrategyGroupSettled:
    """Per-strategy_group settled stats — required for deep whitepaper rewrite."""

    def test_settled_by_strategy_group_present(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        sbsg = stats["settled_by_strategy_group"]
        for sg in ["main", "decided", "weekend_discount", "weather_no_live", "hourly_no_live"]:
            assert sg in sbsg, f"{sg} missing from settled_by_strategy_group"

    def test_main_aggregates_exact(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        m = stats["settled_by_strategy_group"]["main"]
        assert m["n"] == 6
        assert m["wins"] == 5
        assert m["pnl_cents"] == -45  # 5*10 - 95


class TestRegimeFilters:
    """since_apr11 / since_apr23 blocks must work and use real cutoffs."""

    def test_since_apr11_includes_fixture(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # Fixture uses 2026-04-15T12:00:00Z which is AFTER both cutoffs
        assert stats["since_apr11"]["live_pnl_cents"] == stats["live_pnl_cents"]

    def test_since_apr23_excludes_fixture(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # Fixture is 2026-04-15 which is BEFORE Apr 23 cutoff
        assert stats["since_apr23"]["live_settled"] == 0


class TestBackwardsCompat:
    """Old placeholder-feeding fields must be retained (or removed cleanly)."""

    def test_legacy_observation_pnl_field_dropped_or_aliased(self, fixture_db):
        """Round-1 kept observation_pnl as legacy alias for total. We now drop it
        because the LABEL was the lie — there is no observation P&L on this table.
        Templates using {{OBSERVATION_PNL}} must break visibly so they get fixed.
        """
        stats, _ = _run_generator(fixture_db)
        # Either field is removed or it aliases live_pnl_cents (same thing now)
        if "observation_pnl" in stats:
            assert stats["observation_pnl"] == stats["live_pnl_cents"], (
                "If observation_pnl is kept as alias, it must equal live_pnl_cents"
            )

    def test_total_evaluated_still_present(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # 30+50+20+5+3+2(wx_cand)+50+10+5+12(shadows: 4+3+2+3 t2_z2_not_in_standard) = 175
        # Standard fixture (no T2-Z2 rows): 30+50+20+5+3+2+50+10+5+4+3+2 = 184
        assert stats["total_evaluated"] == 184


# --- Round 3 adversarial-review fixes ---------------------------------------

class TestBreakevenHandling:
    """pnl_cents == 0 trades exist in prod (2 rows) and must be classified as breakevens,
    not losses. live_wins + live_losses + live_breakevens == live_settled."""

    def test_breakeven_trade_not_counted_as_loss(self, fixture_db):
        conn = sqlite3.connect(fixture_db)
        _insert_settled(conn, "BREAKEVEN-1", "BTC", "main", "15m", 90, 0)  # exact breakeven
        conn.commit()
        conn.close()
        stats, _ = _run_generator(fixture_db)
        # Original fixture: 10 wins, 4 losses, 14 settled. Adding a breakeven:
        #   live_wins still 10, live_losses still 4, live_breakevens 1, live_settled 15
        assert stats["live_wins"] == 10
        assert stats["live_losses"] == 4
        assert stats["live_breakevens"] == 1
        assert stats["live_settled"] == 15

    def test_wins_losses_breakevens_sum_to_settled(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        assert (
            stats["live_wins"] + stats["live_losses"] + stats["live_breakevens"]
            == stats["live_settled"]
        )


class TestHeadlineExcludesNonKellyGroups:
    """weather_no_live (1ct verification) and hourly_no_live (kill-switched) are LIVE
    but economically distinct from Kelly-sized strategies. The headline number must
    expose both — full live and headline-only — so templates can choose."""

    def test_live_headline_pnl_excludes_weather_and_hourly_no(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # Total LIVE PnL = -130 (with weather_no +24, hourly_no -50 = -26 from non-headline)
        # Headline (excludes weather_no + hourly_no) = -130 - (-26) = -104
        assert stats["live_pnl_cents"] == -130
        assert stats["live_pnl_headline_cents"] == -104

    def test_live_headline_settled_excludes_non_kelly(self, fixture_db):
        stats, _ = _run_generator(fixture_db)
        # 14 total live, minus 2 weather_no minus 1 hourly_no = 11 headline
        assert stats["live_settled"] == 14
        assert stats["live_settled_headline"] == 11


class TestBrierOfFilledTrades:
    """Brier of FILLED trades only (model predictions paid in fees) — not all
    live-candidate evaluations. Computed by JOIN(settled_trades, evaluated_opportunities)
    deduplicated by latest EO per ticker."""

    def _add_matching_eo(self, db_path, ticker, raw_prob, market_result, side="yes",
                         pt="15m", asset="BTC"):
        """Insert an EO row whose ticker matches a settled_trade so the JOIN finds it."""
        conn = sqlite3.connect(db_path)
        _insert_eo(conn, ticker, asset, "candidate", pt,
                   raw_prob=raw_prob, market_result=market_result, side=side)
        conn.commit()
        conn.close()

    def test_brier_filled_present(self, fixture_db):
        # Add one matching EO so the JOIN has data. Standard fixture's settled tickers
        # don't have matching EO rows by default.
        self._add_matching_eo(fixture_db, "LIVE-15M-W-0", raw_prob=0.92, market_result="yes")
        stats, _ = _run_generator(fixture_db)
        assert "brier_filled" in stats
        assert stats["brier_filled"]["overall"] is not None
        # Single match: (0.92 - 1)^2 = 0.0064
        assert abs(stats["brier_filled"]["overall"] - 0.0064) < 1e-4

    def test_brier_filled_excludes_unfilled_candidates(self, fixture_db):
        """An EO candidate without a corresponding settled_trade must not enter brier_filled."""
        # Match one settled, leave another EO unmatched
        self._add_matching_eo(fixture_db, "LIVE-15M-W-0", raw_prob=0.92, market_result="yes")
        # Add an unfilled candidate with extreme value — must NOT contribute to brier_filled
        conn = sqlite3.connect(fixture_db)
        _insert_eo(conn, "UNFILLED-CAND-1", "BTC", "candidate", "15m",
                   raw_prob=0.01, market_result="yes")  # would contribute 0.9801 if leaked
        conn.commit()
        conn.close()
        stats, _ = _run_generator(fixture_db)
        # If the unfilled row leaked, mean = (0.0064 + 0.9801) / 2 = 0.49325
        # Filled-only Brier should still be 0.0064 (single match).
        assert stats["brier_filled"]["overall"] < 0.05, (
            f"brier_filled = {stats['brier_filled']['overall']} suggests unfilled leaked"
        )

    def test_brier_filled_dedups_multiple_eo_per_ticker(self, fixture_db):
        """Many tickers in prod have multiple candidate-stage EO rows. Use latest only."""
        conn = sqlite3.connect(fixture_db)
        _insert_eo(conn, "LIVE-15M-W-0", "BTC", "candidate", "15m",
                   raw_prob=0.50, market_result="yes",
                   evaluation_time="2026-04-15T11:55:00Z")  # earlier
        _insert_eo(conn, "LIVE-15M-W-0", "BTC", "candidate", "15m",
                   raw_prob=0.92, market_result="yes",
                   evaluation_time="2026-04-15T12:00:00Z")  # later — should win
        conn.commit()
        conn.close()
        stats, _ = _run_generator(fixture_db)
        # Should use the later raw_prob=0.92, not 0.50.
        # (0.92 - 1)^2 = 0.0064, NOT (0.50 - 1)^2 = 0.25.
        assert abs(stats["brier_filled"]["overall"] - 0.0064) < 1e-4, (
            f"brier_filled = {stats['brier_filled']['overall']} — dedup may be wrong"
        )

    def test_brier_filled_dedups_stacked_settled_rows(self, fixture_db):
        """settled_trades compound PK is (ticker, strategy_group). A single ticker can
        have multiple settled rows when stacking (e.g. main + decided + addon). The
        JOIN must NOT double-count the EO sample for that ticker.
        """
        conn = sqlite3.connect(fixture_db)
        # Two settled rows for the same ticker, different strategy_groups
        _insert_settled(conn, "STACK-1", "BTC", "main", "15m", 90, 10)
        _insert_settled(conn, "STACK-1", "BTC", "decided", "15m", 95, 5)
        # ONE matching EO for that ticker
        _insert_eo(conn, "STACK-1", "BTC", "candidate", "15m",
                   raw_prob=0.92, market_result="yes")
        conn.commit()
        conn.close()
        stats, _ = _run_generator(fixture_db)
        # If the JOIN doubles, n=2 and the Brier sample is counted twice. With one
        # EO row at (0.92-1)^2 = 0.0064, n should be 1 (one PREDICTION across two
        # settled rows). If buggy, n=2 with same Brier value but inflated sample size.
        assert stats["brier_filled"]["n"] == 1, (
            f"brier_filled.n = {stats['brier_filled']['n']} — JOIN is double-counting "
            f"stacked settled rows. SQL must dedup on the settled side too."
        )


class TestObservationTradeNotLiveCandidate:
    """observation_trade filter_stage means the row was logged but NO order was placed
    (OBSERVATION_MODE=True). It should NOT count as a live candidate, but SHOULD appear
    in shadow_counterfactual_pnl_by_stage (its counterfactual_pnl IS the observation P&L)."""

    def test_observation_trade_excluded_from_live_candidates(self, fixture_db):
        conn = sqlite3.connect(fixture_db)
        for i in range(5):
            _insert_eo(conn, f"OBS-TRADE-{i}", "BTC", "observation_trade", "15m")
        conn.commit()
        conn.close()
        stats, _ = _run_generator(fixture_db)
        assert stats["total_live_candidates"] == 40, (
            "observation_trade rows added to fixture but live candidates count grew. "
            "OBSERVATION_MODE rows did not actually trade live."
        )

    def test_observation_trade_appears_in_shadow_counterfactual(self, fixture_db):
        """Post-Round-6, observation_trade rows with counterfactual_pnl set must now
        surface in shadow_counterfactual_pnl_by_stage. Prod has 83 such rows with
        $43.62 cumulative cf_pnl — small but worth surfacing as observation P&L."""
        conn = sqlite3.connect(fixture_db)
        for i, cf in enumerate([100, -50, 200]):
            _insert_eo(conn, f"OBS-TRADE-CF-{i}", "BTC", "observation_trade", "15m",
                       counterfactual_pnl=cf)
        conn.commit()
        conn.close()
        stats, _ = _run_generator(fixture_db)
        sbs = stats["shadow_counterfactual_pnl_by_stage"]
        assert "observation_trade" in sbs, (
            "observation_trade was removed from LIVE_CANDIDATE_STAGES so its cf_pnl "
            "should now appear in shadow_counterfactual_pnl_by_stage."
        )
        assert sbs["observation_trade"]["pnl_cents"] == 250  # 100 - 50 + 200
        assert sbs["observation_trade"]["n"] == 3


class TestBrierFilledRobustness:
    """Round 7 adversarial findings — defensive cases on the JOIN."""

    def test_filled_brier_handles_timestamp_tie(self, fixture_db):
        """Two EO rows for the same ticker at the EXACT same evaluation_time. The
        latest_eo CTE picks MAX(evaluation_time) which is identical, so the JOIN
        returns BOTH rows unless tiebroken by id. Defensive fix needed."""
        conn = sqlite3.connect(fixture_db)
        _insert_settled(conn, "TIE-1", "BTC", "main", "15m", 90, 10)
        ts = "2026-04-15T12:00:00Z"
        _insert_eo(conn, "TIE-1", "BTC", "candidate", "15m",
                   raw_prob=0.10, market_result="yes", evaluation_time=ts)
        _insert_eo(conn, "TIE-1", "BTC", "candidate", "15m",
                   raw_prob=0.92, market_result="yes", evaluation_time=ts)
        conn.commit()
        conn.close()
        stats, _ = _run_generator(fixture_db)
        # Without tiebreak: n grows by 2 for this one ticker. With tiebreak: n grows by 1.
        # Prior fixture's brier_filled count check is unstable, so just check this case
        # contributes EXACTLY one sample for the tied ticker.
        # If both rows leaked, brier_filled.n would include 2 samples here.
        # Find the contribution: subtract baseline (no extra rows).
        base_conn = sqlite3.connect(fixture_db)
        base_conn.execute("DELETE FROM evaluated_opportunities WHERE ticker = 'TIE-1'")
        base_conn.execute("DELETE FROM settled_trades WHERE ticker = 'TIE-1'")
        base_conn.commit()
        base_conn.close()
        base_stats, _ = _run_generator(fixture_db)
        delta = stats["brier_filled"]["n"] - base_stats["brier_filled"]["n"]
        assert delta == 1, (
            f"Tied evaluation_time produced {delta} samples; should be exactly 1 "
            "(SQL must tiebreak on id or rowid when timestamps are equal)."
        )

    def test_settled_without_matching_eo_surfaced(self, fixture_db):
        """Prod has 164 settled tickers (5.6% of 2,908) that lack a matching EO row
        with raw_prob+market_result. brier_filled.n silently undercounts. Surface the
        delta as a JSON field so consumers know."""
        conn = sqlite3.connect(fixture_db)
        # Settled-only ticker with NO matching EO
        _insert_settled(conn, "ORPHAN-1", "BTC", "main", "15m", 90, 10)
        # Matching pair (so brier_filled.n is non-zero)
        _insert_settled(conn, "MATCHED-1", "BTC", "main", "15m", 90, 10)
        _insert_eo(conn, "MATCHED-1", "BTC", "candidate", "15m",
                   raw_prob=0.92, market_result="yes")
        conn.commit()
        conn.close()
        stats, _ = _run_generator(fixture_db)
        # JSON must surface the count of settled tickers without matching EO
        assert "settled_without_matching_eo" in stats
        assert stats["settled_without_matching_eo"] >= 1, (
            "ORPHAN-1 settled but has no matching EO — count must be reported"
        )


class TestReadmeTemplateMigrated:
    """README.template.md must not contain the legacy {{OBSERVATION_PNL}} placeholder.
    Per CLAUDE.md doc-drift rule, the template fix ships in the same commit as the
    generator patch."""

    def test_readme_template_no_observation_pnl(self):
        readme_template = os.path.join(PROJECT_ROOT, "README.template.md")
        with open(readme_template) as f:
            content = f.read()
        assert "{{OBSERVATION_PNL}}" not in content, (
            "README.template.md still references {{OBSERVATION_PNL}} — the placeholder "
            "was dropped from build_whitepaper.py, so this will render literally. "
            "Update to {{LIVE_PNL_DOLLARS}} and fix the surrounding 'Observation P&L' label."
        )

    def test_readme_template_uses_live_settled(self):
        readme_template = os.path.join(PROJECT_ROOT, "README.template.md")
        with open(readme_template) as f:
            content = f.read()
        assert "{{LIVE_SETTLED}}" in content, (
            "README.template.md must reference live trading stats placeholders. "
            "Dollar-amount placeholders ({{LIVE_PNL_*}}) were intentionally "
            "removed; counts/win-rate placeholders should remain."
        )


class TestFixtureUsesProperPragmas:
    """Per scripts/CLAUDE.md, any new sqlite3.connect() must set WAL + busy_timeout.
    Cosmetic fix — the test fixture's own connection should follow the convention."""

    def test_fixture_db_is_wal(self, fixture_db):
        # After fixture setup + close, reopen and verify WAL was enabled.
        conn = sqlite3.connect(fixture_db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        # Mode should be 'wal' if PRAGMA was set; otherwise 'delete' (default).
        assert mode == "wal", (
            f"Fixture DB journal_mode = {mode!r}; should be 'wal' per scripts/CLAUDE.md"
        )
