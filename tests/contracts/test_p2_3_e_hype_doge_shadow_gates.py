"""Phase 2 P2.3.e — comprehensive HYPE/DOGE shadow-gate audit (ClickUp 86b9xm68y).

Live-promotion pre-flight contract: every `candidates.append(...)` site in
`bot/scanner/__init__.py` must have a HYPE/DOGE safety property that holds
BEFORE the candidate is enqueued, so that flipping `HYPE_15M_SHADOW=False`
or `DOGE_15M_SHADOW=False` in a future P2.3.f deploy will not silently
expose HYPE/DOGE to strategies the calibrator hasn't been validated for.

Origin: the XRP-T1-R1 bypass class caught during HYPE/DOGE T1 onboarding
adversarial review (ticket 86b9vecw9, 2026-05-10). The single downstream
`if HYPE_15M_SHADOW and asset == "HYPE": continue` gate at scanner:~5943
intercepts the *primary* YES-side scan path, but Terminal Momentum (TM),
Weekend Discount (WKND), Overnight Discount (OVN), and Decided Contracts
(DC) all call `candidates.append(...)` BEFORE that gate fires — once a
candidate enters the list, the executor picks it up. The T1 fix added
per-strategy filters at TM/WKND/OVN/DC; this test pins them AND extends
to the four remaining `candidates.append` sites (LPNE, BRACKET_NO,
weather_no_live, hourly_no_live) where the HYPE/DOGE safety comes from a
different mechanism (asset-whitelist / product-type / exclusion-set).

Sister anchors:
  - `tests/integration/test_doge_hype_onboarding_t1.py::TestStrategyKillSwitchClauses`
    — the T1 source-walk that originally locked TM/WKND/OVN/DC; renamed
    in P2.3 ship 2026-05-14 to reflect post-promote kill-switch posture.
    This file is broader (every candidates.append site, not just those 4).
  - `bot.constants.LPNE_ASSETS / HOURLY_NO_EXCLUDED_ASSETS / HOURLY_EXCLUDED_ASSETS`
    — the asset-membership sets that gate the non-strategy sites.

Update discipline: extend `EXPECTED_SITES` ONLY when scanner is
intentionally refactored AND the P2.3.e audit has been re-run on the new
shape. Never add an entry to silence a test failure — a missing or
new-unrecognized `candidates.append` site IS the bypass class this test
exists to catch.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
CONSTANTS_PY = REPO_ROOT / "bot" / "constants.py"


# Every candidates.append site, classified by gating form. The line number
# is captured only as a debug aid in the assertion messages — the test
# matches by gating form, not by line number, so drift-by-LOC is tolerated.
# The strategy_anchor is a unique substring that MUST appear in the
# 600-char window IMMEDIATELY PRECEDING the candidates.append call (this
# is the identifier the test uses to locate which AST-found append site
# this entry describes).
EXPECTED_SITES: dict[str, dict[str, str]] = {
    # `strategy_anchor` must be UNIQUE in bot/scanner/__init__.py (count == 1)
    # — it identifies which candidates.append site this entry describes by
    # finding the anchor's CLOSEST occurrence above the append (within 200
    # lines back). All anchors picked here are log-line literals or distinct
    # gate-flag uses; if scanner is refactored and an anchor changes,
    # update this dict in lockstep with the audit re-run.
    "LPNE": {
        "strategy_anchor": "LPNE_CANDIDATE:",  # log line at scanner:~2706
        "gate_form": "asset_whitelist",  # asset in LPNE_ASSETS (which is {"BTC"})
        "rationale": "LPNE_ASSETS == {'BTC'} — HYPE/DOGE excluded by membership",
    },
    "TM": {
        "strategy_anchor": "TM_CANDIDATE:",  # log line at scanner:~3492
        "gate_form": "shadow_filter_in_if",  # and not (HYPE_15M_SHADOW and asset == 'HYPE')
        "rationale": "TM if-block excludes HYPE/DOGE via 'and not (X_15M_SHADOW...)' filters",
    },
    "WKND": {
        "strategy_anchor": "WKND_DISCOUNT_CANDIDATE:",  # log line at scanner:~3895
        "gate_form": "shadow_filter_in_if",
        "rationale": "WKND if-block excludes HYPE/DOGE via 'and not (X_15M_SHADOW...)' filters",
    },
    "OVN": {
        "strategy_anchor": "OVN_DISCOUNT_CANDIDATE:",  # log line at scanner:~4059
        "gate_form": "shadow_filter_in_if",
        "rationale": "OVN if-block excludes HYPE/DOGE via 'and not (X_15M_SHADOW...)' filters",
    },
    "DC": {
        "strategy_anchor": "DC_CANDIDATE:",  # log line at scanner:~4324
        "gate_form": "shadow_clear_flag",  # _dc_live_enabled = False clear-flag
        "rationale": "DC sets _dc_live_enabled=False when HYPE/DOGE shadow before live-gate",
    },
    "BRACKET_NO": {
        "strategy_anchor": "BRACKET_NO_CANDIDATE:",  # log line at scanner:~5462
        "gate_form": "weather_product_type",  # gated by _wx_mtype == "bracket"
        "rationale": "Only fires when _wx_mtype == 'bracket' — weather routing, asset is city",
    },
    "MAIN_YES": {
        "strategy_anchor": "if _sol_high_edge_shadow:",  # immediately above main append at ~6100
        "gate_form": "downstream_of_shadow_gate",  # HYPE/DOGE shadow gates above with continue
        "rationale": "Sits AFTER HYPE/DOGE shadow gates that 'continue' at scanner:~5943/5970",
    },
    "WEATHER_NO_LIVE": {
        "strategy_anchor": "failed (weather_no_live)",  # log line at scanner:~7528
        "gate_form": "weather_product_type",
        "rationale": "product_type='weather' branch — asset is weather city, never HYPE/DOGE",
    },
    "HOURLY_NO_LIVE": {
        "strategy_anchor": "failed (hourly_no_live)",  # log line at scanner:~7621
        "gate_form": "hourly_excluded_set",  # asset not in HOURLY_NO_EXCLUDED_ASSETS
        "rationale": "HOURLY_NO_EXCLUDED_ASSETS ⊇ {HYPE,DOGE} — excluded by membership",
    },
}


def _find_candidates_append_sites(tree: ast.Module) -> list[ast.Call]:
    """AST-walk every `candidates.append(...)` call in the module.

    Matches the receiver `candidates` (a list local to OpportunityScanner.scan()).
    Other `.append` calls (on `c` ring-buffers, score lists, etc.) are skipped.
    """
    sites: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "append":
            continue
        receiver = func.value
        if isinstance(receiver, ast.Name) and receiver.id == "candidates":
            sites.append(node)
    return sites


def _source_slice(source: str, end_lineno: int, lines_back: int) -> str:
    """Return the source text ending at line `end_lineno`, extending back
    `lines_back` lines (or to the start of the file)."""
    lines = source.splitlines()
    start = max(0, end_lineno - lines_back - 1)
    return "\n".join(lines[start:end_lineno])


def _gate_pattern_matches(form: str, window_back: str, window_fwd: str) -> bool:
    """Return True iff the source windows around an append site match the
    declared gate form. `window_back` is the source slice from ~200 lines
    before the append up to the append itself; `window_fwd` is unused for
    now but kept for future extensions (e.g., gating that fires AFTER the
    append via a subsequent `if candidates[-1]...: candidates.pop()` shape)."""
    if form == "asset_whitelist":
        # LPNE: `if (... and asset in LPNE_ASSETS ...)` immediately surrounding
        # the append. The LPNE block is short, so we accept the membership
        # check appearing in the 600-char vicinity above.
        return "asset in LPNE_ASSETS" in window_back
    if form == "shadow_filter_in_if":
        # TM/WKND/OVN: the if-test for the strategy contains both
        # `HYPE_15M_SHADOW` and `DOGE_15M_SHADOW` clauses. The append fires
        # only inside that if-block, so seeing both names in the preceding
        # window is the necessary structural signature.
        return ("HYPE_15M_SHADOW" in window_back
                and "DOGE_15M_SHADOW" in window_back)
    if form == "shadow_clear_flag":
        # DC: `if (HYPE_15M_SHADOW and asset == "HYPE") or (DOGE_15M_SHADOW
        # and asset == "DOGE"): _dc_live_enabled = False` clears the live
        # flag before the outer if (_dc_live_enabled ...) guard.
        return ("_dc_live_enabled = False" in window_back
                and "HYPE_15M_SHADOW" in window_back
                and "DOGE_15M_SHADOW" in window_back)
    if form == "weather_product_type":
        # BRACKET_NO: `if (bot.constants.BRACKET_NO_ENABLED and _wx_mtype ==
        #   "bracket" ...)`. weather_no_live: `product_type="weather"` in the
        # surrounding insert + candidate dict.
        return ('"bracket"' in window_back
                or '"weather"' in window_back
                or "'weather'" in window_back)
    if form == "downstream_of_shadow_gate":
        # MAIN_YES: must see both the HYPE_15M_SHADOW continue-gate AND the
        # DOGE_15M_SHADOW continue-gate within the preceding window. Anchor
        # phrase from the gate body locks the structural shape: each gate
        # writes a `*_shadow` filter_stage row and then `continue`s.
        return (re.search(
                    r'if HYPE_15M_SHADOW and asset == "HYPE"', window_back) is not None
                and re.search(
                    r'if DOGE_15M_SHADOW and asset == "DOGE"', window_back) is not None
                and "hype_shadow" in window_back
                and "doge_shadow" in window_back)
    if form == "hourly_excluded_set":
        # hourly_no_live: `asset not in HOURLY_NO_EXCLUDED_ASSETS` immediately
        # gates the candidate append.
        return "HOURLY_NO_EXCLUDED_ASSETS" in window_back
    raise AssertionError(f"unknown gate_form: {form!r}")


@pytest.fixture(scope="module")
def scanner_source() -> str:
    assert SCANNER_PY.exists(), f"canonical source missing: {SCANNER_PY}"
    return SCANNER_PY.read_text()


@pytest.fixture(scope="module")
def scanner_ast(scanner_source: str) -> ast.Module:
    return ast.parse(scanner_source, filename=str(SCANNER_PY))


@pytest.fixture(scope="module")
def append_sites(scanner_ast: ast.Module) -> list[ast.Call]:
    return _find_candidates_append_sites(scanner_ast)


def test_candidates_append_site_count(append_sites: list[ast.Call]) -> None:
    """Pin the total number of `candidates.append` sites in the scanner.

    A NEW site means a NEW strategy or a refactor — either way the P2.3.e
    audit must be re-run before the new site is allowed. Update this count
    AND add an EXPECTED_SITES entry classifying the new gating form ONLY
    after re-audit. Default-deny: an unrecognized new site is the bypass
    class this test exists to catch.
    """
    actual = len(append_sites)
    expected = len(EXPECTED_SITES)
    assert actual == expected, (
        f"candidates.append site count drift: found {actual}, expected {expected}. "
        f"Sites at lines: {[s.lineno for s in append_sites]}. "
        f"If a new strategy added a candidates.append, re-run the P2.3.e audit "
        f"per kb/findings/p2-3-e-hype-doge-shadow-gate-audit-may13.md "
        f"and extend EXPECTED_SITES with the new gating form."
    )


def _anchor_lineno(source: str, anchor: str) -> int:
    """Locate the (1-indexed) line number of `anchor` in `source`. Requires
    the anchor to appear EXACTLY ONCE — duplicates are silent-misclassifier
    bait. Returns -1 if absent."""
    occurrences = source.count(anchor)
    if occurrences == 0:
        return -1
    if occurrences > 1:
        raise AssertionError(
            f"anchor not unique in bot/scanner/__init__.py "
            f"(found {occurrences}× — must be 1): {anchor!r}"
        )
    idx = source.find(anchor)
    return source[:idx].count("\n") + 1


def test_every_candidates_append_site_has_hype_doge_safety(
    scanner_source: str, append_sites: list[ast.Call]
) -> None:
    """Every candidates.append site must satisfy ONE of the declared gating
    forms. For each append-call AST node:

      1. Find the EXPECTED_SITES anchor whose unique line in scanner is
         closest above the append (within a 200-line preceding window).
         This avoids false-pairing when a sibling strategy's anchor also
         appears in the back window.
      2. Each site must match exactly one anchor; each anchor must match
         exactly one site (1-to-1).
      3. The matched site must pass the gate_form's structural check.

    Load-bearing P2.3.e contract: failure here means a candidates.append
    site exists that could silently route HYPE/DOGE to live trading after
    P2.3.f flips the shadow flags.
    """
    # Pre-compute the unique line number for each declared anchor.
    anchor_linenos: dict[str, int] = {}
    for key, spec in EXPECTED_SITES.items():
        ln = _anchor_lineno(scanner_source, spec["strategy_anchor"])
        assert ln > 0, (
            f"EXPECTED_SITES[{key!r}] strategy_anchor {spec['strategy_anchor']!r} "
            f"not found in bot/scanner/__init__.py — anchor stale or strategy removed. "
            f"Re-audit P2.3.e and update."
        )
        anchor_linenos[key] = ln

    matched: dict[str, ast.Call] = {}
    unmatched_sites: list[ast.Call] = []
    for site in append_sites:
        best_key: str | None = None
        best_anchor_line = -1
        for key, anchor_line in anchor_linenos.items():
            # anchor must lie strictly above the append, within 200 lines back
            if site.lineno - 200 <= anchor_line < site.lineno:
                if anchor_line > best_anchor_line:
                    best_anchor_line = anchor_line
                    best_key = key
        if best_key is None:
            unmatched_sites.append(site)
            continue
        prior_site = matched.get(best_key)
        if prior_site is not None:
            raise AssertionError(
                f"two candidates.append sites resolved to the same anchor {best_key!r}: "
                f"lines {prior_site.lineno} and {site.lineno}. "
                f"Anchor at line {best_anchor_line} is ambiguous; pick a tighter anchor."
            )
        matched[best_key] = site

    assert not unmatched_sites, (
        f"candidates.append site(s) without a known HYPE/DOGE-safety classification: "
        f"lines={[s.lineno for s in unmatched_sites]}. Each site must match exactly "
        f"one EXPECTED_SITES entry via its `strategy_anchor` substring within 200 "
        f"lines back. Re-run the P2.3.e audit and either extend EXPECTED_SITES "
        f"with a new gating form or add the missing HYPE/DOGE gate to the strategy."
    )

    missing_anchors = set(EXPECTED_SITES) - set(matched)
    assert not missing_anchors, (
        f"EXPECTED_SITES entries with no matching append site (anchors stale or "
        f"strategy removed): {sorted(missing_anchors)}. "
        f"Remove the stale entry OR fix the strategy_anchor."
    )

    # Now assert each matched site satisfies its declared gate_form.
    failures: list[str] = []
    for key, site in matched.items():
        spec = EXPECTED_SITES[key]
        # The per-asset 15M shadow-gate block (XRP/HYPE/DOGE/BNB/ADA/BCH, each
        # writing a `*_shadow` row then `continue`) sits between the strategy
        # anchor and MAIN_YES. That block GROWS as assets onboard: BNB (P2.4)
        # then ADA/BCH (T1 2026-05-30) each added ~30 lines, pushing the
        # HYPE/DOGE gate signatures further above MAIN_YES (~249 lines back as
        # of the ADA/BCH add). The `downstream_of_shadow_gate` form needs a
        # wider lookback to still see those gates; the other forms gate
        # immediately above their site and stay at 200 (a tighter bound that
        # guards against false-positive pairing).
        _lb = 360 if spec["gate_form"] == "downstream_of_shadow_gate" else 200
        window_back = _source_slice(scanner_source, site.lineno, lines_back=_lb)
        window_fwd = _source_slice(scanner_source, site.lineno + 5, lines_back=4)
        if not _gate_pattern_matches(spec["gate_form"], window_back, window_fwd):
            failures.append(
                f"  {key} (line {site.lineno}, form={spec['gate_form']!r}): "
                f"gate signature not found in 200-line preceding window. "
                f"Rationale was: {spec['rationale']}"
            )
    assert not failures, (
        "candidates.append site(s) failed their declared gate_form check — "
        "HYPE/DOGE could route live through these sites:\n" + "\n".join(failures)
    )


def test_lpne_assets_excludes_hype_doge() -> None:
    """The LPNE candidates.append site is gated by `asset in LPNE_ASSETS`.
    That membership check is only safe if LPNE_ASSETS is restricted to
    crypto assets that have an LPNE calibration — currently BTC-only.
    If this set is widened to include HYPE/DOGE without P2.3.a-c training
    a per-asset LPNE calibration, the LPNE gate becomes a bypass site."""
    from bot.constants import LPNE_ASSETS
    assert "HYPE" not in LPNE_ASSETS, (
        f"LPNE_ASSETS contains HYPE — would route HYPE live through LPNE without "
        f"a calibrated predictor. Current LPNE_ASSETS={LPNE_ASSETS}."
    )
    assert "DOGE" not in LPNE_ASSETS, (
        f"LPNE_ASSETS contains DOGE — would route DOGE live through LPNE without "
        f"a calibrated predictor. Current LPNE_ASSETS={LPNE_ASSETS}."
    )


def test_hourly_excluded_assets_contains_hype_doge() -> None:
    """The hourly_no_live and hourly YES-side branches gate by membership in
    HOURLY_EXCLUDED_ASSETS (YES) and HOURLY_NO_EXCLUDED_ASSETS (NO). Both
    sets must contain HYPE and DOGE until T4 promotion."""
    from bot.constants import HOURLY_EXCLUDED_ASSETS, HOURLY_NO_EXCLUDED_ASSETS
    assert "HYPE" in HOURLY_EXCLUDED_ASSETS, (
        f"HYPE not in HOURLY_EXCLUDED_ASSETS — would route HYPE live through hourly YES. "
        f"Current HOURLY_EXCLUDED_ASSETS={HOURLY_EXCLUDED_ASSETS}."
    )
    assert "DOGE" in HOURLY_EXCLUDED_ASSETS, (
        f"DOGE not in HOURLY_EXCLUDED_ASSETS. Current set={HOURLY_EXCLUDED_ASSETS}."
    )
    assert "HYPE" in HOURLY_NO_EXCLUDED_ASSETS, (
        f"HYPE not in HOURLY_NO_EXCLUDED_ASSETS — would route HYPE live through hourly NO. "
        f"Current set={HOURLY_NO_EXCLUDED_ASSETS}."
    )
    assert "DOGE" in HOURLY_NO_EXCLUDED_ASSETS, (
        f"DOGE not in HOURLY_NO_EXCLUDED_ASSETS. Current set={HOURLY_NO_EXCLUDED_ASSETS}."
    )


def test_15m_shadow_flags_false_post_p2_3_live_promotion() -> None:
    """Post-P2.3 live promotion (2026-05-14, ClickUp 86b9xv66a), both flags
    MUST be False. The P2.3.b-fu2 B.5 GO path retired the cal_mlp-training
    arc; raw_prob + conservative per-asset MARKET_BLEND_W (DOGE 0.60,
    HYPE 0.80) is the live signal. Per-asset T4 prereq constants
    (HYPE/DOGE_MIN_ENTRY_PRICE + _MAX_RISK_PER_TRADE) wired atomically.
    Canonical anchor for the post-flip state lives in
    `tests/contracts/test_p2_3_live_promotion_constants.py` — this anchor
    duplicates the assertion here so the original P2.3.e shadow-gate
    audit covers both pre- and post-flip states from a single test
    suite."""
    from bot.constants import HYPE_15M_SHADOW, DOGE_15M_SHADOW
    assert HYPE_15M_SHADOW is False, (
        "HYPE_15M_SHADOW is True but P2.3 live promotion (86b9xv66a) "
        "shipped — flag MUST be False. If you intentionally rolled back, "
        "update test_p2_3_live_promotion_constants.py too."
    )
    assert DOGE_15M_SHADOW is False, (
        "DOGE_15M_SHADOW is True but P2.3 live promotion shipped — see "
        "HYPE assertion."
    )
