"""Phase 1b verifier — runs identical checks on Mac (pre-deploy) and VPS
(post-deploy) to prove the v2 ETH bundle is intact, loadable, and
production-ready under the per-asset CALMLP_BUNDLE_DIR_ETH override.

Pre-deploy (Mac): verifies the bundle exists locally and loads.
Post-deploy (VPS): re-run after scp + restart to prove the same after
copy. Same exit-code contract: 0 = all checks passed; non-zero = one or
more checks failed (read stderr).

Why a script vs only pytest: this verifier exercises real on-disk bundles
(not synthetic fixtures), and we need to run it on a host without pytest
configured (the VPS). The pytest counterparts in
tests/integration/test_phase1b_eth_v2_rollout.py pin train_ids and conventions.

The pinned v2 ETH train_id is INTENTIONALLY hardcoded — Phase 1b is a
one-shot operator handoff, not a generalizable sweep. If a future v3
ETH bundle is rolled out, write a new verifier for that explicit ship.

`KALSHI_PROJECT_ROOT` env var, when set to an existing directory,
overrides the upward filesystem walk for project-root discovery —
matches `integration.CalMLPPredictor`'s convention so test fixtures and
sandbox shells stay isolated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Repo-relative paths only; runs from any cwd via PROJECT_ROOT discovery.
V2_ETH_TRAIN_ID = "2026-05-03T16:28:21.622306Z-f08fbc00"
V1_ETH_TRAIN_ID = "2026-04-28T11:50:48.975743Z-a0000cc1"
V2_ETH_BUNDLE_REL = f"models/cal_mlp_ETH/{V2_ETH_TRAIN_ID}"
V2_ETH_EXPECTED_CFG_FP = "1969b12c6c0c39bf"
V1_ETH_EXPECTED_CFG_FP = "178d14020bd21beb"
EXPECTED_MARKET_BLEND_W = 0.40
EXPECTED_PHASE = 5
EXPECTED_DEPLOY_FOLD_IDX = 1
EXPECTED_N_VOCAB = 3140  # locked at v2 training time; pinned to surface re-train

# Full model_definition field pins. The canonical-JSON SHA in
# `check_file_shas` catches ANY change to ANY field, but is
# binary (match/mismatch). When that fails, this dict surfaces
# WHICH field drifted — useful triage signal. Computed from the
# v2 ETH model_definition.json on 2026-05-06.
EXPECTED_MODEL_DEF_FIELDS = {
    "cfg_fp": "1969b12c6c0c39bf",
    "delta_logit_clamp": 2.5,
    "dropout": 0.1,
    "emb_dim": 4,
    "hidden_1": 64,
    "hidden_2": 32,
    "input_continuous_dim": 8,
    "model_kind": "ResidualMLPV1",
    "n_cont": 8,
    "n_missing_indicator_cols": 0,
    "n_price_tiers": 4,
    "n_sides": 2,
    "n_stc_buckets": 4,
    "n_vocab": EXPECTED_N_VOCAB,
    "n_vol_regimes": 2,
    "raw_prob_clip_eps": 1e-06,
}

# Per-asset v1 train_ids on VPS as of 2026-05-05 (queried from
# evaluated_opportunities.cal_mlp_train_id GROUP BY asset). Pinned so
# Step 4 post-restart SQL can compare exact strings, not "starts-with"
# heuristics. Only ETH should change in Phase 1b.
PROD_V1_TRAIN_IDS = {
    "BTC": "2026-04-28T11:50:29.671752Z-8acc233e",
    "ETH": "2026-04-28T11:50:48.975743Z-a0000cc1",
    "SOL": "2026-04-28T11:50:38.948474Z-2901fa5f",
    "XRP": "2026-04-28T11:50:39.500729Z-b8161b7b",
}

# Required SHA keys per fold artifact. Two distinct lists because the
# extract-bundle and model-bundle eval_fold_artifacts schemas differ:
#   - extract bundle (data/cal_mlp/<asset>/<id>/extract_bundle.json):
#     normstats_path/sha + parquet_path/sha per fold.
#   - model bundle (models/cal_mlp_<asset>/<id>/cal_mlp_..._phase5_bundle.json):
#     predictions_path/sha per fold + members[].{checkpoint,marker}_path/sha.
# Missing key in either list is a FAILURE, not a silent skip.
REQUIRED_EXTRACT_FOLD_SHA_KEYS = ("normstats_sha256", "parquet_sha256")
REQUIRED_MODEL_FOLD_SHA_KEYS = ("predictions_sha256",)

logger = logging.getLogger("phase1b_verify")


def _project_root() -> Path:
    """Discover repo root.
    Precedence: KALSHI_PROJECT_ROOT env var (must exist + be a dir),
    else walk up from this file looking for pyproject.toml + market_config.py.
    """
    override = os.environ.get("KALSHI_PROJECT_ROOT", "").strip()
    if override:
        p = Path(override)
        if p.is_dir():
            return p.resolve()
        raise RuntimeError(f"KALSHI_PROJECT_ROOT set but not a directory: {override}")
    here = Path(__file__).resolve()
    for ancestor in [here, *here.parents]:
        if (ancestor / "pyproject.toml").exists() and (ancestor / "market_config.py").exists():
            return ancestor
    raise RuntimeError("could not locate project root (no pyproject.toml + market_config.py found)")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _PathInsulator:
    """Context manager: insert a path at sys.path[0] only if not already
    present, and remove on exit only if we inserted it. Mirrors the
    conditional `if str(...) not in sys.path` pattern used throughout
    cal_mlp (e.g., integration.py module-level setup at lines ~48-50,
    integration.py:_load `_proj_added` block, and _helpers.py:30 after
    Phase 1b R4 made it conditional). Reduces sys.path drift across
    pytest tests that exercise this verifier.
    """

    def __init__(self, path: Path) -> None:
        self._path_str = str(path)
        self._added = False

    def __enter__(self) -> "_PathInsulator":
        if self._path_str not in sys.path:
            sys.path.insert(0, self._path_str)
            self._added = True
        return self

    def __exit__(self, *exc) -> None:
        if self._added:
            try:
                sys.path.remove(self._path_str)
            except ValueError:
                pass


def check_bundle_exists(project_root: Path) -> tuple[bool, str]:
    bundle_dir = project_root / V2_ETH_BUNDLE_REL
    if not bundle_dir.exists():
        return False, f"bundle dir missing: {bundle_dir}"
    if not bundle_dir.is_dir():
        return False, f"bundle path is not a directory: {bundle_dir}"
    phase5_path = bundle_dir / f"cal_mlp_ETH_{V2_ETH_TRAIN_ID}_phase5_bundle.json"
    if not phase5_path.exists():
        return False, f"phase5 bundle JSON missing: {phase5_path}"
    return True, f"bundle dir + phase5 JSON present at {bundle_dir}"


def check_bundle_metadata(project_root: Path) -> tuple[bool, str]:
    """Validate the phase5 bundle JSON's declared metadata (cfg_fp, phase,
    deploy_fold_idx, market_blend_w, n_vocab) match Phase 0's baseline."""
    bundle_dir = project_root / V2_ETH_BUNDLE_REL
    phase5_path = bundle_dir / f"cal_mlp_ETH_{V2_ETH_TRAIN_ID}_phase5_bundle.json"
    bundle = json.loads(phase5_path.read_text())
    if bundle.get("cfg_fp") != V2_ETH_EXPECTED_CFG_FP:
        return False, f"cfg_fp drift: bundle={bundle.get('cfg_fp')} expected={V2_ETH_EXPECTED_CFG_FP}"
    if bundle.get("phase") != EXPECTED_PHASE:
        return False, f"phase drift: bundle={bundle.get('phase')} expected={EXPECTED_PHASE}"
    if bundle.get("deploy_fold_idx") != EXPECTED_DEPLOY_FOLD_IDX:
        return False, (
            f"deploy_fold_idx drift: bundle={bundle.get('deploy_fold_idx')} "
            f"expected={EXPECTED_DEPLOY_FOLD_IDX}"
        )
    bundle_w = bundle.get("market_blend_w")
    if bundle_w is None or abs(bundle_w - EXPECTED_MARKET_BLEND_W) > 1e-9:
        return False, f"market_blend_w drift: bundle={bundle_w} expected={EXPECTED_MARKET_BLEND_W}"
    if bundle.get("asset") != "ETH":
        return False, f"asset mismatch: bundle={bundle.get('asset')} expected=ETH"
    if bundle.get("train_id") != V2_ETH_TRAIN_ID:
        return False, f"train_id mismatch: bundle={bundle.get('train_id')} expected={V2_ETH_TRAIN_ID}"

    # Full model_definition field pins. Disclose ALL drifts at once
    # (not just the first) — when canonical-SHA mismatches, this is
    # the triage signal: "which field changed?"
    model_def_path = bundle_dir / bundle["model_definition_path"]
    model_def = json.loads(model_def_path.read_text())
    diffs = []
    for key, expected in EXPECTED_MODEL_DEF_FIELDS.items():
        actual = model_def.get(key, "<MISSING>")
        if actual != expected:
            diffs.append(f"{key}: model_def={actual!r} expected={expected!r}")
    if diffs:
        return False, "model_definition field drift: " + "; ".join(diffs)

    return True, (
        f"metadata OK: cfg_fp={V2_ETH_EXPECTED_CFG_FP} phase=5 "
        f"deploy_fold_idx=1 market_blend_w=0.40 "
        f"({len(EXPECTED_MODEL_DEF_FIELDS)} model_def fields pinned)"
    )


def check_file_shas(project_root: Path) -> tuple[bool, str]:
    """Recompute SHA256 of every checkpoint, marker, normstats, parquet,
    predictions, and extract-bundle file referenced by the bundle JSON,
    and assert each matches what the bundle declares. This is the
    scp-corruption check.

    Required keys per fold artifact: enforced via REQUIRED_EXTRACT_FOLD_
    SHA_KEYS — a missing key is a FAILURE, not a silent skip.
    """
    bundle_dir = project_root / V2_ETH_BUNDLE_REL
    phase5_path = bundle_dir / f"cal_mlp_ETH_{V2_ETH_TRAIN_ID}_phase5_bundle.json"
    bundle = json.loads(phase5_path.read_text())

    mismatches: list[str] = []
    n_checked = 0

    for fold_art in bundle["eval_fold_artifacts"]:
        for member in fold_art["members"]:
            for kind in ("checkpoint_path", "marker_path"):
                rel = member[kind]
                expected = member[kind.replace("_path", "_sha256")]
                actual = _sha256_file(bundle_dir / rel)
                n_checked += 1
                if actual != expected:
                    mismatches.append(f"{kind} {rel}: expected={expected} actual={actual}")
        for sha_key in REQUIRED_MODEL_FOLD_SHA_KEYS:
            path_key = sha_key.replace("_sha256", "_path")
            if path_key not in fold_art or sha_key not in fold_art:
                return False, (
                    f"model fold {fold_art.get('fold')} missing "
                    f"{path_key}/{sha_key} (required by Phase 1b verifier)"
                )
            rel = fold_art[path_key]
            expected = fold_art[sha_key]
            actual = _sha256_file(bundle_dir / rel)
            n_checked += 1
            if actual != expected:
                mismatches.append(f"{path_key} {rel}: expected={expected} actual={actual}")

    extract_rel = bundle["extract_bundle_path"]
    extract_path = project_root / extract_rel
    if not extract_path.exists():
        return False, f"extract bundle missing at {extract_path}"
    extract_actual = _sha256_file(extract_path)
    extract_expected = bundle["extract_bundle_sha256"]
    n_checked += 1
    if extract_actual != extract_expected:
        mismatches.append(
            f"extract_bundle: expected={extract_expected} actual={extract_actual}"
        )

    # model_definition.json — phase5 bundle declares its sha; warmup
    # opens it. SHA-checking it here closes a coverage gap (a single
    # byte flip preserving JSON parseability would otherwise survive
    # file-sha + chain-sha and only surface at warmup-time).
    #
    # NOTE: train.py emits model_definition_sha256 over the canonical
    # JSON form (sort_keys=True, separators=(',', ':') — minified),
    # NOT the pretty-printed on-disk bytes. We re-canonicalize before
    # hashing to match. Failure modes covered:
    #   - JSON unparseable (json.loads raises → check raises → caught
    #     by main()'s except → reported as failed check)
    #   - JSON parseable but content differs (canonical SHA mismatch)
    if "model_definition_sha256" not in bundle or "model_definition_path" not in bundle:
        return False, "phase5 bundle missing model_definition_path/sha256"
    md_path = bundle_dir / bundle["model_definition_path"]
    md_obj = json.loads(md_path.read_text())
    md_canonical = json.dumps(md_obj, sort_keys=True, separators=(",", ":")).encode()
    md_actual = hashlib.sha256(md_canonical).hexdigest()
    md_expected = bundle["model_definition_sha256"]
    n_checked += 1
    if md_actual != md_expected:
        mismatches.append(f"model_definition (canonical): expected={md_expected} actual={md_actual}")

    extract_bundle = json.loads(extract_path.read_text())
    extract_dir = extract_path.parent
    for fold_art in extract_bundle["eval_fold_artifacts"]:
        for sha_key in REQUIRED_EXTRACT_FOLD_SHA_KEYS:
            path_key = sha_key.replace("_sha256", "_path")
            if path_key not in fold_art or sha_key not in fold_art:
                return False, (
                    f"extract fold {fold_art.get('fold')} missing "
                    f"{path_key}/{sha_key} (required by Phase 1b verifier)"
                )
            rel = fold_art[path_key]
            expected = fold_art[sha_key]
            actual = _sha256_file(extract_dir / rel)
            n_checked += 1
            if actual != expected:
                mismatches.append(f"{path_key} {rel}: expected={expected} actual={actual}")

    # ticker_vocab.json — extract bundle declares its sha; warmup opens
    # it. Closes the same coverage gap as model_definition.
    if (
        "ticker_vocab_sha256" not in extract_bundle
        or "ticker_vocab_path" not in extract_bundle
    ):
        return False, "extract bundle missing ticker_vocab_path/sha256"
    tv_actual = _sha256_file(extract_dir / extract_bundle["ticker_vocab_path"])
    tv_expected = extract_bundle["ticker_vocab_sha256"]
    n_checked += 1
    if tv_actual != tv_expected:
        mismatches.append(f"ticker_vocab: expected={tv_expected} actual={tv_actual}")

    if mismatches:
        return False, f"SHA mismatches ({len(mismatches)} of {n_checked}): " + "; ".join(mismatches[:3])
    # Disclose what's NOT covered: train_audit, extract_audit, and the
    # _bundle.json sibling (= phase4 unconformalized) are intentionally
    # unverified here. The chain check (#4) covers conformal-via-sha256
    # of all checkpoint+normstats hashes.
    return True, (
        f"all {n_checked} runtime-loaded files SHA-match bundle JSON "
        f"(excludes train_audit / extract_audit / phase4 sibling — "
        f"warmup load is the safety net for those)"
    )


def check_sha_chain(project_root: Path) -> tuple[bool, str]:
    """Recompute Phase 4 + Phase 5 SHA chain via the canonical helper —
    same code path the integration loader uses. Fails on metadata
    inconsistency (e.g., a member's checkpoint_sha256 hand-edited)."""
    with _PathInsulator(project_root / "scripts" / "cal_mlp"):
        from _helpers import verify_bundle_sha_chain
        bundle_dir = project_root / V2_ETH_BUNDLE_REL
        phase5_path = bundle_dir / f"cal_mlp_ETH_{V2_ETH_TRAIN_ID}_phase5_bundle.json"
        bundle = json.loads(phase5_path.read_text())
        try:
            verify_bundle_sha_chain(bundle)
        except RuntimeError as e:
            return False, f"sha chain failed: {e}"
    return True, "phase4 + phase5 sha chain verified"


_ALL_CALMLP_ENV_VARS = (
    "CALMLP_BUNDLE_DIR",
    "CALMLP_BUNDLE_DIR_BTC",
    "CALMLP_BUNDLE_DIR_ETH",
    "CALMLP_BUNDLE_DIR_SOL",
    "CALMLP_BUNDLE_DIR_XRP",
)


def check_load_with_override(project_root: Path) -> tuple[bool, str]:
    """End-to-end: with CALMLP_BUNDLE_DIR_ETH set, ETH loads v2; BTC's
    resolver returns ''. Mirrors what the bot does at warmup.

    Snapshots ALL five CALMLP_BUNDLE_DIR* env vars on entry, clears
    them, sets only `_ETH`, and restores prior values on exit. This
    isolates the standalone CLI verifier from operator shell state
    (the autouse pytest fixture does the same for tests). Without this,
    a developer with `CALMLP_BUNDLE_DIR_BTC=…` exported would see a
    misleading 'resolver leaked override to BTC' failure here.

    Note on sys.path: integration.py module-import installs
    `scripts/cal_mlp` permanently in sys.path (line 49), so we do NOT
    insulate that path here — doing so would evict integration.py's
    permanent installation on __exit__, breaking subsequent imports of
    `train`, `sizing`, etc. inside `_load`. We only insulate
    project_root for `from market_config import ...` inside _load.
    """
    prior = {var: os.environ.get(var) for var in _ALL_CALMLP_ENV_VARS}
    for var in _ALL_CALMLP_ENV_VARS:
        os.environ.pop(var, None)
    os.environ["CALMLP_BUNDLE_DIR_ETH"] = V2_ETH_BUNDLE_REL
    try:
        # Both insulators leave permanent sys.path entries iff the
        # imported modules' own module-level inserts beat ours (race
        # we don't try to win). On second/third invocations within
        # the same process, `if not in sys.path` keeps things idempotent.
        with _PathInsulator(project_root), _PathInsulator(project_root / "scripts" / "cal_mlp"):
            from integration import CalMLPPredictor, _resolve_bundle_dir

            eth_resolved = _resolve_bundle_dir("ETH")
            btc_resolved = _resolve_bundle_dir("BTC")
            if eth_resolved != V2_ETH_BUNDLE_REL:
                return False, f"resolver ETH returned {eth_resolved!r}, expected {V2_ETH_BUNDLE_REL!r}"
            if btc_resolved != "":
                return False, (
                    f"resolver leaked override to BTC: {btc_resolved!r} "
                    f"(only CALMLP_BUNDLE_DIR_ETH set, BTC should resolve to '')"
                )

            predictor = CalMLPPredictor(asset="ETH", project_root=str(project_root))
            predictor.warmup()
            if not predictor._loaded:
                return False, "ETH warmup completed but predictor._loaded is False"
            if predictor.train_id != V2_ETH_TRAIN_ID:
                return False, f"train_id={predictor.train_id} expected={V2_ETH_TRAIN_ID}"
            if abs(predictor.market_blend_w - EXPECTED_MARKET_BLEND_W) > 1e-9:
                return False, (
                    f"market_blend_w={predictor.market_blend_w} expected={EXPECTED_MARKET_BLEND_W}"
                )
            if not predictor.models or len(predictor.models) < 2:
                return False, f"ensemble n_models={len(predictor.models or [])} (<2 → conformal degenerate)"
            n_models = len(predictor.models)
    finally:
        for var, val in prior.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
    return True, f"v2 loads via override: train_id={V2_ETH_TRAIN_ID} n_models={n_models}"


def check_v2_differs_from_v1(project_root: Path) -> tuple[bool, str]:
    """Sanity: v2 cfg_fp must differ from v1's. If they're equal, we're
    not actually shipping a different model."""
    if V2_ETH_EXPECTED_CFG_FP == V1_ETH_EXPECTED_CFG_FP:
        return False, "v2 cfg_fp == v1 cfg_fp — not actually a different model"
    return True, f"v2 cfg_fp ({V2_ETH_EXPECTED_CFG_FP}) ≠ v1 cfg_fp ({V1_ETH_EXPECTED_CFG_FP})"


def check_no_stale_shell_overrides(project_root: Path) -> tuple[bool, str]:
    """Fail if the operator has shell-exported overrides that would
    actually break Phase 1b. The bot reads `.env` at process start
    (via systemd EnvironmentFile=), NOT the operator's interactive
    shell, so non-ETH per-asset shell exports don't affect the
    deployed bot. We only fail on overrides that materially conflict:

    1. CALMLP_BUNDLE_DIR (global) — would override the per-asset value
       if anyone unsets the per-asset var later, AND breaks
       check_load_with_override's BTC-resolves-to-empty assertion
       (handled internally by the snapshot/restore, but still a
       stale-shell hazard for direct invocations).
    2. CALMLP_BUNDLE_DIR_ETH set but != V2_ETH_BUNDLE_REL — the
       operator's shell override would beat what the verifier intends
       to test if any future caller forgets to clear it.

    Stray BTC/SOL/XRP shell exports are demoted to an advisory in the
    success message (not a failure) — they don't affect the bot, and
    failing on them adds operational friction with no security gain.
    """
    advisories = []
    failures = []
    for var in _ALL_CALMLP_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if not val:
            continue
        if var == "CALMLP_BUNDLE_DIR":
            failures.append(
                f"{var}={val!r} (global override would affect non-ETH "
                f"assets if per-asset vars get unset; clear before deploying)"
            )
        elif var == "CALMLP_BUNDLE_DIR_ETH":
            if val != V2_ETH_BUNDLE_REL:
                failures.append(
                    f"{var}={val!r} differs from pin {V2_ETH_BUNDLE_REL!r}"
                )
        else:
            advisories.append(f"{var}={val!r}")
    if failures:
        return False, "; ".join(failures)
    if advisories:
        return True, (
            f"no Phase-1b-blocking shell overrides; advisory: stray "
            f"non-ETH per-asset vars (won't affect systemd-loaded bot, "
            f"but consider unsetting): {', '.join(advisories)}"
        )
    return True, "no stale CALMLP_BUNDLE_DIR* in shell environment"


def check_current_pointer_is_v1(project_root: Path) -> tuple[bool, str]:
    """Pre-flight on the host where this runs: models/cal_mlp_<asset>/CURRENT
    must point at the v1 train_id for ALL FOUR assets. This is the
    rollback contract: on `unset CALMLP_BUNDLE_DIR_ETH` + restart, the
    resolver returns '' and the loader reads CURRENT — which had better
    be v1 for ETH. For BTC/SOL/XRP, CURRENT is what they're loading
    today; if a CURRENT has drifted to a non-v1 train_id (e.g.,
    accidental scp of a Mac-trained bundle), live serving for that
    asset is on a non-shadow-cleared bundle.

    On Mac, all four CURRENTs are typically pointed at v2 train_ids
    for development — this check FAILS by design on Mac. On VPS
    (post-scp, pre-env-set), it should pass for all four.

    Mac users: skip via --skip-current-pointer when running locally.
    """
    drift = []
    for asset, expected in PROD_V1_TRAIN_IDS.items():
        current_path = project_root / "models" / f"cal_mlp_{asset}" / "CURRENT"
        if not current_path.exists():
            drift.append(f"{asset}: CURRENT missing at {current_path}")
            continue
        actual = current_path.read_text().strip()
        if actual != expected:
            drift.append(
                f"{asset}: CURRENT={actual!r} expected v1 {expected!r} "
                f"(rollback would land on wrong bundle)"
            )
    if drift:
        return False, "; ".join(drift) + (
            ". On Mac this is expected — re-run with --skip-current-pointer."
        )
    return True, (
        f"CURRENT points at v1 for all 4 assets (BTC/ETH/SOL/XRP); "
        f"rollback path intact"
    )


# (name, fn, requires_torch_load, requires_current_pointer_check)
# — when corresponding skip flag is set, the check is skipped. Both
# flags are False for most checks; making them explicit per-row
# avoids the prior "skip-flag matched against name string" coupling.
CHECKS = [
    ("bundle_exists",            check_bundle_exists,            False, False),
    ("bundle_metadata",          check_bundle_metadata,          False, False),
    ("file_shas",                check_file_shas,                False, False),
    ("sha_chain",                check_sha_chain,                False, False),
    ("v2_differs_from_v1",       check_v2_differs_from_v1,       False, False),
    ("no_stale_shell_overrides", check_no_stale_shell_overrides, False, False),
    ("current_pointer_is_v1",    check_current_pointer_is_v1,    False, True),
    ("load_with_override",       check_load_with_override,       True,  False),
]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Skip the end-to-end load check (useful in environments without torch/pandas).",
    )
    parser.add_argument(
        "--skip-current-pointer",
        action="store_true",
        help="Skip the CURRENT-points-at-v1 check (use on Mac where CURRENT is set to v2 for local testing).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    project_root = _project_root()
    logger.info("project_root=%s", project_root)
    logger.info("v2_eth_bundle_rel=%s", V2_ETH_BUNDLE_REL)

    failed = []
    for name, fn, needs_load, needs_current_pointer in CHECKS:
        if needs_load and args.skip_load:
            logger.info("[SKIP] %s (--skip-load)", name)
            continue
        if needs_current_pointer and args.skip_current_pointer:
            logger.info("[SKIP] %s (--skip-current-pointer)", name)
            continue
        try:
            ok, msg = fn(project_root)
        except Exception as e:
            ok, msg = False, f"check raised: {e!r}"
        status = "[OK]  " if ok else "[FAIL]"
        logger.info("%s %s — %s", status, name, msg)
        if not ok:
            failed.append(name)

    logger.info("=" * 60)
    if failed:
        logger.error("FAILED checks: %s", ", ".join(failed))
        # KB docs are by-policy uncommitted (memory/feedback_no_kb_commits.md);
        # they're not on VPS via git pull. Embed recovery inline so a
        # VPS-side operator has the steps when this exits non-zero.
        # Branch on host: only emit the VPS-path recipe when the
        # Phase-1b VPS root exists.
        vps_repo = "/home/botuser/kalshi-bot-repo"
        if Path(vps_repo).is_dir():
            logger.error(
                "Rollback recipe (if v2 was already env-set + bot restarted):"
            )
            logger.error(
                "  sudo -u botuser sed -i.bak.phase1b-step7 "
                "'/^CALMLP_BUNDLE_DIR_ETH=/d' %s/.env "
                "&& sudo systemctl restart kalshi-bot.service",
                vps_repo,
            )
        else:
            logger.error(
                "Pre-deploy on local: investigate the failed check; "
                "do NOT proceed to scp / .env edit / restart."
            )
        return 1
    logger.info("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
