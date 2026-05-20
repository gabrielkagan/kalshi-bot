"""Phase 2 P2.1.d — per-asset MARKET_BLEND_W dispatch (ClickUp 86b9xfwkg).

Atomic deploy of cal_mlp v1.1 + per-asset market-price blend weights.

Origin: P2.1.c-fu1 (ClickUp 86b9xev58, RESOLVED) — the 4×6 blend-weight
sweep run on 2026-05-13 against v1.1 candidate bundles showed the
production `MARKET_BLEND_W=0.40` is the WORST setting for v1.1 portfolio
sim PnL (-$125/30d at w=0.4 vs +$74 at w=0.2 and +$71 at w=1.0). v1.1
beats v1 at every weight; per-asset argmaxes vary 0.0–1.0 correlating
with P2.1.b Brier wins (BTC -13% / ETH -11% / SOL -3% / XRP -6%).

Origin of legacy 0.40: commit `89adddf` 2026-02-28 ("model
underconfident 0.8-2.1pp at 90%+") — predates cal_mlp entirely (cal_mlp
v1 deployed late April; pre-Feb-28 was Beta Cal + temperature scaling).
The blend weight has never been re-validated for any cal_mlp version
until P2.1.c-fu1.

This test PINS the per-asset weights operator-confirmed end of P2.1.c
session 2026-05-13:

  BTC: 0.10  (argmax 0.0 → pulled off corner; near-flat low-w top)
  ETH: 0.20  (interior argmax — accept fragility flag, knife-edge w=0.4)
  SOL: 0.80  (interior argmax, clean climb 0.6→0.8→1.0)
  XRP: 0.90  (argmax 1.0 → pulled off corner; barely-positive edge)

Discipline tier HIGH — atomic ship with 4-asset cal_mlp CURRENT pointer
flip to v1.1 bundles; pre-committed 14d Brier-monitored rollback rule
attached. See `kb/decisions/p2-1-d-pickup-prompt-may13.md`.

Sister anchors:
  - `test_p2_1_a_3_fu2_categorical_dispatch.py` — P2.1.a-3-fu2 cal_mlp
    categorical-FE dispatch (ETH/HYPE/DOGE bundle infrastructure that
    feeds this Bit's deploy).
  - `test_calmlp_lockstep.py` — v1.1 cfg_fp pin (existing).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
CALIBRATION_PY = REPO_ROOT / "bot" / "engines" / "calibration.py"
MARKET_CONFIG_PY = REPO_ROOT / "market_config.py"
CONSTANTS_PY = REPO_ROOT / "bot" / "constants.py"


# Operator-confirmed per-asset blend weights.
# - BTC/ETH/SOL/XRP: end of P2.1.c session 2026-05-13, after the 4×6 sweep
#   across {0.0, 0.2, 0.4, 0.6, 0.8, 1.0}. Sourced from
#   `.p2_1_c_run/cross_sweep_summary.txt` argmax + interior-pull discipline
#   (BTC + XRP pulled off corners; ETH + SOL kept at interior argmaxes).
# - HYPE/DOGE: P2.3 live promotion 2026-05-14, ClickUp 86b9xv66a, B.1 Brier
#   sweep on live shadow data accumulated 2026-05-10 → 2026-05-13 (DOGE
#   n=1710 / HYPE n=1469). Both argmins interior — no corner pull needed.
#   See kb/findings/p2-3-b-live-promotion-blend-weights-may14.md.
# - BNB: P2.4 live promotion 2026-05-19, ClickUp 86b9zmj37, B.1-equivalent
#   Brier sweep on live shadow data accumulated 2026-05-17 → 2026-05-19
#   (n=721). Argmin interior at w=0.20 — matches ETH pattern (raw model
#   beats market by ~10% Brier; BNB is well-calibrated, opposite of HYPE/DOGE).
#   See kb/decisions/p2-4-bnb-live-promotion-plan.md.
EXPECTED_PER_ASSET_BLEND_W: dict[str, float] = {
    "BTC": 0.10,
    "ETH": 0.20,
    "SOL": 0.80,
    "XRP": 0.90,
    "HYPE": 0.80,
    "DOGE": 0.60,
    "BNB": 0.20,
}

# v1.1 CURRENT pointer target for atomic deploy. Train_id format:
# `{ISO_TIMESTAMP}-{8-char-recipe-fingerprint}` — pinned to the
# 2026-05-12T11:59:31.654442Z snapshot produced by P2.1.b.
EXPECTED_V11_TRAIN_IDS: dict[str, str] = {
    "BTC": "2026-05-12T11:59:31.654442Z-fa63e067",
    "ETH": "2026-05-12T11:59:31.654442Z-b6eb2704",
    "SOL": "2026-05-12T11:59:31.654442Z-70cbc076",
    "XRP": "2026-05-12T11:59:31.654442Z-ae487abb",
}


def _load_ast(path: Path) -> ast.Module:
    """Parse a file to an AST module. Fails the test (not skip) if the
    source path doesn't exist — these are load-bearing canonical modules,
    a missing path means a relocation we haven't reflected here."""
    assert path.exists(), f"canonical source missing: {path}"
    return ast.parse(path.read_text())


# ─────────────────────────────────────────────────────────────────────
# Anchor 1: bot.constants exposes MARKET_BLEND_W_BY_ASSET with the 4 pinned values
# ─────────────────────────────────────────────────────────────────────

def test_market_blend_w_by_asset_constant_pinned():
    """Anchor 1: `bot.constants.MARKET_BLEND_W_BY_ASSET` MUST be a dict
    mapping the 4 production asset codes to the operator-confirmed
    per-asset blend weights. Drift here means the deploy ships a
    different blend than the sweep validated — the 8-day paired sim
    PnL window from P2.1.c is the only evidence we have for these
    weights, so the constant IS the contract."""
    import bot.constants as constants  # noqa: WPS433

    assert hasattr(constants, "MARKET_BLEND_W_BY_ASSET"), (
        "bot.constants.MARKET_BLEND_W_BY_ASSET missing — P2.1.d ships "
        "the 4-asset per-asset blend-weight map atomically with the v1.1 "
        "CURRENT-pointer flip; without the constant the runtime can't "
        "read the dispatched weights."
    )
    actual = constants.MARKET_BLEND_W_BY_ASSET
    assert isinstance(actual, dict), (
        f"MARKET_BLEND_W_BY_ASSET must be dict, got {type(actual).__name__}"
    )
    assert actual == EXPECTED_PER_ASSET_BLEND_W, (
        f"per-asset blend-weight drift: got {actual} vs pinned "
        f"{EXPECTED_PER_ASSET_BLEND_W}. These weights were chosen from "
        f"the .p2_1_c_run/cross_sweep_summary.txt argmaxes with interior-"
        f"pull discipline; any change here MUST run a fresh sweep first."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 2: MarketTypeConfig.get_blend_w(asset) dispatches per-asset
# ─────────────────────────────────────────────────────────────────────

def test_market_type_config_exposes_get_blend_w():
    """Anchor 2: `MarketTypeConfig` MUST expose a `get_blend_w(asset)`
    instance method that returns the per-asset weight when the dataclass
    has a `market_blend_w_by_asset` map AND the asset is in the map,
    falling back to the dataclass's scalar `market_blend_w` otherwise.

    The fallback path is load-bearing for HYPE/DOGE shadow strategies
    (they're not in MARKET_BLEND_W_BY_ASSET — they keep using the
    legacy 0.40) and for hourly/spx/weather configs (no per-asset map
    set; everything routes through their scalar weight)."""
    from market_config import MARKET_CONFIGS  # noqa: WPS433

    cfg_15m = MARKET_CONFIGS["15m"]
    assert hasattr(cfg_15m, "get_blend_w"), (
        "MarketTypeConfig.get_blend_w(asset) missing — P2.1.d routes "
        "all 15M consumer sites through this method so per-asset "
        "dispatch is one-place rather than scattered."
    )

    # 15M production assets (4 P2.1.d + 2 P2.3 live promotion) → per-asset map
    for asset, expected in EXPECTED_PER_ASSET_BLEND_W.items():
        actual = cfg_15m.get_blend_w(asset)
        assert actual == expected, (
            f"15m get_blend_w({asset!r}) = {actual} != {expected} "
            f"(MARKET_BLEND_W_BY_ASSET pin)"
        )

    # Unknown asset (defensive — no current asset routes here, but the
    # fallback path is still load-bearing for hypothetical future asset
    # additions and for non-15M product types whose configs reuse the
    # same get_blend_w method) → fallback to scalar legacy 0.40.
    # Pre-P2.3 (2026-05-14) HYPE/DOGE used this path; both are now in
    # the map. Use an asset code that does NOT exist in the registry.
    fallback = cfg_15m.get_blend_w("__NONEXISTENT_ASSET__")
    assert fallback == cfg_15m.market_blend_w, (
        f"15m get_blend_w('__NONEXISTENT_ASSET__') = {fallback} != "
        f"fallback {cfg_15m.market_blend_w}; unknown-asset path MUST "
        f"fall back to the scalar `market_blend_w` (legacy 0.40) when "
        f"the per-asset map has no entry."
    )

    # asset=None (defensive) → fallback to scalar
    fallback_none = cfg_15m.get_blend_w(None)
    assert fallback_none == cfg_15m.market_blend_w, (
        f"15m get_blend_w(None) = {fallback_none} != fallback "
        f"{cfg_15m.market_blend_w}; defensive call sites that don't "
        f"have asset context MUST not crash."
    )

    # Hourly config has no per-asset map; every asset returns scalar
    cfg_hourly = MARKET_CONFIGS["hourly"]
    assert cfg_hourly.get_blend_w("BTC") == cfg_hourly.market_blend_w, (
        f"hourly get_blend_w('BTC') = {cfg_hourly.get_blend_w('BTC')} != "
        f"hourly scalar {cfg_hourly.market_blend_w}; hourly has NO "
        f"per-asset dispatch (only 15M does)."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 3: 15m MARKET_CONFIGS entry pins market_blend_w_by_asset
# ─────────────────────────────────────────────────────────────────────

def test_15m_market_config_pins_per_asset_blend_map():
    """Anchor 3: the `15m` entry in `MARKET_CONFIGS` MUST set
    `market_blend_w_by_asset` to the exact 4-asset map. The bot's
    runtime construction goes through `get_market_config('15m')` and
    reads `_mcfg.market_blend_w_by_asset` (via `get_blend_w`); if the
    map isn't pinned on the dataclass instance, the runtime silently
    falls back to the legacy 0.40 for every asset and the deploy
    becomes a no-op."""
    from market_config import MARKET_CONFIGS  # noqa: WPS433

    cfg_15m = MARKET_CONFIGS["15m"]
    assert hasattr(cfg_15m, "market_blend_w_by_asset"), (
        "MarketTypeConfig field `market_blend_w_by_asset` missing — the "
        "dataclass schema must carry it for the 15m instance to set it."
    )
    actual_map = cfg_15m.market_blend_w_by_asset
    assert actual_map == EXPECTED_PER_ASSET_BLEND_W, (
        f"15m MARKET_CONFIGS map drift: {actual_map} vs pinned "
        f"{EXPECTED_PER_ASSET_BLEND_W}. The dataclass and the constant "
        f"MUST be lock-step (validate_market_configs() asserts this at "
        f"startup — but pin it in tests too so a bad commit fails CI "
        f"before reaching production)."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 4: validate_market_configs asserts per-asset lock-step
# ─────────────────────────────────────────────────────────────────────

def test_validate_market_configs_asserts_per_asset_lockstep():
    """Anchor 4: `validate_market_configs()` MUST assert that the 15m
    `market_blend_w_by_asset` map equals `bot.constants.MARKET_BLEND_W_BY_ASSET`
    — same pattern as the existing scalar assertion (line ~255).
    Without this lock-step, a drift between the dataclass instance and
    the constant doesn't crash startup (the legacy assertion at line
    ~255 only covers the scalar fallback)."""
    from market_config import validate_market_configs  # noqa: WPS433

    # Just running it (no mutations) should succeed cleanly when the
    # constant matches the 15m instance. The assertion existence is
    # observed indirectly: if we mutate either side and run, it must
    # raise — but we don't want this test to write to globals. The
    # simpler shape: import + call; if validate_market_configs() is
    # missing the per-asset check, this test still passes — so we
    # additionally AST-walk the function body looking for a comparison
    # involving `bot.constants.MARKET_BLEND_W_BY_ASSET`.
    validate_market_configs()  # smoke

    tree = _load_ast(MARKET_CONFIG_PY)
    saw_check = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "validate_market_configs":
            src = ast.unparse(node)
            if "MARKET_BLEND_W_BY_ASSET" in src and "market_blend_w_by_asset" in src:
                saw_check = True
            break
    assert saw_check, (
        "validate_market_configs() body must reference both "
        "`bot.constants.MARKET_BLEND_W_BY_ASSET` and the 15m "
        "instance's `market_blend_w_by_asset` field — assertion is "
        "load-bearing at startup."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 5: bot/scanner/__init__.py has zero bare-Name MARKET_BLEND_W reads
# ─────────────────────────────────────────────────────────────────────

def test_scanner_has_no_bare_market_blend_w_reads():
    """Anchor 5: AST-walk `bot/scanner/__init__.py` for `ast.Name`
    nodes with `id='MARKET_BLEND_W'` (i.e., RUNTIME READS of the bare
    legacy scalar).

    After P2.1.d, the ONLY permitted bare reads are inside the
    `MARKET_BLEND_W_BY_ASSET.get(asset, MARKET_BLEND_W)` fallback-
    default pattern — that IS the per-asset dispatch for HYPE/DOGE.
    All OTHER reads (multiplicands in blend math, log-format args,
    startup assertions) MUST be replaced with the per-asset map.

    `ast.ImportFrom` references the name through `ast.alias.name` (a
    string), NOT a `Name` node — so the import statement at line 183
    doesn't count toward this guard."""
    tree = _load_ast(SCANNER_PY)
    # Walk with a parent-link pass so we can classify each Name read.
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent

    def _is_get_fallback_arg(name_node: ast.Name) -> bool:
        """True iff this Name is the 2nd positional arg of
        `MARKET_BLEND_W_BY_ASSET.get(...)`."""
        call = parent_map.get(id(name_node))
        if not isinstance(call, ast.Call):
            return False
        if len(call.args) < 2 or call.args[1] is not name_node:
            return False
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            return False
        receiver = func.value
        return isinstance(receiver, ast.Name) and receiver.id == "MARKET_BLEND_W_BY_ASSET"

    bare_violations: list[int] = []
    legitimate_fallbacks: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "MARKET_BLEND_W":
            if not isinstance(node.ctx, ast.Load):
                continue
            if _is_get_fallback_arg(node):
                legitimate_fallbacks.append(node.lineno)
            else:
                bare_violations.append(node.lineno)

    assert bare_violations == [], (
        f"bot/scanner/__init__.py has {len(bare_violations)} bare-Name "
        f"MARKET_BLEND_W read(s) OUTSIDE the dict.get-fallback pattern at "
        f"lines {bare_violations} — P2.1.d requires all such reads to be "
        f"replaced with `MARKET_BLEND_W_BY_ASSET[asset]` (literal map), "
        f"`MARKET_BLEND_W_BY_ASSET.get(asset, MARKET_BLEND_W)` (fallback "
        f"dispatch), or `_mcfg.get_blend_w(asset)` (dataclass method). "
        f"The CONFIG_VERIFY log + startup assertion must enumerate over "
        f"the per-asset map; the bare scalar read is the legacy behavior "
        f"we're replacing. (Legitimate fallback-arg reads at lines "
        f"{legitimate_fallbacks} are allowed.)"
    )
    # Sanity: we expect at least 1 legitimate fallback (the HYPE/DOGE
    # shadow path uses MARKET_BLEND_W_BY_ASSET.get(asset, MARKET_BLEND_W)).
    # Zero would mean the fallback pattern was removed — fine in principle,
    # but worth a docstring update on this anchor if so.
    assert legitimate_fallbacks, (
        "Anchor 5 expected at least 1 `MARKET_BLEND_W_BY_ASSET.get(asset, "
        "MARKET_BLEND_W)` fallback-default usage in bot/scanner/__init__.py "
        "(HYPE/DOGE shadow paths) — if you intentionally removed them, "
        "update this anchor's docstring."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 6: scanner imports MARKET_BLEND_W_BY_ASSET from bot.constants
# ─────────────────────────────────────────────────────────────────────

def test_scanner_imports_market_blend_w_by_asset():
    """Anchor 6: `bot/scanner/__init__.py` MUST import
    `MARKET_BLEND_W_BY_ASSET` from `bot.constants` so the AST guard at
    anchor 5 is satisfied by *replacing* the bare reads, not by
    deleting them and relying on `_mcfg.get_blend_w` everywhere
    (some sites — startup assertion + CONFIG_VERIFY log — predate the
    `_mcfg` fetch in scope)."""
    tree = _load_ast(SCANNER_PY)
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.constants":
            for alias in node.names:
                if alias.name == "MARKET_BLEND_W_BY_ASSET":
                    imported = True
                    break
        if imported:
            break
    assert imported, (
        "bot/scanner/__init__.py must `from bot.constants import "
        "MARKET_BLEND_W_BY_ASSET` — the startup assertion at line "
        "~466 + CONFIG_VERIFY log at line ~476 enumerate the per-asset "
        "map directly (no `_mcfg` in scope yet at startup)."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 7: calibration.shadow_calibration_pipeline accepts asset param
# ─────────────────────────────────────────────────────────────────────

def test_shadow_calibration_pipeline_accepts_asset_param():
    """Anchor 7: `CalibrationEngine.shadow_calibration_pipeline` MUST
    accept an `asset` keyword parameter so its returned dict's
    `prod_blend_w` field reflects the per-asset weight in shadow logs
    (currently logs the legacy 0.40 scalar via `MARKET_BLEND_W` —
    breaks observability of the new per-asset behavior in
    shadow_cal_pipeline lines).

    Optional kwarg with `None` default preserves the existing
    sole caller (`bot/scanner/__init__.py:3179`) compatibility if it
    hasn't been updated yet — but anchor 8 below pins the caller to
    pass it explicitly."""
    tree = _load_ast(CALIBRATION_PY)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "shadow_calibration_pipeline":
            arg_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            assert "asset" in arg_names, (
                f"CalibrationEngine.shadow_calibration_pipeline signature "
                f"missing `asset` param; got args={sorted(arg_names)}. "
                f"The per-asset blend-weight value can't be returned in "
                f"the shadow dict without the caller passing it."
            )
            found = True
            break
    assert found, "shadow_calibration_pipeline function definition not found"


# ─────────────────────────────────────────────────────────────────────
# Anchor 8: scanner shadow_calibration_pipeline call passes asset=asset
# ─────────────────────────────────────────────────────────────────────

def test_scanner_passes_asset_to_shadow_calibration_pipeline():
    """Anchor 8: the lone call site at `bot/scanner/__init__.py:3179`
    MUST pass `asset=asset` (or positional, but the kwarg form is
    more grep-friendly + matches the surrounding style)."""
    tree = _load_ast(SCANNER_PY)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "shadow_calibration_pipeline":
                kw_names = {kw.arg for kw in node.keywords if kw.arg}
                # Either a kwarg named 'asset' OR a positional Name 'asset'
                pos_names = [a.id for a in node.args if isinstance(a, ast.Name)]
                if "asset" in kw_names or "asset" in pos_names:
                    found = True
                    break
    assert found, (
        "bot/scanner/__init__.py shadow_calibration_pipeline(...) call "
        "must pass asset= so the returned prod_blend_w reflects the "
        "per-asset value."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 9: 4 cal_mlp CURRENT pointers flipped to v1.1 train_ids
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("asset,train_id", sorted(EXPECTED_V11_TRAIN_IDS.items()))
def test_cal_mlp_current_pointer_flipped_to_v11(asset: str, train_id: str):
    """Anchor 9 (parametrized × 4): each production asset's
    `models/cal_mlp_<asset>/CURRENT` file MUST contain the v1.1
    train_id pinned above. Pre-deploy these point to v1 baselines
    (e.g., `2026-04-28T11:50:48.975743Z-a0000cc1` for ETH) — those are
    INTENTIONAL no-flip state per P2.1.c session resume "CURRENT
    pointer hygiene" section. P2.1.d's central effect IS this flip,
    so a passing test here is the ship signal.

    **CI behavior**: `/models/` is gitignored (`.gitignore:29`) — bundles
    are local-only artifacts produced by P2.1.b on Mac. CI on Linux runs
    `make test-contract-pytest` without bundles staged; this test skips
    cleanly when the asset's `models/cal_mlp_<asset>/` directory is
    absent. The local-Mac pre-push gate is preserved: the operator runs
    this file locally where bundles exist, and the 4 anchors fire.
    Precedent: `tests/contracts/test_bit_11_2_scripts_subdirs.py`
    skip-on-absent pattern."""
    asset_dir = REPO_ROOT / "models" / f"cal_mlp_{asset}"
    if not asset_dir.exists():
        pytest.skip(
            f"models/cal_mlp_{asset}/ absent — local-only deploy "
            f"artifact (gitignored). Pre-push gate runs on Mac where "
            f"bundles are staged; CI skips cleanly."
        )
    pointer = asset_dir / "CURRENT"
    assert pointer.exists(), (
        f"cal_mlp_{asset}/CURRENT missing — production pointer file "
        f"required for the runtime to resolve the bundle dir"
    )
    actual = pointer.read_text().strip()
    assert actual == train_id, (
        f"cal_mlp_{asset}/CURRENT = {actual!r} != pinned v1.1 train_id "
        f"{train_id!r}. The atomic deploy flips all 4 pointers to v1.1; "
        f"if this is the v1 baseline, the deploy hasn't run yet. If "
        f"this is a different v1.1 train_id, the SCP'd bundle may "
        f"mismatch the deploy commit — verify "
        f"`models/cal_mlp_{asset}/{train_id}/` exists on disk."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 10: v1.1 bundle dirs exist on disk (sanity for SCP staging)
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("asset,train_id", sorted(EXPECTED_V11_TRAIN_IDS.items()))
def test_v11_bundle_dir_exists_on_disk(asset: str, train_id: str):
    """Anchor 10 (parametrized × 4): each v1.1 train_id MUST resolve
    to a populated bundle directory under `models/cal_mlp_<asset>/`
    locally. Defends against committing the CURRENT flip without
    SCP'ing the gitignored bundle artifacts to the VPS first (first
    scan tick on the VPS would crash with FileNotFoundError otherwise).

    This is a *local* presence check — operator-confirm-before-push
    is the human gate that the VPS has been SCP'd to. But local
    presence is the precondition: if the bundle isn't here, it can't
    be SCP'd anywhere.

    **CI behavior**: same skip-on-absent semantics as anchor 9. CI
    on Linux without `models/` cleanly skips; local-Mac pre-push runs
    all 4 parametrizations."""
    asset_dir = REPO_ROOT / "models" / f"cal_mlp_{asset}"
    if not asset_dir.exists():
        pytest.skip(
            f"models/cal_mlp_{asset}/ absent — local-only deploy "
            f"artifact (gitignored). CI skips cleanly; local-Mac "
            f"pre-push gate exercises this anchor."
        )
    bundle = asset_dir / train_id
    assert bundle.exists() and bundle.is_dir(), (
        f"v1.1 bundle missing: {bundle} — bundles are gitignored "
        f"Mac-local artifacts produced by P2.1.b. Without the dir, "
        f"the CURRENT flip points at nothing and the runtime crashes "
        f"on first scan."
    )
    # Phase 5 conformal-calibrated artifact should be present (one of
    # `predictor.pkl` or equivalent under the phase5 namespace). Avoid
    # over-pinning specific file names — just sanity that the dir is
    # non-empty.
    contents = list(bundle.iterdir())
    assert contents, (
        f"v1.1 bundle directory exists but is EMPTY: {bundle} — "
        f"likely a partial extract; re-run P2.1.b for this asset."
    )
