"""Phase 2 P2.3 — HYPE/DOGE live promotion atomic-deploy contract (ClickUp 86b9xv66a).

Pins the post-promote state for HYPE/DOGE: shadow flags flipped to False,
T4-prerequisite constants wired into the elif chains (per the safety
contract written in `bot/constants.py:65-72` at T1 onboarding), and
per-asset MARKET_BLEND_W entries from the B.1 + B.1b shadow-data sweep
([[p2-3-b-live-promotion-blend-weights-may14]] +
[[p2-3-b-live-promotion-price-tier-analysis-may14]]).

Origin: P2.3.b-fu2 B.5 GO path (2026-05-13) — DOGE corpus precision fix
revealed both assets clear the +4% Brier-improvement gate over coinflip
via raw_prob + conservative MARKET_BLEND_W blend, retiring the
cal_mlp-training arc (P2.3.a-d obsolete) and unblocking direct live
promotion without cal_mlp retrain.

Operator-confirmed values (B.1b decision fork Option II, 2026-05-14):

  DOGE: MIN_ENTRY_PRICE=85, MAX_RISK_PER_TRADE=0.10, MARKET_BLEND_W=0.60
  HYPE: MIN_ENTRY_PRICE=90, MAX_RISK_PER_TRADE=0.10, MARKET_BLEND_W=0.80

NBBO_FALLBACK_GATES: HYPE/DOGE INTENTIONALLY OMITTED — orderbook-only
first-step; widen later from observed spread distribution per the
operator's T4 prereq language. `bot/executor.py:5035` handles missing
keys gracefully via `.get(asset)` → None → fallback disabled.

Sister anchors:
  - `test_p2_1_d_per_asset_blend_weights.py` — pins the 4 live-asset
    weights; this test extends EXPECTED to 6 assets (HYPE+DOGE added
    in the same atomic commit).
  - `test_p2_3_e_hype_doge_shadow_gates.py::test_15m_shadow_flags_false_post_p2_3_live_promotion`
    — renamed + flipped in the same atomic commit (was the pre-promote
    `test_15m_shadow_flags_default_true_pre_p2_3_f`).
  - `tests/integration/test_doge_hype_onboarding_t1.py::TestAtomicActivationSafety`
    — sister T1 lock-step test, flipped in the same atomic commit.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
CONSTANTS_PY = REPO_ROOT / "bot" / "constants.py"


# Operator-confirmed B.1b decision Option II (2026-05-14). DOGE clean
# data, HYPE conservative-borderline-EV stance with maker-fill discount
# expected to rescue.
EXPECTED_HYPE_MIN_ENTRY_PRICE = 90
EXPECTED_DOGE_MIN_ENTRY_PRICE = 85
EXPECTED_HYPE_MAX_RISK = 0.10
EXPECTED_DOGE_MAX_RISK = 0.10
EXPECTED_HYPE_BLEND_W = 0.80
EXPECTED_DOGE_BLEND_W = 0.60


# ─────────────────────────────────────────────────────────────────────
# Anchor 1: shadow flags flipped to False (P2.3.f atomic ship signal)
# ─────────────────────────────────────────────────────────────────────

def test_hype_15m_shadow_is_false():
    """HYPE_15M_SHADOW MUST be False post-P2.3 live promotion. Sister
    test in test_p2_3_e_hype_doge_shadow_gates.py was inverted in the
    same atomic commit (was the pre-promote `is True` anchor)."""
    from bot.constants import HYPE_15M_SHADOW
    assert HYPE_15M_SHADOW is False, (
        f"HYPE_15M_SHADOW={HYPE_15M_SHADOW} but P2.3 live promotion "
        f"shipped — flag MUST be False to route HYPE through the main "
        f"YES candidate path. If you intentionally unshipped, revert "
        f"this test too."
    )


def test_doge_15m_shadow_is_false():
    """DOGE_15M_SHADOW MUST be False post-P2.3 live promotion."""
    from bot.constants import DOGE_15M_SHADOW
    assert DOGE_15M_SHADOW is False, (
        f"DOGE_15M_SHADOW={DOGE_15M_SHADOW} but P2.3 live promotion "
        f"shipped — flag MUST be False to route DOGE through the main "
        f"YES candidate path."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 2: per-asset MIN_ENTRY_PRICE constants exist and match B.1b
# ─────────────────────────────────────────────────────────────────────

def test_hype_min_entry_price_pinned():
    """HYPE_MIN_ENTRY_PRICE = 90 per B.1b operator decision Option II.
    Data: HYPE 90+ shadow subset has WR 94.4% post-blend (n=250) with
    Wilson 95% CI [90.9%, 96.7%]. Conservative floor — accepts marginal
    EV that maker-first execution discount should rescue. Per-tier
    90-91 band shows +5.08c/trade (n=24)."""
    from bot.constants import HYPE_MIN_ENTRY_PRICE
    assert HYPE_MIN_ENTRY_PRICE == EXPECTED_HYPE_MIN_ENTRY_PRICE, (
        f"HYPE_MIN_ENTRY_PRICE={HYPE_MIN_ENTRY_PRICE} != "
        f"{EXPECTED_HYPE_MIN_ENTRY_PRICE} (B.1b operator pick). Re-run "
        f"price-tier WR analysis before changing."
    )


def test_doge_min_entry_price_pinned():
    """DOGE_MIN_ENTRY_PRICE = 85 per B.1b operator decision Option II.
    Data: DOGE 85+ shadow subset has WR 95.5% post-blend (n=445) with
    PnL +1.93c/trade — best cumulative floor in the sweep."""
    from bot.constants import DOGE_MIN_ENTRY_PRICE
    assert DOGE_MIN_ENTRY_PRICE == EXPECTED_DOGE_MIN_ENTRY_PRICE, (
        f"DOGE_MIN_ENTRY_PRICE={DOGE_MIN_ENTRY_PRICE} != "
        f"{EXPECTED_DOGE_MIN_ENTRY_PRICE} (B.1b operator pick)."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 3: per-asset MAX_RISK_PER_TRADE constants exist and match B.1b
# ─────────────────────────────────────────────────────────────────────

def test_hype_max_risk_per_trade_pinned():
    """HYPE_MAX_RISK_PER_TRADE = 0.10 — conservative new-asset start.
    Below live BTC/SOL/XRP 0.15 and ETH 0.20."""
    from bot.constants import HYPE_MAX_RISK_PER_TRADE
    assert HYPE_MAX_RISK_PER_TRADE == EXPECTED_HYPE_MAX_RISK, (
        f"HYPE_MAX_RISK_PER_TRADE={HYPE_MAX_RISK_PER_TRADE} != "
        f"{EXPECTED_HYPE_MAX_RISK} (B.1b conservative default)."
    )


def test_doge_max_risk_per_trade_pinned():
    """DOGE_MAX_RISK_PER_TRADE = 0.10."""
    from bot.constants import DOGE_MAX_RISK_PER_TRADE
    assert DOGE_MAX_RISK_PER_TRADE == EXPECTED_DOGE_MAX_RISK, (
        f"DOGE_MAX_RISK_PER_TRADE={DOGE_MAX_RISK_PER_TRADE} != "
        f"{EXPECTED_DOGE_MAX_RISK} (B.1b conservative default)."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 4: MARKET_BLEND_W_BY_ASSET extends to 6 assets including HYPE+DOGE
# ─────────────────────────────────────────────────────────────────────

def test_market_blend_w_by_asset_includes_hype_doge():
    """MARKET_BLEND_W_BY_ASSET dict MUST include HYPE+DOGE entries with
    the B.1 sweep-derived weights (DOGE 0.60, HYPE 0.80). Sister test
    `test_p2_1_d_per_asset_blend_weights.py::test_market_blend_w_by_asset_constant_pinned`
    was updated in the same atomic commit to expect the 6-key dict."""
    from bot.constants import MARKET_BLEND_W_BY_ASSET
    assert "HYPE" in MARKET_BLEND_W_BY_ASSET, (
        f"MARKET_BLEND_W_BY_ASSET missing HYPE key — P2.3 live "
        f"promotion requires per-asset weight to override the legacy "
        f"0.40 scalar fallback. Current keys: {sorted(MARKET_BLEND_W_BY_ASSET)}."
    )
    assert "DOGE" in MARKET_BLEND_W_BY_ASSET, (
        f"MARKET_BLEND_W_BY_ASSET missing DOGE key. Current keys: "
        f"{sorted(MARKET_BLEND_W_BY_ASSET)}."
    )
    assert MARKET_BLEND_W_BY_ASSET["HYPE"] == EXPECTED_HYPE_BLEND_W, (
        f"MARKET_BLEND_W_BY_ASSET['HYPE']={MARKET_BLEND_W_BY_ASSET['HYPE']} != "
        f"{EXPECTED_HYPE_BLEND_W} (B.1 sweep argmin on full-population, "
        f"n=1469 settled shadow rows)."
    )
    assert MARKET_BLEND_W_BY_ASSET["DOGE"] == EXPECTED_DOGE_BLEND_W, (
        f"MARKET_BLEND_W_BY_ASSET['DOGE']={MARKET_BLEND_W_BY_ASSET['DOGE']} != "
        f"{EXPECTED_DOGE_BLEND_W} (B.1 sweep argmin on full-population, "
        f"n=1710 settled shadow rows)."
    )


def test_market_blend_w_by_asset_has_seven_keys():
    """MARKET_BLEND_W_BY_ASSET MUST have exactly 7 keys after P2.4 ship:
    the 4 P2.1.d assets + HYPE + DOGE (P2.3) + BNB (P2.4 2026-05-19,
    86b9zmj37). Locks against accidental addition of an 8th asset
    without a Bit + sweep."""
    from bot.constants import MARKET_BLEND_W_BY_ASSET
    expected_keys = {"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"}
    actual_keys = set(MARKET_BLEND_W_BY_ASSET.keys())
    assert actual_keys == expected_keys, (
        f"MARKET_BLEND_W_BY_ASSET key set drift: got {sorted(actual_keys)} "
        f"vs expected {sorted(expected_keys)}. Adding a new asset "
        f"requires a fresh per-asset sweep + a new Bit."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 5: TM_ASSET_RISK_CAPS dict extended to 6 assets
# ─────────────────────────────────────────────────────────────────────

def test_tm_asset_risk_caps_includes_hype_doge():
    """TM_ASSET_RISK_CAPS at bot/constants.py:1225 MUST include HYPE
    and DOGE entries lock-step with their MAX_RISK_PER_TRADE constants.
    Without these, TM strategy candidates for HYPE/DOGE fall through to
    the dict's default (None → no per-asset cap), defeating the
    purpose of the per-asset risk cap."""
    from bot.constants import (
        TM_ASSET_RISK_CAPS,
        HYPE_MAX_RISK_PER_TRADE,
        DOGE_MAX_RISK_PER_TRADE,
    )
    assert "HYPE" in TM_ASSET_RISK_CAPS, (
        f"TM_ASSET_RISK_CAPS missing HYPE key. Current keys: "
        f"{sorted(TM_ASSET_RISK_CAPS)}."
    )
    assert "DOGE" in TM_ASSET_RISK_CAPS, (
        f"TM_ASSET_RISK_CAPS missing DOGE key. Current keys: "
        f"{sorted(TM_ASSET_RISK_CAPS)}."
    )
    assert TM_ASSET_RISK_CAPS["HYPE"] == HYPE_MAX_RISK_PER_TRADE, (
        f"TM_ASSET_RISK_CAPS['HYPE']={TM_ASSET_RISK_CAPS['HYPE']} != "
        f"HYPE_MAX_RISK_PER_TRADE={HYPE_MAX_RISK_PER_TRADE} — lock-step "
        f"violation."
    )
    assert TM_ASSET_RISK_CAPS["DOGE"] == DOGE_MAX_RISK_PER_TRADE


# ─────────────────────────────────────────────────────────────────────
# Anchor 6: NBBO_FALLBACK_GATES INTENTIONALLY excludes HYPE+DOGE
# ─────────────────────────────────────────────────────────────────────

def test_nbbo_fallback_gates_omits_hype_doge_intentionally():
    """B.1b decision: HYPE/DOGE INTENTIONALLY OMITTED from
    NBBO_FALLBACK_GATES — orderbook-only first-step; widen later from
    observed spread distribution. `bot/executor.py:5035` handles
    missing keys gracefully via `.get(asset)` → None → fallback
    disabled. This anchor pins the intentionally-omitted state so a
    future agent doesn't add HYPE/DOGE without a fresh spread-
    distribution analysis."""
    from bot.constants import NBBO_FALLBACK_GATES
    assert "HYPE" not in NBBO_FALLBACK_GATES, (
        f"NBBO_FALLBACK_GATES['HYPE'] = {NBBO_FALLBACK_GATES.get('HYPE')} "
        f"but P2.3 ship explicitly omits HYPE — orderbook-only first "
        f"step. Adding HYPE here requires a fresh spread-distribution "
        f"analysis on observed HYPE NBBO-fallback samples."
    )
    assert "DOGE" not in NBBO_FALLBACK_GATES, (
        f"NBBO_FALLBACK_GATES['DOGE'] = {NBBO_FALLBACK_GATES.get('DOGE')} "
        f"but P2.3 ship explicitly omits DOGE — see HYPE assertion."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 7: AST guard — scanner MIN_ENTRY_PRICE elif chain has HYPE+DOGE
# ─────────────────────────────────────────────────────────────────────

def test_scanner_min_entry_price_chain_has_hype_doge_branches():
    """`bot/scanner/__init__.py` MUST have `elif asset == "HYPE": ...
    _asset_floor = HYPE_MIN_ENTRY_PRICE` and `elif asset == "DOGE": ...
    _asset_floor = DOGE_MIN_ENTRY_PRICE` branches in the per-asset
    floor elif chain at scanner:~2675-2684. Without these the bot
    falls back to global MIN_ENTRY_PRICE=75c for HYPE/DOGE — the
    safety violation the T4 prereq comment warned about."""
    source = SCANNER_PY.read_text()
    # The 4 live-asset branches all use the same shape:
    # `_asset_floor = <ASSET>_MIN_ENTRY_PRICE`. The HYPE/DOGE branches
    # MUST follow the same shape so the IDE jump-to-def works.
    assert "_asset_floor = HYPE_MIN_ENTRY_PRICE" in source, (
        "scanner MIN_ENTRY_PRICE elif chain missing HYPE branch — "
        "without `_asset_floor = HYPE_MIN_ENTRY_PRICE` the bot uses "
        "global 75c floor for HYPE. See bot/constants.py:65-72."
    )
    assert "_asset_floor = DOGE_MIN_ENTRY_PRICE" in source, (
        "scanner MIN_ENTRY_PRICE elif chain missing DOGE branch."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 8: AST guard — scanner MAX_RISK_PER_TRADE chain has HYPE+DOGE
# ─────────────────────────────────────────────────────────────────────

def test_scanner_max_risk_chain_has_hype_doge_branches():
    """`bot/scanner/__init__.py` MUST have HYPE/DOGE branches in the
    asset-specific risk caps elif chain at scanner:~4690-4713.
    Pattern: `elif asset == "HYPE" and _pt in (None, "15m"):
    _hype_max = int((_sizing_balance * HYPE_MAX_RISK_PER_TRADE) /
    best_ask)`. Without these branches the bot's sizing inherits the
    global MAX_RISK_PER_TRADE cap (currently 0.25 = 25%) — much
    larger than the conservative 0.10 we shipped."""
    source = SCANNER_PY.read_text()
    assert "HYPE_MAX_RISK_PER_TRADE" in source, (
        "scanner has no reference to HYPE_MAX_RISK_PER_TRADE — the "
        "per-asset sizing cap is unused for HYPE."
    )
    assert "DOGE_MAX_RISK_PER_TRADE" in source, (
        "scanner has no reference to DOGE_MAX_RISK_PER_TRADE — the "
        "per-asset sizing cap is unused for DOGE."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 9: AST guard — scanner imports new constants from bot.constants
# ─────────────────────────────────────────────────────────────────────

def test_scanner_imports_new_constants():
    """`bot/scanner/__init__.py` MUST `from bot.constants import
    HYPE_MIN_ENTRY_PRICE, DOGE_MIN_ENTRY_PRICE, HYPE_MAX_RISK_PER_TRADE,
    DOGE_MAX_RISK_PER_TRADE` — without imports the elif branches fail
    with NameError at the first HYPE/DOGE tick.

    AST-walked import block, not regex — robust to formatting drift."""
    tree = ast.parse(SCANNER_PY.read_text())
    imported_from_constants: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.constants":
            for alias in node.names:
                imported_from_constants.add(alias.name)
    required = {
        "HYPE_MIN_ENTRY_PRICE",
        "DOGE_MIN_ENTRY_PRICE",
        "HYPE_MAX_RISK_PER_TRADE",
        "DOGE_MAX_RISK_PER_TRADE",
    }
    missing = required - imported_from_constants
    assert not missing, (
        f"scanner missing `from bot.constants import` entries: "
        f"{sorted(missing)}. Imported names: "
        f"{sorted(imported_from_constants)[:50]}..."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 10: blend-weight + risk-cap bounds (defensive)
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("asset,weight_attr,risk_attr", [
    ("HYPE", "HYPE_BLEND_W", "HYPE_MAX_RISK"),
    ("DOGE", "DOGE_BLEND_W", "DOGE_MAX_RISK"),
])
def test_blend_weight_and_risk_bounds(asset: str, weight_attr: str, risk_attr: str) -> None:
    """Defensive bounds — catch a future agent typo like 0.8 → 8 or
    0.10 → 10."""
    from bot.constants import MARKET_BLEND_W_BY_ASSET
    w = MARKET_BLEND_W_BY_ASSET[asset]
    assert 0.0 <= w <= 1.0, (
        f"MARKET_BLEND_W_BY_ASSET[{asset!r}]={w} out of [0.0, 1.0] — "
        f"probability blend weight must be a fraction."
    )
    from bot.constants import HYPE_MAX_RISK_PER_TRADE, DOGE_MAX_RISK_PER_TRADE
    risk = HYPE_MAX_RISK_PER_TRADE if asset == "HYPE" else DOGE_MAX_RISK_PER_TRADE
    assert 0.0 < risk <= 0.25, (
        f"{asset}_MAX_RISK_PER_TRADE={risk} out of (0.0, 0.25] — "
        f"global MAX_RISK_PER_TRADE is 0.25; per-asset cap must be <= that."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 11: prose-drift guard — tracked user-facing docs MUST NOT claim
#            "four crypto assets" or "(BTC, ETH, SOL, XRP) live"
# ─────────────────────────────────────────────────────────────────────

# This anchor is the structural-pin remediation for the R2/R5/R7/R8 sister-
# paragraph prose-drift class (lesson L97 from P2.1.d, re-played at higher
# surface area in P2.3). 8 adversarial review rounds surfaced the same
# pattern: source-of-truth narrative docs (README.md/template, whitepaper.md,
# whitepaper_investor.md) had per-asset enumerations and "four crypto
# assets" claims that drifted from the canonical 6-asset post-promote
# state. Each round fixed some sites but missed sister paragraphs.
# This test pins the absence of the stale phrase patterns so future agents
# get a CI failure if they reintroduce them.
PROSE_DRIFT_FORBIDDEN_PHRASES = [
    # "four crypto assets" — when 6 are live, this is incorrect.
    "four crypto assets",
    "four assets at multiple strike",
    "15M crypto across four assets",
    # Per-floor enumerations missing HYPE/DOGE.
    # Allow the 4-asset list ONLY in historical/decision/findings paragraphs
    # (those are local-only kb/ files; this anchor only checks tracked
    # user-facing prose). The forbidden form is the active-tense LIVE claim.
    "(BTC, ETH, SOL, XRP) live",
    "(BTC, ETH, SOL, XRP) --- LIVE",
    "BTC, ETH, SOL, XRP (live)",
    "BTC, ETH, SOL, XRP spot prices",  # data-source row — must include HYPE/DOGE post-promote
]
PROSE_DRIFT_TRACKED_DOCS = [
    "README.md",
    "README.template.md",
    "docs/whitepaper/whitepaper.md",
    "docs/whitepaper/whitepaper_investor.md",
    "agent_docs/current_state.md",
    "agent_docs/db_schema.md",
    "agent_docs/calibration_pipeline.md",
    "agent_docs/config_reference.md",
    "CLAUDE.md",
]


@pytest.mark.parametrize("doc_path", PROSE_DRIFT_TRACKED_DOCS)
def test_no_stale_4_asset_prose_in_tracked_user_facing_docs(doc_path: str) -> None:
    """Tracked user/agent-facing docs MUST NOT claim 4-asset live state
    post-P2.3 promotion (2026-05-14, 86b9xv66a). 8 adversarial review
    rounds in P2.3 ship surfaced this drift class repeatedly; this
    structural pin breaks the loop. If a NEW 4-asset literal must be
    introduced (e.g., a historical paragraph describing pre-P2.3 state),
    qualify it with explicit time-bounding language ("until P2.3
    2026-05-14", "pre-P2.3", etc.) so the regex below doesn't catch it."""
    full_path = REPO_ROOT / doc_path
    if not full_path.exists():
        pytest.skip(f"{doc_path} not present")
    content = full_path.read_text()
    found: list[tuple[str, int]] = []
    for phrase in PROSE_DRIFT_FORBIDDEN_PHRASES:
        idx = content.find(phrase)
        if idx >= 0:
            line_no = content[:idx].count("\n") + 1
            found.append((phrase, line_no))
    assert not found, (
        f"{doc_path} contains stale 4-asset prose post-P2.3 promotion: {found}. "
        f"Update to the 6-asset (BTC, ETH, SOL, XRP, HYPE, DOGE) form, or "
        f"qualify with explicit pre-P2.3 time-bounding language. See "
        f"kb/findings/p2-3-b-live-promotion-blend-weights-may14.md for the "
        f"canonical post-promote state."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 12: defensive bounds parametrized — keep last
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("asset,price_attr", [
    ("HYPE", "HYPE_MIN_ENTRY_PRICE"),
    ("DOGE", "DOGE_MIN_ENTRY_PRICE"),
])
def test_min_entry_price_bounds(asset: str, price_attr: str) -> None:
    """Defensive bounds — MIN_ENTRY_PRICE in cents [50, 99]."""
    import bot.constants as constants
    val = getattr(constants, price_attr)
    assert 50 <= val <= 99, (
        f"{price_attr}={val} out of [50, 99] cents."
    )
    # Per-asset floor MUST be >= global MIN_ENTRY_PRICE (defensive — a
    # per-asset floor BELOW global makes no sense). Currently global=75
    # and HYPE=90, DOGE=85 both satisfy.
    assert val >= constants.MIN_ENTRY_PRICE, (
        f"{price_attr}={val} < global MIN_ENTRY_PRICE={constants.MIN_ENTRY_PRICE} — "
        f"per-asset floor cannot be below global floor."
    )
