"""Sprint A Bit 1a + 1b — cal_mlp feature-transform lock-step CI guards.

Post-A.1b (ticket 86b9veppa) all 9 tests PASS. The 6 originally-sealed
tests verify the canonical refactor landed: `features.compute_hour_features`
helper exists + is correct, no inline `np.sin/cos(2π·h/24)` in the 4
drift sites (extract_data, post_hoc_processor, integration, sim_pnl),
integration.py imports + calls `compute_derived_features` from
`bot.helpers.derived_features` instead of inlining `buf_pct / sigma_denom`
+ `cb_prob - market_price/100`. Sister tests 1 + 2 + 9 (sigma_winsor SoT,
no-shadow clipping, bot/CLAUDE.md doc seal) pass pre- and post-A.1b.

Site map (verified 2026-05-12, post-Bit-9.3-iii.c which deleted bot/_impl.py):

  Drift surface (post-A.1b: all 4 tracked drift sites call canonical helpers;
                 features.py hosts the helpers):
    scripts/cal_mlp/extract_data.py        # hour_sin/cos via compute_hour_features
    scripts/cal_mlp/post_hoc_processor.py  # hour_sin/cos via compute_hour_features
    scripts/cal_mlp/integration.py         # hour_sin/cos + sigma + breakeven via helpers
    scripts/cal_mlp/sim_pnl.py             # hour_sin/cos via compute_hour_features
    scripts/cal_mlp/features.py            # SIGMA_WINSOR_ABS_CAP + apply_sigma_winsor + compute_hour_features home

  Helper-call sites (already call canonical):
    bot/state.py:1713 + 1723 + 2010        # pre-DB-write compute_derived_features + apply_sigma_winsor
    bot/engines/sports_engine.py           # 2 call sites
    scripts/backfill/backfill_extended_features.py
    scripts/backfill/wave1_derived_cols.py  # B.1a-fu2 2026-05-12: rejected_opportunities Wave 1 + evaluated_opportunities prob_breakeven_gap backfill
    scripts/backfill/hype_doge_replay_backfill.py  # Phase 2 86b9wy7v3 2026-05-12: per-market replay_market() calls compute_hour_sin_cos + compute_derived_features + apply_sigma_winsor on historical_replay_calmlp rows

  Canonical helpers:
    bot/helpers/derived_features.py::compute_derived_features
    bot/helpers/derived_features.py::compute_hour_sin_cos
    bot/helpers/derived_features.py::apply_sigma_winsor (mirrored by scripts/cal_mlp/features.apply_sigma_winsor)

See kb/decisions/sprint-a-bit-1-four-site-lockstep-rca-may09.md for the
full A.1a RCA and the kb/decisions/bit-a.1b-shipped-* closeout for
the A.1b refactor.
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
# so they're out of this test's surface. Followup ticket `86b9wjd3e`
# investigates whether they should be tracked-in-git or deleted as stale
# local dev artifacts.
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


def test_hour_features_helper_exists():
    """A.1b ships `features.compute_hour_features(hour) -> tuple[float, float]`.

    Scalar API matches `bot.helpers.derived_features.compute_hour_sin_cos`.
    The cal_mlp helper additionally accepts a numpy/pandas Series for
    DataFrame-side extract paths (extract_data.py, sim_pnl.py).
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


def test_hour_features_helper_correctness():
    """compute_hour_features must agree with sin(2π·h/24), cos(2π·h/24)."""
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


def test_no_inline_hour_sin_cos_in_other_sites():
    """No inline `sin|cos(...pi...24...)` survives in the 4 tracked drift sites.

    AST-walks scripts/cal_mlp/{extract_data,post_hoc_processor,integration,
    sim_pnl}.py for any `Call` whose func is `<np|math|_math>.<sin|cos>`
    AND whose argument subtree references BOTH `pi` (as `Attribute(attr='pi')`)
    AND the constant `24` / `24.0`. This catches all natural drift variants
    (bare `2`, argument-order swap `pi*2*h/24`, regrouped `2*pi/24*h`,
    constant-folded `0.2617993878*h` — wait, constant-folded variants don't
    reference pi by name and slip through, but they require a deliberate
    bypass that no reasonable refactor would produce; widening to catch
    constant-folded forms would over-match unrelated code).

    Post-A.1b: all 4 sites route through `features.compute_hour_features`.
    """
    survivors = []
    for path in HOUR_SINCOS_DRIFT_SITES:
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in ("sin", "cos")):
                continue
            # Verify the namespace is one of the math libraries (skip e.g.
            # asyncio.sin or unrelated `.sin()` method calls).
            ns = func.value
            if not (isinstance(ns, ast.Name) and ns.id in ("np", "math", "_math")):
                continue
            # Walk the argument subtree(s) for both `pi` reference + `24` const.
            has_pi = False
            has_24 = False
            for arg in node.args:
                for child in ast.walk(arg):
                    if isinstance(child, ast.Attribute) and child.attr == "pi":
                        has_pi = True
                    if isinstance(child, ast.Constant) and isinstance(child.value, (int, float)):
                        if float(child.value) == 24.0:
                            has_24 = True
                if has_pi and has_24:
                    break
            if has_pi and has_24:
                survivors.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                    f"{ns.id}.{func.attr}(...) call with pi + 24 inline"
                )
    assert not survivors, (
        "Inline hour_sin/cos formulas reintroduced — call "
        "`scripts/cal_mlp/features.compute_hour_features` (cal_mlp-local "
        "wrapper, accepts scalar OR Series) or "
        "`bot.helpers.derived_features.compute_hour_sin_cos` (scalar only) "
        "instead. Survivors:\n  " + "\n  ".join(survivors)
    )


# ─────────────────────────────────────────────────────────────────────
# Anchors 3 + 4 — breakeven_gap + sigma_derivation canonical helper
# ─────────────────────────────────────────────────────────────────────


def test_breakeven_gap_uses_canonical_helper():
    """integration.py must NOT inline `prob - market_price/100`.

    The canonical formula lives in
    bot.helpers.derived_features.compute_derived_features; integration.py
    must import and call it (mirroring bot/state.py:1713 + 2010).
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
        "Inline prob_breakeven_gap formula reintroduced in integration.py. "
        "Use `compute_derived_features(...)['prob_breakeven_gap']` instead. "
        "Survivors:\n  " + "\n  ".join(real_matches)
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
        "bot.helpers.derived_features at module top. Comment mentions "
        "don't count — needs a real import statement."
    )


def test_sigma_derivation_uses_canonical_helper():
    """integration.py must NOT inline `buf_pct / sigma_denom` sigma derivation.

    The canonical formula lives in
    bot.helpers.derived_features.compute_derived_features.
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
        "Inline sigma derivation reintroduced in integration.py. "
        "Use `compute_derived_features(...)['spot_distance_to_strike_sigma']` "
        "(then apply_sigma_winsor) instead. "
        "Evidence:\n  " + "\n  ".join(inline_evidence)
    )


def test_integration_py_has_canonical_helper_call_site():
    """integration.py's serve path must CALL compute_derived_features.

    AST-walks integration.py looking for a `Call` node whose func is named
    `compute_derived_features` (either as a bare Name after `from ... import`
    or attribute access `<mod>.compute_derived_features`). Strongest seal —
    comments mentioning the helper don't count, neither does the import alone.

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
        "compute_derived_features. Comment mentions + import alone don't "
        "count — need an actual invocation that replaces the inline "
        "buf_pct/sigma_denom + breakeven_gap formulas."
    )


# ─────────────────────────────────────────────────────────────────────
# Phase 2 replay backfill seal (86b9wy7v3, 2026-05-12) — R3 adv-review M3
# ─────────────────────────────────────────────────────────────────────


REPLAY_BACKFILL_PY = REPO_ROOT / "scripts" / "backfill" / "hype_doge_replay_backfill.py"


def test_replay_backfill_calls_canonical_hour_helper():
    """``scripts/backfill/hype_doge_replay_backfill.py`` must Call
    ``compute_hour_sin_cos`` (not inline `sin/cos(...pi...24...)`).

    Mirrors ``test_integration_py_has_canonical_helper_call_site``'s
    seal pattern. Required so the harness's lock-step claim in
    ``bot/CLAUDE.md`` + ``agent_docs/calibration_pipeline.md`` is
    AST-enforced, not just docstring-claimed (R3 adv-review M3).
    """
    if not REPLAY_BACKFILL_PY.is_file():
        pytest.fail(
            f"REPLAY_BACKFILL_PY missing at {REPLAY_BACKFILL_PY} — the "
            "Phase 2 (86b9wy7v3) harness is a tracked load-bearing file; "
            "this seal must not silently skip. If the file was renamed, "
            "update REPLAY_BACKFILL_PY; if intentionally deleted, also "
            "remove this test."
        )
    tree = _parse(REPLAY_BACKFILL_PY)
    call_sites = []
    inline_sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "compute_hour_sin_cos":
                call_sites.append(node.lineno)
            elif isinstance(f, ast.Attribute) and f.attr == "compute_hour_sin_cos":
                call_sites.append(node.lineno)
            # Inline-drift guard: same shape as test_no_inline_hour_sin_cos_in_other_sites
            if isinstance(f, ast.Attribute) and f.attr in ("sin", "cos"):
                ns = f.value
                if isinstance(ns, ast.Name) and ns.id in ("np", "math", "_math"):
                    has_pi = False
                    has_24 = False
                    for arg in node.args:
                        for child in ast.walk(arg):
                            if isinstance(child, ast.Attribute) and child.attr == "pi":
                                has_pi = True
                            if isinstance(child, ast.Constant) and isinstance(
                                child.value, (int, float)
                            ) and float(child.value) == 24.0:
                                has_24 = True
                    if has_pi and has_24:
                        inline_sites.append(node.lineno)
    assert not inline_sites, (
        f"Inline hour_sin/cos reintroduced in {REPLAY_BACKFILL_PY.name} at "
        f"line(s) {inline_sites} — call bot.helpers.derived_features."
        f"compute_hour_sin_cos instead."
    )
    assert call_sites, (
        f"{REPLAY_BACKFILL_PY.name} must contain a Call to compute_hour_sin_cos "
        "(lock-step contract per bot/CLAUDE.md cal_mlp lock-step). Comment + "
        "import alone don't count."
    )


def test_replay_backfill_calls_canonical_derived_features():
    """``scripts/backfill/hype_doge_replay_backfill.py`` must Call
    ``compute_derived_features`` (not inline sigma/breakeven_gap math)."""
    if not REPLAY_BACKFILL_PY.is_file():
        pytest.fail(
            f"REPLAY_BACKFILL_PY missing at {REPLAY_BACKFILL_PY} — the "
            "Phase 2 (86b9wy7v3) harness is a tracked load-bearing file; "
            "this seal must not silently skip. If the file was renamed, "
            "update REPLAY_BACKFILL_PY; if intentionally deleted, also "
            "remove this test."
        )
    tree = _parse(REPLAY_BACKFILL_PY)
    call_sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "compute_derived_features":
                call_sites.append(node.lineno)
            elif isinstance(f, ast.Attribute) and f.attr == "compute_derived_features":
                call_sites.append(node.lineno)
    assert call_sites, (
        f"{REPLAY_BACKFILL_PY.name} must contain a Call to compute_derived_features. "
        "Lock-step contract per bot/CLAUDE.md cal_mlp lock-step."
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
