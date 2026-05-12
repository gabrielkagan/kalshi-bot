"""Sprint A Bit 1a — cal_mlp feature-transform lock-step CI guards.

TDD invariant (2026-05-12): 3 tests PASS (sigma_winsor SoT + sigma_winsor
no-shadow + doc seal); 6 tests are marked `xfail(strict=True)` until
sister Bit A.1b (ticket 86b9veppa) ships the refactor adding
`features.compute_hour_features()` and replacing the inline
`buf_pct / sigma_denom` + `cb_prob - market_price/100` formulas in
integration.py with a call to
`bot.helpers.derived_features.compute_derived_features`. The xfail
keeps the CI gate green pre-A.1b; `strict=True` flips xfail→FAILED
on xpass once A.1b lands, forcing the implementer to remove the
decorator and reseal.

Site map (verified 2026-05-12, post-Bit-9.3-iii.c which deleted bot/_impl.py):

  Drift surface (inline duplicate formulas — A.1b will refactor):
    scripts/cal_mlp/extract_data.py        # hour_sin/cos inline
    scripts/cal_mlp/post_hoc_processor.py  # hour_sin/cos inline
    scripts/cal_mlp/integration.py         # hour_sin/cos + sigma + breakeven inline
    scripts/cal_mlp/features.py            # SIGMA_WINSOR_ABS_CAP + apply_sigma_winsor home

  Helper-call sites (good — already call canonical):
    bot/state.py:1926                      # (was bot/_impl.py:2192 pre-9.3-iii.c)
    bot/engines/sports_engine.py           # 2 call sites
    scripts/backfill_extended_features.py

  Canonical helper:
    bot/helpers/derived_features.py::compute_derived_features

See kb/decisions/sprint-a-bit-1-four-site-lockstep-rca-may09.md for the
full RCA (the "site 5 = bot/_impl.py:2192" entry there is stale; the
caller relocated to bot/state.py:1926 during Bit 9.3-iii.c).
"""
import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CAL_MLP = REPO_ROOT / "scripts" / "cal_mlp"
BOT_CLAUDE_MD = REPO_ROOT / "bot" / "CLAUDE.md"

FEATURES_PY = CAL_MLP / "features.py"
EXTRACT_DATA_PY = CAL_MLP / "extract_data.py"
POST_HOC_PY = CAL_MLP / "post_hoc_processor.py"
INTEGRATION_PY = CAL_MLP / "integration.py"
SIM_PNL_PY = CAL_MLP / "sim_pnl.py"
DERIVED_FEATURES_PY = REPO_ROOT / "bot" / "helpers" / "derived_features.py"

# All TRACKED sites with inline hour_sin/cos formulas (drift surface walked
# by test_no_inline_hour_sin_cos_in_other_sites). The first 3 are the
# primary train/serve paths from the RCA; sim_pnl.py surfaced in R2
# adversarial review 2026-05-12.
#
# Two other inline-drift sites exist as LOCAL-ONLY untracked files on dev
# machines (`scripts/cal_mlp/backfill_offline.py` + `scripts/cal_mlp/
# mac_diagnostics/v2_live_audit/score_live_ws.py`); CI doesn't have them
# so they're out of this test's surface. Followup ticket investigates
# whether they should be tracked-in-git or deleted as stale local dev
# artifacts (see ClickUp `86b9wgfff` or successor).
HOUR_SINCOS_DRIFT_SITES = (
    EXTRACT_DATA_PY,
    POST_HOC_PY,
    INTEGRATION_PY,
    SIM_PNL_PY,
)


def _read(path: Path) -> str:
    return path.read_text()


def _parse(path: Path) -> ast.Module:
    return ast.parse(_read(path), filename=str(path))


# ─────────────────────────────────────────────────────────────────────
# Anchor 1 — SIGMA_WINSOR_ABS_CAP single source of truth
# ─────────────────────────────────────────────────────────────────────


def test_sigma_winsor_constant_single_source_of_truth():
    """`SIGMA_WINSOR_ABS_CAP = 25.0` must be assigned in exactly one place.

    Source of truth: scripts/cal_mlp/features.py. Any other assignment of
    the constant is a shadow re-implementation.
    """
    tree = _parse(FEATURES_PY)
    assignments = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SIGMA_WINSOR_ABS_CAP":
                    val = node.value
                    if isinstance(val, ast.Constant) and val.value == 25.0:
                        assignments.append(node.lineno)
    assert len(assignments) == 1, (
        f"Expected exactly 1 assignment of SIGMA_WINSOR_ABS_CAP=25.0 in "
        f"{FEATURES_PY}, found {len(assignments)} at lines {assignments}"
    )

    # Also confirm no other cal_mlp script defines it.
    for path in HOUR_SINCOS_DRIFT_SITES:
        if path == FEATURES_PY:
            continue
        other = _parse(path)
        for node in ast.walk(other):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "SIGMA_WINSOR_ABS_CAP":
                        pytest.fail(
                            f"Shadow assignment of SIGMA_WINSOR_ABS_CAP at "
                            f"{path}:{node.lineno} — only features.py may define it"
                        )


def test_sigma_winsor_no_shadow_clipping_in_other_sites():
    """No inline `25.0`-paired sigma clipping outside the canonical helper.

    AST-walks all cal_mlp scripts. Flags any numeric literal `25.0` within
    a Compare or BinOp expression that also references an identifier
    containing 'sigma' or 'winsor' or 'sd' (loosely the names a copy-paste
    re-implementation would use).
    """
    for path in HOUR_SINCOS_DRIFT_SITES:
        if path == FEATURES_PY:
            continue
        tree = _parse(path)
        src = _read(path).splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == 25.0:
                # Find the line context (5 lines around the literal).
                start = max(0, node.lineno - 3)
                end = min(len(src), node.lineno + 2)
                context = "\n".join(src[start:end]).lower()
                if any(tok in context for tok in ("sigma", "winsor", "sd ", "abs(sd")):
                    pytest.fail(
                        f"Shadow sigma-clip literal `25.0` at "
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}; "
                        f"context: {context!r}. Use apply_sigma_winsor()."
                    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 2 — hour_sin / hour_cos centralization (A.1b refactor target)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.xfail(strict=True, reason="A.1b refactor pending (ticket 86b9veppa) — remove xfail when A.1b ships")
def test_hour_features_helper_exists():
    """A.1b ships `features.compute_hour_features(hour: int) -> tuple[float, float]`.

    Until A.1b lands, this test FAILS — that's the TDD seal. Marked
    `xfail(strict=True)` so pytest reports XFAIL (not FAILED) while
    A.1b is pending, and CONVERTS xfail-passes to FAILED once A.1b
    ships (forcing the A.1b implementer to remove the decorator).
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("cal_mlp_features", FEATURES_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "compute_hour_features"), (
        "scripts/cal_mlp/features.py must define compute_hour_features() — "
        "ships in sister Bit A.1b (86b9veppa)"
    )
    assert callable(mod.compute_hour_features)


@pytest.mark.xfail(strict=True, reason="A.1b refactor pending (ticket 86b9veppa) — remove xfail when A.1b ships")
def test_hour_features_helper_correctness():
    """compute_hour_features must agree with sin(2π·h/24), cos(2π·h/24).

    Until A.1b lands, this test FAILS (helper doesn't exist).
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("cal_mlp_features", FEATURES_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    f = getattr(mod, "compute_hour_features", None)
    assert f is not None, "compute_hour_features helper missing (A.1b not yet shipped)"

    # Cardinal hours.
    s, c = f(0)
    assert s == pytest.approx(0.0, abs=1e-12)
    assert c == pytest.approx(1.0, abs=1e-12)
    s, c = f(6)
    assert s == pytest.approx(1.0, abs=1e-12)
    assert c == pytest.approx(0.0, abs=1e-12)
    s, c = f(12)
    assert s == pytest.approx(0.0, abs=1e-12)
    assert c == pytest.approx(-1.0, abs=1e-12)
    s, c = f(18)
    assert s == pytest.approx(-1.0, abs=1e-12)
    assert c == pytest.approx(0.0, abs=1e-12)


@pytest.mark.xfail(strict=True, reason="A.1b refactor pending (ticket 86b9veppa) — remove xfail when A.1b ships")
def test_no_inline_hour_sin_cos_in_other_sites():
    """After A.1b refactor, none of the 3 inline hour_sin/cos sites survive.

    AST-walks scripts/cal_mlp/{extract_data,post_hoc_processor,integration}.py
    for `sin|cos(2*pi*X/24)` patterns. Flags any survivor.

    Until A.1b lands, this test FAILS (6 inline sites still present:
    3 primary train/serve + 3 sister scripts surfaced in R2 adv 2026-05-12).
    """
    # Match `sin(2 * pi * X / 24)` or `cos(2 * pi * X / 24)` with any spacing.
    pattern = re.compile(
        r"(?:np|math|_math)\.(?:sin|cos)\s*\(\s*2\.0?\s*\*\s*(?:np|math|_math)\.pi\s*\*\s*\w+\s*/\s*24(?:\.0)?\s*\)"
    )
    survivors = []
    for path in HOUR_SINCOS_DRIFT_SITES:
        for i, line in enumerate(_read(path).splitlines(), start=1):
            if pattern.search(line):
                survivors.append(f"{path.relative_to(REPO_ROOT)}:{i}: {line.strip()}")
    assert not survivors, (
        "Inline hour_sin/cos formulas still present (A.1b not yet shipped). "
        "Replace each with the canonical helper "
        "(`bot.helpers.derived_features.compute_hour_sin_cos` per Bit B.1a; "
        "or `scripts/cal_mlp/features.compute_hour_features` if A.1b "
        "introduces a thin cal_mlp-local wrapper). "
        "Survivors:\n  " + "\n  ".join(survivors)
    )


# ─────────────────────────────────────────────────────────────────────
# Anchors 3 + 4 — breakeven_gap + sigma_derivation canonical helper
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.xfail(strict=True, reason="A.1b refactor pending (ticket 86b9veppa) — remove xfail when A.1b ships")
def test_breakeven_gap_uses_canonical_helper():
    """integration.py must NOT inline `prob - market_price/100`.

    The canonical formula lives in bot.helpers.derived_features.compute_derived_features.
    integration.py:1287-1309 currently re-implements it inline.

    Until A.1b lands, this test FAILS.
    """
    src = _read(INTEGRATION_PY)
    # Match `<prob_var> - <market_price_var> / 100[.0]` patterns.
    pattern = re.compile(
        r"\b(cb_prob|calibrated_prob|prob)\s*-\s*\(?\s*(market_price|market_price_cents)\s*/\s*100(?:\.0)?\s*\)?"
    )
    matches = [
        (i, line.strip())
        for i, line in enumerate(src.splitlines(), start=1)
        if pattern.search(line)
    ]
    # Allow occurrences inside comments / docstrings — strip those.
    real_matches = []
    for i, line in matches:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        real_matches.append(f"integration.py:{i}: {line}")
    assert not real_matches, (
        "Inline prob_breakeven_gap formula still present in integration.py "
        "(A.1b not yet shipped). Replace with "
        "`from bot.helpers.derived_features import compute_derived_features` "
        "and call the helper. Survivors:\n  " + "\n  ".join(real_matches)
    )

    # Also assert integration.py imports the canonical helper (not just
    # mentions it in a comment).
    import_present = re.search(
        r"^\s*(from\s+bot\.helpers\.derived_features\s+import\s+[^#\n]*compute_derived_features"
        r"|import\s+bot\.helpers\.derived_features)",
        src,
        re.MULTILINE,
    )
    assert import_present, (
        "integration.py must import compute_derived_features from "
        "bot.helpers.derived_features at module top (A.1b). Comment "
        "mentions don't count — needs a real import statement."
    )


@pytest.mark.xfail(strict=True, reason="A.1b refactor pending (ticket 86b9veppa) — remove xfail when A.1b ships")
def test_sigma_derivation_uses_canonical_helper():
    """integration.py must NOT inline `buf_pct / sigma_denom` sigma derivation.

    The canonical formula lives in bot.helpers.derived_features.compute_derived_features.
    integration.py:1289-1295 currently re-implements it inline.

    Until A.1b lands, this test FAILS.
    """
    src = _read(INTEGRATION_PY)
    # Inline pattern: `buf_pct = (spot - threshold) / threshold * 100` OR
    # `sigma_denom = ... * sqrt(seconds_to_close / 5.0) * 100`.
    has_buf_pct_assign = re.search(r"\bbuf_pct\s*=\s*\(", src)
    has_sigma_denom_assign = re.search(
        r"sigma_denom\s*=.*sqrt\s*\(\s*seconds_to_close\s*/\s*5", src
    )
    inline_evidence = []
    if has_buf_pct_assign and not has_buf_pct_assign.group(0).startswith("#"):
        inline_evidence.append(f"buf_pct assignment at byte {has_buf_pct_assign.start()}")
    if has_sigma_denom_assign:
        inline_evidence.append(f"sigma_denom assignment at byte {has_sigma_denom_assign.start()}")

    assert not inline_evidence, (
        "Inline sigma derivation still present in integration.py "
        "(A.1b not yet shipped). Replace with compute_derived_features call. "
        "Evidence:\n  " + "\n  ".join(inline_evidence)
    )


@pytest.mark.xfail(strict=True, reason="A.1b refactor pending (ticket 86b9veppa) — remove xfail when A.1b ships")
def test_integration_py_has_canonical_helper_call_site():
    """integration.py's serve path must CALL compute_derived_features.

    AST-walks integration.py looking for a `Call` node whose func is named
    `compute_derived_features` (either as a bare Name after `from ... import`
    or attribute access `<mod>.compute_derived_features`). This is the
    strongest TDD seal — comments mentioning the helper don't count, neither
    does the import alone. There has to be a real call site replacing
    integration.py:1287-1309's current inline formulas.

    Until A.1b lands, this test FAILS.

    (Replaces the prior `test_compute_derived_features_runtime_parity` test,
    which was a canonical-vs-canonical tautology — caught by R1 adv review
    MN-2 on 2026-05-12.)
    """
    tree = _parse(INTEGRATION_PY)
    call_sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "compute_derived_features":
                call_sites.append(node.lineno)
            elif isinstance(f, ast.Attribute) and f.attr == "compute_derived_features":
                call_sites.append(node.lineno)
    assert call_sites, (
        "integration.py must contain at least one Call to "
        "compute_derived_features (A.1b refactor). Comment mentions + "
        "import alone don't count — need an actual invocation that "
        "replaces the inline buf_pct/sigma_denom + breakeven_gap formulas."
    )


# ─────────────────────────────────────────────────────────────────────
# Documentation seal
# ─────────────────────────────────────────────────────────────────────


def test_bot_claude_md_documents_lockstep_surface():
    """bot/CLAUDE.md's lock-step section must name the canonical helper + owner language.

    Documents the full surface (3 inline drift sites in scripts/cal_mlp/ +
    the canonical helper home) so future contributors don't reintroduce
    inline duplicates. Owner-language assertion (`owns spot_distance_to_strike_sigma`
    or `canonical helper home`) prevents partial-revert vulnerabilities
    where the helper is mentioned in a stale sentence but the rule is
    silently weakened (R1 adv MN-3, 2026-05-12).
    """
    src = _read(BOT_CLAUDE_MD)
    # Must reference the canonical helper with the qualified path.
    assert "bot/helpers/derived_features.py" in src, (
        "bot/CLAUDE.md lock-step section must cite "
        "bot/helpers/derived_features.py as the canonical helper home"
    )
    assert "compute_derived_features" in src, (
        "bot/CLAUDE.md lock-step section must reference compute_derived_features"
    )
    # Owner-language seal: the rule must explicitly state ownership, not
    # just mention the helper name in passing. Normalize whitespace
    # (multi-line text in markdown wraps "owns\n  spot_distance_..." across
    # lines; we want substring match on the canonical phrase regardless).
    src_normalized = " ".join(src.split())
    owner_phrases = (
        "canonical helper home",
        "owns `spot_distance_to_strike_sigma`",
        "owns spot_distance_to_strike_sigma",
    )
    assert any(p in src_normalized for p in owner_phrases), (
        f"bot/CLAUDE.md lock-step section must include owner-language "
        f"(one of {owner_phrases!r}) — without this seal a partial-revert "
        f"can leave compute_derived_features mentioned but the rule weakened."
    )
    # All 3 inline-drift cal_mlp scripts must be named.
    for site in ("scripts/cal_mlp/extract_data.py",
                 "scripts/cal_mlp/post_hoc_processor.py",
                 "scripts/cal_mlp/integration.py"):
        assert site in src, (
            f"bot/CLAUDE.md lock-step section must name {site} as part of the "
            f"drift surface"
        )
