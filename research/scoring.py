"""Composite gate — Ph1a shared eval harness.

NOT a single scalar score. ALL six gates must pass to ACCEPT:

  1. Bootstrap delta > 2σ on BOTH holdouts.
  2. Candidate max-DD ≤ baseline max-DD × 1.10 on each holdout.
  3. Per-product candidate trade count ≥ 30 (for products candidate trades).
  4. Per-product candidate PnL ≥ 0.
  5. No (product, ISO-day) bucket fires IMPLAUSIBLE_FILL
     (daily cf-PnL > balance × threshold; default threshold = 0.10).
     BALANCE_UNKNOWN (None / 0 / <0 balance) auto-rejects with a
     distinct verdict (REJECT_BALANCE_UNKNOWN).
  6. Regime cutoff + cell-block UNION applied BEFORE scoring (caller
     responsibility — this module assumes records are already
     sanitized, but `evaluate()` accepts a `regime_cutoff` kwarg
     and applies it for safety).

Verdicts (`accepted = False` for all REJECT_* and PROMOTE_WITH_FILL_RISK):

  - PROMOTE                       — all gates pass.
  - PROMOTE_WITH_FILL_RISK        — IMPLAUSIBLE_FILL is the ONLY
                                    failing gate. Mirrors
                                    alpha_audit's softer downgrade.
  - REJECT_INSUFFICIENT_CORPUS    — candidate or baseline empty
                                    (after regime + cell-block filter).
  - REJECT_INSUFFICIENT_HOLDOUT   — candidate or baseline holdout
                                    slice empty after temporal/strat
                                    split (corpus too old or too thin).
  - REJECT_BALANCE_UNKNOWN        — balance source missing; cannot
                                    evaluate fill plausibility.
  - REJECT_BOOTSTRAP_BELOW_2SIGMA
  - REJECT_DD_BOUND
  - REJECT_PRODUCT_TRADE_FLOOR
  - REJECT_PRODUCT_NEGATIVE_PNL

Verdict precedence (when multiple gates fail):
  REJECT_INSUFFICIENT_CORPUS
    > REJECT_BALANCE_UNKNOWN
    > REJECT_BOOTSTRAP_BELOW_2SIGMA
    > REJECT_DD_BOUND
    > REJECT_PRODUCT_TRADE_FLOOR
    > REJECT_PRODUCT_NEGATIVE_PNL
    > PROMOTE_WITH_FILL_RISK (only IMPLAUSIBLE_FILL fails)
    > PROMOTE

(R1-finding-2: there is no `REJECT_IMPLAUSIBLE_FILL` because
`IMPLAUSIBLE_FILL coincides with other hard rejects` reduces to
"the other hard reject's verdict" by precedence — the
`REJECT_IMPLAUSIBLE_FILL` slot the previous design proposed was
unreachable in code. IMPLAUSIBLE_FILL is always either the lone
failure → soft `PROMOTE_WITH_FILL_RISK`, or coincident → flagged
in `flags`, demoted to whichever hard reject fired.)
"""
from __future__ import annotations

import json
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from research.cell_blocks import classify_stage
from research.holdouts import stratified_split, temporal_split
from research.regime import apply_regime_cutoff


DEFAULT_IMPLAUSIBLE_FILL_THRESHOLD = 0.10
DEFAULT_PER_PRODUCT_TRADE_FLOOR = 30
DEFAULT_DD_BOUND_MULTIPLIER = 1.10
# DD-bound floor: when baseline DD ≈ 0 (monotonic-up corpus or thin
# holdout slice), the multiplier-only bound poisons the gate. Floor
# the bound at 0.5% of balance so a tiny baseline DD doesn't reject
# every candidate. R1-finding-5.
DEFAULT_DD_BOUND_BALANCE_FLOOR = 0.005
DEFAULT_BOOTSTRAP_RESAMPLES = 1000
DEFAULT_BOOTSTRAP_SIGMA_THRESHOLD = 2.0
DEFAULT_HOLDOUT_DAYS = 14
DEFAULT_STRATIFIED_FRACTION = 0.20
DEFAULT_SEED = 42


@dataclass(frozen=True)
class TradeRecord:
    """One simulated trade as emitted by replay (or a synthetic fixture).

    Ph1a consumes this schema; B3 (research/replay.py) is expected
    to emit it. Adapter is one-line if B3 schema diverges.
    """
    evaluation_time: datetime
    settled_at: datetime
    product: str
    asset: str
    side: Optional[str]
    entry_price_cents: int
    contracts: int
    cf_pnl_cents: int
    filter_stage: str
    market_result: Optional[str]
    available_balance_cents: Optional[int]


@dataclass(frozen=True)
class HoldoutMetrics:
    name: str
    n_trades_candidate: int
    n_trades_baseline: int
    n_trades_per_product_candidate: dict
    candidate_pnl_cents: int
    baseline_pnl_cents: int
    pnl_per_product_candidate: dict
    candidate_max_dd_cents: int
    baseline_max_dd_cents: int
    daily_cf_pnl_buckets: dict   # str(product+'|'+iso_day) → cents
    bootstrap_delta_cents: float
    bootstrap_sigma_cents: float
    implausible_fill_buckets: list  # of (product, iso_day) tuples


@dataclass(frozen=True)
class CompositeResult:
    accepted: bool
    verdict: str
    reasons: List[str]
    flags: List[str]
    per_holdout: dict   # name → HoldoutMetrics


def _parse_dt_naive_utc(value: str) -> datetime:
    """Parse an ISO-8601 datetime string and normalize to naive UTC.

    R2-finding-2: replay/log writers commonly emit `+00:00` aware
    timestamps; default `--regime-cutoff` is naive. Comparing aware
    vs naive raises `TypeError` on Python 3.9+. Normalize on load
    so all downstream comparisons are consistent.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        # Convert to UTC then strip tzinfo. astimezone(timezone.utc)
        # normalizes any offset to UTC; replace(tzinfo=None) makes
        # it naive while preserving the wall-clock-in-UTC value.
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _coerce_optional_int(value) -> Optional[int]:
    """R2-finding-2: `int(None)` raises TypeError. Distinguish
    explicit None from a present numeric field."""
    if value is None:
        return None
    return int(value)


def load_records_from_jsonl(path: Path) -> List[TradeRecord]:
    """Load TradeRecords from a JSONL corpus file. ISO datetime
    strings parsed to datetime, normalized to naive UTC. Missing
    required fields raise KeyError; malformed JSON raises
    json.JSONDecodeError; bad types raise TypeError or ValueError.
    All three are caught by `research.eval.main` and routed to
    EXIT_DATAERR (65)."""
    out: List[TradeRecord] = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append(TradeRecord(
                evaluation_time=_parse_dt_naive_utc(d["evaluation_time"]),
                settled_at=_parse_dt_naive_utc(d["settled_at"]),
                product=d["product"],
                asset=d["asset"],
                side=d.get("side"),
                entry_price_cents=int(d["entry_price_cents"]),
                contracts=int(d["contracts"]),
                cf_pnl_cents=int(d["cf_pnl_cents"]),
                filter_stage=d["filter_stage"],
                market_result=d.get("market_result"),
                available_balance_cents=_coerce_optional_int(
                    d.get("available_balance_cents")
                ),
            ))
    return out


def filter_block_tier(records: Iterable[TradeRecord]) -> List[TradeRecord]:
    """Drop BLOCK-tier rows from candidate volume. Mirrors the
    alpha_audit cell-block UNION pattern."""
    return [r for r in records if classify_stage(r.filter_stage) != "BLOCK"]


def latest_known_balance(records: Iterable[TradeRecord]) -> Optional[int]:
    """Latest non-null `available_balance_cents` by evaluation_time.
    Returns None if no record has a usable balance. Mirrors
    alpha_audit's balance derivation when --balance-cents is absent."""
    latest_t: Optional[datetime] = None
    latest_bal: Optional[int] = None
    for r in records:
        if r.available_balance_cents is None:
            continue
        if r.available_balance_cents <= 0:
            continue
        if latest_t is None or r.evaluation_time > latest_t:
            latest_t = r.evaluation_time
            latest_bal = r.available_balance_cents
    return latest_bal


def implausible_fill_buckets(
    records: Iterable[TradeRecord],
    balance_cents: Optional[int],
    threshold: float = DEFAULT_IMPLAUSIBLE_FILL_THRESHOLD,
) -> Tuple[List[Tuple[str, str]], bool]:
    """Per-(product, ISO-day) bucket detection. Returns
    (firing_buckets, balance_unknown). When balance_unknown is
    True, firing_buckets is empty and the caller MUST treat
    the candidate as REJECT_BALANCE_UNKNOWN."""
    if balance_cents is None or balance_cents <= 0:
        return [], True
    by_bucket: dict[Tuple[str, str], int] = defaultdict(int)
    for r in records:
        day = r.settled_at.date().isoformat()
        by_bucket[(r.product, day)] += r.cf_pnl_cents
    fired = [
        b for b, pnl in by_bucket.items()
        if pnl > balance_cents * threshold
    ]
    return sorted(fired), False


def daily_cf_pnl_buckets(records: Iterable[TradeRecord]) -> dict:
    out: dict[str, int] = defaultdict(int)
    for r in records:
        key = f"{r.product}|{r.settled_at.date().isoformat()}"
        out[key] += r.cf_pnl_cents
    return dict(out)


def product_key(r: TradeRecord) -> str:
    """Gate-axis key for floor + NEGATIVE_PNL gates.

    The Ph1a AC says "per-product trade count ≥ 30 (kills 'narrow
    the universe' degenerates)". Since "narrow the universe" includes
    asset-narrowing within a product (e.g., scoping 15m to {BTC}
    only), the floor and NEGATIVE_PNL gates aggregate at
    (product, asset) granularity. IMPLAUSIBLE_FILL stays at the
    coarser (product, day) granularity per D-12.
    """
    return f"{r.product}/{r.asset}"


def per_product_pnl(records: Iterable[TradeRecord]) -> dict:
    out: dict[str, int] = defaultdict(int)
    for r in records:
        out[product_key(r)] += r.cf_pnl_cents
    return dict(out)


def per_product_count(records: Iterable[TradeRecord]) -> dict:
    out: dict[str, int] = defaultdict(int)
    for r in records:
        out[product_key(r)] += 1
    return dict(out)


def cumulative_max_dd(records: Sequence[TradeRecord]) -> int:
    """Max drawdown of the cumulative cf_pnl curve, cents (≥ 0).

    Cumulative curve = running sum of cf_pnl ordered by settled_at.
    Drawdown at point t = running_max(t) − running_value(t).
    Returns the maximum drawdown across the curve; 0 if curve is
    non-decreasing.
    """
    if not records:
        return 0
    ordered = sorted(records, key=lambda r: r.settled_at)
    running = 0
    peak = 0
    max_dd = 0
    for r in ordered:
        running += r.cf_pnl_cents
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return max_dd


def bootstrap_delta(
    candidate: Sequence[TradeRecord],
    baseline: Sequence[TradeRecord],
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> Tuple[float, float]:
    """Per-day stratified bootstrap on (candidate − baseline) total PnL.

    For each resample:
      - sample with replacement K days from the union of candidate
        and baseline days (where K = number of distinct days).
      - sum candidate cf_pnl on those days, sum baseline cf_pnl on
        those days, take the difference.
    Return (mean_delta, sigma_delta) over n_resamples.

    Seeded for determinism.
    """
    cand_by_day: dict[str, int] = defaultdict(int)
    base_by_day: dict[str, int] = defaultdict(int)
    for r in candidate:
        cand_by_day[r.settled_at.date().isoformat()] += r.cf_pnl_cents
    for r in baseline:
        base_by_day[r.settled_at.date().isoformat()] += r.cf_pnl_cents
    days = sorted(set(cand_by_day) | set(base_by_day))
    # R3-finding-1: defensive guard for degenerate inputs that
    # bypass CLI validation (e.g., `evaluate()` called programmatically
    # with n_resamples=0). statistics.fmean([]) raises StatisticsError;
    # we'd rather return zero stats than crash.
    if not days or n_resamples <= 0:
        return 0.0, 0.0
    rng = random.Random(seed)
    deltas: List[float] = []
    k = len(days)
    for _ in range(n_resamples):
        sample = [rng.choice(days) for _ in range(k)]
        cand_sum = sum(cand_by_day.get(d, 0) for d in sample)
        base_sum = sum(base_by_day.get(d, 0) for d in sample)
        deltas.append(float(cand_sum - base_sum))
    if len(deltas) <= 1:
        return statistics.fmean(deltas), 0.0
    return statistics.fmean(deltas), statistics.pstdev(deltas)


def _per_holdout_metrics(
    name: str,
    candidate: Sequence[TradeRecord],
    baseline: Sequence[TradeRecord],
    balance_cents: Optional[int],
    implausible_fill_threshold: float,
    n_resamples: int,
    seed: int,
) -> Tuple[HoldoutMetrics, bool]:
    """Compute one holdout's metrics. Returns (metrics, balance_unknown_flag).

    R2-finding-4: BLOCK-tier filtering applied to BOTH candidate and
    baseline. Per the autoresearch design, BLOCK rows must never
    count toward "would-have-traded volume" — symmetric filtering
    keeps the baseline sane in shadow-vs-shadow comparisons (where
    baseline is also a non-live config that may include block rows).
    """
    cand_block_filtered = filter_block_tier(candidate)
    base_block_filtered = filter_block_tier(baseline)
    fired, balance_unknown = implausible_fill_buckets(
        cand_block_filtered, balance_cents, implausible_fill_threshold,
    )
    # R4-finding-1: zero out bootstrap stats when either holdout is
    # empty so the persisted HoldoutMetrics record can't mislead a
    # downstream consumer (dashboard, sweep aggregator) into reading
    # a pseudo-significant `bootstrap_delta_cents` for a holdout
    # that didn't actually compare anything. The verdict gate
    # short-circuits to REJECT_INSUFFICIENT_HOLDOUT for this case;
    # this just keeps the metrics object honest.
    if not cand_block_filtered or not base_block_filtered:
        delta, sigma = 0.0, 0.0
    else:
        delta, sigma = bootstrap_delta(
            cand_block_filtered, base_block_filtered,
            n_resamples=n_resamples, seed=seed,
        )
    return HoldoutMetrics(
        name=name,
        n_trades_candidate=len(cand_block_filtered),
        n_trades_baseline=len(base_block_filtered),
        n_trades_per_product_candidate=per_product_count(cand_block_filtered),
        candidate_pnl_cents=sum(r.cf_pnl_cents for r in cand_block_filtered),
        baseline_pnl_cents=sum(r.cf_pnl_cents for r in base_block_filtered),
        pnl_per_product_candidate=per_product_pnl(cand_block_filtered),
        candidate_max_dd_cents=cumulative_max_dd(cand_block_filtered),
        baseline_max_dd_cents=cumulative_max_dd(base_block_filtered),
        daily_cf_pnl_buckets=daily_cf_pnl_buckets(cand_block_filtered),
        bootstrap_delta_cents=delta,
        bootstrap_sigma_cents=sigma,
        implausible_fill_buckets=[list(b) for b in fired],
    ), balance_unknown


def evaluate(
    candidate: Sequence[TradeRecord],
    baseline: Sequence[TradeRecord],
    *,
    regime_cutoff: Optional[datetime],
    balance_cents: Optional[int] = None,
    holdout_days: int = DEFAULT_HOLDOUT_DAYS,
    stratified_fraction: float = DEFAULT_STRATIFIED_FRACTION,
    seed: int = DEFAULT_SEED,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    sigma_threshold: float = DEFAULT_BOOTSTRAP_SIGMA_THRESHOLD,
    per_product_floor: int = DEFAULT_PER_PRODUCT_TRADE_FLOOR,
    dd_multiplier: float = DEFAULT_DD_BOUND_MULTIPLIER,
    dd_bound_balance_floor: float = DEFAULT_DD_BOUND_BALANCE_FLOOR,
    implausible_fill_threshold: float = DEFAULT_IMPLAUSIBLE_FILL_THRESHOLD,
) -> CompositeResult:
    """Score a candidate against a baseline.

    Caller may pre-apply regime cutoff; passing it again here is
    idempotent (records below cutoff would already be absent).
    Cell-block BLOCK-tier filtering happens internally via
    `filter_block_tier` so candidate volume is sanitized identically
    in research as in alpha_audit.

    `balance_cents` falls back to `latest_known_balance(candidate)`
    if not specified explicitly. `None` after the fallback means
    REJECT_BALANCE_UNKNOWN.

    Empty corpus after regime + cell-block filtering returns
    REJECT_INSUFFICIENT_CORPUS with the input sizes in `reasons`.
    """
    raw_candidate_n = len(candidate)
    raw_baseline_n = len(baseline)
    candidate = list(apply_regime_cutoff(candidate, regime_cutoff))
    baseline = list(apply_regime_cutoff(baseline, regime_cutoff))
    cand_after_block = filter_block_tier(candidate)

    # R1-finding-6: empty corpus guard. Distinguishes "candidate
    # legitimately rejects every row" from "caller pointed at an
    # empty file" — both look the same downstream otherwise.
    if not cand_after_block or not baseline:
        return CompositeResult(
            accepted=False,
            verdict="REJECT_INSUFFICIENT_CORPUS",
            reasons=[
                f"candidate: {raw_candidate_n} raw, "
                f"{len(candidate)} post-regime, "
                f"{len(cand_after_block)} post-cell-block; "
                f"baseline: {raw_baseline_n} raw, {len(baseline)} post-regime"
            ],
            flags=[],
            per_holdout={},
        )

    if balance_cents is None:
        balance_cents = (latest_known_balance(candidate)
                         or latest_known_balance(baseline))

    # R1-finding-1: shared reference_now anchors temporal split to
    # the same cutoff on BOTH corpora. Without this, asymmetric
    # date coverage (the normal sweep case where a candidate filters
    # some days) makes the two holdout windows diverge — e.g.,
    # cand=30d/base=25d → cand_holdout = base_dates+5..30 while
    # base_holdout = base_dates+11..25, only 10/15 days overlap, and
    # the bootstrap unions all 20 distinct days creating phantom
    # candidate-only positive sums on every resample.
    # Use min() of per-corpus max so the holdout window has data
    # in BOTH corpora (conservative choice — caps the cutoff at
    # whichever corpus stops earlier).
    max_cand_eval = max(r.evaluation_time for r in candidate)
    max_base_eval = max(r.evaluation_time for r in baseline)
    shared_now = min(max_cand_eval, max_base_eval)
    # R2-finding-8: stale-corpus warning. If the two corpora end more
    # than 24h apart, the shared_now clamp drops fresh data from the
    # later corpus silently. Surface so the operator can decide
    # whether their inputs are correctly aligned.
    skew = abs((max_cand_eval - max_base_eval).total_seconds())
    if skew > 86_400:
        import sys as _sys
        print(
            f"WARNING: candidate and baseline corpora end "
            f"{skew/86_400:.1f} days apart — shared_now clamp at "
            f"{shared_now.isoformat()} drops post-shared-now rows. "
            f"Confirm both corpora cover the intended evaluation window.",
            file=_sys.stderr,
        )
    # R2-finding-7: only holdouts are consumed downstream; in-sample
    # returns are dropped here to avoid implying they participate
    # in the gate (the docstring previously misled on this point).
    _, cand_temporal_holdout = temporal_split(
        candidate, holdout_days, reference_now=shared_now,
    )
    _, base_temporal_holdout = temporal_split(
        baseline, holdout_days, reference_now=shared_now,
    )
    _, cand_strat_holdout = stratified_split(
        candidate, stratified_fraction, seed,
    )
    _, base_strat_holdout = stratified_split(
        baseline, stratified_fraction, seed,
    )

    metrics_temporal, balance_unknown_t = _per_holdout_metrics(
        "temporal", cand_temporal_holdout, base_temporal_holdout,
        balance_cents, implausible_fill_threshold, n_resamples, seed,
    )
    metrics_strat, balance_unknown_s = _per_holdout_metrics(
        "stratified", cand_strat_holdout, base_strat_holdout,
        balance_cents, implausible_fill_threshold, n_resamples, seed,
    )
    per_holdout = {"temporal": metrics_temporal, "stratified": metrics_strat}
    flags: List[str] = []
    reasons: List[str] = []

    if balance_unknown_t or balance_unknown_s:
        flags.append("BALANCE_UNKNOWN: cannot evaluate IMPLAUSIBLE_FILL gate")
        return CompositeResult(
            accepted=False,
            verdict="REJECT_BALANCE_UNKNOWN",
            reasons=[
                "balance source is None / 0 / negative on at least one holdout"
            ],
            flags=flags,
            per_holdout=per_holdout,
        )

    bootstrap_failed = []
    dd_failed = []
    floor_failed = []
    pnl_failed = []
    fill_failed = []
    insufficient_holdout = []

    for m in (metrics_temporal, metrics_strat):
        # R2-finding-5: empty holdout (or empty baseline-on-holdout)
        # → "insufficient holdout" verdict, not a misleading bootstrap
        # rejection. Empty candidate holdout means evaluate() can't
        # honestly score this corpus at this slice.
        if m.n_trades_candidate == 0 or m.n_trades_baseline == 0:
            insufficient_holdout.append(
                f"{m.name}: candidate={m.n_trades_candidate} "
                f"baseline={m.n_trades_baseline} rows in holdout window"
            )
            continue
        # R2-finding-1: sigma ≤ 0 with non-trivial corpus means all
        # per-day deltas are identical (single day, or every day has
        # the same cand−base sum). The 2σ test is undefined in that
        # regime — fail the gate explicitly rather than silently
        # auto-passing on positive delta. The autoresearch design's
        # "selection pressure" gate REQUIRES day-variance; zero
        # variance is the gameable degenerate (single-day candidates).
        if m.bootstrap_sigma_cents <= 0:
            bootstrap_failed.append(
                f"{m.name}: insufficient day-variance "
                f"(delta={m.bootstrap_delta_cents:+.0f}c, sigma=0) — "
                f"single-day or constant-delta corpus, 2σ test undefined"
            )
        elif m.bootstrap_delta_cents <= sigma_threshold * m.bootstrap_sigma_cents:
            bootstrap_failed.append(
                f"{m.name}: delta {m.bootstrap_delta_cents:+.0f}c "
                f"<= {sigma_threshold:g}σ ({m.bootstrap_sigma_cents:.0f}c)"
            )
        # R2-finding-3: DD multiplier governs whenever baseline has
        # any non-zero DD signal. The balance-proportional floor only
        # kicks in for monotonic-up baselines (DD = 0 exactly), where
        # the multiplier-only bound (= 0) would reject any candidate
        # DD ≥ 1c regardless of how small absolutely. R1-finding-5
        # original intent preserved; R2 makes the floor surgical.
        if m.baseline_max_dd_cents == 0:
            bound = balance_cents * dd_bound_balance_floor
        else:
            bound = m.baseline_max_dd_cents * dd_multiplier
        if m.candidate_max_dd_cents > bound:
            dd_failed.append(
                f"{m.name}: candidate max_dd {m.candidate_max_dd_cents}c "
                f"> bound {bound:.0f}c "
                f"(baseline_dd={m.baseline_max_dd_cents}c, "
                f"multiplier={dd_multiplier:g}, "
                f"balance_floor={dd_bound_balance_floor:g}×balance)"
            )
        for product, n in m.n_trades_per_product_candidate.items():
            if n < per_product_floor:
                floor_failed.append(
                    f"{m.name}: product {product!r} only {n} candidate "
                    f"trades (< {per_product_floor})"
                )
        for product, pnl in m.pnl_per_product_candidate.items():
            if pnl < 0:
                pnl_failed.append(
                    f"{m.name}: product {product!r} candidate PnL "
                    f"{pnl}c (< 0)"
                )
        if m.implausible_fill_buckets:
            for b in m.implausible_fill_buckets:
                fill_failed.append(
                    f"{m.name}: IMPLAUSIBLE_FILL bucket "
                    f"({b[0]}, {b[1]}) (cf_pnl assumes 100% fill; "
                    f"slippage/depth not modeled)"
                )

    if insufficient_holdout:
        return CompositeResult(
            accepted=False,
            verdict="REJECT_INSUFFICIENT_HOLDOUT",
            reasons=insufficient_holdout,
            flags=flags,
            per_holdout=per_holdout,
        )

    # R2-finding-6: temporal and stratified holdouts both sample
    # from the same `candidate` list with no enforced disjointness.
    # A row in the trailing 14d is eligible to also land in the
    # stratified-20% sample. The "2σ on BOTH holdouts" gate is
    # therefore correlated evidence on overlapping data, not the
    # independent two-pronged gate the autoresearch literature
    # implies. The conservative AND-of-two-2σ tests is still
    # stronger than a single 2σ test (it requires both axes —
    # time and price/asset regime — to clear), but downstream
    # consumers should know the AND is NOT statistically
    # independent. Surface as a flag.
    flags.append(
        "HOLDOUTS_NON_INDEPENDENT: temporal and stratified holdouts "
        "may share rows; '2σ on both' is correlated evidence, not "
        "two independent draws"
    )

    reasons = (
        bootstrap_failed + dd_failed + floor_failed + pnl_failed + fill_failed
    )
    flags.extend(fill_failed)

    other_failures = bool(bootstrap_failed or dd_failed or floor_failed or pnl_failed)
    has_fill_risk = bool(fill_failed)

    if not reasons:
        return CompositeResult(
            accepted=True, verdict="PROMOTE",
            reasons=[], flags=flags, per_holdout=per_holdout,
        )
    if has_fill_risk and not other_failures:
        return CompositeResult(
            accepted=False, verdict="PROMOTE_WITH_FILL_RISK",
            reasons=reasons, flags=flags, per_holdout=per_holdout,
        )
    # R1-finding-2: the fall-through `REJECT_IMPLAUSIBLE_FILL` slot
    # in the previous design was unreachable — `other_failures` is
    # True iff at least one of {bootstrap, dd, floor, pnl}_failed
    # is non-empty, so one of the elifs below MUST hit. Asserted
    # explicitly so a future refactor that breaks this invariant
    # fails loudly instead of silently producing a misleading verdict.
    if bootstrap_failed:
        verdict = "REJECT_BOOTSTRAP_BELOW_2SIGMA"
    elif dd_failed:
        verdict = "REJECT_DD_BOUND"
    elif floor_failed:
        verdict = "REJECT_PRODUCT_TRADE_FLOOR"
    elif pnl_failed:
        verdict = "REJECT_PRODUCT_NEGATIVE_PNL"
    else:
        raise AssertionError(
            "evaluate() verdict-precedence invariant broken: "
            f"other_failures={other_failures} but no hard-reject list "
            "matched; this branch should be unreachable. "
            f"reasons={reasons!r}"
        )
    return CompositeResult(
        accepted=False, verdict=verdict,
        reasons=reasons, flags=flags, per_holdout=per_holdout,
    )


def result_to_dict(result: CompositeResult) -> dict:
    """JSON-safe rendering for CLI output."""
    return {
        "accepted": result.accepted,
        "verdict": result.verdict,
        "reasons": list(result.reasons),
        "flags": list(result.flags),
        "per_holdout": {
            name: asdict(m) for name, m in result.per_holdout.items()
        },
    }
