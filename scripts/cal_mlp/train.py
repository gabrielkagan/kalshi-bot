#!/usr/bin/env python3
"""
P2 Phase 4: per-asset M=5 ensemble training.

Implements `kb-research/bot/p2-phase4-training.md` (converged at R4) with:
- Phase 3 architecture (skip-term residual MLP, EMB_DIM=4, 64→32 hidden,
  GELU+LayerNorm+Dropout 0.1, Δlogit clamp ±2.5)
- BCE × w_cell loss with calibration-residual weighting + n-precision factor
- M=5 seed-ensemble with deterministic per-member seeds
- Walk-forward 3-fold; deploy fold = K-1 (per Phase 3 R1#C8)
- Atomic 3-step write: per-fold tmps → rename in batch → CURRENT pointer
- Marker-based resume (per-checkpoint verification of cfg_fp/logical_sha)
- bundle_sha_v1 chain: phase4 = sha256(model_id:normstats_concat:phase4)
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import random
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# Determinism env vars MUST be set before `import torch` per Phase 3 R1#C6.
os.environ.setdefault('CUBLAS_WORKSPACE_CONFIG', ':4096:8')

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

torch.use_deterministic_algorithms(True, warn_only=False)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

sys.path.insert(0, str(Path(__file__).parent))

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from features import (  # noqa: E402
    CONT_FEATURE_COLS,
    CONT_FEATURE_TRANSFORMS,
    MISSING_INDICATOR_COLS,
    RAW_PROB_CLIP_EPS,
    resolve_recipe,
)
from normalize import apply_norm  # noqa: E402

# R2#C7: FORWARD_KEYS imported from _helpers (single source of truth).
from _helpers import FORWARD_KEYS  # noqa: E402

N_MISSING = len(MISSING_INDICATOR_COLS)
N_CONT = len(CONT_FEATURE_COLS)


# ---------------------------------------------------------------------------
# Exit-code hierarchy
# ---------------------------------------------------------------------------

class Phase4Error(RuntimeError):
    exit_code: int = 1


class Phase4DBError(Phase4Error):
    exit_code = 2


class Phase4ContractError(Phase4Error):
    exit_code = 3


class Phase4LockError(Phase4Error):
    exit_code = 4


class Phase4WriteError(Phase4Error):
    exit_code = 5


class Phase4SchemaError(Phase4Error):
    exit_code = 6


class Phase4ResourceError(Phase4Error):
    exit_code = 7


# ---------------------------------------------------------------------------
# Model architecture (Phase 3 lock)
# ---------------------------------------------------------------------------

DEFAULT_DROPOUT = 0.1
DELTA_LOGIT_CLAMP = 2.5
EMB_DIM = 4
HIDDEN_1 = 64
HIDDEN_2 = 32


class CalibrationMLP(nn.Module):
    """Per Phase 3: input is x_cont(28) + 4 categoricals one-hot (12) +
    ticker embedding (EMB_DIM=4) → 44. Two GELU layers (64, 32). Output
    Δ in logit space, clamped ±2.5. Final prob = sigmoid(skip + Δ)."""

    def __init__(self, n_vocab: int, n_cont: int = N_CONT, n_missing: int = N_MISSING,
                 emb_dim: int = EMB_DIM, hidden_1: int = HIDDEN_1,
                 hidden_2: int = HIDDEN_2, dropout: float = DEFAULT_DROPOUT):
        super().__init__()
        self.n_cont = n_cont
        self.n_missing = n_missing
        self.emb = nn.Embedding(n_vocab, emb_dim)
        nn.init.normal_(self.emb.weight, mean=0.0, std=emb_dim ** -0.5)
        input_dim = n_cont + n_missing + 4 + 4 + 2 + 2 + emb_dim
        self.norm0 = nn.LayerNorm(input_dim)
        self.fc1 = nn.Linear(input_dim, hidden_1)
        self.act1 = nn.GELU()
        self.drop1 = nn.Dropout(dropout)
        self.norm1 = nn.LayerNorm(hidden_1)
        self.fc2 = nn.Linear(hidden_1, hidden_2)
        self.act2 = nn.GELU()
        self.drop2 = nn.Dropout(dropout)
        self.head = nn.Linear(hidden_2, 1)
        # Initialize the head with small weights so Δ ≈ 0 at start (residual).
        nn.init.zeros_(self.head.weight)
        nn.init.zeros_(self.head.bias)

    def init_unk_embedding(self, member_seed: int) -> None:
        """Per Phase 3 R1#C8 — UNK row 0 random per-member for ensemble disagreement.
        R1#C1: copy onto the parameter's device to avoid CUDA mismatch."""
        rng = np.random.default_rng(member_seed)
        with torch.no_grad():
            cpu_init = torch.from_numpy(
                rng.normal(0.0, EMB_DIM ** -0.5, size=EMB_DIM).astype(np.float32)
            )
            self.emb.weight.data[0].copy_(cpu_init.to(self.emb.weight.device))

    def forward(self, x_cont, x_missing, price_tier, stc_bucket,
                vol_regime_int, side_int, ticker_id, logit_raw_prob_clipped):
        # x_cont, x_missing: float [B, N]; categoricals: long [B]
        oh_price = F.one_hot(price_tier, num_classes=4).float()
        oh_stc = F.one_hot(stc_bucket, num_classes=4).float()
        oh_vol = F.one_hot(vol_regime_int, num_classes=2).float()
        oh_side = F.one_hot(side_int, num_classes=2).float()
        emb_t = self.emb(ticker_id)
        x_cont_full = torch.cat([x_cont, x_missing], dim=-1)
        h = torch.cat([x_cont_full, oh_price, oh_stc, oh_vol, oh_side, emb_t], dim=-1)
        h = self.norm0(h)
        h = self.act1(self.fc1(h))
        h = self.drop1(h)
        h = self.norm1(h)
        h = self.act2(self.fc2(h))
        h = self.drop2(h)
        delta_raw = self.head(h).squeeze(-1)
        delta = torch.clamp(delta_raw, -DELTA_LOGIT_CLAMP, DELTA_LOGIT_CLAMP)
        final_logit = logit_raw_prob_clipped + delta
        final_prob = torch.sigmoid(final_logit)
        return final_logit, final_prob


def build_model_from_definition(model_def: dict, n_vocab: int) -> CalibrationMLP:
    """Construct CalibrationMLP from a model_definition.json. Phase 7 imports."""
    return CalibrationMLP(
        n_vocab=n_vocab,
        n_cont=model_def.get('n_cont', N_CONT),
        n_missing=model_def.get('n_missing_indicator_cols', N_MISSING),
        emb_dim=model_def.get('emb_dim', EMB_DIM),
        hidden_1=model_def.get('hidden_1', HIDDEN_1),
        hidden_2=model_def.get('hidden_2', HIDDEN_2),
        dropout=model_def.get('dropout', DEFAULT_DROPOUT),
    )


# ---------------------------------------------------------------------------
# Phase4Dataset (per Phase 4 R3#C3)
# ---------------------------------------------------------------------------

# The full 4-tuple `CalibrationMLP.forward` consumes for one-hot encoding
# (see model architecture above: price_tier=4 classes, stc_bucket=4,
# vol_regime_int=2, side_int=2). Production recipe parquets contain all
# four columns; replay recipe parquets contain only `stc_bucket` +
# `side_int`. See `features.RecipeSpec.categorical_feature_cols` for the
# per-recipe subset declaration (P2.1.a-3-fu2, ClickUp 86b9xd9hn).
_ALL_CATEGORICAL_COLS: tuple = (
    'price_tier', 'stc_bucket', 'vol_regime_int', 'side_int',
)


def _read_categorical_or_default(
    df: pd.DataFrame,
    col: str,
    categorical_feature_cols: tuple,
) -> np.ndarray:
    """Read `col` from `df` as int64, OR return a same-length zero array
    when `col` is not listed in the recipe's `categorical_feature_cols`.

    Centralized helper so Phase4Dataset.__init__ and the preds_df concat
    in train.py:run() apply identical fallback semantics — without this,
    one site reading `df[col]` while the other reads zeros would silently
    drift preds_df from Phase4Dataset's view of the same categorical
    surface.

    Defaulting to zero (rather than raising) is the Option A design
    choice (operator-confirmed 2026-05-13). Replay rows fall in the
    `(price_tier=0, vol_regime_int=0)` cell — `CalibrationMLP.forward`'s
    one-hot collapses to a constant `[1,0,0,0]`/`[1,0]` for those two
    axes. A degenerate categorical signal on the absent axes by design
    (cfg_fp_replay stays stable; no parquet re-extract). See ClickUp
    86b9xd9hn ticket body for the Option B (proxy derivation) escalation
    path if HYPE/DOGE Brier/ECE turn out to need real categorical signal.

    Belt-and-braces: even when `col` IS in `categorical_feature_cols` but
    the DataFrame is missing the column (a producer/consumer mismatch),
    we raise rather than silently defaulting — that would mask a recipe
    bug. The default-to-zero path requires the recipe to explicitly
    DECLARE the col absent.
    """
    if col in categorical_feature_cols:
        if col not in df.columns:
            raise KeyError(
                f"Phase4Dataset categorical column {col!r} listed in "
                f"recipe.categorical_feature_cols but missing from DataFrame; "
                f"producer/consumer recipe mismatch."
            )
        return df[col].to_numpy(np.int64)
    return np.zeros(len(df), dtype=np.int64)


class Phase4Dataset(Dataset):
    """Wraps a normalized fold DataFrame + ticker vocab + per-cell weights.
    __getitem__ returns dict with FORWARD_KEYS + 'outcome' + 'w_cell'."""

    def __init__(self, df: pd.DataFrame, vocab: dict,
                 w_cell_lookup: Optional[np.ndarray] = None,
                 cont_feature_cols: Optional[list] = None,
                 missing_indicator_cols: Optional[list] = None,
                 categorical_feature_cols: Optional[tuple] = None):
        """R3#C1: w_cell_lookup is optional for inference-only callers.
        When None, defaults to zeros[16] (no per-cell weighting at inference).

        cont_feature_cols / missing_indicator_cols: per-recipe overrides
        (P2.1.a-3-fu1, ClickUp 86b9xbd2u). Both default to module-globals
        for back-compat with v1-production callers. Replay-namespace
        callers route through `features.resolve_recipe('replay_v1')` and
        pass `recipe.cont_feature_cols` / `recipe.missing_indicator_cols`
        explicitly so Phase4Dataset slices the right column subset.

        categorical_feature_cols: per-recipe tuple of categorical column
        names the bundle's parquet ACTUALLY contains (P2.1.a-3-fu2,
        ClickUp 86b9xd9hn). Subset of `_ALL_CATEGORICAL_COLS`; absent
        entries default to int64 zeros at construction (does NOT touch
        the parquet on disk → no cfg_fp_replay drift). Default-None
        resolves to the full 4-tuple — back-compat with production callers
        that have always passed all four cols through the DataFrame."""
        if w_cell_lookup is None:
            w_cell_lookup = np.zeros(16, dtype=np.float32)
        cont_cols = list(cont_feature_cols) if cont_feature_cols is not None else list(CONT_FEATURE_COLS)
        missing_cols = (
            list(missing_indicator_cols) if missing_indicator_cols is not None
            else list(MISSING_INDICATOR_COLS)
        )
        cat_cols = (
            tuple(categorical_feature_cols) if categorical_feature_cols is not None
            else _ALL_CATEGORICAL_COLS
        )
        self.cont_feature_cols = cont_cols
        self.missing_indicator_cols = missing_cols
        self.categorical_feature_cols = cat_cols
        self.df = df.reset_index(drop=True)
        self.vocab = vocab
        self.w_cell_lookup = torch.from_numpy(w_cell_lookup.astype(np.float32))
        self._cont_arr = self.df[cont_cols].to_numpy(np.float32)
        self._missing_arr = self.df[missing_cols].to_numpy(np.float32)
        # P2.1.a-3-fu2 (86b9xd9hn): default-zero any categorical NOT
        # listed in `categorical_feature_cols`. Replay parquets lack
        # `price_tier` + `vol_regime_int` columns; defaulting at the
        # construction site keeps cfg_fp_replay stable (no re-extract).
        self._price = _read_categorical_or_default(self.df, 'price_tier', cat_cols)
        self._stc = _read_categorical_or_default(self.df, 'stc_bucket', cat_cols)
        self._vol = _read_categorical_or_default(self.df, 'vol_regime_int', cat_cols)
        self._side = _read_categorical_or_default(self.df, 'side_int', cat_cols)
        self._tid = self.df['ticker_id'].to_numpy(np.int64)
        self._logit_raw = self.df['logit_raw_prob_clipped'].to_numpy(np.float32)
        self._outcome = self.df['outcome'].to_numpy(np.float32)
        # R-p4-r5#H5: a constant-valued feature in a fold gives std=0 in
        # apply_norm → NaN/inf in z-score → poisons gradients silently.
        if not np.isfinite(self._cont_arr).all():
            bad = [c for c in cont_cols
                   if not np.isfinite(self.df[c].to_numpy(np.float32)).all()]
            raise ValueError(
                f"Phase4Dataset: non-finite values in cont_feature_cols {bad} "
                f"after apply_norm — likely zero-variance feature in this fold"
            )
        if not np.isfinite(self._logit_raw).all():
            raise ValueError(
                "Phase4Dataset: non-finite logit_raw_prob_clipped — extract "
                "RAW_PROB_CLIP_EPS guard violated"
            )

    def __len__(self) -> int:
        return len(self.df)

    def __getitem__(self, i: int) -> dict:
        return {
            'x_cont': torch.from_numpy(self._cont_arr[i]),
            'x_missing': torch.from_numpy(self._missing_arr[i]),
            'price_tier': torch.tensor(self._price[i], dtype=torch.long),
            'stc_bucket': torch.tensor(self._stc[i], dtype=torch.long),
            'vol_regime_int': torch.tensor(self._vol[i], dtype=torch.long),
            'side_int': torch.tensor(self._side[i], dtype=torch.long),
            'ticker_id': torch.tensor(self._tid[i], dtype=torch.long),
            'logit_raw_prob_clipped': torch.tensor(self._logit_raw[i], dtype=torch.float32),
            'outcome': torch.tensor(self._outcome[i], dtype=torch.float32),
            'w_cell': self.w_cell_lookup[self._price[i] * 4 + self._stc[i]],
        }


# R2#C1 + R3 reverify: backward-compat shim for Phase 6 inference-only use.
# Phase 6 (sim_pnl.py / validate.py) was rebuilt before Phase 4 redesigned
# the Dataset constructor. Phase 6 only needs INFERENCE batches (no w_cell).

class CalibrationDataset(Phase4Dataset):
    """Inference-only wrapper supporting Phase 6's old constructor signature
    `CalibrationDataset(df, cont_cols, ticker_to_id_or_vocab)`. Internally
    maps to Phase4Dataset with a zero w_cell_lookup (unused at inference).

    P2.1.a-3-fu1 (86b9xbd2u): when callers pass a recipe-derived
    `cont_cols` list (e.g., replay_v1's 4-feature recipe), forward it
    through to Phase4Dataset so inference slices the same column subset
    the bundle was trained on. Pre-fu1 the `cont_cols` arg was inert.

    P2.1.a-3-fu2 (86b9xd9hn): `categorical_feature_cols` is now forwarded
    too — replay-namespace inference DataFrames lack `price_tier` +
    `vol_regime_int`. Without forwarding, validate.py's `CalibrationDataset`
    construction against a replay test_df would KeyError mid-loop."""
    def __init__(self, df: pd.DataFrame, cont_cols=None, ticker_to_id_or_vocab=None,
                 missing_indicator_cols=None,
                 categorical_feature_cols=None):
        # Detect old signature: 3-positional args from Phase 6 call sites.
        if isinstance(ticker_to_id_or_vocab, dict):
            vocab = ticker_to_id_or_vocab
        else:
            # Fallback: treat as vocab-less mapping; build identity from df.
            unique = sorted(df['ticker'].astype(str).unique())
            vocab = {'<UNK>': 0, **{t: i + 1 for i, t in enumerate(unique)}}
        # Phase 6 doesn't need per-cell weights at inference; pass zeros.
        w_cell_zeros = np.zeros(16, dtype=np.float32)
        # Ensure ticker_id column is present for Phase4Dataset's __getitem__.
        df = df.copy()
        if 'ticker_id' not in df.columns:
            df['ticker_id'] = df['ticker'].astype(str).map(vocab).fillna(0).astype(np.int64)
        super().__init__(
            df, vocab, w_cell_zeros,
            cont_feature_cols=cont_cols,
            missing_indicator_cols=missing_indicator_cols,
            categorical_feature_cols=categorical_feature_cols,
        )


def collate_dict(batch_list: list) -> dict:
    """Default-collate equivalent for Phase4Dataset's __getitem__ output."""
    if not batch_list:
        return {}
    out = {}
    for k in batch_list[0].keys():
        out[k] = torch.stack([b[k] for b in batch_list])
    return out


def predict_p_out(model: nn.Module, dataset) -> np.ndarray:
    """Alias for predict_p — Phase 6 imports this name."""
    return predict_p(model, dataset)


def compute_weighted_bce(model: nn.Module, batch: dict) -> torch.Tensor:
    """Forward + weighted BCE. Dataset attached per-row w_cell."""
    final_logit, _ = model(**{k: batch[k] for k in FORWARD_KEYS})
    bce = F.binary_cross_entropy_with_logits(final_logit, batch['outcome'], reduction='none')
    return (bce * batch['w_cell']).mean()


def compute_cal_brier_weighted(model: nn.Module, ca_dataset: Dataset) -> float:
    """Per-cell weighted Brier on cal split using train-fold w_cell.
    R-p4-r5#H2: denominator is sum-of-weights, not unweighted count, so the
    metric is a true weighted mean (consistent across folds with differing
    w_cell distributions)."""
    model.eval()
    with torch.no_grad():
        loader = DataLoader(ca_dataset, batch_size=512, shuffle=False, num_workers=0)
        total = 0.0
        wsum = 0.0
        for batch in loader:
            _, p = model(**{k: batch[k] for k in FORWARD_KEYS})
            err = (p - batch['outcome']) ** 2
            total += float((err * batch['w_cell']).sum())
            wsum += float(batch['w_cell'].sum())
    model.train()
    return total / max(1e-12, wsum)


@torch.no_grad()
def predict_p(model: nn.Module, dataset: Dataset) -> np.ndarray:
    model.eval()
    loader = DataLoader(dataset, batch_size=2048, shuffle=False, num_workers=0)
    out = []
    for batch in loader:
        _, p = model(**{k: batch[k] for k in FORWARD_KEYS})
        out.append(p.detach().cpu().numpy())
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


# ---------------------------------------------------------------------------
# w_cell precomputation
# ---------------------------------------------------------------------------

N_TRAIN_PER_CELL_FLOOR = 50


def compute_w_cell_lookup(per_cell: dict) -> np.ndarray:
    """Build a 16-cell lookup: w_cell = 1 + 4 * |p_cell - prior_cell| * min(n/50, 1).
    Empty cells get 1.0 (no NaN)."""
    n_arr = np.zeros(16, dtype=np.float64)
    p_arr = np.zeros(16, dtype=np.float64)
    prior_arr = np.zeros(16, dtype=np.float64)
    for key, cell in per_cell.items():
        # key is "(price_tier,stc_bucket)" string; parse via regex
        import re
        m = re.fullmatch(r'\((\d+),(\d+)\)', key)
        if not m:
            continue
        pt, sb = int(m.group(1)), int(m.group(2))
        idx = pt * 4 + sb
        n_arr[idx] = cell.get('n_train', 0)
        p_arr[idx] = cell.get('train_positive_rate') or 0.0
        prior_arr[idx] = cell.get('train_mean_method_output') or 0.0
    p_safe = np.nan_to_num(p_arr, nan=0.0)
    prior_safe = np.nan_to_num(prior_arr, nan=0.0)
    miscal = np.where(n_arr > 0, np.abs(p_safe - prior_safe), 0.0)
    precision = np.minimum(n_arr / N_TRAIN_PER_CELL_FLOOR, 1.0)
    return 1.0 + 4.0 * miscal * precision


# ---------------------------------------------------------------------------
# Logical sha (R-p4-spec-r3#C1)
# ---------------------------------------------------------------------------

# R1#C5: compute_extract_logical_sha lives in _helpers.py; imported above.
from _helpers import compute_extract_logical_sha  # noqa: E402


# ---------------------------------------------------------------------------
# Lock helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def acquire_lock(lock_path: Path, mode: int):
    """flock with NB; raises Phase4LockError on contention."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise Phase4LockError(f"failed to open lock {lock_path}: {e}") from e
    handle = os.fdopen(fd, 'r+')
    try:
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise Phase4LockError(f"lock {lock_path} held by another process")
        yield handle
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------

def write_json_tmp(payload: dict, final_path: Path) -> Path:
    """Write to tmp + fsync; return tmp path. Caller does the rename later."""
    tmp = final_path.with_suffix(
        final_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    raw = json.dumps(payload, indent=2, sort_keys=True, default=str).encode()
    with open(tmp, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def write_torch_tmp(state_dict: dict, final_path: Path) -> Path:
    tmp = final_path.with_suffix(
        final_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    with open(tmp, 'wb') as f:
        torch.save(state_dict, f)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def write_parquet_tmp(table: pa.Table, final_path: Path) -> Path:
    tmp = final_path.with_suffix(
        final_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    with open(tmp, 'wb') as f:
        pq.write_table(table, f)
        f.flush()
        os.fsync(f.fileno())
    return tmp


# R1#C5/C8: fsync_directory + sha256_file moved to _helpers.py.
from _helpers import fsync_directory, sha256_file  # noqa: E402, F811


def _safe_relative(p: Path, root: Path) -> str:
    """R1#C9: relative_to() raises ValueError if p is outside root.
    Wrap to fall back to absolute string with a logged warning."""
    try:
        return str(p.resolve().relative_to(root))
    except ValueError:
        logging.warning("[train] %s is outside project_root %s; storing absolute path", p, root)
        return str(p.resolve())


# ---------------------------------------------------------------------------
# LR schedule (warmup + cosine)
# ---------------------------------------------------------------------------

def build_warmup_then_cosine(opt: torch.optim.Optimizer, warmup_steps: int,
                              total_steps: int, eta_min: float = 1e-4):
    base_lrs = [pg['lr'] for pg in opt.param_groups]

    def lr_lambda(step: int) -> float:
        if step < warmup_steps:
            return float(step + 1) / max(1, warmup_steps)
        progress = (step - warmup_steps) / max(1, total_steps - warmup_steps)
        progress = min(max(progress, 0.0), 1.0)
        cos = 0.5 * (1.0 + np.cos(np.pi * progress))
        # Map cos in [0, 1] to LR in [eta_min, base_lr].
        return float(eta_min / base_lrs[0] + (1 - eta_min / base_lrs[0]) * cos)

    return torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda)


# ---------------------------------------------------------------------------
# Resume marker check
# ---------------------------------------------------------------------------

def marker_matches(marker_path: Path, expected: dict) -> bool:
    """R-p4-r7#MED1: iterate `expected.keys()` (NOT a hardcoded whitelist) so
    new fields like `metric_version` actually gate resume. The previous
    whitelist made the R6 metric_version=2 field inert — pre-R6 markers
    would still match because the whitelist excluded it."""
    if not marker_path.exists():
        return False
    try:
        m = json.load(open(marker_path))
    except (OSError, json.JSONDecodeError):
        return False
    return all(m.get(k) == expected.get(k) for k in expected.keys())


# ---------------------------------------------------------------------------
# Main training entrypoint
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Phase 4 cal_mlp training")
    ap.add_argument(
        '--asset', required=True,
        choices=['BTC', 'ETH', 'SOL', 'XRP', 'HYPE', 'DOGE'],
    )
    ap.add_argument('--extract-train-id', default=None)
    ap.add_argument('--folds-to-train', type=str, default=None,
                    help='comma-separated fold indices; default = all')
    ap.add_argument('--ensemble-size', type=int, default=5)
    ap.add_argument('--base-seed', type=int, default=42)
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--n-train-min', type=int, default=2000)
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--allow-resume', action='store_true')
    ap.add_argument('--epochs', type=int, default=30)
    ap.add_argument('--batch-size', type=int, default=256)
    ap.add_argument('--lr', type=float, default=1e-3)
    ap.add_argument('--wall-ceiling-s', type=int, default=3600)
    return ap.parse_args()


def _setup_logging(quiet: bool, verbose: bool) -> None:
    level = logging.WARN if quiet else (logging.DEBUG if verbose else logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
        stream=sys.stderr,
        force=True,
    )


def _setup_walltime_alarm(seconds: int) -> None:
    def _handler(signum, frame):
        raise Phase4ResourceError(f"wall-time {seconds}s exceeded (SIGALRM)")
    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)


def _disarm_walltime_alarm() -> None:
    """R-p4-r5#H6: cancel SIGALRM so callers reusing the process aren't
    interrupted mid-next-call. Idempotent."""
    try:
        signal.alarm(0)
    except (ValueError, OSError):
        pass


def _check_rss(label: str, ceiling_mb: float = 1500.0) -> None:
    if not _HAS_PSUTIL:
        return
    rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
    if rss_mb > ceiling_mb:
        raise Phase4ResourceError(f"RSS {rss_mb:.0f} MB > {ceiling_mb} MB at {label}")


def run(args: argparse.Namespace) -> dict:
    if not _HAS_PSUTIL:
        raise Phase4ContractError("psutil REQUIRED — install psutil before training")

    # R1#C7: alarm armed AFTER lock acquisition (below). Spec line 411.
    asset = args.asset
    project_root = Path(__file__).resolve().parents[2]
    extract_dir = project_root / 'data' / 'cal_mlp' / asset

    # Discover train_id via CURRENT pointer (or CLI).
    if args.extract_train_id:
        extract_train_id = args.extract_train_id
    else:
        current_path = extract_dir / 'CURRENT'
        if not current_path.exists():
            raise Phase4Error(f"CURRENT pointer absent at {current_path}; run extract_data.py first")
        extract_train_id = current_path.read_text().strip()
    train_dir_extract = extract_dir / extract_train_id

    # SHARED lock on extract_lock for the duration of the run.
    extract_lock = extract_dir / '.extract.lock'
    with acquire_lock(extract_lock, fcntl.LOCK_SH):
        bundle_path = train_dir_extract / 'extract_bundle.json'
        if not bundle_path.exists():
            raise Phase4SchemaError(f"extract bundle missing at {bundle_path}")
        with open(bundle_path) as f:
            ext_bundle = json.load(f)
        if ext_bundle.get('schema_version') != 2:
            raise Phase4SchemaError(
                f"extract_bundle schema_version={ext_bundle.get('schema_version')}, expected 2"
            )
        for k in ('cfg_fp', 'audit_path', 'audit_sha256', 'ticker_vocab_path',
                  'eval_fold_artifacts'):
            if k not in ext_bundle:
                raise Phase4SchemaError(f"extract bundle missing key {k!r}")
        cfg_fp = ext_bundle['cfg_fp']

        # P2.1.a-3-fu1 (86b9xbd2u): route bundle's recipe_namespace through
        # the single dispatch entry. Pre-P2.1.a-3 production bundles do NOT
        # stamp recipe_namespace; resolve_recipe(None) → production for
        # back-compat. Replay-namespace bundles produced by
        # extract_data_replay.py route to CONT_FEATURE_COLS_REPLAY (4
        # features) + ASSET_FLOORS_REPLAY (HYPE/DOGE).
        recipe = resolve_recipe(ext_bundle.get('recipe_namespace'))
        if asset not in recipe.asset_floors:
            raise Phase4ContractError(
                f"asset {asset!r} not in recipe {recipe.namespace!r} asset_floors "
                f"{sorted(recipe.asset_floors)} — bundle recipe_namespace "
                f"{ext_bundle.get('recipe_namespace')!r} is inconsistent with "
                f"--asset choice. Train against a bundle whose namespace covers "
                f"this asset."
            )
        n_cont_recipe = len(recipe.cont_feature_cols)
        n_missing_recipe = len(recipe.missing_indicator_cols)

        audit_path = train_dir_extract / ext_bundle['audit_path']
        if sha256_file(audit_path) != ext_bundle['audit_sha256']:
            raise Phase4SchemaError("audit_sha256 mismatch")
        audit_json = json.load(open(audit_path))

        vocab_path = train_dir_extract / ext_bundle['ticker_vocab_path']
        vocab_payload = json.load(open(vocab_path))
        vocab = vocab_payload['vocab']
        n_vocab = len(vocab)

        # Load all fold data + normstats while holding LOCK_SH.
        # R1#C8: index by fold integer to avoid list-position fragility.
        fold_records = ext_bundle['eval_fold_artifacts']
        normstats_by_fold: dict[int, dict] = {}
        fold_dfs: dict[int, pd.DataFrame] = {}
        for fr in fold_records:
            ns_path = train_dir_extract / fr['normstats_path']
            if sha256_file(ns_path) != fr['normstats_sha256']:
                raise Phase4SchemaError(f"normstats_sha256 mismatch fold {fr['fold']}")
            normstats_by_fold[int(fr['fold'])] = json.load(open(ns_path))
            pq_path = train_dir_extract / fr['parquet_path']
            if sha256_file(pq_path) != fr['parquet_sha256']:
                raise Phase4SchemaError(f"parquet_sha256 mismatch fold {fr['fold']}")
            fold_dfs[int(fr['fold'])] = pd.read_parquet(pq_path, engine='pyarrow', dtype_backend='numpy_nullable')
        # Stable list ordered by fold for sha-chain.
        normstats_per_fold = [normstats_by_fold[f] for f in sorted(normstats_by_fold)]

        # logical sha (Phase 4 R3#C1)
        extract_bundle_logical_sha256 = compute_extract_logical_sha(
            audit_json, normstats_per_fold,
        )

    # Models lock (EX) — acquire AFTER extract_lock per ordering invariant.
    models_dir = project_root / 'models' / f'cal_mlp_{asset}'
    models_dir.mkdir(parents=True, exist_ok=True)
    models_lock = models_dir / '.lock'
    with acquire_lock(models_lock, fcntl.LOCK_EX):
        # R1#C7: arm wall-time alarm AFTER both locks are held.
        # R-p4-r5#H6: disarm via try/finally below so callers reusing the
        # process don't see SIGALRM after run() returns.
        _setup_walltime_alarm(args.wall_ceiling_s)
        # Compute train_id (matches Phase 2 cutoff_end + sha8 over our params).
        train_id_inputs = (
            f"{asset}|{cfg_fp}|{extract_train_id}|{args.base_seed}|{args.ensemble_size}|"
            f"{args.epochs}|{args.batch_size}|{args.lr}"
        )
        train_id_sha8 = hashlib.sha256(train_id_inputs.encode()).hexdigest()[:8]
        train_id = f"{ext_bundle['cutoff_end']}-{train_id_sha8}"
        train_dir = models_dir / train_id
        train_dir.mkdir(parents=True, exist_ok=True)

        # Stale-tmp cleanup
        for stale in list(train_dir.glob('*.tmp-*')) + list(models_dir.glob('CURRENT.tmp-*')):
            try:
                stale.unlink()
            except OSError:
                pass

        # Determine folds to train.
        all_folds = sorted(fr['fold'] for fr in fold_records)
        if args.folds_to_train:
            folds_to_train = [int(x) for x in args.folds_to_train.split(',')]
        else:
            folds_to_train = all_folds

        # Build model_definition.json (early — needed for marker hashing).
        # R2#C4: include cfg_fp so Phase 5 can cross-check.
        # P2.1.a-3-fu1 (86b9xbd2u): n_cont/n_missing derived from the
        # resolved recipe so replay-namespace bundles size the linear
        # layer correctly (4-feature input, not the 8-feature production
        # input). Module-globals N_CONT/N_MISSING remain as production
        # defaults for legacy callers (build_model_from_definition).
        model_def = {
            'model_kind': 'ResidualMLPV1',
            'cfg_fp': cfg_fp,
            'recipe_namespace': recipe.namespace,
            'n_cont': n_cont_recipe,
            'n_missing_indicator_cols': n_missing_recipe,
            'input_continuous_dim': n_cont_recipe + n_missing_recipe,
            'n_price_tiers': 4,
            'n_stc_buckets': 4,
            'n_vol_regimes': 2,
            'n_sides': 2,
            'n_vocab': n_vocab,
            'emb_dim': EMB_DIM,
            'hidden_1': HIDDEN_1,
            'hidden_2': HIDDEN_2,
            'dropout': DEFAULT_DROPOUT,
            'delta_logit_clamp': DELTA_LOGIT_CLAMP,
            'raw_prob_clip_eps': RAW_PROB_CLIP_EPS,
        }
        model_def_canonical = json.dumps(model_def, sort_keys=True, separators=(',', ':')).encode()
        model_def_sha = hashlib.sha256(model_def_canonical).hexdigest()

        wall_start = time.monotonic()
        rss_samples: list[dict] = []
        eval_fold_artifacts: list[dict] = []
        per_fold_audit: list[dict] = []
        pending_renames: list[tuple[Path, Path]] = []
        renamed: list[Path] = []  # R2#C6: hoisted for outer-except cleanup

        try:
            for fold in folds_to_train:
                fold_start = time.monotonic()
                fold_record = next(fr for fr in fold_records if fr['fold'] == fold)
                normstats = normstats_by_fold[fold]
                df_raw = fold_dfs[fold]
                df_norm = apply_norm(
                    df_raw, normstats['stats'], recipe.cont_feature_cols,
                    transforms=normstats.get('transforms', recipe.cont_feature_transforms),
                )

                tr = df_norm[df_norm['split'] == 'train'].reset_index(drop=True)
                ca = df_norm[df_norm['split'] == 'cal'].reset_index(drop=True)
                te = df_norm[df_norm['split'] == 'test'].reset_index(drop=True)
                if len(tr) < args.n_train_min:
                    raise Phase4ContractError(
                        f"fold {fold}: n_train={len(tr)} < {args.n_train_min}"
                    )
                if len(te) < 50:
                    raise Phase4ContractError(f"fold {fold}: n_test={len(te)} < 50")
                # Sanity: is_unk_ticker MUST be 0 in training data.
                # R-p4-r5#C1: column ABSENCE is also a hard failure — we rely
                # on extract_data.py emitting it; silent skip on rename would
                # let UNK rows leak into training and defeat per-member
                # randomization (kills ensemble disagreement signal).
                if 'is_unk_ticker' not in tr.columns:
                    raise Phase4ContractError(
                        f"fold {fold}: is_unk_ticker column missing from train split; "
                        f"extract_data.py contract violated"
                    )
                if (tr['is_unk_ticker'] != 0).any():
                    raise Phase4ContractError(f"fold {fold}: is_unk_ticker != 0 in train")

                # Per-cell stats from extract audit.
                fold_pf = next(pf for pf in audit_json['per_fold'] if pf['fold'] == fold)
                w_cell_lookup = compute_w_cell_lookup(fold_pf['per_cell'])

                ds_train = Phase4Dataset(
                    tr, vocab, w_cell_lookup,
                    cont_feature_cols=recipe.cont_feature_cols,
                    missing_indicator_cols=recipe.missing_indicator_cols,
                    categorical_feature_cols=recipe.categorical_feature_cols,
                )
                ds_cal = Phase4Dataset(
                    ca, vocab, w_cell_lookup,
                    cont_feature_cols=recipe.cont_feature_cols,
                    missing_indicator_cols=recipe.missing_indicator_cols,
                    categorical_feature_cols=recipe.categorical_feature_cols,
                )
                ds_test = Phase4Dataset(
                    te, vocab, w_cell_lookup,
                    cont_feature_cols=recipe.cont_feature_cols,
                    missing_indicator_cols=recipe.missing_indicator_cols,
                    categorical_feature_cols=recipe.categorical_feature_cols,
                )

                cal_preds = np.zeros((args.ensemble_size, len(ca)), dtype=np.float32)
                test_preds = np.zeros((args.ensemble_size, len(te)), dtype=np.float32)
                fold_members_audit: list[dict] = []

                for member in range(args.ensemble_size):
                    if args.base_seed < 1:
                        raise Phase4ContractError("BASE_SEED must be >= 1")
                    member_seed = args.base_seed * 1000 + member
                    member_final = train_dir / f"fold{fold}_member{member}.pt"
                    marker_final = train_dir / f"fold{fold}_member{member}.marker.json"
                    expected_marker = {
                        'cfg_fp': cfg_fp,
                        'extract_bundle_logical_sha256': extract_bundle_logical_sha256,
                        'base_seed': args.base_seed,
                        'model_definition_sha256': model_def_sha,
                        'train_id': train_id,
                        'fold': fold,
                        'member': member,
                        # R-p4-r6#HIGH2: bumped after compute_cal_brier_weighted
                        # denominator switched to sum-of-weights (was unweighted
                        # count). Pre-2 markers have count-weighted Brier; mixing
                        # them in resumed runs would silently corrupt audit metrics.
                        'metric_version': 2,
                    }
                    resumed = False
                    if (args.allow_resume
                            and member_final.exists()
                            and marker_matches(marker_final, expected_marker)):
                        logging.info("[fold %d member %d] resuming from existing checkpoint", fold, member)
                        state_dict = torch.load(member_final, map_location='cpu')
                        model = build_model_from_definition(model_def, n_vocab=n_vocab)
                        model.load_state_dict(state_dict)
                        model.eval()
                        resumed = True
                        marker = json.load(open(marker_final))
                        best_cal_brier_w = float(marker.get('best_cal_brier_w', float('nan')))
                        early_stop_epoch = int(marker.get('early_stop_epoch', 0))
                    else:
                        # Fresh train.
                        torch.manual_seed(member_seed)
                        np.random.seed(member_seed)
                        random.seed(member_seed)
                        device = torch.device(args.device)
                        model = build_model_from_definition(model_def, n_vocab=n_vocab).to(device)
                        model.init_unk_embedding(member_seed)
                        opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
                        steps_per_epoch = max(1, len(ds_train) // args.batch_size)
                        scheduler = build_warmup_then_cosine(
                            opt, warmup_steps=50,
                            total_steps=steps_per_epoch * args.epochs,
                            eta_min=1e-4,
                        )

                        best_cal_brier_w = float('inf')
                        epochs_since_improve = 0
                        best_state_dict = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                        early_stop_epoch = args.epochs
                        # R2#C10: pre-bind epoch/loss for the empty-epochs case.
                        epoch = -1
                        loss = torch.tensor(float('nan'))
                        for epoch in range(args.epochs):
                            model.train()
                            loader = DataLoader(
                                ds_train, batch_size=args.batch_size, shuffle=True,
                                num_workers=0,
                                generator=torch.Generator().manual_seed(member_seed + epoch),
                            )
                            for step_idx, batch in enumerate(loader):
                                if step_idx % 100 == 0:
                                    _check_rss(f"fold{fold}_m{member}_e{epoch}_s{step_idx}")
                                    rss_mb = (psutil.Process().memory_info().rss / (1024 * 1024)
                                                if _HAS_PSUTIL else None)
                                    if rss_mb is not None:
                                        rss_samples.append({'step': step_idx, 'rss_mb': float(rss_mb)})
                                if time.monotonic() - wall_start > args.wall_ceiling_s:
                                    raise Phase4ResourceError("wall-time exceeded mid-batch")
                                loss = compute_weighted_bce(model, batch)
                                opt.zero_grad()
                                loss.backward()
                                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                                opt.step()
                                scheduler.step()
                            cal_brier_w = compute_cal_brier_weighted(model, ds_cal)
                            if cal_brier_w < best_cal_brier_w - 1e-5:
                                best_cal_brier_w = cal_brier_w
                                best_state_dict = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                                epochs_since_improve = 0
                                early_stop_epoch = epoch + 1
                            else:
                                epochs_since_improve += 1
                                if epochs_since_improve >= 5:
                                    break
                        model.load_state_dict(best_state_dict)
                        # Write tmps; rename happens later.
                        member_tmp = write_torch_tmp(best_state_dict, member_final)
                        marker_payload = dict(expected_marker,
                                                best_cal_brier_w=float(best_cal_brier_w),
                                                early_stop_epoch=early_stop_epoch)
                        marker_tmp = write_json_tmp(marker_payload, marker_final)
                        # IMPORTANT (R3#C2): marker BEFORE checkpoint in pending_renames.
                        pending_renames.append((marker_tmp, marker_final))
                        pending_renames.append((member_tmp, member_final))
                        # R1#C4: capture shas of tmps (post-rename bytes are identical).
                        marker_sha = sha256_file(marker_tmp)
                        ckpt_sha = sha256_file(member_tmp)
                        epochs_trained = epoch + 1
                        # R2#C9: float('nan') in JSON serializes to non-standard NaN.
                        loss_val = float(loss.detach().cpu().item()) if not torch.isnan(loss) else None
                        final_train_loss = loss_val
                    if resumed:
                        # Resume path: shas of final files (already on disk).
                        marker_sha = sha256_file(marker_final)
                        ckpt_sha = sha256_file(member_final)
                        epochs_trained = early_stop_epoch
                        final_train_loss = None  # not reconstructable from saved state

                    # Compute cal predictions on the (possibly resumed) model.
                    cal_preds[member] = predict_p(model, ds_cal)
                    test_preds[member] = predict_p(model, ds_test)

                    # R1#C3: best_cal_brier_raw — unweighted Brier on cal.
                    best_cal_brier_raw = float(((cal_preds[member] - ca['outcome'].to_numpy(np.float32)) ** 2).mean())

                    # R-p4-r8-CRIT: was `all_member_checkpoint_shas.append(...)`
                    # for SHA chain — now derived from eval_fold_artifacts
                    # deploy fold members directly so consumer matches.
                    fold_members_audit.append({
                        'member': member, 'seed': member_seed,
                        'checkpoint_path': member_final.name,
                        'checkpoint_sha256': ckpt_sha,
                        'marker_path': marker_final.name,
                        'marker_sha256': marker_sha,
                        'best_cal_brier_w': float(best_cal_brier_w),
                        'best_cal_brier_raw': best_cal_brier_raw,
                        'early_stop_epoch': early_stop_epoch,
                        'epochs_trained': epochs_trained,
                        'final_train_loss': final_train_loss,
                        'resumed_from_marker': resumed,
                    })

                # Ensemble aggregate (float64 → float32).
                cal_preds_f64 = cal_preds.astype(np.float64)
                test_preds_f64 = test_preds.astype(np.float64)
                cal_p_mean = cal_preds_f64.mean(axis=0).astype(np.float32)
                cal_p_std = cal_preds_f64.std(axis=0, ddof=0).astype(np.float32)
                test_p_mean = test_preds_f64.mean(axis=0).astype(np.float32)
                test_p_std = test_preds_f64.std(axis=0, ddof=0).astype(np.float32)
                assert (cal_p_std >= 0).all() and (test_p_std >= 0).all()

                # Build predictions parquet.
                # P2.1.a-3-fu2 (86b9xd9hn): route each categorical column
                # through `_read_categorical_or_default` so replay-namespace
                # bundles emit 0-arrays for absent `price_tier` +
                # `vol_regime_int` (matching Phase4Dataset's view of the
                # same surface). Phase 5 conformal.py reads this parquet
                # and groups on the 4-tuple — defaulting here keeps the
                # required-column-set check in conformal.fit_conformal
                # passing while degenerating the grouping to a single
                # (0, sb, 0) cell per stc_bucket value (by design for
                # replay; not load-bearing until Phase 5 Brier numbers
                # show it).
                _ca_cat = {
                    col: _read_categorical_or_default(
                        ca, col, recipe.categorical_feature_cols,
                    )
                    for col in _ALL_CATEGORICAL_COLS
                }
                _te_cat = {
                    col: _read_categorical_or_default(
                        te, col, recipe.categorical_feature_cols,
                    )
                    for col in _ALL_CATEGORICAL_COLS
                }
                preds_df = pd.DataFrame({
                    'split': ['cal'] * len(ca) + ['test'] * len(te),
                    'ticker': pd.concat([ca['ticker'], te['ticker']], ignore_index=True),
                    'evaluation_time': pd.concat([ca['evaluation_time'], te['evaluation_time']],
                                                    ignore_index=True),
                    'price_tier': np.concatenate([_ca_cat['price_tier'], _te_cat['price_tier']]),
                    'stc_bucket': np.concatenate([_ca_cat['stc_bucket'], _te_cat['stc_bucket']]),
                    'vol_regime_int': np.concatenate([_ca_cat['vol_regime_int'], _te_cat['vol_regime_int']]),
                    'side_int': np.concatenate([_ca_cat['side_int'], _te_cat['side_int']]),
                    'outcome': pd.concat([ca['outcome'], te['outcome']], ignore_index=True),
                    'method_output_raw': pd.concat([ca['method_output_raw'], te['method_output_raw']],
                                                     ignore_index=True),
                    'p_mean': np.concatenate([cal_p_mean, test_p_mean]),
                    'p_std': np.concatenate([cal_p_std, test_p_std]),
                })
                preds_final = train_dir / f"fold{fold}_predictions.parquet"
                preds_table = pa.Table.from_pandas(preds_df, preserve_index=False)
                preds_tmp = write_parquet_tmp(preds_table, preds_final)
                pending_renames.append((preds_tmp, preds_final))
                preds_sha = sha256_file(preds_tmp)

                eval_fold_artifacts.append({
                    'fold': fold,
                    'n_train': int(len(tr)),
                    'n_cal': int(len(ca)),
                    'n_test': int(len(te)),
                    'test_window_start': fold_record['test_window_start'],
                    'test_window_end': fold_record['test_window_end'],
                    'parquet_path': fold_record['parquet_path'],
                    'parquet_sha256': fold_record['parquet_sha256'],
                    'normstats_path': fold_record['normstats_path'],
                    'normstats_sha256': fold_record['normstats_sha256'],
                    'predictions_path': preds_final.name,
                    'predictions_sha256': preds_sha,
                    'members': fold_members_audit,
                })
                std_dist_p = np.percentile(test_p_std, [10, 50, 90, 100])
                per_fold_audit.append({
                    'fold': fold,
                    'wall_time_s': time.monotonic() - fold_start,
                    'members': [
                        {'member': m['member'], 'seed': m['seed'],
                         'early_stop_epoch': m['early_stop_epoch'],
                         'best_cal_brier_weighted': m['best_cal_brier_w'],
                         'resumed_from_marker': m['resumed_from_marker']}
                        for m in fold_members_audit
                    ],
                    'ensemble_std_distribution': {
                        'p10': float(std_dist_p[0]), 'p50': float(std_dist_p[1]),
                        'p90': float(std_dist_p[2]), 'max': float(std_dist_p[3]),
                    },
                    'n_zero_std_rows': int((test_p_std == 0).sum()),
                    'n_zero_std_pct': float((test_p_std == 0).mean()),
                })
                # R-p4-r5#C2: ensemble disagreement is the entire reason we
                # train M=5 members. If >50% of test rows have zero ensemble
                # std, members converged to identical predictions — a
                # degenerate ensemble. Hard fail so the bundle isn't deployed.
                _zero_pct = float((test_p_std == 0).mean())
                if _zero_pct > 0.5:
                    raise Phase4ContractError(
                        f"fold {fold}: ensemble disagreement collapsed "
                        f"({_zero_pct:.1%} zero-std rows > 50%); members may "
                        f"share identical weights post-zero-init head"
                    )

            # bundle_sha chain (R-p4-r7-CRIT: producer matches the canonical
            # _helpers.verify_bundle_sha_chain consumer exactly. Both sides
            # hash the per-file normstats SHA hex strings so the consumer
            # can verify with no extra I/O. Previously the producer hashed
            # canonical-JSON bytes of the dicts while the consumer hashed
            # the eval_fold_artifacts[].normstats_sha256 strings — every
            # real Phase-5 bundle would fail verify_bundle_sha_chain.)
            # R-p4-r8-CRIT: model_identity_sha256 must aggregate ONLY the
            # deploy fold's members (matching consumer at _helpers.py:59-60).
            # Aggregating across all folds would make every multi-fold bundle
            # fail verify_bundle_sha_chain at load.
            # R-p4-r8-MED: deploy_fold_idx is derived from fold_records (the
            # extract-side ALL folds), NOT eval_fold_artifacts (only-trained
            # folds). Per Phase 3 spec R1#C8, deploy = K-1 = max extract fold.
            # If user passed --folds-to-train omitting the max fold, hard fail
            # rather than ship a bundle whose deploy_fold_idx silently points
            # to a non-final-walk-forward fold.
            # R-p4-r8-LOW: typed exception (not bare ValueError) on empty trained-folds
            if not eval_fold_artifacts:
                raise Phase4ContractError(
                    "eval_fold_artifacts is empty; no folds were trained"
                )
            true_deploy_fold_idx = max(fr['fold'] for fr in fold_records)
            if not any(a['fold'] == true_deploy_fold_idx for a in eval_fold_artifacts):
                raise Phase4ContractError(
                    f"deploy fold K-1={true_deploy_fold_idx} not in trained folds "
                    f"{sorted(a['fold'] for a in eval_fold_artifacts)}; "
                    f"--folds-to-train must include the max extract fold or "
                    f"the bundle would advertise a non-final-walk-forward deploy"
                )
            deploy_fold_idx = true_deploy_fold_idx
            deploy_fold_artifact = next(
                a for a in eval_fold_artifacts if a['fold'] == deploy_fold_idx
            )
            deploy_member_ckpt_shas = sorted(
                m['checkpoint_sha256'] for m in deploy_fold_artifact['members']
            )
            model_identity_sha256 = hashlib.sha256(
                ':'.join(deploy_member_ckpt_shas).encode()
            ).hexdigest()
            ns_concat = hashlib.sha256()
            for fold_art in eval_fold_artifacts:
                ns_concat.update(fold_art['normstats_sha256'].encode())
            normstats_concat_sha256 = ns_concat.hexdigest()
            phase4_bundle_sha = hashlib.sha256(
                f"{model_identity_sha256}:{normstats_concat_sha256}:phase4".encode()
            ).hexdigest()

            # model_definition.json
            model_def_path = train_dir / 'model_definition.json'
            model_def_tmp = write_json_tmp(model_def, model_def_path)
            pending_renames.append((model_def_tmp, model_def_path))

            # train_audit.json
            audit_path_out = train_dir / 'train_audit.json'
            audit_payload = {
                'schema_version': 1,
                'train_id': train_id,
                'asset': asset,
                'wall_time_total_s': time.monotonic() - wall_start,
                'peak_rss_mb': max((s['rss_mb'] for s in rss_samples), default=0.0),
                'rss_samples': rss_samples,
                'torch_version': torch.__version__,
                'numpy_version': np.__version__,
                'pandas_version': pd.__version__,
                'pyarrow_version': pa.__version__,
                'folds': per_fold_audit,
            }
            audit_tmp = write_json_tmp(audit_payload, audit_path_out)
            pending_renames.append((audit_tmp, audit_path_out))

            # bundle.json (LAST in pending_renames before CURRENT).
            # deploy_fold_idx already computed above for SHA chain.
            bundle_payload = {
                'phase': 4,
                'schema_version': 2,
                'asset': asset,
                'train_id': train_id,
                'cfg_fp': cfg_fp,
                'model_definition_path': model_def_path.name,
                'model_definition_sha256': model_def_sha,
                'ensemble_size': args.ensemble_size,
                'base_seed': args.base_seed,
                'extract_bundle_path': _safe_relative(bundle_path, project_root),
                'extract_bundle_sha256': sha256_file(bundle_path),
                'extract_bundle_logical_sha256': extract_bundle_logical_sha256,
                'ticker_vocab_path': ext_bundle['ticker_vocab_path'],
                'ticker_vocab_sha256': ext_bundle['ticker_vocab_sha256'],
                'deploy_fold_idx': int(deploy_fold_idx),
                '_path_resolution': 'basenames_relative_to_bundle_dir; cross-bundle refs (extract_bundle_path) relative to project_root',
                'eval_fold_artifacts': eval_fold_artifacts,
                'model_identity_sha256': model_identity_sha256,
                'normstats_concat_sha256': normstats_concat_sha256,
                'bundle_sha': phase4_bundle_sha,
                'phase4_bundle_sha': phase4_bundle_sha,
                # R-p4-r5#C3 + R-p4-r7-CRIT: ordering convention for
                # deterministic recomputation. Phases 5/7 use this to verify
                # bundle integrity via _helpers.verify_bundle_sha_chain.
                '_sha_chain_conventions': {
                    'checkpoint_shas_order': 'sorted_ascending',
                    'normstats_concat_order': 'fold_index_ascending',
                    # PRODUCER & CONSUMER both concat the per-file SHA hex
                    # strings (from eval_fold_artifacts[].normstats_sha256),
                    # NOT the canonical-JSON bytes of the normstats dicts.
                    # This keeps the verifier I/O-free.
                    'normstats_concat_input': 'eval_fold_artifacts[].normstats_sha256_hex',
                    'phase4_formula': 'sha256(model_identity_sha256:normstats_concat_sha256:phase4)',
                    'phase5_formula': 'sha256(phase4_bundle_sha:conformal_sha256)',
                },
                'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                'train_py_sha256': sha256_file(Path(__file__)),
                'torch_version': torch.__version__,
                'numpy_version': np.__version__,
                'pandas_version': pd.__version__,
                'pyarrow_version': pa.__version__,
            }
            bundle_final = train_dir / f"cal_mlp_{asset}_{train_id}_bundle.json"
            bundle_tmp = write_json_tmp(bundle_payload, bundle_final)
            pending_renames.append((bundle_tmp, bundle_final))

            # Rename phase — ordering: markers first per R3#C2 (already
            # appended in order: marker, member, marker, member, ..., preds,
            # model_def, audit, bundle). Bundle is last.
            try:
                for tmp, final in pending_renames:
                    os.replace(tmp, final)
                    renamed.append(final)
            except Exception:
                # R1#C2: unlink already-renamed finals in REVERSE order.
                for final in reversed(renamed):
                    try:
                        if final.exists():
                            final.unlink()
                    except (FileNotFoundError, OSError):
                        pass
                raise
            fsync_directory(train_dir)

            # Update CURRENT.
            current_path = models_dir / 'CURRENT'
            current_tmp = current_path.with_suffix(
                current_path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            )
            with open(current_tmp, 'wb') as f:
                f.write(train_id.encode('utf-8'))
                f.flush()
                os.fsync(f.fileno())
            os.replace(current_tmp, current_path)
            fsync_directory(models_dir)

            return bundle_payload
        except Exception:
            # R2#C6: cleanup tmps AND already-renamed finals in REVERSE.
            for tmp, _final in pending_renames:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except (FileNotFoundError, OSError):
                    pass
            for final in reversed(renamed):
                try:
                    if final.exists():
                        final.unlink()
                except (FileNotFoundError, OSError):
                    pass
            raise
        finally:
            # R-p4-r5#H6: disarm SIGALRM unconditionally before exiting run()
            _disarm_walltime_alarm()


def main() -> None:
    args = parse_args()
    _setup_logging(args.quiet, args.verbose)
    try:
        bundle = run(args)
    except Phase4Error as e:
        logging.error("Phase4Error: %s", e)
        sys.exit(e.exit_code)
    except Exception:
        logging.exception("unexpected error")
        sys.exit(1)
    summary = {
        'train_id': bundle['train_id'],
        'cfg_fp': bundle['cfg_fp'],
        'asset': bundle['asset'],
        'ensemble_size': bundle['ensemble_size'],
        'deploy_fold_idx': bundle['deploy_fold_idx'],
    }
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
