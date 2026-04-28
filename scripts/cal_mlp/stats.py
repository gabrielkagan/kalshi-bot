"""Statistics helpers for Phase 6 (validation + sim PnL).

Cluster-bootstrap (resample tickers) and day-bootstrap (resample trading
days) with seeded RNG and tiered N escalation per
`kb-research/bot/p2-phase6-validation.md` R-p6-1#C9 + R-p6-1#C14 + R-p6-3#C3.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Tiered-N escalation (R-p6-1#C9)
# ---------------------------------------------------------------------------

# margin = |CI bound − threshold|. Tier escalates from N=2000 → 10000 → 50000
# → abstain. Returned alongside the CI for audit.
TIERS = [(2000, 0.01), (10000, 0.003), (50000, 0.001)]


def cluster_bootstrap_ci(
    df: pd.DataFrame,
    stat_fn: Callable[[pd.DataFrame], float],
    cluster_col: str = 'ticker',
    n_bootstrap: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float, dict]:
    """Cluster-bootstrap by tickers (R-p6-1#E3 + R-p6-1#C14 streaming).

    Returns (point, ci_lo, ci_hi, audit_dict). Streams resamples — only the
    per-iter scalar metric accumulates (O(N + n_rows) RAM, NOT O(N × n_rows)).
    """
    point = float(stat_fn(df))
    rng = np.random.default_rng(seed)
    clusters = df[cluster_col].unique()
    n_clusters = len(clusters)
    if n_clusters == 0:
        return (point, point, point, {'n_clusters': 0, 'n_bootstrap': 0})
    # Pre-index cluster→rows for O(1) resample lookups.
    cluster_to_rows: dict = {}
    for cluster in clusters:
        cluster_to_rows[cluster] = df[df[cluster_col] == cluster].index.to_numpy()
    deltas = np.empty(n_bootstrap, dtype=np.float64)
    for i in range(n_bootstrap):
        sampled_clusters = rng.choice(clusters, size=n_clusters, replace=True)
        sampled_indices = np.concatenate([
            cluster_to_rows[c] for c in sampled_clusters
        ])
        sample = df.loc[sampled_indices]
        deltas[i] = float(stat_fn(sample))
    lo = float(np.quantile(deltas, alpha / 2))
    hi = float(np.quantile(deltas, 1 - alpha / 2))
    # Monte Carlo SE on the CI endpoint via jackknife approximation.
    mc_se = float(np.std(deltas) / math.sqrt(n_bootstrap))
    return (point, lo, hi, {
        'n_clusters': int(n_clusters),
        'n_bootstrap': int(n_bootstrap),
        'seed': int(seed),
        'mc_se': mc_se,
    })


def day_bootstrap_ci(
    daily_metrics: np.ndarray,
    n_bootstrap: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float, dict]:
    """Day-bootstrap for A/B PnL paired comparisons (R-p6-3#C3).

    `daily_metrics` is a 1D array of per-day paired deltas (length n_days).
    Resamples days with replacement. Returns (mean_point, ci_lo, ci_hi, audit).
    """
    n_days = len(daily_metrics)
    if n_days == 0:
        return (0.0, 0.0, 0.0, {'n_days': 0, 'n_bootstrap': 0})
    point = float(np.mean(daily_metrics))
    rng = np.random.default_rng(seed)
    means = np.empty(n_bootstrap, dtype=np.float64)
    for i in range(n_bootstrap):
        idx = rng.integers(0, n_days, size=n_days)
        means[i] = float(np.mean(daily_metrics[idx]))
    lo = float(np.quantile(means, alpha / 2))
    hi = float(np.quantile(means, 1 - alpha / 2))
    mc_se = float(np.std(means) / math.sqrt(n_bootstrap))
    return (point, lo, hi, {
        'n_days': int(n_days),
        'n_bootstrap': int(n_bootstrap),
        'seed': int(seed),
        'mc_se': mc_se,
    })


def escalate_n_if_close(
    margin: float,
    current_n: int,
) -> tuple[int, bool]:
    """R-p6-1#C9 tiered escalation. Returns (new_n, abstain).

    margin = |CI bound − threshold|. Tier transitions:
      margin >= 0.01    → stay (any N)
      margin in [0.003, 0.01) → escalate to ≥10000
      margin in [0.001, 0.003) → escalate to ≥50000
      margin < 0.001 and current_n >= 50000 → abstain
    R-p6-impl-2#C8: no `current_n >= 2000` guard on the top branch — that
    forced sub-2000 user runs to silently escalate to 10000.
    """
    if margin >= 0.01:
        return (current_n, False)
    if margin >= 0.003:
        return (max(current_n, 10000), False)
    if margin >= 0.001:
        return (max(current_n, 50000), False)
    if current_n >= 50000:
        return (current_n, True)  # abstain
    return (max(current_n, 50000), False)


def wilson_ci_helper(n_success: int, n_total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI on binomial proportion. Mirrors _helpers.wilson_ci so
    Phase 6 has its own copy and doesn't depend on Phase 5 import path."""
    if n_total <= 0:
        return (0.0, 1.0)
    p = n_success / n_total
    denom = 1 + z * z / n_total
    centre = (p + z * z / (2 * n_total)) / denom
    half = (z * math.sqrt(p * (1 - p) / n_total + z * z / (4 * n_total * n_total))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))
