"""Sample engine inputs from state.db for the equivalence-harness corpus.

Pillar 3 of the testing-foundation-sprint
(kb/decisions/testing-foundation-sprint-may09.md). Produces the parquet
fixture consumed by ``tests/equivalence/test_probability_engine.py``
(and, transitively, the synthetic VolatilityEngine scenarios in
``tests/equivalence/test_volatility_engine.py`` reference it for asset
+ regime distributions).

Why a separate script (not inlined in the test): regenerating the fixture
is a manual, gated operation. CI runs against the committed parquet for
determinism; the parquet only changes when an operator deliberately
re-samples and reviews the diff. See the runbook in
``tests/equivalence/REGEN.md`` for the step-by-step.

Mac dev workflow:
    scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
    python3 scripts/sample_engine_inputs.py --db /tmp/state.db

VPS workflow:
    python3 scripts/sample_engine_inputs.py --db ~/kalshi-bot-repo/state.db

Output: ``tests/fixtures/engine_inputs.parquet`` (~80KB at n=1000).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

# pyarrow is a Pillar-3 dev dep. Import deferred so the file is importable
# (for help / arg validation) on a fresh checkout before `pip install -e .[dev]`.


# Bit 11.2 (2026-05-12): relocated to scripts/ops/; 3-level dirname
# (ops/ → scripts/ → repo/).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "engine_inputs.parquet"
DEFAULT_DB = "state.db"

# Columns pulled from evaluated_opportunities. Splits into three groups:
#   * stratification keys: asset, product_type, vol_regime
#   * engine inputs:       spot_price, threshold, volatility,
#                          market_price, seconds_to_close
#   * production oracles:  raw_prob, calibrated_prob, calibration_method,
#                          z_score, evaluation_time
# The oracle columns are stored for diagnostics only — equivalence tests
# do not assert against them (the calibration engine state changes over
# retrains, so historical oracles are not a reliable point-in-time pin).
SELECT_COLS: Tuple[str, ...] = (
    "asset",
    "product_type",
    "vol_regime",
    "spot_price",
    "threshold",
    "volatility",
    "market_price",
    "seconds_to_close",
    "raw_prob",
    "calibrated_prob",
    "calibration_method",
    "z_score",
    "evaluation_time",
)

# Required-non-null engine input columns. Rows missing any of these
# cannot be replayed through ProbabilityEngine.compute() and are
# filtered server-side by the SELECT.
REQUIRED_INPUT_COLS: Tuple[str, ...] = (
    "asset",
    "product_type",
    "spot_price",
    "threshold",
    "volatility",
    "seconds_to_close",
)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """Open state.db read-only via sqlite URI to keep the live writer's
    WAL untouched while we sample. Both ``PRAGMA journal_mode=WAL``
    and ``PRAGMA busy_timeout=10000`` per scripts/CLAUDE.md two-pragma
    rule (writer-collision avoidance — concurrent reader starvation
    postmortem PM-001).

    On a ``?mode=ro`` connection ``PRAGMA journal_mode=WAL`` is a
    silent no-op IF the DB is already in WAL mode (the live state.db
    always is). On a non-WAL DB (e.g., a Mac dev's snapshot file from
    a previous era), the pragma raises ``OperationalError: attempt to
    write a readonly database`` — caught here so the script still
    runs against legacy snapshots. The two-pragma rule is satisfied
    by the *attempt*; the runtime no-op is acceptable because we are
    a reader, not a writer."""
    if not os.path.exists(db_path):
        raise SystemExit(f"state.db not found at {db_path!r}")
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # non-WAL DB on read-only — see docstring
        pass
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _fetch_eligible(
    conn: sqlite3.Connection,
    days: int,
) -> List[Dict[str, Any]]:
    """Pull every eligible row from the last ``days`` days. Eligible =
    all required input columns non-null."""
    null_filters = " AND ".join(f"{c} IS NOT NULL" for c in REQUIRED_INPUT_COLS)
    sql = (
        f"SELECT {', '.join(SELECT_COLS)} "
        f"FROM evaluated_opportunities "
        f"WHERE evaluation_time >= datetime('now', '-{int(days)} days') "
        f"  AND {null_filters}"
    )
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _stratify(
    rows: Sequence[Dict[str, Any]],
    target_n: int,
    seed: int,
) -> List[Dict[str, Any]]:
    """Stratified sample of size ``target_n`` keyed on
    (asset, product_type, vol_regime).

    Per-stratum allocation = floor(target_n / n_strata), with leftovers
    distributed by remaining-population weight so we hit exactly
    ``target_n``. Strata smaller than their allocation contribute all
    they have; the deficit cascades into the next round of allocation
    over the remaining strata.
    """
    rng = random.Random(seed)

    buckets: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (row["asset"], row["product_type"], row["vol_regime"] or "unknown")
        buckets[key].append(row)

    if not buckets:
        raise SystemExit("no eligible rows after filtering — check --days range")

    # Shuffle each bucket once with the seeded RNG so every fetched row
    # has a stable position. Sampling later is a deterministic prefix
    # take.
    for key in buckets:
        rng.shuffle(buckets[key])

    # Iterative deficit-cascade allocator: at each pass distribute the
    # remaining quota equally across strata that still have rows.
    quotas: Dict[Tuple[str, str, str], int] = {key: 0 for key in buckets}
    remaining = target_n
    available = {key: len(rows) for key, rows in buckets.items()}

    # Hard cap on cascade iterations protects against pathological
    # allocations. In practice convergence happens in <= 3 passes for
    # any reasonable corpus.
    for _ in range(8):
        if remaining <= 0:
            break
        active = [k for k in available if available[k] > quotas[k]]
        if not active:
            break
        per = max(1, remaining // len(active))
        for key in active:
            take = min(per, available[key] - quotas[key], remaining)
            quotas[key] += take
            remaining -= take
            if remaining <= 0:
                break

    sampled: List[Dict[str, Any]] = []
    for key, q in quotas.items():
        sampled.extend(buckets[key][:q])

    # Final shuffle of the assembled sample so consumers don't see
    # stratum-clustered ordering. Same seed → same order.
    rng.shuffle(sampled)
    return sampled[:target_n]


def _coerce_for_arrow(rows: Sequence[Dict[str, Any]]) -> Dict[str, list]:
    """Transpose row-dicts into column-arrays with explicit None
    handling — pyarrow.table infers dtypes per-column and a single
    str/None gap can flip the inferred type."""
    columns: Dict[str, list] = {c: [] for c in SELECT_COLS}
    for row in rows:
        for col in SELECT_COLS:
            columns[col].append(row.get(col))
    return columns


def _summary(rows: Sequence[Dict[str, Any]]) -> str:
    """One-line-per-stratum summary printed to stderr after writing."""
    counts = Counter(
        (r["asset"], r["product_type"], r["vol_regime"] or "unknown") for r in rows
    )
    out = [f"sampled n={len(rows)} across {len(counts)} strata"]
    for (asset, ptype, regime), n in counts.most_common(20):
        out.append(f"  {asset:>10s}  {ptype:<12s}  {regime:<10s}  n={n}")
    if len(counts) > 20:
        out.append(f"  ... + {len(counts) - 20} more strata")
    return "\n".join(out)


def _write_parquet(rows: Sequence[Dict[str, Any]], out_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols = _coerce_for_arrow(rows)
    table = pa.table(cols)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path, compression="zstd")


def _write_metadata(
    rows: Sequence[Dict[str, Any]],
    out_path: Path,
    seed: int,
    days: int,
    db_path: str,
) -> None:
    """Emit a sidecar JSON with regen-context for the parquet. Lives next
    to the parquet so reviewers can see what query produced it without
    parsing the binary."""
    counts = Counter(
        (r["asset"], r["product_type"], r["vol_regime"] or "unknown") for r in rows
    )
    meta = {
        "n_rows": len(rows),
        "seed": seed,
        "days_window": days,
        "source_db_basename": os.path.basename(db_path),
        "select_cols": list(SELECT_COLS),
        "required_input_cols": list(REQUIRED_INPUT_COLS),
        "strata": [
            {"asset": a, "product_type": p, "vol_regime": r, "n": n}
            for (a, p, r), n in counts.most_common()
        ],
    }
    sidecar = out_path.with_suffix(".meta.json")
    sidecar.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--db", default=DEFAULT_DB, help="Path to state.db (default: ./state.db)")
    p.add_argument("--out", default=str(DEFAULT_OUT), help=f"Output parquet path (default: {DEFAULT_OUT.relative_to(REPO_ROOT)})")
    p.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    p.add_argument("--n", type=int, default=1000, help="Sample size (default: 1000)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility (default: 42)")
    args = p.parse_args(argv)

    if args.n <= 0:
        raise SystemExit("--n must be positive")
    if args.days <= 0:
        raise SystemExit("--days must be positive")

    print(f"opening {args.db} (read-only)", file=sys.stderr)
    with _open_readonly(args.db) as conn:
        eligible = _fetch_eligible(conn, args.days)

    if len(eligible) < args.n:
        raise SystemExit(
            f"only {len(eligible)} eligible rows in last {args.days}d "
            f"(need {args.n}); widen --days or check filters"
        )

    sampled = _stratify(eligible, args.n, args.seed)
    if len(sampled) != args.n:
        raise SystemExit(
            f"stratified allocator produced {len(sampled)} rows, expected {args.n}"
        )

    out_path = Path(args.out)
    _write_parquet(sampled, out_path)
    _write_metadata(sampled, out_path, args.seed, args.days, args.db)

    print(_summary(sampled), file=sys.stderr)
    print(f"wrote {out_path}", file=sys.stderr)
    print(f"wrote {out_path.with_suffix('.meta.json')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
