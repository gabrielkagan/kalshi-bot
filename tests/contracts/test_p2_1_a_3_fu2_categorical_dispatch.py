"""Phase 2 P2.1.a-3-fu2 — Phase4Dataset + preds_df categorical-FE dispatch
(ClickUp 86b9xd9hn).

P2.1.a-3-fu1 (ClickUp 86b9xbd2u, SHIPPED `04982c6`) added the continuous-
feature dispatch surface via `features.resolve_recipe(...)`. fu1's R1
adversarial M1 finding (deferred, filed as fu2) surfaced a second
structural gap on the categorical side:

  scripts/cal_mlp/train.py::Phase4Dataset.__init__ unconditionally reads
  `df['price_tier']` + `df['vol_regime_int']` int columns to feed
  `CalibrationMLP.forward`'s 4 categorical one-hots (price_tier=4 classes,
  stc_bucket=4, vol_regime_int=2, side_int=2).

  Replay parquets produced by `scripts/cal_mlp/extract_data_replay.py`
  STRUCTURALLY LACK both columns (no `market_price` → no
  PRICE_BIN_CUTOFFS digitization; no vol regime feed for HYPE/DOGE
  replay). Result: `python -m scripts.cal_mlp.train --asset HYPE`
  KeyErrors at Phase4Dataset construction.

fu2 (Option A — operator-confirmed 2026-05-13): add a
`categorical_feature_cols` field to RecipeSpec. Production recipe lists
the full 4-tuple; replay recipe lists only the 2 categoricals the parquet
ACTUALLY has (`stc_bucket`, `side_int`). Phase4Dataset (and the preds_df
concat in train.py:run) default the absent categorical columns to 0 at
the construction site — does NOT touch the parquet on disk (cfg_fp_replay
stays stable; no re-extract).

Effect for replay: every replay row's one-hot is `[1,0,0,0]` for
price_tier and `[1,0]` for vol_regime_int — the model trains on a
degenerate categorical signal for those two axes but the architecture
runs end-to-end. If HYPE/DOGE Brier/ECE turn out degenerate vs BTC/ETH,
that's the signal to escalate to Option B (proxy derivation in
extract_data_replay.py + cfg_fp_replay bump + re-extract). Sister
follow-up file lives in the resume doc.

Sister anchors:
  - `test_p2_1_a_3_fu1_recipe_dispatch.py` — cont_feature_cols dispatch
    (this file's prerequisite — adds the namespace-routing surface).
  - `test_p2_1_a_3_corpus_snapshots.py` — replay-bundle producer contract.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CAL_MLP = REPO_ROOT / "scripts" / "cal_mlp"

TRAIN_PY = CAL_MLP / "train.py"
VALIDATE_PY = CAL_MLP / "validate.py"
FEATURES_PY = CAL_MLP / "features.py"

# On-disk replay bundles (P2.1.a-3, Mac-local, gitignored). Self-skipping
# fixtures so CI without these dirs collects clean.
HYPE_REPLAY_BUNDLE = (
    REPO_ROOT / "data" / "cal_mlp" / "HYPE"
    / "2026-05-09T00:00:00.000000Z-28743d0f"
)
DOGE_REPLAY_BUNDLE = (
    REPO_ROOT / "data" / "cal_mlp" / "DOGE"
    / "2026-05-09T00:00:00.000000Z-d24546e7"
)

NAMESPACE_PRODUCTION = "v1.1_production"
NAMESPACE_REPLAY = "replay_v1"

# The full 4-tuple production model architecture consumes (matches
# CalibrationMLP.forward signature in train.py).
ALL_CATEGORICAL_COLS = ("price_tier", "stc_bucket", "vol_regime_int", "side_int")
# Categoricals the replay parquet structurally has (the rest default
# to 0 at Phase4Dataset construction).
REPLAY_PRESENT_CATEGORICALS = ("stc_bucket", "side_int")
# Categoricals that are structurally absent from the replay parquet —
# pinned in anchor 3 (`test_replay_recipe_lists_only_present_categoricals`).
REPLAY_ABSENT_CATEGORICALS = ("price_tier", "vol_regime_int")


def _import_features():
    if str(CAL_MLP) not in sys.path:
        sys.path.insert(0, str(CAL_MLP))
    import features  # noqa: WPS433
    return features


def _import_train():
    """Lazy-import scripts/cal_mlp/train.py for Phase4Dataset access. The
    module top-level imports `torch` + `psutil`; tests skip when torch isn't
    available rather than failing collection."""
    pytest.importorskip("torch")
    pytest.importorskip("pandas")
    pytest.importorskip("numpy")
    if str(CAL_MLP) not in sys.path:
        sys.path.insert(0, str(CAL_MLP))
    import train as train_mod  # noqa: WPS433
    return train_mod


def _replay_fold_path(bundle: Path) -> Path | None:
    """Resolve a replay bundle's fold0 parquet path. Returns None if the
    bundle dir doesn't exist (Mac-local artifact, CI-friendly skip)."""
    if not bundle.exists():
        return None
    candidate = bundle / "fold0.parquet"
    if candidate.exists():
        return candidate
    return None


def _replay_extract_bundle(bundle: Path) -> dict | None:
    """Load the bundle's extract_bundle.json (carries recipe_namespace).
    Returns None if absent so tests self-skip on CI."""
    ext = bundle / "extract_bundle.json"
    if not ext.exists():
        return None
    return json.loads(ext.read_text())


# ─────────────────────────────────────────────────────────────────────
# Anchor 1: RecipeSpec exposes `categorical_feature_cols`
# ─────────────────────────────────────────────────────────────────────

def test_recipe_spec_has_categorical_feature_cols():
    """Anchor 1: every recipe MUST expose `categorical_feature_cols` as
    a tuple of categorical column names the bundle's parquet actually
    contains. Without this field, callers can't tell which of the 4
    `CalibrationMLP.forward` categorical inputs need a defaulting guard.

    Tuple (not list) so callers can't mutate it in place and silently
    drift the per-bundle invariant — mirrors `cont_feature_cols`
    contract introduced in fu1."""
    features = _import_features()
    for ns in (NAMESPACE_PRODUCTION, NAMESPACE_REPLAY):
        recipe = features.resolve_recipe(ns)
        assert hasattr(recipe, "categorical_feature_cols"), (
            f"RecipeSpec for {ns!r} missing `categorical_feature_cols` — "
            f"fu2 dispatch surface requires it. Without this, Phase4Dataset "
            f"can't tell which categoricals to default-to-zero per recipe."
        )
        cat_cols = recipe.categorical_feature_cols
        assert isinstance(cat_cols, tuple), (
            f"categorical_feature_cols for {ns!r} must be a tuple, got "
            f"{type(cat_cols).__name__}; lists are mutable and would drift "
            f"per-bundle invariant."
        )


# ─────────────────────────────────────────────────────────────────────
# Anchor 2: production recipe lists all 4 categoricals
# ─────────────────────────────────────────────────────────────────────

def test_production_recipe_lists_full_categorical_quartet():
    """Anchor 2: production recipe's `categorical_feature_cols` MUST be
    the full 4-tuple (price_tier, stc_bucket, vol_regime_int, side_int)
    — the production parquet (built by `extract_data.py`) digitizes all
    four columns. Missing any would silently default a present column
    to zeros and corrupt training of v1/v1.1 BTC/ETH/SOL/XRP bundles
    (REGRESSION).
    """
    features = _import_features()
    recipe = features.resolve_recipe(NAMESPACE_PRODUCTION)
    assert tuple(recipe.categorical_feature_cols) == ALL_CATEGORICAL_COLS, (
        f"production categorical_feature_cols drift: "
        f"{tuple(recipe.categorical_feature_cols)} vs {ALL_CATEGORICAL_COLS}. "
        f"Dropping any of these would default a present col to zeros — silent "
        f"categorical signal collapse on production training."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 3: replay recipe lists ONLY the 2 present categoricals
# ─────────────────────────────────────────────────────────────────────

def test_replay_recipe_lists_only_present_categoricals():
    """Anchor 3: replay recipe's `categorical_feature_cols` MUST list only
    the 2 categoricals the replay parquet actually has (stc_bucket,
    side_int). price_tier + vol_regime_int are structurally absent from
    `extract_data_replay.py` output and Phase4Dataset defaults them to 0.

    Including `price_tier` here would cause Phase4Dataset.__init__ to
    fall through to `df['price_tier']` and KeyError on the replay parquet
    — the exact gap fu2 closes.
    """
    features = _import_features()
    recipe = features.resolve_recipe(NAMESPACE_REPLAY)
    cat_cols = tuple(recipe.categorical_feature_cols)
    assert set(cat_cols) == set(REPLAY_PRESENT_CATEGORICALS), (
        f"replay categorical_feature_cols drift: {cat_cols} vs "
        f"{REPLAY_PRESENT_CATEGORICALS}. price_tier + vol_regime_int are "
        f"absent from the replay parquet by design — including them here "
        f"would silently re-introduce the KeyError fu2 closes."
    )
    # Belt-and-braces: the two absent cols MUST NOT be listed.
    for absent in REPLAY_ABSENT_CATEGORICALS:
        assert absent not in cat_cols, (
            f"replay categorical_feature_cols MUST NOT list {absent!r} — "
            f"the replay parquet doesn't contain that column. Listing it "
            f"would re-introduce the KeyError gap."
        )


# ─────────────────────────────────────────────────────────────────────
# Anchor 4: Phase4Dataset accepts `categorical_feature_cols`
# ─────────────────────────────────────────────────────────────────────

def test_phase4_dataset_accepts_categorical_feature_cols_kwarg():
    """Anchor 4: Phase4Dataset.__init__ MUST accept an explicit
    `categorical_feature_cols` keyword. Without it, train.py's per-recipe
    routing has no way to communicate the present-categorical subset.

    AST inspection — importing train.py at test time has heavy side
    effects (torch determinism flags, SIGALRM wiring).
    """
    if not TRAIN_PY.exists():
        pytest.skip("train.py not found")
    tree = ast.parse(TRAIN_PY.read_text())
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Phase4Dataset":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    found = item
                    break
    assert found is not None, "Phase4Dataset.__init__ not found in train.py"
    arg_names = {a.arg for a in (found.args.args + found.args.kwonlyargs)}
    assert "categorical_feature_cols" in arg_names, (
        f"Phase4Dataset.__init__ does NOT accept `categorical_feature_cols`; "
        f"got args={sorted(arg_names)}. Without this kwarg, replay bundles "
        f"cannot signal which categoricals to default-to-zero."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 5: Phase4Dataset defaults absent categoricals to 0 (smoke)
# ─────────────────────────────────────────────────────────────────────

def test_phase4_dataset_defaults_absent_categoricals_to_zero():
    """Anchor 5: when a categorical column is NOT in
    `categorical_feature_cols` AND NOT in the DataFrame, Phase4Dataset
    MUST default it to int64 zeros. Tested on a synthetic 4-row
    DataFrame that mirrors the replay parquet's column subset.

    This anchor catches a regression where Phase4Dataset reverts to
    `df['price_tier']` unconditionally (the fu2 gap).
    """
    import numpy as np
    import pandas as pd

    train_mod = _import_train()
    # Synthetic replay-shaped DataFrame. Mirrors the cont-feature
    # subset for replay_v1; categorical subset = (stc_bucket, side_int).
    df = pd.DataFrame({
        "spot_distance_to_strike_sigma": np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32),
        "abs_spot_distance_to_strike_sigma": np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32),
        "hour_sin": np.array([0.0, 0.5, 1.0, 0.5], dtype=np.float32),
        "hour_cos": np.array([1.0, 0.5, 0.0, -0.5], dtype=np.float32),
        "stc_bucket": np.array([0, 1, 2, 3], dtype=np.int64),
        "side_int": np.array([0, 1, 0, 1], dtype=np.int64),
        "ticker_id": np.array([1, 1, 2, 2], dtype=np.int64),
        "logit_raw_prob_clipped": np.array([-1.0, 0.0, 1.0, 0.5], dtype=np.float32),
        "outcome": np.array([0.0, 1.0, 0.0, 1.0], dtype=np.float32),
    })
    vocab = {"<UNK>": 0, "T1": 1, "T2": 2}
    cont_cols = [
        "spot_distance_to_strike_sigma", "abs_spot_distance_to_strike_sigma",
        "hour_sin", "hour_cos",
    ]
    ds = train_mod.Phase4Dataset(
        df,
        vocab,
        w_cell_lookup=np.zeros(16, dtype=np.float32),
        cont_feature_cols=cont_cols,
        missing_indicator_cols=[],
        categorical_feature_cols=("stc_bucket", "side_int"),
    )
    # _price + _vol must be zero-defaulted int64 arrays the same length
    # as the DataFrame.
    assert ds._price.shape == (4,)
    assert ds._vol.shape == (4,)
    assert (ds._price == 0).all(), (
        f"Phase4Dataset _price array must default to all-zeros when "
        f"`price_tier` is not in categorical_feature_cols AND not in df; "
        f"got {ds._price.tolist()}."
    )
    assert (ds._vol == 0).all(), (
        f"Phase4Dataset _vol array must default to all-zeros when "
        f"`vol_regime_int` is not in categorical_feature_cols AND not in "
        f"df; got {ds._vol.tolist()}."
    )
    # stc_bucket + side_int MUST come from the DataFrame (not defaulted).
    assert ds._stc.tolist() == [0, 1, 2, 3]
    assert ds._side.tolist() == [0, 1, 0, 1]


# ─────────────────────────────────────────────────────────────────────
# Anchor 6: production back-compat — Phase4Dataset reads df cols when
# categorical_feature_cols defaults to ALL_CATEGORICAL_COLS
# ─────────────────────────────────────────────────────────────────────

def test_phase4_dataset_production_back_compat():
    """Anchor 6 (REGRESSION GUARD): when `categorical_feature_cols` is
    omitted OR set to the full quartet, Phase4Dataset MUST read every
    categorical column from the DataFrame as before. Without this guard,
    a refactor could silently break BTC/ETH/SOL/XRP training by reading
    zeros even when the production parquet has non-zero values."""
    import numpy as np
    import pandas as pd

    train_mod = _import_train()
    df = pd.DataFrame({
        "spot_distance_to_strike_sigma": np.array([0.1, 0.2], dtype=np.float32),
        "abs_spot_distance_to_strike_sigma": np.array([0.1, 0.2], dtype=np.float32),
        "hour_sin": np.array([0.0, 1.0], dtype=np.float32),
        "hour_cos": np.array([1.0, 0.0], dtype=np.float32),
        # Production-only cont cols (apply_norm output presence; not
        # actually z-scored here — we only stress the categorical path).
        "market_price": np.array([0.1, 0.2], dtype=np.float32),
        "seconds_to_close": np.array([0.1, 0.2], dtype=np.float32),
        "time_decayed_proximity": np.array([0.1, 0.2], dtype=np.float32),
        "prob_breakeven_gap": np.array([0.1, 0.2], dtype=np.float32),
        # The 4-categorical quartet — all present, non-zero values.
        "price_tier": np.array([1, 3], dtype=np.int64),
        "stc_bucket": np.array([2, 0], dtype=np.int64),
        "vol_regime_int": np.array([1, 0], dtype=np.int64),
        "side_int": np.array([0, 1], dtype=np.int64),
        "ticker_id": np.array([1, 2], dtype=np.int64),
        "logit_raw_prob_clipped": np.array([-1.0, 1.0], dtype=np.float32),
        "outcome": np.array([0.0, 1.0], dtype=np.float32),
    })
    vocab = {"<UNK>": 0, "T1": 1, "T2": 2}
    cont_cols = list(_import_features().CONT_FEATURE_COLS)
    # Path A: explicit production categorical_feature_cols.
    ds = train_mod.Phase4Dataset(
        df, vocab,
        w_cell_lookup=np.zeros(16, dtype=np.float32),
        cont_feature_cols=cont_cols,
        missing_indicator_cols=[],
        categorical_feature_cols=ALL_CATEGORICAL_COLS,
    )
    assert ds._price.tolist() == [1, 3], (
        f"Phase4Dataset MUST read price_tier from the DataFrame for "
        f"production recipe; got {ds._price.tolist()}, expected [1,3]."
    )
    assert ds._vol.tolist() == [1, 0]
    assert ds._stc.tolist() == [2, 0]
    assert ds._side.tolist() == [0, 1]
    # Path B: omit categorical_feature_cols — back-compat default MUST
    # behave the same way (= all 4 cols, read from df).
    ds2 = train_mod.Phase4Dataset(
        df, vocab,
        w_cell_lookup=np.zeros(16, dtype=np.float32),
        cont_feature_cols=cont_cols,
        missing_indicator_cols=[],
    )
    assert ds2._price.tolist() == [1, 3]
    assert ds2._vol.tolist() == [1, 0]


# ─────────────────────────────────────────────────────────────────────
# Anchor 7: HYPE replay bundle Phase4Dataset construction succeeds
# (self-skipping integration test against the on-disk Mac-local bundle)
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bundle_path", [HYPE_REPLAY_BUNDLE, DOGE_REPLAY_BUNDLE])
def test_replay_bundle_phase4_dataset_construction_succeeds(bundle_path):
    """Anchor 7: positive integration test — Phase4Dataset constructed
    against an on-disk HYPE/DOGE replay fold0 parquet MUST succeed without
    KeyError. Self-skips when the bundle dir doesn't exist (Mac-local
    artifact, CI runs with empty data/ tree).

    This is the empirical pin for fu2's headline AC: `python -m
    scripts.cal_mlp.train --asset HYPE --extract-train-id ... ` runs to
    completion. The asymmetric path here (Phase4Dataset alone) catches
    the construction-time KeyError that blocked fu1 from claiming
    HYPE/DOGE were unblocked.
    """
    import numpy as np
    import pandas as pd

    fold_path = _replay_fold_path(bundle_path)
    ext = _replay_extract_bundle(bundle_path)
    if fold_path is None or ext is None:
        pytest.skip(
            f"replay bundle {bundle_path.name} not on disk (Mac-local "
            f"artifact); skipping integration anchor"
        )
    train_mod = _import_train()
    features = _import_features()
    # Recipe routing — the bundle stamps recipe_namespace='replay_v1'.
    ns = ext.get("recipe_namespace")
    recipe = features.resolve_recipe(ns)
    assert recipe.namespace == NAMESPACE_REPLAY, (
        f"bundle at {bundle_path} stamped recipe_namespace={ns!r}; expected "
        f"replay_v1. Anchor mis-targets if the bundle is production-namespace."
    )
    df = pd.read_parquet(fold_path, engine="pyarrow", dtype_backend="numpy_nullable")
    # Restrict to the cal split for the construction smoke (Phase4Dataset
    # is called on fold-split DataFrames in train.py:run).
    cal_df = df[df["split"] == "cal"].reset_index(drop=True)
    assert len(cal_df) > 0, "replay cal split is empty — unexpected"
    # Build vocab from ticker column.
    tickers = sorted(cal_df["ticker"].astype(str).unique())
    vocab = {"<UNK>": 0, **{t: i + 1 for i, t in enumerate(tickers)}}
    # The pre-apply_norm DataFrame may not have z-scored values yet, but
    # Phase4Dataset's __init__ requires finite values. Replay fold parquets
    # are pre-normalized in `extract_data_replay.py` so the cont columns
    # arrive z-scored — feed them through directly. We do NOT call
    # apply_norm() here to keep the test focused on the construction-time
    # KeyError that fu2 closes.
    cont_cols = list(recipe.cont_feature_cols)
    # The replay parquet stamps `hour_sin`/`hour_cos` raw (not z-scored)
    # per CONT_FEATURE_TRANSFORMS_REPLAY (identity_no_zscore); other cols
    # are z-scored in-bundle.
    ds = train_mod.Phase4Dataset(
        cal_df,
        vocab,
        w_cell_lookup=np.zeros(16, dtype=np.float32),
        cont_feature_cols=cont_cols,
        missing_indicator_cols=list(recipe.missing_indicator_cols),
        categorical_feature_cols=recipe.categorical_feature_cols,
    )
    # Phase4Dataset must populate all 4 categorical arrays — absent cols
    # default to zeros, present cols round-trip from the parquet.
    assert ds._price.shape == (len(cal_df),)
    assert ds._vol.shape == (len(cal_df),)
    assert ds._stc.shape == (len(cal_df),)
    assert ds._side.shape == (len(cal_df),)
    # Absent categoricals are all-zero (degenerate signal — by design).
    assert (ds._price == 0).all(), (
        f"replay bundle {bundle_path.name}: _price MUST be zeros (price_tier "
        f"absent from replay parquet); got non-zero values"
    )
    assert (ds._vol == 0).all(), (
        f"replay bundle {bundle_path.name}: _vol MUST be zeros "
        f"(vol_regime_int absent); got non-zero values"
    )
    # Present categoricals come from the parquet.
    assert (ds._stc >= 0).all() and (ds._stc <= 3).all(), (
        f"replay bundle {bundle_path.name}: _stc bucket values out of "
        f"[0,3] range; got {ds._stc.tolist()[:10]}..."
    )
    assert (ds._side >= 0).all() and (ds._side <= 1).all()


# ─────────────────────────────────────────────────────────────────────
# Anchor 8: preds_df concat in train.py:run handles missing categoricals
# ─────────────────────────────────────────────────────────────────────

def test_train_py_preds_df_concat_uses_recipe_categorical_dispatch():
    """Anchor 8 (R1 M1 strengthened): the preds_df concat in train.py's
    `run()` MUST NOT contain literal `ca['price_tier']` / `te['price_tier']`
    (or `ca['vol_regime_int']` / `te['vol_regime_int']`) subscript reads.
    Those readouts ARE the gap fu2 closes — keeping them via the
    `pd.concat([ca[col], te[col]], ...)` shape would silently KeyError
    on replay fold DataFrames AFTER Phase4Dataset succeeds (which now
    routes through the helper) → an asymmetric break right before the
    Phase 5 bundle is written.

    AST-walk catches the regression even if the helper `_read_categorical
    _or_default` is reverted but the `categorical_feature_cols=` kwarg
    surface is retained (R1 M1's exact regression pattern).
    """
    if not TRAIN_PY.exists():
        pytest.skip("train.py not found")
    tree = ast.parse(TRAIN_PY.read_text())
    # Locate the `run` function and inspect its body for any
    # ast.Subscript(value=ast.Name('ca'|'te'), slice='price_tier'|...).
    forbidden_cols = {"price_tier", "vol_regime_int"}
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            varname = node.value.id
            if varname not in {"ca", "te"}:
                continue
            slc = node.slice
            # Py 3.9+: ast.Subscript.slice is the index expression directly.
            if isinstance(slc, ast.Constant) and slc.value in forbidden_cols:
                offending.append(f"{varname}[{slc.value!r}] @ line {node.lineno}")
    forbidden_str = ", ".join(sorted(forbidden_cols))
    assert not offending, (
        f"train.py contains literal `ca['<{forbidden_str}>']` / "
        f"`te['<{forbidden_str}>']` subscript reads — these KeyError on "
        f"replay fold DataFrames. Route through "
        f"`_read_categorical_or_default(...)` per fu2 Option A wiring. "
        f"Found: {offending}"
    )
    # Belt-and-braces: token-level fallback (catches a helper rename + Subscript-bypass).
    src = TRAIN_PY.read_text()
    assert "categorical_feature_cols" in src, (
        "train.py does not reference recipe.categorical_feature_cols — "
        "fu2 dispatch surface requires categorical-col routing through "
        "the recipe."
    )


def test_phase4_dataset_listed_but_absent_categorical_raises():
    """Anchor 10 (R1 MN3): when a categorical col IS in the recipe's
    `categorical_feature_cols` allow-list but ABSENT from the DataFrame,
    Phase4Dataset MUST raise KeyError (NOT silently default to zeros).

    Producer/consumer recipe drift is a recipe bug; silent default-to-zero
    would mask it and corrupt training with a constant categorical signal
    even on production data. The error message MUST mention
    'producer/consumer recipe mismatch' so operators can debug.
    """
    import numpy as np
    import pandas as pd

    train_mod = _import_train()
    # Synthetic production-shaped DataFrame MISSING the price_tier col.
    df = pd.DataFrame({
        "spot_distance_to_strike_sigma": np.array([0.1, 0.2], dtype=np.float32),
        "abs_spot_distance_to_strike_sigma": np.array([0.1, 0.2], dtype=np.float32),
        "hour_sin": np.array([0.0, 1.0], dtype=np.float32),
        "hour_cos": np.array([1.0, 0.0], dtype=np.float32),
        "market_price": np.array([0.1, 0.2], dtype=np.float32),
        "seconds_to_close": np.array([0.1, 0.2], dtype=np.float32),
        "time_decayed_proximity": np.array([0.1, 0.2], dtype=np.float32),
        "prob_breakeven_gap": np.array([0.1, 0.2], dtype=np.float32),
        # price_tier intentionally absent ↓
        "stc_bucket": np.array([0, 1], dtype=np.int64),
        "vol_regime_int": np.array([0, 1], dtype=np.int64),
        "side_int": np.array([0, 1], dtype=np.int64),
        "ticker_id": np.array([1, 2], dtype=np.int64),
        "logit_raw_prob_clipped": np.array([-1.0, 1.0], dtype=np.float32),
        "outcome": np.array([0.0, 1.0], dtype=np.float32),
    })
    vocab = {"<UNK>": 0, "T1": 1, "T2": 2}
    cont_cols = list(_import_features().CONT_FEATURE_COLS)
    with pytest.raises(KeyError, match=r"producer/consumer recipe mismatch"):
        train_mod.Phase4Dataset(
            df, vocab,
            w_cell_lookup=np.zeros(16, dtype=np.float32),
            cont_feature_cols=cont_cols,
            missing_indicator_cols=[],
            # price_tier listed in allow-list but absent from df → must raise.
            categorical_feature_cols=ALL_CATEGORICAL_COLS,
        )


def test_replay_extract_pinning_side_int_constant_yes():
    """Anchor 11 (R1 M3 sibling pin): the replay backfill convention is
    `side_int=1` (YES side) for every replay row — hard-coded at
    `extract_data_replay.py::build_feature_frame_replay` line ~528:

        df['side_int'] = np.int8(1)   # always YES side in replay

    The model's categorical surface for replay therefore has `side_int`
    as a constant (degenerate one-hot `[0,1]` on every row) even though
    the recipe lists `side_int` in `categorical_feature_cols`. This pin
    catches a future replay-extract change that flips to a YES/NO mix
    without updating the recipe docstring / fu3 follow-up.

    AST-level (not import): file-level constant scan.
    """
    extract_data_replay = CAL_MLP / "extract_data_replay.py"
    if not extract_data_replay.exists():
        pytest.skip("extract_data_replay.py not found (Mac-local artifact)")
    src = extract_data_replay.read_text()
    # The literal that establishes the YES invariant.
    assert "df['side_int'] = np.int8(1)" in src or 'df["side_int"] = np.int8(1)' in src, (
        "extract_data_replay.py no longer stamps `side_int = np.int8(1)` "
        "(the YES-side replay invariant). If the convention has changed "
        "to a mixed YES/NO replay corpus, update the recipe docstring at "
        "features.py:resolve_recipe(REPLAY) categorical_feature_cols "
        "block + escalate to fu3 (proxy derivation Option B)."
    )


def test_validate_py_forwards_categorical_feature_cols_to_calibration_dataset():
    """Anchor 12 (R1 C1): validate.py's `CalibrationDataset(...)`
    construction in `main()` (lines ~622-626) MUST forward
    `categorical_feature_cols=recipe.categorical_feature_cols`. Without it
    the default `_ALL_CATEGORICAL_COLS` triggers the listed-but-absent
    KeyError when the replay test_df lacks `price_tier` /
    `vol_regime_int` — partially blocks P2.1.c HYPE/DOGE Brier even though
    P2.1.b training itself succeeds.

    AST-level inspection: strict literal match on the canonical
    recipe-routed spelling `categorical_feature_cols=recipe.categorical_feature_cols`.
    A refactor that renames `recipe` (e.g., to `_recipe`) or moves the
    construction site WILL trip this guard — that's intentional;
    re-pinning the assertion is a sister-doc step in the refactor's own
    Bit. R2 MN3 drift fix.
    """
    if not VALIDATE_PY.exists():
        pytest.skip("validate.py not found")
    src = VALIDATE_PY.read_text()
    assert "categorical_feature_cols=recipe.categorical_feature_cols" in src, (
        "validate.py does NOT pass `categorical_feature_cols="
        "recipe.categorical_feature_cols` to CalibrationDataset(). Without "
        "it the default falls through to ALL 4 categoricals and "
        "_read_categorical_or_default raises KeyError when the replay "
        "test_df lacks `price_tier`/`vol_regime_int`. P2.1.c HYPE/DOGE "
        "validate cannot run until this is wired."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 9: production parquet path remains bit-identical
# ─────────────────────────────────────────────────────────────────────

def test_phase4_dataset_production_parquet_round_trip_smoke():
    """Anchor 9 (REGRESSION): for production-recipe DataFrames that DO
    contain all 4 categorical columns, Phase4Dataset MUST produce
    bit-identical arrays whether `categorical_feature_cols` is set to
    the full 4-tuple OR omitted (back-compat default).

    Catches a refactor where the default branch silently zeros a
    column even when the DataFrame has the value — would corrupt
    BTC/ETH/SOL/XRP training.
    """
    import numpy as np
    import pandas as pd

    train_mod = _import_train()
    features = _import_features()
    rng = np.random.default_rng(42)
    n = 8
    df = pd.DataFrame({
        col: rng.standard_normal(n).astype(np.float32)
        for col in features.CONT_FEATURE_COLS
    })
    df["price_tier"] = np.array([0, 1, 2, 3, 0, 1, 2, 3], dtype=np.int64)
    df["stc_bucket"] = np.array([3, 2, 1, 0, 3, 2, 1, 0], dtype=np.int64)
    df["vol_regime_int"] = np.array([0, 1, 0, 1, 0, 1, 0, 1], dtype=np.int64)
    df["side_int"] = np.array([0, 0, 1, 1, 0, 0, 1, 1], dtype=np.int64)
    df["ticker_id"] = np.array([1, 1, 1, 1, 1, 1, 1, 1], dtype=np.int64)
    df["logit_raw_prob_clipped"] = np.zeros(n, dtype=np.float32)
    df["outcome"] = np.array([0, 1, 0, 1, 0, 1, 0, 1], dtype=np.float32)
    vocab = {"<UNK>": 0, "T1": 1}
    cont_cols = list(features.CONT_FEATURE_COLS)
    # With explicit production quartet.
    ds_explicit = train_mod.Phase4Dataset(
        df, vocab,
        w_cell_lookup=np.zeros(16, dtype=np.float32),
        cont_feature_cols=cont_cols,
        missing_indicator_cols=[],
        categorical_feature_cols=ALL_CATEGORICAL_COLS,
    )
    # Without (back-compat default).
    ds_default = train_mod.Phase4Dataset(
        df, vocab,
        w_cell_lookup=np.zeros(16, dtype=np.float32),
        cont_feature_cols=cont_cols,
        missing_indicator_cols=[],
    )
    np_test = np.testing
    np_test.assert_array_equal(ds_explicit._price, ds_default._price)
    np_test.assert_array_equal(ds_explicit._stc, ds_default._stc)
    np_test.assert_array_equal(ds_explicit._vol, ds_default._vol)
    np_test.assert_array_equal(ds_explicit._side, ds_default._side)
    # And both must round-trip the DataFrame values exactly.
    np_test.assert_array_equal(ds_explicit._price, df["price_tier"].to_numpy(np.int64))
    np_test.assert_array_equal(ds_explicit._stc, df["stc_bucket"].to_numpy(np.int64))
