"""CLI smoke entry: `python -m research.eval --corpus PATH --baseline PATH ...`

Numeric arg validation (R3-finding-1): negative / zero / out-of-range
flags route to EXIT_USAGE (64) instead of producing internal-bug exit
1's that sweep harnesses would mistake for clean rejects.

Exit codes (R1-finding-3 — distinguishable from harness errors so
Ph1b/c sweep callers can tell ACCEPT/REJECT apart from corpus
parse failures):

  0  ACCEPT       — all gates passed.
  1  REJECT       — any REJECT_* or PROMOTE_WITH_FILL_RISK verdict.
  2  argparse     — invalid CLI flags (set by argparse, not us).
  65 EX_DATAERR   — corpus file unreadable / JSON malformed.
  66 EX_NOINPUT   — corpus path doesn't exist.
  70 EX_SOFTWARE  — internal bug surfaced (assertion failure etc.).

Sysexits codes per /usr/include/sysexits.h. Sweep harnesses MUST
treat exit ≥ 64 as "harness error, retry or surface to operator,"
exit 1 as "candidate scored and was rejected — log it and move on,"
exit 0 as "shadow this candidate."

Stderr carries warnings (regime cutoff disabled, etc.). Stdout
carries JSON (default) or one-line verdict (--quiet).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import traceback
from pathlib import Path

from research.regime import REGISTERED_DEFAULT_CUTOFF, parse_cutoff
from research.scoring import (
    DEFAULT_BOOTSTRAP_RESAMPLES,
    DEFAULT_BOOTSTRAP_SIGMA_THRESHOLD,
    DEFAULT_DD_BOUND_BALANCE_FLOOR,
    DEFAULT_DD_BOUND_MULTIPLIER,
    DEFAULT_HOLDOUT_DAYS,
    DEFAULT_IMPLAUSIBLE_FILL_THRESHOLD,
    DEFAULT_PER_PRODUCT_TRADE_FLOOR,
    DEFAULT_SEED,
    DEFAULT_STRATIFIED_FRACTION,
    evaluate,
    load_records_from_jsonl,
    result_to_dict,
)


_DEFAULT_CUTOFF_ISO = REGISTERED_DEFAULT_CUTOFF.isoformat()

EXIT_ACCEPT = 0
EXIT_REJECT = 1
EXIT_USAGE = 64
EXIT_DATAERR = 65
EXIT_NOINPUT = 66
EXIT_SOFTWARE = 70


def _positive_int(s: str) -> int:
    v = int(s)
    if v < 1:
        raise argparse.ArgumentTypeError(f"{s!r}: must be ≥ 1")
    return v


def _nonneg_int(s: str) -> int:
    v = int(s)
    if v < 0:
        raise argparse.ArgumentTypeError(f"{s!r}: must be ≥ 0")
    return v


def _open_fraction(s: str) -> float:
    v = float(s)
    if not (0.0 < v < 1.0):
        raise argparse.ArgumentTypeError(f"{s!r}: must be in (0, 1)")
    return v


def _nonneg_float(s: str) -> float:
    v = float(s)
    if v < 0:
        raise argparse.ArgumentTypeError(f"{s!r}: must be ≥ 0")
    return v


def _ge_one_float(s: str) -> float:
    v = float(s)
    if v < 1.0:
        raise argparse.ArgumentTypeError(f"{s!r}: must be ≥ 1.0")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m research.eval",
        description="Phase 1a — shared eval harness composite gate.",
    )
    p.add_argument("--corpus", required=True, type=Path,
                   help="JSONL of candidate TradeRecord rows.")
    p.add_argument("--baseline", required=True, type=Path,
                   help="JSONL of baseline (live-config replay) TradeRecord rows.")
    p.add_argument(
        "--regime-cutoff", default=_DEFAULT_CUTOFF_ISO,
        help=(
            "ISO-8601 datetime; rows with evaluation_time < cutoff are "
            f"dropped before scoring. Default: {_DEFAULT_CUTOFF_ISO} "
            "(bleed-cell blocks LIVE). Pass 'none' to disable with stderr warning."
        ),
    )
    p.add_argument("--balance-cents", type=_positive_int, default=None,
                   help="Override the IMPLAUSIBLE_FILL balance reference "
                        "(must be ≥ 1c). Defaults to the latest "
                        "available_balance_cents in the corpus.")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--holdout-days", type=_positive_int,
                   default=DEFAULT_HOLDOUT_DAYS)
    p.add_argument("--stratified-fraction", type=_open_fraction,
                   default=DEFAULT_STRATIFIED_FRACTION)
    p.add_argument("--n-resamples", type=_positive_int,
                   default=DEFAULT_BOOTSTRAP_RESAMPLES)
    p.add_argument("--sigma-threshold", type=_nonneg_float,
                   default=DEFAULT_BOOTSTRAP_SIGMA_THRESHOLD)
    p.add_argument("--per-product-floor", type=_nonneg_int,
                   default=DEFAULT_PER_PRODUCT_TRADE_FLOOR)
    p.add_argument("--dd-multiplier", type=_ge_one_float,
                   default=DEFAULT_DD_BOUND_MULTIPLIER)
    p.add_argument("--dd-bound-balance-floor", type=_nonneg_float,
                   default=DEFAULT_DD_BOUND_BALANCE_FLOOR)
    p.add_argument("--implausible-fill-threshold", type=_open_fraction,
                   default=DEFAULT_IMPLAUSIBLE_FILL_THRESHOLD)
    p.add_argument("--quiet", action="store_true",
                   help="Print only the verdict line, not the full JSON.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cutoff = parse_cutoff(args.regime_cutoff)
    if cutoff is None:
        print(
            "WARNING: regime cutoff DISABLED — pre-cell-block-era data "
            "will be included. Yield rate may be inflated.",
            file=sys.stderr,
        )

    for path in (args.corpus, args.baseline):
        if not path.exists():
            print(f"ERROR: corpus path not found: {path}", file=sys.stderr)
            return EXIT_NOINPUT
    try:
        candidate = load_records_from_jsonl(args.corpus)
        baseline = load_records_from_jsonl(args.baseline)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        # R2-finding-2: TypeError covers `int(None)` and naive-vs-aware
        # datetime mismatches that can surface during corpus parse.
        print(f"ERROR: corpus parse failure: {exc}", file=sys.stderr)
        return EXIT_DATAERR

    try:
        result = evaluate(
            candidate, baseline,
            regime_cutoff=cutoff,
            balance_cents=args.balance_cents,
            holdout_days=args.holdout_days,
            stratified_fraction=args.stratified_fraction,
            seed=args.seed,
            n_resamples=args.n_resamples,
            sigma_threshold=args.sigma_threshold,
            per_product_floor=args.per_product_floor,
            dd_multiplier=args.dd_multiplier,
            dd_bound_balance_floor=args.dd_bound_balance_floor,
            implausible_fill_threshold=args.implausible_fill_threshold,
        )
    except (AssertionError, TypeError, statistics.StatisticsError) as exc:
        # R2-finding-2 + R3-finding-1: TypeError here means an unexpected
        # naive-vs-aware datetime slipped past load normalization;
        # StatisticsError means a degenerate input slipped past arg
        # validation (e.g., evaluate() called programmatically with
        # n_resamples=0). Surface as software bug rather than
        # masquerading as REJECT.
        print(f"INTERNAL ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_SOFTWARE

    if args.quiet:
        print(f"{result.verdict} (accepted={result.accepted})")
    else:
        print(json.dumps(result_to_dict(result), indent=2, default=str))

    return EXIT_ACCEPT if result.accepted else EXIT_REJECT


if __name__ == "__main__":
    sys.exit(main())
