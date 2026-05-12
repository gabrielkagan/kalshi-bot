"""TDD for alpha_audit.py rebuild (2026-05-05).

Why this exists: the prior alpha_audit.py shipped with multiple structural
bugs that made the output misleading even when individual numbers were
arithmetically correct. Documented bug surface:

A. SHADOW_STAGES hardcoded to 6 stages; production has 25+ → ~95% of
   shadow volume invisible. Section 8 + Section 10 ("top opportunities")
   would draw conclusions from a stale subset.

B. Cell-block stages (TM98_97_98C_2_5MIN_BLEED, SOL_TAKER_85_89C_2_5MIN_BLEED,
   tm96_calmlp_gate_blocked, 96C_SOL_XRP_STC_DANGER_BAND) deflate
   `filter_stage='candidate'` rollups per CLAUDE.md. Funnel under-counts
   true "would-have-traded" volume.

C. Win rate computed `market_result IN ('yes','all_yes')` regardless of
   side. NO-side shadows (no_side_price_shadow_*, ~3,300 rows/30d) had
   their WR inverted: a NO opp wins when result is NO, not YES.

D. counterfactual_pnl IS net-of-fees + Kelly-sized + side-aware in source
   (bot/_impl.py:25721, 25731-25736). RETRACTED early concern; tests confirm.

E. Section 8 promotion gate omitted Wilson lower CI check. Promoted
   strategies could fail the statistical-significance bar from SKILL.md.

These tests pin the rebuild's contract before code is written.
"""
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "alpha_audit.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────


def _make_eval_db(path: Path) -> sqlite3.Connection:
    """Create a tmp SQLite with the eval/settled tables alpha_audit reads.

    Schema is a minimal projection of state.db — only columns the script
    queries — so tests stay independent of unrelated schema churn.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            event_ticker TEXT,
            asset TEXT,
            filter_stage TEXT NOT NULL,
            evaluation_time TEXT NOT NULL,
            market_price INTEGER,
            seconds_to_close REAL,
            calibrated_prob REAL,
            edge REAL,
            fee_adjusted_edge REAL,
            z_score REAL,
            market_result TEXT,
            status TEXT,
            counterfactual_pnl INTEGER,
            position_size INTEGER,
            strategy TEXT,
            side TEXT DEFAULT 'yes',
            order_id TEXT,
            order_outcome TEXT,
            available_balance_cents INTEGER,
            product_type TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE settled_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            event_ticker TEXT,
            asset TEXT,
            settled_at TEXT NOT NULL,
            entry_price_cents INTEGER,
            count INTEGER,
            seconds_to_close REAL,
            market_result TEXT,
            side TEXT DEFAULT 'yes',
            pnl_cents INTEGER,
            fee_cents INTEGER,
            product_type TEXT
        )
        """
    )
    conn.commit()
    return conn


def _insert_eval(conn, **kwargs):
    """Insert one evaluated_opportunities row with sensible defaults."""
    defaults = {
        "ticker": "TEST-T1",
        "event_ticker": "TEST-EVENT",
        "asset": "BTC",
        "filter_stage": "candidate",
        "evaluation_time": "2026-05-04T12:00:00",
        "market_price": 90,
        "seconds_to_close": 250,
        "calibrated_prob": 0.95,
        "edge": 0.05,
        "fee_adjusted_edge": 0.04,
        "z_score": -3.0,
        "market_result": "yes",
        "status": "settled",
        "counterfactual_pnl": 0,
        "position_size": 5,
        "strategy": "TAKER_NOW",
        "side": "yes",
        "order_id": None,
        "order_outcome": None,
        "available_balance_cents": 100000,
        "product_type": None,
    }
    defaults.update(kwargs)
    cols = ", ".join(defaults)
    placeholders = ", ".join(["?"] * len(defaults))
    conn.execute(
        f"INSERT INTO evaluated_opportunities ({cols}) VALUES ({placeholders})",
        list(defaults.values()),
    )


# ─────────────────────────────────────────────────────────────────────────
# Importable surface — alpha_audit must expose these helpers
# ─────────────────────────────────────────────────────────────────────────


def test_module_imports_clean():
    """alpha_audit must import without side-effects (no DB connect at import)."""
    spec_path = SCRIPT_PATH
    assert spec_path.exists(), "alpha_audit.py must exist"
    src = spec_path.read_text()
    # No top-level sqlite3.connect (would run on import).
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#") or not s:
            continue
        # connect inside a def/class is fine; bare top-level is not.
        if s.startswith("sqlite3.connect"):
            pytest.fail(f"top-level sqlite3.connect: {s!r}")


def test_classify_stage_categories():
    """Stage classifier must distinguish candidate/shadow/block/hard_reject."""
    import alpha_audit  # noqa: E402

    f = alpha_audit.classify_stage
    # CANDIDATE: the actual fill path.
    assert f("candidate") == "CANDIDATE"

    # HARD_REJECT: pre-evaluation stop.
    for s in ("insufficient_edge", "price_out_of_range", "zero_sizing",
              "silent_loss_cooldown", "silent_vol_none", "dead_hour_passed",
              "usaft_short_stc"):
        assert f(s) == "HARD_REJECT", s

    # BLOCK: would-have-traded but cell-block intervened.
    for s in ("TM98_97_98C_2_5MIN_BLEED",
              "SOL_TAKER_85_89C_2_5MIN_BLEED",
              "96C_SOL_XRP_STC_DANGER_BAND",
              "tm96_calmlp_gate_blocked"):
        assert f(s) == "BLOCK", s

    # SHADOW: untaken evaluation, still tracked.
    for s in ("decided_contract_t1", "decided_contract_t2",
              "relaxed_edge_shadow", "weekend_discount_shadow",
              "overnight_discount_shadow", "low_price_shadow",
              "floor_raise_shadow", "no_side_price_shadow_no_xrp",
              "tm_nbbo_buffer_shadow", "stc_extended_floor_shadow",
              "golden_hour_shadow", "dead_hour_shadow_2.0x"):
        assert f(s) == "SHADOW", s

    # Unknown but matches BLOCK heuristic (all-caps + BLEED) → BLOCK.
    assert f("FOO_BAR_NEW_BLEED") == "BLOCK"
    # Unknown but suffix _shadow → SHADOW.
    assert f("foo_bar_shadow") == "SHADOW"
    # Truly unknown → UNKNOWN (so it surfaces, doesn't get silently dropped).
    assert f("totally_made_up_stage") == "UNKNOWN"


def test_wilson_lower_known_values():
    """Wilson 95% lower-bound matches scipy reference values."""
    import alpha_audit

    # n=0 → defined as 0.0 (no signal).
    assert alpha_audit.wilson_lower(0, 0) == 0.0

    # n=100, w=85 → Wilson lower ~76.7%.
    lb = alpha_audit.wilson_lower(85, 100)
    assert 0.76 < lb < 0.78, lb

    # n=50, w=47 (94% WR) → Wilson lower ~84.0%.
    lb = alpha_audit.wilson_lower(47, 50)
    assert 0.83 < lb < 0.85, lb

    # n=200, w=190 (95% WR) → Wilson lower ~91.1%.
    lb = alpha_audit.wilson_lower(190, 200)
    assert 0.90 < lb < 0.92, lb

    # Perfect record n=10 w=10 → asymmetric Wilson > 0.69.
    lb = alpha_audit.wilson_lower(10, 10)
    assert lb > 0.69


def test_is_win_side_aware():
    """A NO opp wins when market_result is NO, not YES. (Bug C.)"""
    import alpha_audit

    f = alpha_audit.is_win
    # YES side
    assert f("yes", "yes") is True
    assert f("yes", "all_yes") is True
    assert f("yes", "no") is False
    assert f("yes", "all_no") is False
    # NO side
    assert f("no", "no") is True
    assert f("no", "all_no") is True
    assert f("no", "yes") is False
    assert f("no", "all_yes") is False
    # Default to 'yes' when side is None or empty
    assert f(None, "yes") is True
    assert f("", "yes") is True
    # Unrecognized result
    assert f("yes", "void") is False
    assert f("no", None) is False


# ─────────────────────────────────────────────────────────────────────────
# Funnel — sum invariant + tier separation
# ─────────────────────────────────────────────────────────────────────────


def test_funnel_sums_to_total(tmp_path):
    """Section 1 funnel must account for every row, summing to total."""
    import alpha_audit

    db = tmp_path / "fn.db"
    conn = _make_eval_db(db)
    # Insert 100 rows across 5 stages.
    for i in range(40):
        _insert_eval(conn, ticker=f"T{i}", filter_stage="insufficient_edge")
    for i in range(30):
        _insert_eval(conn, ticker=f"T{i+40}", filter_stage="candidate",
                     order_id=f"ORD{i}", order_outcome="filled")
    for i in range(15):
        _insert_eval(conn, ticker=f"T{i+70}", filter_stage="low_price_shadow",
                     position_size=None)
    for i in range(10):
        _insert_eval(conn, ticker=f"T{i+85}", filter_stage="TM98_97_98C_2_5MIN_BLEED",
                     strategy="terminal_momentum_98", market_price=98,
                     position_size=50, counterfactual_pnl=200)
    for i in range(5):
        _insert_eval(conn, ticker=f"T{i+95}", filter_stage="totally_unknown_new")
    conn.commit()

    funnel = alpha_audit.compute_funnel(conn, since="2026-05-01T00:00:00",
                                        asset_filter="")
    total = sum(funnel["by_tier"].values())
    assert total == 100, funnel
    # Tiers correctly populated
    assert funnel["by_tier"]["HARD_REJECT"] == 40
    assert funnel["by_tier"]["CANDIDATE"] == 30
    assert funnel["by_tier"]["SHADOW"] == 15
    assert funnel["by_tier"]["BLOCK"] == 10
    assert funnel["by_tier"]["UNKNOWN"] == 5
    # Candidate tier reports filled / submitted breakdown
    assert funnel["candidate_filled"] == 30


def test_funnel_excludes_non_15m(tmp_path):
    """Sections must apply 15M filter: non-15M product_type rows excluded."""
    import alpha_audit

    db = tmp_path / "fn2.db"
    conn = _make_eval_db(db)
    for i in range(10):
        _insert_eval(conn, ticker=f"T{i}", filter_stage="candidate",
                     product_type=None)
    for i in range(5):
        _insert_eval(conn, ticker=f"H{i}", filter_stage="candidate",
                     product_type="hourly")
    for i in range(3):
        _insert_eval(conn, ticker=f"W{i}", filter_stage="candidate",
                     product_type="weather")
    conn.commit()

    funnel = alpha_audit.compute_funnel(conn, since="2026-05-01T00:00:00",
                                        asset_filter="")
    # Only the 10 NULL-product_type rows should count.
    assert sum(funnel["by_tier"].values()) == 10


# ─────────────────────────────────────────────────────────────────────────
# Shadow promotion — dynamic discovery + side-aware WR + Wilson
# ─────────────────────────────────────────────────────────────────────────


def test_shadow_discovery_dynamic(tmp_path):
    """Section 8 must discover stages from DB, not a hardcoded list."""
    import alpha_audit

    db = tmp_path / "sh.db"
    conn = _make_eval_db(db)
    # Two stages NOT in any plausible hardcoded list.
    for i in range(60):
        _insert_eval(conn, ticker=f"A{i}", filter_stage="brand_new_shadow_xyz",
                     position_size=5, counterfactual_pnl=50,
                     market_price=92, market_result="yes")
    for i in range(60):
        _insert_eval(conn, ticker=f"B{i}", filter_stage="another_unseen_shadow",
                     position_size=5, counterfactual_pnl=-30,
                     market_price=92, market_result="no")
    conn.commit()

    shadows = alpha_audit.compute_shadows(conn, since="2026-05-01T00:00:00",
                                          asset_filter="")
    stage_names = {s["stage"] for s in shadows}
    assert "brand_new_shadow_xyz" in stage_names
    assert "another_unseen_shadow" in stage_names


def test_shadow_no_side_wr_inverted(tmp_path):
    """Bug C regression: NO-side shadow WR is correct (not inverted)."""
    import alpha_audit

    db = tmp_path / "no.db"
    conn = _make_eval_db(db)
    # 10 NO-side opps: 8 with result=no (wins), 2 with result=yes (losses)
    for i in range(8):
        _insert_eval(conn, ticker=f"N{i}", filter_stage="no_side_price_shadow_no_xrp",
                     side="no", market_result="no", position_size=5,
                     counterfactual_pnl=400, market_price=12)
    for i in range(2):
        _insert_eval(conn, ticker=f"L{i}", filter_stage="no_side_price_shadow_no_xrp",
                     side="no", market_result="yes", position_size=5,
                     counterfactual_pnl=-100, market_price=12)
    conn.commit()

    shadows = alpha_audit.compute_shadows(conn, since="2026-05-01T00:00:00",
                                          asset_filter="")
    no_shadow = [s for s in shadows if s["stage"] == "no_side_price_shadow_no_xrp"][0]
    assert no_shadow["wins"] == 8, no_shadow
    assert no_shadow["losses"] == 2
    assert abs(no_shadow["wr"] - 0.80) < 0.001


def test_shadow_promotion_gates(tmp_path):
    """Section 8 promotion verdict requires Wilson > BE + n>=50 + WR>BE+2pp + PnL>0."""
    import alpha_audit

    db = tmp_path / "pg.db"
    conn = _make_eval_db(db)
    # Case 1: high WR but n=20 → KEEP (insufficient sample)
    for i in range(19):
        _insert_eval(conn, ticker=f"S1W{i}", filter_stage="case1_shadow",
                     market_price=90, market_result="yes",
                     counterfactual_pnl=50, position_size=5)
    _insert_eval(conn, ticker="S1L0", filter_stage="case1_shadow",
                 market_price=90, market_result="no",
                 counterfactual_pnl=-450, position_size=5)
    # Case 2: n=200, w=190, avg price=88 → BE 88.6%, WR 95% → PROMOTE
    for i in range(190):
        _insert_eval(conn, ticker=f"S2W{i}", filter_stage="case2_shadow",
                     market_price=88, market_result="yes",
                     counterfactual_pnl=60, position_size=5)
    for i in range(10):
        _insert_eval(conn, ticker=f"S2L{i}", filter_stage="case2_shadow",
                     market_price=88, market_result="no",
                     counterfactual_pnl=-440, position_size=5)
    # Case 3: n=200, w=180, avg price=90 → WR 90%, BE 90.6% → KEEP/KILL (not promote)
    for i in range(180):
        _insert_eval(conn, ticker=f"S3W{i}", filter_stage="case3_shadow",
                     market_price=90, market_result="yes",
                     counterfactual_pnl=50, position_size=5)
    for i in range(20):
        _insert_eval(conn, ticker=f"S3L{i}", filter_stage="case3_shadow",
                     market_price=90, market_result="no",
                     counterfactual_pnl=-450, position_size=5)
    conn.commit()

    shadows = alpha_audit.compute_shadows(conn, since="2026-05-01T00:00:00",
                                          asset_filter="")
    by_stage = {s["stage"]: s for s in shadows}
    assert by_stage["case1_shadow"]["verdict"] == "KEEP"
    assert by_stage["case2_shadow"]["verdict"] == "PROMOTE"
    # case3 must NOT promote even though WR is high.
    assert by_stage["case3_shadow"]["verdict"] != "PROMOTE"


def test_shadow_pnl_uses_cf_pnl_when_populated(tmp_path):
    """When counterfactual_pnl is populated, sum it directly (it's net + Kelly)."""
    import alpha_audit

    db = tmp_path / "cf.db"
    conn = _make_eval_db(db)
    # 10 rows, each with cf_pnl=1000c → total 10000c = $100.00
    for i in range(10):
        _insert_eval(conn, ticker=f"C{i}", filter_stage="cf_test_shadow",
                     counterfactual_pnl=1000, position_size=10,
                     market_price=88, market_result="yes")
    conn.commit()

    shadows = alpha_audit.compute_shadows(conn, since="2026-05-01T00:00:00",
                                          asset_filter="")
    s = [r for r in shadows if r["stage"] == "cf_test_shadow"][0]
    assert s["pnl_cents"] == 10000
    # Source: should be 'cf_pnl' (not the recomputed fallback)
    assert s["pnl_source"] == "cf_pnl"


def test_shadow_pnl_flags_1ct_sim_when_size_null(tmp_path):
    """Shadows with NULL position_size: bot/_impl.py falls back to 1ct sim. Flag it."""
    import alpha_audit

    db = tmp_path / "1ct.db"
    conn = _make_eval_db(db)
    for i in range(60):
        _insert_eval(conn, ticker=f"X{i}", filter_stage="floor_raise_shadow",
                     counterfactual_pnl=10, position_size=None,
                     market_price=90, market_result="yes")
    conn.commit()

    shadows = alpha_audit.compute_shadows(conn, since="2026-05-01T00:00:00",
                                          asset_filter="")
    s = [r for r in shadows if r["stage"] == "floor_raise_shadow"][0]
    # flag must indicate 1ct-sim. Either via a tag/note field or pnl_scale.
    assert s.get("size_basis") in ("1ct_sim", "unsized"), s


# ─────────────────────────────────────────────────────────────────────────
# Cell-block effectiveness (NEW Section)
# ─────────────────────────────────────────────────────────────────────────


def test_cell_block_section_reports_per_block(tmp_path):
    """Section 9: per-block stage, foregone PnL + disaster catch rate."""
    import alpha_audit

    db = tmp_path / "blk.db"
    conn = _make_eval_db(db)
    # Block stage: 75 rows, 75 wins → blocks were costing wins (foregone PnL > 0)
    for i in range(75):
        _insert_eval(conn, ticker=f"B{i}", filter_stage="TM98_97_98C_2_5MIN_BLEED",
                     market_price=98, market_result="yes",
                     counterfactual_pnl=130, position_size=65,
                     strategy="terminal_momentum_98")
    # 5 cell-block rows that lost (would-have-been-disaster, the block worked)
    for i in range(5):
        _insert_eval(conn, ticker=f"D{i}", filter_stage="TM98_97_98C_2_5MIN_BLEED",
                     market_price=98, market_result="no",
                     counterfactual_pnl=-9700, position_size=65,
                     strategy="terminal_momentum_98")
    conn.commit()

    blocks = alpha_audit.compute_block_effectiveness(
        conn, since="2026-05-01T00:00:00", asset_filter="")
    by_stage = {b["stage"]: b for b in blocks}
    assert "TM98_97_98C_2_5MIN_BLEED" in by_stage
    b = by_stage["TM98_97_98C_2_5MIN_BLEED"]
    assert b["n"] == 80
    assert b["disasters_caught"] == 5
    # foregone_pnl_cents ≈ wins×130 + losses×(-9700) summed
    expected = 75 * 130 + 5 * -9700
    assert b["foregone_pnl_cents"] == expected


# ─────────────────────────────────────────────────────────────────────────
# Top opportunities — ranked + significance-gated
# ─────────────────────────────────────────────────────────────────────────


def test_top_opportunities_significance_gated(tmp_path):
    """Section 10: only candidates with Wilson_lower > breakeven appear."""
    import alpha_audit

    db = tmp_path / "top.db"
    conn = _make_eval_db(db)
    # Stage A: high apparent WR but n=20 (Wilson too wide) → must NOT appear
    for i in range(19):
        _insert_eval(conn, ticker=f"A{i}", filter_stage="hot_new_shadow",
                     market_price=88, market_result="yes",
                     counterfactual_pnl=60, position_size=5)
    _insert_eval(conn, ticker="A_loss", filter_stage="hot_new_shadow",
                 market_price=88, market_result="no",
                 counterfactual_pnl=-440, position_size=5)
    # Stage B: n=200 95% WR → appears
    for i in range(190):
        _insert_eval(conn, ticker=f"B{i}", filter_stage="solid_shadow",
                     market_price=88, market_result="yes",
                     counterfactual_pnl=60, position_size=5)
    for i in range(10):
        _insert_eval(conn, ticker=f"BL{i}", filter_stage="solid_shadow",
                     market_price=88, market_result="no",
                     counterfactual_pnl=-440, position_size=5)
    conn.commit()

    opps = alpha_audit.compute_top_opportunities(
        conn, since="2026-05-01T00:00:00",
        asset_filter="", days=14)
    stages = [o.get("stage") for o in opps]
    assert "solid_shadow" in stages
    assert "hot_new_shadow" not in stages


# ─────────────────────────────────────────────────────────────────────────
# Smoke / integration: script runs end-to-end on a tmp DB
# ─────────────────────────────────────────────────────────────────────────


def test_script_runs_end_to_end(tmp_path):
    """Run the script as a subprocess against a tmp DB; non-zero exit fails."""
    db = tmp_path / "smoke.db"
    conn = _make_eval_db(db)
    for i in range(10):
        _insert_eval(conn, ticker=f"T{i}", filter_stage="candidate")
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--db", str(db), "--days", "30"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"alpha_audit exited {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    # Output must include each section header.
    out = result.stdout
    assert "Filter Funnel" in out
    assert "Shadow" in out  # Section 8
    assert "Block" in out or "Cell" in out  # Section 9


# ─────────────────────────────────────────────────────────────────────────
# Existing AST regression — preserve fee-correctness on settled_trades
# ─────────────────────────────────────────────────────────────────────────


def test_known_block_stages_match_canonical_set():
    """AST guard: KNOWN_BLOCK_STAGES must equal the canonical bleed-cell set.

    If a future PR drops one of these from the constant, this test fails.
    Round-2 review #1C-2. SOL_BLEED_V2 added 2026-05-10 (ticket 86b9vqt3f).
    """
    import alpha_audit
    expected = {
        "TM98_97_98C_2_5MIN_BLEED",
        "SOL_TAKER_85_89C_2_5MIN_BLEED",
        "SOL_BLEED_V2_88_93C_2_5MIN",
        "96C_SOL_XRP_STC_DANGER_BAND",
        "tm96_calmlp_gate_blocked",
    }
    assert set(alpha_audit.KNOWN_BLOCK_STAGES) == expected


def test_no_hardcoded_shadow_stages_constant():
    """Regression guard against re-introducing a hardcoded shadow stage list.

    The original alpha_audit.py shipped with `SHADOW_STAGES = [...]` of 6
    stages, missing 20+ live shadows. The rebuild discovers shadows
    dynamically. This test fails if any future refactor adds a
    `SHADOW_STAGES` / `KNOWN_SHADOW_STAGES` / `FALLBACK_SHADOW_STAGES`
    constant. Round-2 review #1C-3.
    """
    import re
    src = SCRIPT_PATH.read_text()
    forbidden = re.findall(
        r"\b(SHADOW_STAGES|KNOWN_SHADOW_STAGES|FALLBACK_SHADOW_STAGES|HARDCODED_SHADOW_STAGES)\s*=",
        src,
    )
    assert not forbidden, (
        f"hardcoded shadow stage list reintroduced: {forbidden}. "
        f"Use classify_stage() heuristics instead."
    )


def test_breakeven_wr_includes_taker_fee():
    """Numerical invariant: breakeven_wr(p) = (p + taker_fee(1,p)) / 100.

    Locks the fee-correctness contract: if anyone changes the taker fee
    formula without updating breakeven, this test fails. Round-2 #1C-4.
    """
    import alpha_audit
    for price_cents in (50, 75, 80, 86, 88, 90, 93, 95, 97, 99):
        be = alpha_audit.breakeven_wr(price_cents)
        fee = alpha_audit.taker_fee_cents(1, price_cents)
        expected = (price_cents + fee) / 100.0
        assert abs(be - expected) < 1e-9, (
            f"price {price_cents}: BE={be} expected={expected} fee={fee}"
        )


def test_shadow_promotion_wilson_is_load_bearing(tmp_path):
    """Mutation test: removing the Wilson check would let case-4 PROMOTE.

    Case 4: n=200, w=160 (80% WR), price=70c (BE≈70.6%, BE+2pp=72.6%).
    WR > BE+2pp ✓, PnL > 0 ✓. But Wilson_lower at n=200,w=160 ≈ 73.9%,
    which IS above BE here — recompute carefully.

    Actually safer mutation: n=60, w=51 (85% WR), price=80c (BE≈81.4%).
    WR (85%) > BE+2pp (83.4%) ✓, PnL > 0 ✓. Wilson_lower at n=60, w=51
    is ~73.5% which is BELOW BE 81.4% → must NOT promote.
    Round-2 #1C-1.
    """
    import alpha_audit

    db = tmp_path / "wilson.db"
    conn = _make_eval_db(db)
    for i in range(51):
        _insert_eval(conn, ticker=f"W{i}", filter_stage="case4_shadow",
                     market_price=80, market_result="yes",
                     counterfactual_pnl=200, position_size=10)
    for i in range(9):
        _insert_eval(conn, ticker=f"L{i}", filter_stage="case4_shadow",
                     market_price=80, market_result="no",
                     counterfactual_pnl=-820, position_size=10)
    conn.commit()
    shadows = alpha_audit.compute_shadows(
        conn, since="2026-05-01T00:00:00", asset_filter="")
    s = [r for r in shadows if r["stage"] == "case4_shadow"][0]
    # Sanity: WR clears BE+2pp and PnL>0; only Wilson can block.
    assert s["wr"] > s["breakeven_wr"] + 0.02
    assert s["pnl_cents"] > 0
    assert s["wilson_lower"] < s["breakeven_wr"], (
        f"fixture broken: Wilson_lo {s['wilson_lower']} not < BE {s['breakeven_wr']}"
    )
    assert s["verdict"] != "PROMOTE"


def test_hardreject_by_price_side_aware(tmp_path):
    """Round-2 #1A-1: NO-side insufficient_edge wins on result='no'."""
    import alpha_audit

    db = tmp_path / "hr.db"
    conn = _make_eval_db(db)
    # 5 NO-side insufficient_edge rows at 90c, all result='no' → all wins.
    for i in range(5):
        _insert_eval(conn, ticker=f"N{i}", filter_stage="insufficient_edge",
                     side="no", market_price=90, market_result="no",
                     status="settled", counterfactual_pnl=100)
    conn.commit()
    rows = alpha_audit.compute_hardreject_by_price(
        conn, since="2026-05-01T00:00:00", asset_filter="")
    by_price = {r["market_price"]: r for r in rows}
    assert by_price[90]["wins"] == 5
    assert by_price[90]["settled"] == 5


def test_promote_with_fill_risk_flag(tmp_path):
    """Round-2 #1A-3: implausible daily $/balance ratio downgrades verdict."""
    import alpha_audit

    db = tmp_path / "fr.db"
    conn = _make_eval_db(db)
    # Build a stage that would PROMOTE, then claim daily PnL 100x balance.
    # Balance $4.95 = 495c so 10% = 49.5c. We need daily > 49.5c.
    # n=200 settled, all wins, cf_pnl=10000c each → total 2,000,000c
    # Over 14d: 2,000,000/14 = 142,857c/day. >> 49.5c. Trips IMPLAUSIBLE_FILL.
    for i in range(190):
        _insert_eval(conn, ticker=f"W{i}", filter_stage="huge_pnl_shadow",
                     market_price=70, market_result="yes",
                     counterfactual_pnl=10000, position_size=147)
    for i in range(10):
        _insert_eval(conn, ticker=f"L{i}", filter_stage="huge_pnl_shadow",
                     market_price=70, market_result="no",
                     counterfactual_pnl=-7000, position_size=147)
    conn.commit()

    opps = alpha_audit.compute_top_opportunities(
        conn, since="2026-05-01T00:00:00",
        asset_filter="", days=14, balance_cents=49500)  # $495 balance
    assert opps, "expected one opportunity"
    o = opps[0]
    assert o["verdict"] == "PROMOTE_WITH_FILL_RISK", o
    assert any("IMPLAUSIBLE_FILL" in f for f in o["flags"]), o


def test_explicit_stage_classification(tmp_path):
    """Stages with non-obvious names get explicit classifications.

    Round-2 #1B-1: terminal_momentum, weekend_discount etc. were UNKNOWN
    in round 1. Now explicit.
    """
    import alpha_audit
    f = alpha_audit.classify_stage
    assert f("terminal_momentum") == "SHADOW"
    assert f("weekend_discount") == "SHADOW"
    assert f("overnight_discount") == "SHADOW"
    assert f("usaft_short_stc") == "HARD_REJECT"
    # New HARD_REJECT additions
    assert f("low_probability") == "HARD_REJECT"
    assert f("no_best_ask") == "HARD_REJECT"
    assert f("no_orderbook") == "HARD_REJECT"
    assert f("strategy_wait") == "HARD_REJECT"
    assert f("threshold_implausible") == "HARD_REJECT"
    assert f("silent_spot_none") == "HARD_REJECT"


def test_balance_unknown_flag_when_no_balance(tmp_path):
    """Round-2 #2A: balance=None → BALANCE_UNKNOWN flag, gate not silently skipped."""
    import alpha_audit

    db = tmp_path / "bu.db"
    conn = _make_eval_db(db)
    # Note: NO available_balance_cents column populated.
    for i in range(60):
        _insert_eval(conn, ticker=f"W{i}", filter_stage="ok_shadow",
                     market_price=80, market_result="yes",
                     counterfactual_pnl=100, position_size=10,
                     available_balance_cents=None)
    for i in range(2):
        _insert_eval(conn, ticker=f"L{i}", filter_stage="ok_shadow",
                     market_price=80, market_result="no",
                     counterfactual_pnl=-820, position_size=10,
                     available_balance_cents=None)
    conn.commit()
    opps = alpha_audit.compute_top_opportunities(
        conn, since="2026-05-01T00:00:00", asset_filter="", days=14,
        balance_cents=None)
    if opps:
        assert any("BALANCE_UNKNOWN" in f for o in opps for f in o["flags"]), opps


def test_balance_zero_treated_as_unknown(tmp_path):
    """Round-2 #2A: balance=0 also triggers BALANCE_UNKNOWN, never zero-div."""
    import alpha_audit

    db = tmp_path / "bz.db"
    conn = _make_eval_db(db)
    for i in range(60):
        _insert_eval(conn, ticker=f"W{i}", filter_stage="ok2_shadow",
                     market_price=80, market_result="yes",
                     counterfactual_pnl=100, position_size=10)
    for i in range(2):
        _insert_eval(conn, ticker=f"L{i}", filter_stage="ok2_shadow",
                     market_price=80, market_result="no",
                     counterfactual_pnl=-820, position_size=10)
    conn.commit()
    opps = alpha_audit.compute_top_opportunities(
        conn, since="2026-05-01T00:00:00", asset_filter="", days=14,
        balance_cents=0)
    if opps:
        assert any("BALANCE_UNKNOWN" in f for o in opps for f in o["flags"]), opps


def test_regime_banner_bounds_to_now(capsys):
    """Round-2 #2B: regime banner only fires for cutoffs IN [since, now)."""
    import alpha_audit

    # Future-dated cutoff: should not fire even if since < cutoff.
    saved = alpha_audit.REGIME_CUTOFFS
    try:
        alpha_audit.REGIME_CUTOFFS = [
            ("2099-01-01T00:00:00", "future regime"),
        ]
        alpha_audit.render_regime_banner(
            since_iso="2026-04-01T00:00:00",
            now_iso="2026-05-04T00:00:00",
        )
        out = capsys.readouterr().out
        assert "REGIME NOTE" not in out, "future cutoff must not trigger"

        # Past cutoff inside window: must fire.
        alpha_audit.REGIME_CUTOFFS = [
            ("2026-04-30T16:16:00", "in-window cutoff"),
        ]
        alpha_audit.render_regime_banner(
            since_iso="2026-04-01T00:00:00",
            now_iso="2026-05-04T00:00:00",
        )
        out = capsys.readouterr().out
        assert "REGIME NOTE" in out
        assert "in-window cutoff" in out
    finally:
        alpha_audit.REGIME_CUTOFFS = saved


def test_settled_trades_no_side_wins_correct(tmp_path):
    """Round-3 regression: NO-side settled trades win on market_result='no'.

    Production has ~109 NO-side settled trades / 30d (mostly weather NO).
    Prior compute_settled_by_asset/stc/weekend used `market_result='yes'`
    blindly → NO-side wins inverted.
    """
    import alpha_audit

    db = tmp_path / "ns.db"
    conn = _make_eval_db(db)
    # 5 NO-side trades on BTC, settled_at recent, all result='no' → 5 wins
    for i in range(5):
        conn.execute("""
            INSERT INTO settled_trades (
                ticker, event_ticker, asset, settled_at, entry_price_cents,
                count, seconds_to_close, market_result, side, pnl_cents, fee_cents
            ) VALUES (?, ?, 'BTC', ?, 12, 10, 200, 'no', 'no', 880, 1)
        """, (f"BTC-N{i}", "BTC-EVENT", "2026-05-04T12:00:00"))
    # 5 YES-side trades on BTC, all result='yes' → 5 wins
    for i in range(5):
        conn.execute("""
            INSERT INTO settled_trades (
                ticker, event_ticker, asset, settled_at, entry_price_cents,
                count, seconds_to_close, market_result, side, pnl_cents, fee_cents
            ) VALUES (?, ?, 'BTC', ?, 88, 10, 200, 'yes', 'yes', 120, 1)
        """, (f"BTC-Y{i}", "BTC-EVENT", "2026-05-04T13:00:00"))
    conn.commit()

    rows = alpha_audit.compute_settled_by_asset(
        conn, since="2026-05-01T00:00:00", asset_filter="")
    btc = [r for r in rows if r["asset"] == "BTC"][0]
    assert btc["trades"] == 10
    assert btc["wins"] == 10, btc  # both sides won

    stc_rows = alpha_audit.compute_settled_by_stc(
        conn, since="2026-05-01T00:00:00", asset_filter="")
    # seconds_to_close=200 → '200-300' bucket (boundary < 300).
    bucket = [r for r in stc_rows if r["stc_bucket"] == "200-300"][0]
    assert bucket["wins"] == 10

    we_rows = alpha_audit.compute_weekend_split(
        conn, since="2026-05-01T00:00:00", asset_filter="")
    total_wins = sum(r["wins"] for r in we_rows)
    assert total_wins == 10


def test_settled_trades_legacy_null_side_treated_as_yes(tmp_path):
    """Some historical rows pre-date the side column. Treat NULL as YES."""
    import alpha_audit

    db = tmp_path / "ls.db"
    conn = _make_eval_db(db)
    for i in range(3):
        conn.execute("""
            INSERT INTO settled_trades (
                ticker, event_ticker, asset, settled_at, entry_price_cents,
                count, seconds_to_close, market_result, side, pnl_cents, fee_cents
            ) VALUES (?, 'E', 'ETH', '2026-05-04T12:00:00', 88, 10, 200, 'yes', NULL, 120, 1)
        """, (f"ETH-L{i}",))
    conn.commit()
    rows = alpha_audit.compute_settled_by_asset(
        conn, since="2026-05-01T00:00:00", asset_filter="")
    eth = [r for r in rows if r["asset"] == "ETH"][0]
    assert eth["wins"] == 3


def test_size_basis_uses_settled_denominator(tmp_path):
    """Round-5 regression: size_basis must count sized rows in SETTLED only.

    A stage with 100 pending Kelly-sized rows and 60 settled 1ct-sim rows
    must report 1ct_sim, not kelly.
    """
    import alpha_audit

    db = tmp_path / "sb.db"
    conn = _make_eval_db(db)
    # 60 settled, 1ct (NULL position_size).
    for i in range(60):
        _insert_eval(conn, ticker=f"S{i}", filter_stage="size_test_shadow",
                     status="settled", position_size=None,
                     market_price=88, market_result="yes",
                     counterfactual_pnl=10)
    # 100 pending with full Kelly sizing.
    for i in range(100):
        _insert_eval(conn, ticker=f"P{i}", filter_stage="size_test_shadow",
                     status="pending", position_size=80,
                     market_price=88, market_result=None,
                     counterfactual_pnl=None)
    conn.commit()
    shadows = alpha_audit.compute_shadows(
        conn, since="2026-05-01T00:00:00", asset_filter="")
    s = [r for r in shadows if r["stage"] == "size_test_shadow"][0]
    assert s["size_basis"] == "1ct_sim", s


def test_avg_price_denominator_matches_wr(tmp_path):
    """Round-6 regression: avg_price uses SETTLED prices only.

    Without this, a stage with 50 settled @88c + 100 pending @50c would
    average to 62c → BE 63% → wrongly PROMOTE on WR=90% when the real BE
    at 88c is ~89%.
    """
    import alpha_audit

    db = tmp_path / "ap.db"
    conn = _make_eval_db(db)
    # 50 settled @88c, 90% WR, all wins net positive cf_pnl.
    for i in range(45):
        _insert_eval(conn, ticker=f"S{i}", filter_stage="ap_test_shadow",
                     status="settled", market_price=88,
                     market_result="yes", counterfactual_pnl=100,
                     position_size=10)
    for i in range(5):
        _insert_eval(conn, ticker=f"L{i}", filter_stage="ap_test_shadow",
                     status="settled", market_price=88,
                     market_result="no", counterfactual_pnl=-880,
                     position_size=10)
    # 100 PENDING rows at very different price (50c) — would skew avg_price
    # if both settled and pending were averaged.
    for i in range(100):
        _insert_eval(conn, ticker=f"P{i}", filter_stage="ap_test_shadow",
                     status="pending", market_price=50,
                     market_result=None, counterfactual_pnl=None,
                     position_size=10)
    conn.commit()
    shadows = alpha_audit.compute_shadows(
        conn, since="2026-05-01T00:00:00", asset_filter="")
    s = [r for r in shadows if r["stage"] == "ap_test_shadow"][0]
    # avg_price must reflect SETTLED price only (88), not the (88+50)/2 mix.
    assert abs(s["avg_price"] - 88) < 0.5, s
    # Therefore BE ≈ 0.89, WR 0.90 < BE+0.02 (0.91), Wilson is also < BE.
    # Verdict must be KEEP, not PROMOTE.
    assert s["verdict"] == "KEEP", s


def test_no_bare_sum_pnl_cents_in_alpha_audit():
    """Regression: every SUM(pnl_cents) must subtract COALESCE(fee_cents,0).

    Mirrors test_audit_scripts_net_pnl.py guard. Duplicated here so the
    rebuild can be validated without running the broader suite.
    """
    src = SCRIPT_PATH.read_text()
    import re
    bare = re.compile(r"SUM\s*\(\s*pnl_cents\s*\)")
    corrected = re.compile(
        r"SUM\s*\(\s*pnl_cents\s*-\s*COALESCE\s*\(\s*fee_cents\s*,\s*0\s*\)\s*\)"
    )
    for i, line in enumerate(src.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if bare.search(line) and not corrected.search(line):
            if "noqa:" in line:
                continue
            pytest.fail(f"alpha_audit.py:{i}: bare SUM(pnl_cents) — must use net-of-fees")
