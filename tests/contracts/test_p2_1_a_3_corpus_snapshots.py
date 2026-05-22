"""Phase 2 P2.1.a-3 — corpus-snapshot contract pins for v1.1 retrain.

Pins the artifacts P2.1.a-3 produces so P2.1.b/P2.1.c can rely on them:

  Production-asset arm (BTC/ETH/SOL/XRP, source = fresh state.db snapshot):
    1.  Default-flag bundle exists per asset, cfg_fp = `345978797274721f`
        (the v1.1 candidate cfg_fp pinned in `test_calmlp_lockstep.py`
         anchor 5, P2.1.a-2).
    2.  Ablation-arm bundle exists per asset under
        `--include-sub-floor --provenance-filter=full_dataset`, cfg_fp =
        `1969b12c6c0c39bf` (also pinned in lockstep anchor 5).
    3.  All 8 production bundles share the same `state_db_snapshot_sha256`
        (single-snapshot invariant — guarantees apples-to-apples recipe).
    4.  Per-asset row counts within ±10% of the C0 ticket data-volume
        table (see `86b9wuhhr` "Why" section). Not strict bounds; guards
        against silent extract regressions.

  HYPE/DOGE replay arm (source = local `data/replay/state.db`,
  `historical_replay_calmlp` table; harness shipped at `fe75cf0`):
    5.  `scripts/cal_mlp/extract_data_replay.py` exists.
    6.  `scripts/cal_mlp/features.py` exports `compute_cfg_fp_replay`,
        `CONT_FEATURE_COLS_REPLAY`, `CONT_FEATURE_TRANSFORMS_REPLAY`,
        `DROP_PREDICATES_ORDER_REPLAY`, `REPLAY_PROVENANCE_FILTER_CHOICES`.
    7.  `compute_cfg_fp_replay()` returns `_PINNED_CFG_FP_REPLAY` —
        deterministic recipe fingerprint locked at first-extract time.
    8.  `ASSET_FLOORS` includes HYPE + DOGE entries.
    9.  HYPE/DOGE replay bundles exist with cfg_fp == `_PINNED_CFG_FP_REPLAY`
        and row counts ≥ 4500 (replay corpus is 4,897 per asset; allow
        small reduction from drop predicates).
    10. Lock-step AST guard: extract_data_replay.py contains NO inline
        hour_sin/cos or sigma_winsor formulas — must route through
        canonical helpers (`features.compute_hour_features` /
        `features.apply_sigma_winsor` or `bot.helpers.derived_features`).
        Mirrors `test_calmlp_lockstep.py` anchors 2 + 3 for the new module.

When the snapshot/extract step hasn't run yet, bundle-existence tests
self-skip with a clear message so CI doesn't fail on a fresh checkout.
The local dev/Mac runs the snapshot+extract once (operator-driven) and
the bundles persist under `data/cal_mlp/<asset>/<train_id>/`. Both
arms' artifacts are gitignored.

REGEN protocol: see `kb/decisions/session-resume-may13-from-p2-1-a-3-*`
(once filed) for the snapshot+extract command sequence. Do NOT run
extract_data_replay.py for HYPE/DOGE on the production VPS state.db —
HYPE/DOGE replay corpus lives in the separate Mac-only DB at
`data/replay/state.db` (Phase 2 backfill design, ticket `86b9wy7v3`).
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CAL_MLP = REPO_ROOT / "scripts" / "cal_mlp"
DATA_CAL_MLP = REPO_ROOT / "data" / "cal_mlp"
DATA_REPLAY_DB = REPO_ROOT / "data" / "replay" / "state.db"

EXTRACT_DATA_PY = CAL_MLP / "extract_data.py"
EXTRACT_DATA_REPLAY_PY = CAL_MLP / "extract_data_replay.py"
FEATURES_PY = CAL_MLP / "features.py"


# ─────────────────────────────────────────────────────────────────────
# Pinned cfg_fps (verified empirically; sister pin in
# tests/contracts/test_calmlp_lockstep.py anchor 5)
# ─────────────────────────────────────────────────────────────────────

_PINNED_CFG_FP_V1_1 = "345978797274721f"
_PINNED_CFG_FP_ABLATION = "1969b12c6c0c39bf"
# Captured 2026-05-13 at features.py first-extension commit (P2.1.a-3),
# revised after first replay-extract RCA (see below). Final recipe:
#   - 4 CONT_FEATURE_COLS_REPLAY (spot_distance_to_strike_sigma + abs_*
#     + hour_sin/cos). `seconds_to_close` and `time_decayed_proximity`
#     were dropped from the recipe because `replay_market(...)` evaluates
#     each market at `open_time` exactly, so stc=900s constant for every
#     row — `fit_normstats` raises on zero-variance columns.
#   - DROP_PREDICATES_ORDER_REPLAY (8 predicates)
#   - ASSET_FLOORS_REPLAY (HYPE=75, DOGE=75)
#   - replay_phase2_v1 provenance
# Drift-detector — any change to the replay recipe shifts this hash and
# trips test_p2_1_a_3_compute_cfg_fp_replay_pinned.
# Bit F (2026-05-21, ticket 86ba1wpck) rotated from `9347942aaba71146` via
# the ASSET_FLOORS_REPLAY['BNB']=75 addition. The pre-Bit-F value is
# captured in `kb/decisions/bit-f-bnb-replay-backfill-plan.md` for audit.
_PINNED_CFG_FP_REPLAY = "ea9c30477f844afa"


PRODUCTION_ASSETS = ("BTC", "ETH", "SOL", "XRP")
# Bit F (2026-05-21, ticket 86ba1wpck) widened replay recipe to include BNB.
# Anchor 9 self-skips when a per-asset bundle is absent on the workstation,
# so widening here is safe — the BNB bundle artifact lives Mac-side post-
# Bit F (`data/cal_mlp/BNB/<train_id>/` + `models/cal_mlp_BNB/<train_id>/`).
REPLAY_ASSETS = ("HYPE", "DOGE", "BNB")

# Per-asset post-DROP_PREDICATES row-count expectations from the P2.1.a-3
# extract run. The C0 ticket `86b9wuhhr` "Why" table cites RAW source-table
# counts (e.g., BTC=23,663 settled_15m); the extract drops most of those
# via per-asset price floor (BTC 88¢+), settled_after_cutoff (now-24h),
# null-side, etc. Numbers below are the actual `source_total_rows_post_filter`
# on the 4feb13b8 snapshot (extracted 2026-05-13). Tolerance ±10% catches
# silent drift; tighter ratchets are train.py's job (P2.1.b).
_EXPECTED_ROWS_DEFAULT = {
    "BTC": 8_608,
    "ETH": 7_157,
    "SOL": 10_096,
    "XRP": 9_045,
}
_ROW_TOLERANCE_PCT = 0.10
# Replay corpus = 4,897 per asset. Drop predicates may remove a handful for
# null-eval-time / settled-after-cutoff / non-yes-no-result.
_MIN_ROWS_REPLAY = 4500


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _load_current(asset: str) -> str | None:
    cur = DATA_CAL_MLP / asset / "CURRENT"
    if not cur.exists():
        return None
    return cur.read_text().strip()


def _list_bundles(asset: str) -> list[Path]:
    asset_dir = DATA_CAL_MLP / asset
    if not asset_dir.is_dir():
        return []
    out = []
    for sub in asset_dir.iterdir():
        if not sub.is_dir():
            continue
        bundle = sub / "extract_bundle.json"
        if bundle.exists():
            out.append(bundle)
    return out


def _load_bundle(path: Path) -> dict:
    return json.loads(path.read_text())


def _find_bundle_with_cfg_fp(asset: str, cfg_fp: str) -> dict | None:
    """Return the matching bundle that is ALSO snapshot-bound (post-A.7).
    Pre-A.7 (2026-05-03 era) bundles have `state_db_snapshot_sha256: null`
    and do not count as P2.1.a-3 artifacts — they were extracted from a
    live state.db without snapshot pinning."""
    for bundle_path in _list_bundles(asset):
        bundle = _load_bundle(bundle_path)
        if bundle.get("cfg_fp") != cfg_fp:
            continue
        if not bundle.get("state_db_snapshot_sha256"):
            continue
        return bundle
    return None


def _bundle_dir_with_cfg_fp(asset: str, cfg_fp: str) -> Path | None:
    """Like `_find_bundle_with_cfg_fp` but returns the parent dir for paths
    that need the bundle's resolution context (e.g., audit JSON sister)."""
    for bundle_path in _list_bundles(asset):
        bundle = _load_bundle(bundle_path)
        if bundle.get("cfg_fp") != cfg_fp:
            continue
        if not bundle.get("state_db_snapshot_sha256"):
            continue
        return bundle_path.parent
    return None


def _import_features():
    """Lazy import of scripts/cal_mlp/features so the test file can be
    collected even when the helper hasn't been written yet."""
    if str(CAL_MLP) not in sys.path:
        sys.path.insert(0, str(CAL_MLP))
    import features  # noqa: WPS433
    return features


# ─────────────────────────────────────────────────────────────────────
# Production-asset arm — anchors 1-4
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("asset", PRODUCTION_ASSETS)
def test_p2_1_a_3_default_bundle_exists_per_asset(asset):
    """Anchor 1: default-flag bundle exists per production asset with the
    v1.1 candidate cfg_fp. Self-skips when no bundle has been extracted."""
    bundle = _find_bundle_with_cfg_fp(asset, _PINNED_CFG_FP_V1_1)
    if bundle is None:
        pytest.skip(
            f"no default-flag bundle for {asset} with cfg_fp={_PINNED_CFG_FP_V1_1} "
            f"yet — run snapshot+extract per kb/decisions/session-resume-may13-"
            f"from-p2-1-a-3-* (REGEN section)."
        )
    # Bundle metadata sanity
    assert bundle["asset"] == asset
    assert bundle["cfg_fp"] == _PINNED_CFG_FP_V1_1
    assert bundle["include_sub_floor"] is False
    assert bundle["provenance_filter"] == "all"
    assert bundle["state_db_snapshot_sha256"], (
        "default-flag bundle MUST be bound to a snapshot — run with "
        "--snapshot-sha256 or --auto-snapshot, not --db only."
    )


@pytest.mark.parametrize("asset", PRODUCTION_ASSETS)
def test_p2_1_a_3_ablation_bundle_exists_per_asset(asset):
    """Anchor 2: ablation-arm bundle exists per production asset with the
    `--include-sub-floor --provenance-filter=full_dataset` cfg_fp."""
    bundle = _find_bundle_with_cfg_fp(asset, _PINNED_CFG_FP_ABLATION)
    if bundle is None:
        pytest.skip(
            f"no ablation-arm bundle for {asset} with cfg_fp={_PINNED_CFG_FP_ABLATION} "
            f"yet — run snapshot+extract with `--include-sub-floor "
            f"--provenance-filter=full_dataset`."
        )
    assert bundle["asset"] == asset
    assert bundle["cfg_fp"] == _PINNED_CFG_FP_ABLATION
    assert bundle["include_sub_floor"] is True
    assert bundle["provenance_filter"] == "full_dataset"


def test_p2_1_a_3_all_production_bundles_share_snapshot_sha256():
    """Anchor 3: single-snapshot invariant — all 8 production bundles must
    bind to the SAME state_db_snapshot_sha256, proving they were extracted
    from one consistent point-in-time DB. Skips if any bundle missing.

    Without this invariant, two bundles could be extracted from
    different snapshots and the resulting train/cal/test splits would
    quietly diverge — ticker-disjoint enforcement assumes a single
    monotonic ticker timeline.
    """
    shas: set[str] = set()
    asset_arm_count = 0
    for asset in PRODUCTION_ASSETS:
        for cfg_fp in (_PINNED_CFG_FP_V1_1, _PINNED_CFG_FP_ABLATION):
            bundle = _find_bundle_with_cfg_fp(asset, cfg_fp)
            if bundle is None:
                continue
            asset_arm_count += 1
            sha = bundle.get("state_db_snapshot_sha256")
            if sha:
                shas.add(sha)
    if asset_arm_count < 8:
        pytest.skip(
            f"only {asset_arm_count}/8 production bundles present; cannot enforce "
            f"single-snapshot invariant until all 8 extracted."
        )
    assert len(shas) == 1, (
        f"production bundles bind to multiple snapshots: {shas}. "
        f"All 8 must share one state_db_snapshot_sha256 (single-snapshot invariant)."
    )


@pytest.mark.parametrize("asset", PRODUCTION_ASSETS)
def test_p2_1_a_3_default_bundle_row_counts_within_tolerance(asset):
    """Anchor 4: per-asset post-DROP_PREDICATES row counts within ±10% of
    the P2.1.a-3 captured baseline. Guards against silent extract
    regressions (e.g., a DROP_PREDICATE silently expands and chops the
    corpus, or the price-floor const drifts)."""
    bundle_dir = _bundle_dir_with_cfg_fp(asset, _PINNED_CFG_FP_V1_1)
    if bundle_dir is None:
        pytest.skip(f"no default-flag snapshot-bound bundle for {asset}")
    audit = json.loads((bundle_dir / "extract_audit.json").read_text())
    n_kept = audit["source_total_rows_post_filter"]
    expected = _EXPECTED_ROWS_DEFAULT[asset]
    lo = int(expected * (1 - _ROW_TOLERANCE_PCT))
    hi = int(expected * (1 + _ROW_TOLERANCE_PCT))
    assert lo <= n_kept <= hi, (
        f"{asset} default extract n_kept={n_kept} outside ±{_ROW_TOLERANCE_PCT:.0%} "
        f"of expected {expected} (range [{lo}, {hi}]). If this is an intentional "
        f"corpus growth (e.g., 60+ days since baseline), update _EXPECTED_ROWS_DEFAULT."
    )


# ─────────────────────────────────────────────────────────────────────
# HYPE/DOGE replay arm — anchors 5-9
# ─────────────────────────────────────────────────────────────────────

def test_p2_1_a_3_extract_data_replay_module_exists():
    """Anchor 5: scripts/cal_mlp/extract_data_replay.py must exist as the
    parallel pull path for HYPE/DOGE replay corpus."""
    assert EXTRACT_DATA_REPLAY_PY.exists(), (
        f"extract_data_replay.py not found at {EXTRACT_DATA_REPLAY_PY} — "
        f"P2.1.a-3 HYPE/DOGE arm requires this module."
    )


def test_p2_1_a_3_features_py_exports_replay_recipe():
    """Anchor 6: features.py must export the replay-recipe constants/helpers
    so extract_data_replay.py + downstream train.py have a single source
    of truth for the replay cfg_fp."""
    features = _import_features()
    required = (
        "compute_cfg_fp_replay",
        "CONT_FEATURE_COLS_REPLAY",
        "CONT_FEATURE_TRANSFORMS_REPLAY",
        "DROP_PREDICATES_ORDER_REPLAY",
        "REPLAY_PROVENANCE_FILTER_CHOICES",
        "ASSET_FLOORS_REPLAY",
    )
    missing = [name for name in required if not hasattr(features, name)]
    assert not missing, (
        f"features.py missing required replay-recipe exports: {missing}. "
        f"Add per P2.1.a-3 design (kb/decisions/session-resume-may13-from-p2-1-a-3-*)."
    )


def test_p2_1_a_3_production_cfg_fps_unchanged():
    """Sister-doc lock: extending features.py for HYPE/DOGE replay support
    must NOT shift `compute_cfg_fp()` defaults. The v1.1 production pin
    (`345978797274721f`) and ablation arm (`1969b12c6c0c39bf`) are
    load-bearing for the v1.1 retrain. Adding HYPE/DOGE to ASSET_FLOORS
    (instead of ASSET_FLOORS_REPLAY) would silently shift both pins —
    this test catches that class of drift.

    Mirrors `tests/contracts/test_calmlp_lockstep.py` anchor 5; kept
    here as a defense-in-depth check that the P2.1.a-3 features.py
    extension preserves anchor 5's invariants."""
    features = _import_features()
    default = features.compute_cfg_fp(include_sub_floor=False, provenance_filter='all')
    ablation = features.compute_cfg_fp(include_sub_floor=True, provenance_filter='full_dataset')
    assert default == _PINNED_CFG_FP_V1_1, (
        f"compute_cfg_fp(default) drifted: expected {_PINNED_CFG_FP_V1_1}, got {default}. "
        f"Did you add a new asset to ASSET_FLOORS instead of ASSET_FLOORS_REPLAY? "
        f"Or change SIGMA_WINSOR_ABS_CAP / RAW_PROB_CLIP_EPS / canonical-dict shape?"
    )
    assert ablation == _PINNED_CFG_FP_ABLATION, (
        f"compute_cfg_fp(ablation) drifted: expected {_PINNED_CFG_FP_ABLATION}, got {ablation}."
    )


def test_p2_1_a_3_compute_cfg_fp_replay_pinned():
    """Anchor 7: compute_cfg_fp_replay() returns the pinned hash. Locks the
    recipe — any change to CONT_FEATURE_COLS_REPLAY / transforms / drop
    predicates / sigma_winsor / raw_prob_clip / provenance_filter shifts
    this hash and trips this test."""
    if _PINNED_CFG_FP_REPLAY is None:
        pytest.skip(
            "_PINNED_CFG_FP_REPLAY not yet populated — capture by calling "
            "features.compute_cfg_fp_replay() once the helper lands, then "
            "edit this constant to lock the recipe."
        )
    features = _import_features()
    actual = features.compute_cfg_fp_replay(provenance_filter="replay_phase2_v1")
    assert actual == _PINNED_CFG_FP_REPLAY, (
        f"compute_cfg_fp_replay() drift: expected {_PINNED_CFG_FP_REPLAY}, got {actual}. "
        f"If recipe change is intentional: bump _PINNED_CFG_FP_REPLAY in this test "
        f"AND add a sister anchor to tests/contracts/test_calmlp_lockstep.py "
        f"documenting the v→v' delta."
    )


def test_p2_1_a_3_asset_floors_replay_has_hype_doge():
    """Anchor 8: features.ASSET_FLOORS_REPLAY includes HYPE + DOGE + BNB.
    (BNB added Bit F `86ba1wpck` 2026-05-21 — rotates cfg_fp_replay
    9347942aaba71146 → ea9c30477f844afa.) Kept SEPARATE from production
    ASSET_FLOORS so adding/removing replay assets doesn't shift the v1.1
    production cfg_fp pin (which bakes ASSET_FLOORS into its canonical
    dict). See test_p2_1_a_3_production_cfg_fps_unchanged for the
    load-bearing companion check."""
    features = _import_features()
    assert "HYPE" in features.ASSET_FLOORS_REPLAY, "ASSET_FLOORS_REPLAY missing HYPE"
    assert "DOGE" in features.ASSET_FLOORS_REPLAY, "ASSET_FLOORS_REPLAY missing DOGE"
    assert "BNB" in features.ASSET_FLOORS_REPLAY, (
        "ASSET_FLOORS_REPLAY missing BNB (Bit F `86ba1wpck` 2026-05-21)"
    )
    # Ensure HYPE/DOGE NOT silently in production ASSET_FLOORS (would shift
    # production cfg_fp pin).
    assert "HYPE" not in features.ASSET_FLOORS, (
        "HYPE added to production ASSET_FLOORS — this shifts the v1.1 cfg_fp "
        "pin. Move to ASSET_FLOORS_REPLAY instead."
    )
    assert "DOGE" not in features.ASSET_FLOORS, (
        "DOGE added to production ASSET_FLOORS — this shifts the v1.1 cfg_fp "
        "pin. Move to ASSET_FLOORS_REPLAY instead."
    )


@pytest.mark.parametrize("asset", REPLAY_ASSETS)
def test_p2_1_a_3_replay_bundle_exists_per_asset(asset):
    """Anchor 9: HYPE/DOGE/BNB replay bundles exist with the pinned replay
    cfg_fp and row counts ≥ 4500 (HYPE/DOGE corpus is 4,897 each; BNB is
    5,906 post-Bit-F, 2026-05-21).

    Replay bundles do NOT have `state_db_snapshot_sha256` because their
    source is `data/replay/state.db::historical_replay_calmlp` (a
    Mac-only DB built by the Phase 2 backfill harness, not a sqlite
    snapshot). The bundle records the source path + table-name fingerprint
    instead — see `extract_data_replay.py` bundle schema."""
    bundle_dir = None
    asset_dir = DATA_CAL_MLP / asset
    if asset_dir.is_dir():
        for d in sorted(asset_dir.iterdir()):
            bp = d / "extract_bundle.json"
            if not bp.exists():
                continue
            b = json.loads(bp.read_text())
            if b.get("cfg_fp") == _PINNED_CFG_FP_REPLAY:
                bundle_dir = d
                break
    if bundle_dir is None:
        pytest.skip(
            f"no replay bundle for {asset} with cfg_fp={_PINNED_CFG_FP_REPLAY} yet — "
            f"run extract_data_replay.py per REGEN protocol."
        )
    bundle = json.loads((bundle_dir / "extract_bundle.json").read_text())
    assert bundle["asset"] == asset
    # R1 M2: recipe_namespace is the ONLY signal P2.1.b train.py will use to
    # route replay bundles through CONT_FEATURE_COLS_REPLAY (vs production
    # CONT_FEATURE_COLS). Pin it here so a future refactor that drops the
    # field is caught at this Bit's contract gate, not at P2.1.b runtime.
    assert bundle.get("recipe_namespace") == "replay_v1", (
        f"{asset} replay bundle missing or incorrect recipe_namespace: "
        f"got {bundle.get('recipe_namespace')!r}, expected 'replay_v1'. "
        f"Downstream train.py needs this signal to switch CONT_FEATURE_COLS sets."
    )
    # Replay-specific bundle audit fields — the bundle records the source
    # DB by path + size (NOT a sqlite snapshot sha) per the module
    # docstring's "Bundle shape" section.
    assert bundle.get("state_db_snapshot_sha256") is None, (
        "replay bundle should NOT have state_db_snapshot_sha256 — replay corpus "
        "is operator-curated, not a content-addressed sqlite snapshot."
    )
    assert bundle.get("replay_db_path"), "replay bundle missing replay_db_path"
    assert bundle.get("replay_table_name") == "historical_replay_calmlp"
    audit = json.loads((bundle_dir / "extract_audit.json").read_text())
    n_kept = audit["source_total_rows_post_filter"]
    assert n_kept >= _MIN_ROWS_REPLAY, (
        f"{asset} replay extract n_kept={n_kept} below floor {_MIN_ROWS_REPLAY}. "
        f"Inspect drop_buckets in {bundle_dir}/extract_audit.json."
    )


# ─────────────────────────────────────────────────────────────────────
# Lock-step AST guard — anchor 10
# ─────────────────────────────────────────────────────────────────────

# Mirrors patterns from test_calmlp_lockstep.py anchors 2 + 3.
_INLINE_HOUR_SINCOS_PATTERNS = (
    re.compile(r"\bnp\.sin\s*\(\s*2(?:\.0)?\s*\*\s*np\.pi\s*\*\s*[A-Za-z_][\w]*\s*/\s*24"),
    re.compile(r"\bnp\.cos\s*\(\s*2(?:\.0)?\s*\*\s*np\.pi\s*\*\s*[A-Za-z_][\w]*\s*/\s*24"),
    re.compile(r"\bmath\.sin\s*\(\s*2(?:\.0)?\s*\*\s*math\.pi\s*\*\s*[A-Za-z_][\w]*\s*/\s*24"),
    re.compile(r"\bmath\.cos\s*\(\s*2(?:\.0)?\s*\*\s*math\.pi\s*\*\s*[A-Za-z_][\w]*\s*/\s*24"),
)


def test_p2_1_a_3_extract_data_replay_no_inline_hour_sincos():
    """Anchor 10a: extract_data_replay.py must NOT inline hour_sin/cos
    formulas. Routes through `features.compute_hour_features` (DataFrame
    branch) or `bot.helpers.derived_features.compute_hour_sin_cos`
    (scalar branch). Same lock-step contract as the 4 existing drift sites
    (test_calmlp_lockstep.py anchor 2)."""
    if not EXTRACT_DATA_REPLAY_PY.exists():
        pytest.skip("extract_data_replay.py not yet written")
    src = EXTRACT_DATA_REPLAY_PY.read_text()
    for pat in _INLINE_HOUR_SINCOS_PATTERNS:
        m = pat.search(src)
        assert m is None, (
            f"inline hour_sin/cos formula in extract_data_replay.py: {m.group(0)!r}. "
            f"Use canonical helper from features.compute_hour_features or "
            f"bot.helpers.derived_features.compute_hour_sin_cos."
        )


def test_p2_1_a_3_extract_data_replay_no_inline_sigma_winsor():
    """Anchor 10b: extract_data_replay.py must NOT inline a sigma-winsorize
    `clip(-25, 25)` or equivalent — must use `features.apply_sigma_winsor`
    or `bot.helpers.derived_features.apply_sigma_winsor`. Mirrors
    test_calmlp_lockstep.py anchor 3."""
    if not EXTRACT_DATA_REPLAY_PY.exists():
        pytest.skip("extract_data_replay.py not yet written")
    src = EXTRACT_DATA_REPLAY_PY.read_text()
    # Numeric `25` or `25.0` in a clip-like context (not the helper-import
    # line). Match `.clip(... 25 ...)` patterns + bare numeric literal use.
    bad_patterns = (
        re.compile(r"\.clip\([^)]*\b25(?:\.0)?\b"),
        re.compile(r"\bSIGMA_WINSOR_ABS_CAP\s*=\s*25"),  # local re-defining
    )
    for pat in bad_patterns:
        m = pat.search(src)
        assert m is None, (
            f"inline sigma-winsor in extract_data_replay.py: {m.group(0)!r}. "
            f"Use canonical helper from features.apply_sigma_winsor."
        )


def test_p2_1_a_3_extract_data_replay_imports_canonical_helpers():
    """Anchor 10c: extract_data_replay.py must IMPORT either
    `features.compute_hour_features` (or `compute_hour_sin_cos` from
    bot.helpers.derived_features) AND `apply_sigma_winsor`. Belt-and-
    braces companion to anchors 10a/10b — catches files that don't have
    inline drift but also forgot to call the helper."""
    if not EXTRACT_DATA_REPLAY_PY.exists():
        pytest.skip("extract_data_replay.py not yet written")
    src = EXTRACT_DATA_REPLAY_PY.read_text()
    has_hour_helper = (
        "compute_hour_features" in src or "compute_hour_sin_cos" in src
    )
    has_winsor_helper = "apply_sigma_winsor" in src
    assert has_hour_helper, (
        "extract_data_replay.py does not reference compute_hour_features or "
        "compute_hour_sin_cos — replay rows already have hour_sin/cos so "
        "the module may be using them as-is, but the canonical helper should "
        "still be imported and used to recompute or verify the values."
    )
    assert has_winsor_helper, (
        "extract_data_replay.py does not reference apply_sigma_winsor — "
        "even if replay rows have a sigma_winsorize column, the canonical "
        "helper must run on derived spot_distance_to_strike_sigma values."
    )
