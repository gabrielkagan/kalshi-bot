"""Two holdout splits — temporal and stratified.

Per Ph1a AC + autoresearch design § "Replay on two splits":

- Temporal: last N days vs prior period. Crypto trades 24/7 so
  calendar days = trading days. Default N=14.
- Stratified random: 20% of rows held out, stratified by
  (asset, price_tier). Seed-deterministic. Same seed shared between
  candidate and baseline so the row split is identical.

Both splits return (in_sample, holdout) tuples of TradeRecord lists.
A candidate is judged on holdout PnL via bootstrap; in-sample is the
reference for the per-day delta distribution.
"""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import List, Sequence, Tuple


def price_tier(entry_price_cents: int) -> str:
    """Stratification tier — must mirror Ph1a AC bucket boundaries.

    Ranges chosen to match the dominant 15M trading band (80-99c)
    plus a single sub-80 lump for shadow strategies. Adjust only
    if the trading universe changes (e.g., DC sub-80c promotion).
    """
    if entry_price_cents < 80:
        return "<80"
    if entry_price_cents < 90:
        return "80-89"
    if entry_price_cents < 98:
        return "90-97"
    return "98-99"


def temporal_split(
    records: Sequence,
    holdout_days: int = 14,
    reference_now: datetime | None = None,
) -> Tuple[List, List]:
    """Holdout = records with evaluation_time in window
    `[reference_now − holdout_days, reference_now]`. In-sample =
    records strictly before the window. Records strictly after
    `reference_now` are DROPPED from both — out-of-window data
    doesn't belong in a fixed historical evaluation slice.

    `reference_now` defaults to the max evaluation_time across the
    corpus (in which case there are no post-window rows by
    construction). When the caller supplies an explicit
    `reference_now` smaller than the corpus max — e.g. `evaluate()`
    sharing `min(max_cand, max_base)` between candidate and baseline
    so both windows are identical — post-window rows in the corpus
    that holds the larger date range MUST be dropped, otherwise the
    bootstrap unions with phantom candidate-only days. R1-finding-1.
    """
    rs = list(records)
    if not rs:
        return [], []
    now = reference_now if reference_now is not None else max(
        r.evaluation_time for r in rs
    )
    cutoff = now - timedelta(days=holdout_days)
    in_sample, holdout = [], []
    for r in rs:
        if r.evaluation_time < cutoff:
            in_sample.append(r)
        elif r.evaluation_time <= now:
            holdout.append(r)
        # else: post-`now` → drop (out of evaluation window)
    return in_sample, holdout


def stratified_split(
    records: Sequence,
    holdout_fraction: float = 0.20,
    seed: int = 42,
) -> Tuple[List, List]:
    """Per-(asset, price_tier) stratified random split.

    Algorithm: bucket by stratum, shuffle within bucket using
    `random.Random(seed)`, take ceil(N * holdout_fraction) as holdout.
    The ceil ensures small strata (<5 rows) still contribute at
    least one holdout row when they have ≥1; strata with N=0 are
    absent from both outputs.

    Seed pinning makes a SINGLE split call reproducible across
    re-runs over the same input (deterministic). It does NOT
    guarantee that candidate and baseline corpora — when they have
    different per-stratum populations — produce row-identical
    holdouts: the rng state advances per `rng.shuffle(rows)` so
    even strata that come earlier in iteration order are only
    row-aligned when their full row sequences are byte-equal across
    the two corpora.

    R1-finding-4: this means the stratified gate compares two
    independently-stratified samples drawn from overlapping
    populations, NOT the same rows projected through two configs.
    The bootstrap-on-days gate dampens the impact (per-day deltas
    aggregate trades by date), but `pnl_per_product_candidate` and
    holdout-DD on stratified are computed on a non-paired sample.
    A future fix (deferred to B3) is to project a single split via
    a stable opp_id key onto each corpus, ensuring identical row
    coverage. For Ph1a we accept the wider holdout uncertainty and
    document the limitation here + in the plan doc.
    """
    if holdout_fraction <= 0 or holdout_fraction >= 1:
        raise ValueError(
            f"holdout_fraction must be in (0, 1), got {holdout_fraction!r}"
        )
    rng = random.Random(seed)
    by_stratum: dict[tuple[str, str], list] = defaultdict(list)
    for r in records:
        by_stratum[(r.asset, price_tier(r.entry_price_cents))].append(r)

    in_sample: list = []
    holdout: list = []
    for stratum in sorted(by_stratum.keys()):
        rows = list(by_stratum[stratum])
        # Sort by evaluation_time for reproducibility before shuffle.
        rows.sort(key=lambda r: (r.evaluation_time, r.asset, r.entry_price_cents))
        rng.shuffle(rows)
        n_holdout = max(1, int(round(len(rows) * holdout_fraction)))
        n_holdout = min(n_holdout, len(rows))
        holdout.extend(rows[:n_holdout])
        in_sample.extend(rows[n_holdout:])
    return in_sample, holdout
