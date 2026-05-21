"""S.2 — Spot-staleness vs PnL attribution (BNB / HYPE / DOGE since T4).

Plan: kb/decisions/bit-s-2-spot-staleness-rca-plan.md
ClickUp: 86ba1wrh7
Verdict doc: kb/findings/spot-staleness-pnl-attribution.md

Read-only. Idempotent. Mac-side. Reads /tmp/state.db (synced via
.claude/skills/references/db-sync.md); fetches Coinbase REST 1-min
candles per asset; buckets settled trades by proxy_staleness_s at
decision time; reports per-bucket n / WR + Wilson CI / net PnL / mean
|edge| + Pearson + Spearman correlation w/ bootstrap CI.

Per-row PnL is GROSS in `settled_trades.pnl_cents`; we net via
`SUM(pnl_cents - COALESCE(fee_cents, 0))` per scripts/CLAUDE.md +
feedback_use_corrected_pnl_always.md. Phantom-corrected pass adds
`delta_pnl_cents` from `phantom_corrections` (deduped by (ticker, side)
on MAX(detected_at) so multiple audit runs don't double-count).

CLI:
  python3 scripts/research/spot_staleness_pnl_attribution.py \
    --db /tmp/state.db --asset all --since 2026-05-14

  --asset {BNB,HYPE,DOGE,all}: which assets to score (default all).
  --since YYYY-MM-DD: T4 epoch (default 2026-05-14, HYPE/DOGE).
  --cache-dir PATH: candle cache dir (default data/research_cache/spot_staleness).
  --bootstrap-n N: bootstrap resamples for correlation CI (default 1000).
  --offline: skip Coinbase REST; require cache hit on every window.

Outputs:
  - Markdown summary to stdout.
  - CSV per-asset to <cache-dir>/<asset>_buckets.csv + <asset>_trades.csv.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Sequence

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# T4 promotion dates per CLAUDE.md (BNB 2026-05-19; HYPE/DOGE 2026-05-14).
ASSET_T4_DATE = {
    "BNB": "2026-05-19",
    "HYPE": "2026-05-14",
    "DOGE": "2026-05-14",
}

# Coinbase Exchange REST product slug per asset.
COINBASE_PRODUCT = {
    "BNB": "BNB-USD",
    "HYPE": "HYPE-USD",
    "DOGE": "DOGE-USD",
}

# Staleness buckets per plan-doc § Step 3.
# (low_inclusive_s, high_exclusive_s, label)
BUCKETS: list[tuple[float, float, str]] = [
    (0.0, 30.0, "0-30s"),
    (30.0, 120.0, "30-120s"),
    (120.0, 300.0, "120-300s"),
    (300.0, 600.0, "300-600s"),
    (600.0, math.inf, ">=600s"),
]

# Coinbase REST: 300-candle cap @ granularity=60 → 300 min = 5h per request.
COINBASE_GRANULARITY_S = 60
COINBASE_MAX_CANDLES = 300
# Rate-limit courtesy.
REST_INTERVAL_S = 0.1
REST_USER_AGENT = "kalshi-bot-s2-rca-spot-staleness/1.0"


# -----------------------------------------------------------------------------
# Statistics helpers
# -----------------------------------------------------------------------------

def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p_hat = wins / n
    denom = 1.0 + z * z / n
    centre = (p_hat + z * z / (2.0 * n)) / denom
    half = z * math.sqrt((p_hat * (1.0 - p_hat) + z * z / (4.0 * n)) / n) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation. Returns NaN on degenerate input."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx2 = sum((xs[i] - mx) ** 2 for i in range(n))
    dy2 = sum((ys[i] - my) ** 2 for i in range(n))
    den = math.sqrt(dx2 * dy2)
    if den == 0.0:
        return float("nan")
    return num / den


def _ranks(vs: Sequence[float]) -> list[float]:
    """Average-rank for ties."""
    n = len(vs)
    indexed = sorted(range(n), key=lambda i: vs[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vs[indexed[j + 1]] == vs[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-indexed
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation."""
    if len(xs) < 2 or len(xs) != len(ys):
        return float("nan")
    return pearson(_ranks(xs), _ranks(ys))


def bootstrap_corr_ci(
    xs: Sequence[float],
    ys: Sequence[float],
    n_resamples: int = 1000,
    seed: int = 42,
    method: str = "pearson",
) -> tuple[float, float]:
    """Bootstrap 95% CI for correlation. Returns (lo, hi)."""
    n = len(xs)
    if n < 3 or n_resamples < 1:
        return (float("nan"), float("nan"))
    fn = pearson if method == "pearson" else spearman
    rng = _LCG(seed)
    resamples: list[float] = []
    for _ in range(n_resamples):
        rs_x = [0.0] * n
        rs_y = [0.0] * n
        for i in range(n):
            idx = rng.randint(0, n - 1)
            rs_x[i] = xs[idx]
            rs_y[i] = ys[idx]
        c = fn(rs_x, rs_y)
        if not math.isnan(c):
            resamples.append(c)
    if not resamples:
        return (float("nan"), float("nan"))
    resamples.sort()
    lo = resamples[int(0.025 * len(resamples))]
    hi = resamples[int(0.975 * len(resamples))]
    return (lo, hi)


class _LCG:
    """Tiny deterministic RNG so we don't depend on numpy/random module state."""

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def randint(self, lo: int, hi: int) -> int:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return lo + self.state % (hi - lo + 1)


# -----------------------------------------------------------------------------
# DB I/O
# -----------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def fetch_trades(
    conn: sqlite3.Connection,
    *,
    asset: str,
    since_iso_date: str,
) -> list[dict]:
    """Pull settled trades (T4 window) + LEFT JOIN phantom_corrections.

    Dedup of phantom_corrections: take MAX(detected_at) per (ticker, side)
    so duplicate audit runs (e.g., `may18` + `backfill-2026-05-19`) don't
    double-count when both rows have the same delta.

    For decision_time: take MIN(evaluation_time) on `candidate` OR
    `decided_contract_*` filter_stage rows per ticker (the earliest of
    these two = first decision moment). Mainline (BNB / non-STC HYPE /
    non-STC DOGE) goes through `candidate` only; STC cell-block-hits
    add `decided_contract_*` variants. When no decision row exists
    (rare; pre-Bit-2.1a-era orphan or orphan-DB-watchdog ghost), the
    decision_time is NULL and we fall back to `settled_at - 900s`
    (15min window open) — those rows are surfaced in the per-asset
    `n_fallback_decision_time` counter.
    """
    rows = conn.execute(
        """
        WITH
          pc_latest AS (
            -- Dedup phantom_corrections: take MAX(detected_at) per
            -- (ticker, side) so multiple audit runs (e.g., `may18` +
            -- `backfill-2026-05-19` + `auto-2026-05-19`) don't multiply.
            SELECT ticker, side,
                   delta_count, delta_pnl_cents,
                   detected_at,
                   ROW_NUMBER() OVER (
                     PARTITION BY ticker, side
                     ORDER BY detected_at DESC
                   ) AS rn
            FROM phantom_corrections
          ),
          st_anchor AS (
            -- Settled-trades has one row per fill, so a (ticker, side)
            -- with multiple fills appears multiple times. The
            -- phantom_corrections.delta_pnl_cents is a TICKER-level
            -- adjustment (matches sum-of-local vs Kalshi-truth on the
            -- ticker), so we apply it to the earliest row only via
            -- this anchor flag — else the delta multiplies by n-fills.
            SELECT ticker, side, MIN(settled_at) AS first_settled_at
            FROM settled_trades
            GROUP BY ticker, side
          ),
          decision AS (
            -- Decision moment = earliest of `candidate` (mainline) or
            -- `decided_contract_*` (STC cell-block) per ticker. BNB went
            -- T4 2026-05-19 and trades through mainline, so `candidate`
            -- is the canonical decision-time signal for BNB; HYPE/DOGE
            -- can route either path depending on STC cell-block hits.
            SELECT ticker, MIN(evaluation_time) AS decision_time
            FROM evaluated_opportunities
            WHERE filter_stage = 'candidate'
               OR filter_stage LIKE 'decided_contract%'
            GROUP BY ticker
          )
        SELECT
          st.ticker, st.asset, st.product_type, st.settled_at, st.side,
          st.pnl_cents, st.fee_cents,
          st.entry_price_cents, st.count, st.market_result,
          st.calibrated_prob, st.edge,
          -- Attach phantom delta to ONLY the earliest settled_trades row
          -- per (ticker, side) so the ticker-level adjustment is applied
          -- once across n-fills. Later fills get NULL → 0 contribution.
          CASE WHEN st.settled_at = a.first_settled_at
               THEN pc.delta_count ELSE NULL END AS delta_count,
          CASE WHEN st.settled_at = a.first_settled_at
               THEN pc.delta_pnl_cents ELSE NULL END AS delta_pnl_cents,
          d.decision_time
        FROM settled_trades st
        JOIN st_anchor a
          ON a.ticker = st.ticker AND a.side = st.side
        LEFT JOIN pc_latest pc
          ON pc.ticker = st.ticker
         AND pc.side = st.side
         AND pc.rn = 1
        LEFT JOIN decision d ON d.ticker = st.ticker
        WHERE st.asset = ?
          AND st.product_type = '15m'
          AND st.settled_at >= ?
        ORDER BY st.settled_at;
        """,
        (asset, since_iso_date),
    ).fetchall()
    return [dict(r) for r in rows]


# -----------------------------------------------------------------------------
# Coinbase REST candle fetch + cache
# -----------------------------------------------------------------------------

def _parse_iso(ts: str) -> dt.datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return dt.datetime.fromisoformat(ts)


def _floor_to_minute(epoch_s: float) -> int:
    return int(epoch_s) // 60 * 60


def _coinbase_url(product: str, start_iso: str, end_iso: str) -> str:
    return (
        f"https://api.exchange.coinbase.com/products/{product}/candles"
        f"?granularity={COINBASE_GRANULARITY_S}&start={start_iso}&end={end_iso}"
    )


def fetch_coinbase_candles(
    product: str, start_epoch: int, end_epoch: int
) -> list[tuple[int, float, float, float, float, float]]:
    """Fetch Coinbase 1-min candles for [start, end] (epoch seconds, UTC).

    Returns list of (open_ts, low, high, open, close, volume). Coinbase
    returns DESC by ts; we re-sort ASC. 300-candle cap per request enforced
    by caller.
    """
    start_iso = dt.datetime.fromtimestamp(start_epoch, tz=dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    end_iso = dt.datetime.fromtimestamp(end_epoch, tz=dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    url = _coinbase_url(product, start_iso, end_iso)
    req = urllib.request.Request(url, headers={"User-Agent": REST_USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        # Coinbase error format: {"message": "..."}
        raise RuntimeError(f"Coinbase returned non-list for {product}: {data}")
    out = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in data]
    out.sort(key=lambda x: x[0])
    return out


def _cache_path(cache_dir: Path, product: str, chunk_start: int) -> Path:
    return cache_dir / product / f"{chunk_start}.json"


def get_candles_cached(
    product: str,
    decision_epochs: Sequence[int],
    cache_dir: Path,
    *,
    offline: bool = False,
    log_fn=print,
) -> dict[int, int]:
    """Return mapping `decision_epoch -> latest_candle_open_ts` for product.

    Fetches Coinbase REST in 300-min chunks aligned to the decision times,
    cached to disk for idempotency. Each chunk = [chunk_start, chunk_start+300*60).
    For each decision epoch we look back up to LOOKBACK_S=600s into the cache
    and pick max(open_ts) where open_ts <= decision_epoch.
    """
    LOOKBACK_S = 600  # 10-min lookback; per plan-doc § Step 2 / Step 3 extreme bucket.
    cache_dir = Path(cache_dir)
    (cache_dir / product).mkdir(parents=True, exist_ok=True)

    # Group decision epochs by chunk so we fetch each chunk once.
    chunk_window = COINBASE_MAX_CANDLES * COINBASE_GRANULARITY_S  # 18000s = 5h
    needed_chunks: dict[int, list[int]] = {}
    for ep in decision_epochs:
        # The chunk that covers [ep - LOOKBACK_S, ep]. Anchor at floor-multiple
        # of chunk_window so cache keys are stable across runs.
        anchor = ((ep - LOOKBACK_S) // chunk_window) * chunk_window
        # The lookback might straddle a chunk boundary; add the next one too.
        needed_chunks.setdefault(anchor, []).append(ep)
        if ep > anchor + chunk_window - 60:
            needed_chunks.setdefault(anchor + chunk_window, []).append(ep)

    # Load / fetch each chunk.
    chunk_candles: dict[int, list[tuple[int, ...]]] = {}
    for chunk_start in sorted(needed_chunks):
        cp = _cache_path(cache_dir, product, chunk_start)
        if cp.exists():
            with cp.open("r") as fh:
                chunk_candles[chunk_start] = [tuple(c) for c in json.load(fh)]
            continue
        if offline:
            raise RuntimeError(
                f"--offline set but no cache for {product} chunk {chunk_start}"
            )
        # Fetch.
        attempt = 0
        while True:
            attempt += 1
            try:
                candles = fetch_coinbase_candles(
                    product, chunk_start, chunk_start + chunk_window
                )
                break
            except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as e:
                if attempt >= 3:
                    raise
                log_fn(f"  [retry {attempt}] {product} {chunk_start}: {e}")
                time.sleep(1.0 * attempt)
        with cp.open("w") as fh:
            json.dump(candles, fh)
        chunk_candles[chunk_start] = [tuple(c) for c in candles]
        time.sleep(REST_INTERVAL_S)

    # Resolve each decision epoch to its latest qualifying candle open_ts.
    out: dict[int, int] = {}
    for ep in decision_epochs:
        if ep in out:
            continue
        candidates: list[int] = []
        for anchor in (
            ((ep - LOOKBACK_S) // chunk_window) * chunk_window,
            (((ep - LOOKBACK_S) // chunk_window) + 1) * chunk_window,
        ):
            if anchor not in chunk_candles:
                continue
            for c in chunk_candles[anchor]:
                open_ts = int(c[0])
                if ep - LOOKBACK_S <= open_ts <= ep:
                    candidates.append(open_ts)
        if candidates:
            out[ep] = max(candidates)
        else:
            # No candle in the 10-min lookback → "extreme" bucket sentinel.
            # We encode this as `open_ts = ep - 9999` so staleness = 9999s (well past 600s).
            out[ep] = ep - 9999
    return out


# -----------------------------------------------------------------------------
# Bucket aggregation
# -----------------------------------------------------------------------------

def bucket_index(stale_s: float) -> int:
    for i, (lo, hi, _) in enumerate(BUCKETS):
        if lo <= stale_s < hi:
            return i
    return len(BUCKETS) - 1


def aggregate_buckets(
    rows: Iterable[dict],
    *,
    phantom_corrected: bool,
) -> list[dict]:
    """Aggregate per-bucket stats from enriched trade rows.

    Each row must have keys: bucket_idx, pnl_cents, fee_cents (may be None),
    delta_pnl_cents (may be None), delta_count (may be None), count,
    market_result, side, edge, staleness_s.

    Returns list[dict] one per bucket, in BUCKETS order.
    """
    buckets: list[dict] = [
        {
            "label": label,
            "lo_s": lo,
            "hi_s": hi,
            "n": 0,
            "wins": 0,
            "losses": 0,
            "net_pnl_cents": 0,
            "abs_edges": [],
            "staleness_vals": [],
        }
        for (lo, hi, label) in BUCKETS
    ]
    for r in rows:
        b = buckets[r["bucket_idx"]]
        b["n"] += 1
        # Net per-row PnL: pnl_cents - fee_cents (settled_trades is GROSS).
        net = int(r["pnl_cents"]) - int(r.get("fee_cents") or 0)
        if phantom_corrected and r.get("delta_pnl_cents") is not None:
            net += int(r["delta_pnl_cents"])
        b["net_pnl_cents"] += net
        result = (r.get("market_result") or "").lower()
        side = (r.get("side") or "").lower()
        # YES side wins on result=yes; NO side wins on result=no.
        if (side == "yes" and result == "yes") or (side == "no" and result == "no"):
            b["wins"] += 1
        elif result in {"yes", "no"}:
            b["losses"] += 1
        if r.get("edge") is not None:
            b["abs_edges"].append(abs(float(r["edge"])))
        b["staleness_vals"].append(float(r["staleness_s"]))
    for b in buckets:
        n = b["n"]
        b["mean_pnl_cents"] = (b["net_pnl_cents"] / n) if n else 0.0
        decided = b["wins"] + b["losses"]
        b["wr"] = (b["wins"] / decided) if decided else float("nan")
        wr_lo, wr_hi = wilson_ci(b["wins"], decided) if decided else (float("nan"), float("nan"))
        b["wr_lo"] = wr_lo
        b["wr_hi"] = wr_hi
        b["mean_abs_edge"] = (sum(b["abs_edges"]) / len(b["abs_edges"])) if b["abs_edges"] else float("nan")
        b["mean_staleness_s"] = (sum(b["staleness_vals"]) / len(b["staleness_vals"])) if b["staleness_vals"] else float("nan")
        # Free large lists from output.
        del b["abs_edges"]
        del b["staleness_vals"]
    return buckets


# -----------------------------------------------------------------------------
# Per-asset pipeline
# -----------------------------------------------------------------------------

def run_asset(
    asset: str,
    *,
    conn: sqlite3.Connection,
    since: str,
    cache_dir: Path,
    bootstrap_n: int,
    offline: bool,
    log_fn=print,
) -> dict:
    log_fn(f"[{asset}] fetching trades since {since}...")
    trades = fetch_trades(conn, asset=asset, since_iso_date=since)
    log_fn(f"[{asset}] n={len(trades)} trades")
    if not trades:
        return {"asset": asset, "n": 0, "buckets_raw": [], "buckets_corrected": [], "trades": []}

    # Resolve decision_time per trade; fall back to settled_at - 900s on NULL.
    decision_epochs: list[int] = []
    for t in trades:
        if t.get("decision_time"):
            t["_dt_iso"] = t["decision_time"]
            t["_dt_epoch"] = int(_parse_iso(t["decision_time"]).timestamp())
            t["_dt_source"] = "decided_contract"
        else:
            settled_ep = int(_parse_iso(t["settled_at"]).timestamp())
            t["_dt_epoch"] = settled_ep - 900  # 15min window open
            t["_dt_iso"] = dt.datetime.fromtimestamp(t["_dt_epoch"], tz=dt.timezone.utc).isoformat()
            t["_dt_source"] = "fallback_settled_minus_900s"
        decision_epochs.append(t["_dt_epoch"])

    product = COINBASE_PRODUCT[asset]
    log_fn(f"[{asset}] fetching Coinbase candles for {len(set(decision_epochs))} decision epochs...")
    candle_map = get_candles_cached(
        product, sorted(set(decision_epochs)), cache_dir, offline=offline, log_fn=log_fn
    )

    # Enrich trades with proxy_staleness_s + bucket_idx.
    n_extreme = 0
    fallback_decision = 0
    for t in trades:
        ep = t["_dt_epoch"]
        latest_open = candle_map[ep]
        stale = float(ep - latest_open)
        if stale > 600 + 1:
            n_extreme += 1
        t["staleness_s"] = stale
        t["bucket_idx"] = bucket_index(stale)
        if t["_dt_source"] != "decided_contract":
            fallback_decision += 1
    log_fn(
        f"[{asset}] n_extreme_no_candle={n_extreme}; "
        f"fallback_decision_time={fallback_decision}"
    )

    buckets_raw = aggregate_buckets(trades, phantom_corrected=False)
    buckets_corrected = aggregate_buckets(trades, phantom_corrected=True)

    # Slope tests.
    stale_vals = [t["staleness_s"] for t in trades]
    net_vals = []
    for t in trades:
        v = int(t["pnl_cents"]) - int(t.get("fee_cents") or 0)
        if t.get("delta_pnl_cents") is not None:
            v += int(t["delta_pnl_cents"])
        net_vals.append(float(v))
    p = pearson(stale_vals, net_vals)
    p_ci = bootstrap_corr_ci(stale_vals, net_vals, n_resamples=bootstrap_n, method="pearson")
    s = spearman(stale_vals, net_vals)
    s_ci = bootstrap_corr_ci(stale_vals, net_vals, n_resamples=bootstrap_n, method="spearman")

    # Aggregate totals (raw vs phantom-corrected).
    total_raw = sum(int(t["pnl_cents"]) - int(t.get("fee_cents") or 0) for t in trades)
    total_corrected = sum(
        int(t["pnl_cents"]) - int(t.get("fee_cents") or 0) + int(t.get("delta_pnl_cents") or 0)
        for t in trades
    )
    n_phantom = sum(1 for t in trades if t.get("delta_pnl_cents") is not None)

    return {
        "asset": asset,
        "since": since,
        "n": len(trades),
        "n_phantom_corrections": n_phantom,
        "n_extreme_no_candle": n_extreme,
        "n_fallback_decision_time": fallback_decision,
        "buckets_raw": buckets_raw,
        "buckets_corrected": buckets_corrected,
        "total_raw_cents": total_raw,
        "total_corrected_cents": total_corrected,
        "pearson_r": p,
        "pearson_ci": p_ci,
        "spearman_r": s,
        "spearman_ci": s_ci,
        "trades": trades,
    }


# -----------------------------------------------------------------------------
# Markdown / CSV output
# -----------------------------------------------------------------------------

def _fmt_cents(c: int | float) -> str:
    return f"${c/100:+,.2f}"


def _fmt_pct(p: float) -> str:
    if math.isnan(p):
        return "n/a"
    return f"{p*100:.1f}%"


def _fmt_corr(r: float, ci: tuple[float, float]) -> str:
    if math.isnan(r):
        return "n/a"
    lo, hi = ci
    if math.isnan(lo) or math.isnan(hi):
        return f"{r:+.3f} (CI n/a)"
    return f"{r:+.3f} [{lo:+.3f}, {hi:+.3f}]"


def bucket_table_md(buckets: list[dict], title: str) -> str:
    out = [f"\n#### {title}\n"]
    out.append("| Bucket | n | net PnL | mean PnL | W-L | WR (95% CI) | mean |edge| | mean stale (s) |")
    out.append("|---|---:|---:|---:|---|---|---:|---:|")
    for b in buckets:
        wr_str = "n/a" if math.isnan(b["wr"]) else f"{_fmt_pct(b['wr'])} [{_fmt_pct(b['wr_lo'])}, {_fmt_pct(b['wr_hi'])}]"
        ms = "n/a" if math.isnan(b["mean_staleness_s"]) else f"{b['mean_staleness_s']:.1f}"
        me = "n/a" if math.isnan(b["mean_abs_edge"]) else f"{b['mean_abs_edge']:.3f}"
        out.append(
            f"| {b['label']} | {b['n']} | {_fmt_cents(b['net_pnl_cents'])} | "
            f"{_fmt_cents(b['mean_pnl_cents'])} | {b['wins']}-{b['losses']} | "
            f"{wr_str} | {me} | {ms} |"
        )
    return "\n".join(out) + "\n"


def render_asset_md(result: dict) -> str:
    asset = result["asset"]
    if result["n"] == 0:
        return f"\n### {asset}\nNo settled trades in window — honest-NULL.\n"
    out = [f"\n### {asset} (since {result['since']}, n={result['n']})\n"]
    out.append(f"- n_phantom_corrections: {result['n_phantom_corrections']}")
    out.append(f"- n_extreme_no_candle (proxy staleness >600s): {result['n_extreme_no_candle']}")
    out.append(f"- n_fallback_decision_time (no decided_contract row): {result['n_fallback_decision_time']}")
    out.append(f"- total_raw_net_pnl: {_fmt_cents(result['total_raw_cents'])}")
    out.append(f"- total_phantom_corrected_pnl: {_fmt_cents(result['total_corrected_cents'])}")
    out.append(
        f"- phantom_correction_delta: "
        f"{_fmt_cents(result['total_corrected_cents'] - result['total_raw_cents'])}"
    )
    out.append(
        f"- Pearson r(staleness_s, net_pnl_cents): "
        f"{_fmt_corr(result['pearson_r'], result['pearson_ci'])}"
    )
    out.append(
        f"- Spearman ρ(staleness_s, net_pnl_cents): "
        f"{_fmt_corr(result['spearman_r'], result['spearman_ci'])}"
    )
    out.append(bucket_table_md(result["buckets_raw"], "Buckets — raw (pre-phantom)"))
    out.append(bucket_table_md(result["buckets_corrected"], "Buckets — phantom-corrected"))
    return "\n".join(out)


def write_csv(result: dict, cache_dir: Path) -> tuple[Path, Path]:
    asset = result["asset"]
    out_dir = cache_dir / "_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    buckets_path = out_dir / f"{asset}_buckets.csv"
    trades_path = out_dir / f"{asset}_trades.csv"
    with buckets_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "label", "lo_s", "hi_s", "n", "wins", "losses",
            "wr", "wr_lo", "wr_hi", "net_pnl_cents", "mean_pnl_cents",
            "mean_abs_edge", "mean_staleness_s", "phantom_corrected",
        ])
        for b in result["buckets_raw"]:
            w.writerow([
                b["label"], b["lo_s"], b["hi_s"], b["n"], b["wins"], b["losses"],
                b["wr"], b["wr_lo"], b["wr_hi"], b["net_pnl_cents"], b["mean_pnl_cents"],
                b["mean_abs_edge"], b["mean_staleness_s"], 0,
            ])
        for b in result["buckets_corrected"]:
            w.writerow([
                b["label"], b["lo_s"], b["hi_s"], b["n"], b["wins"], b["losses"],
                b["wr"], b["wr_lo"], b["wr_hi"], b["net_pnl_cents"], b["mean_pnl_cents"],
                b["mean_abs_edge"], b["mean_staleness_s"], 1,
            ])
    with trades_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "ticker", "asset", "settled_at", "side", "market_result",
            "pnl_cents", "fee_cents", "delta_pnl_cents", "delta_count",
            "calibrated_prob", "edge",
            "decision_time", "decision_time_source", "staleness_s", "bucket",
        ])
        for t in result["trades"]:
            w.writerow([
                t["ticker"], t["asset"], t["settled_at"], t.get("side"),
                t.get("market_result"),
                t["pnl_cents"], t.get("fee_cents"),
                t.get("delta_pnl_cents"), t.get("delta_count"),
                t.get("calibrated_prob"), t.get("edge"),
                t["_dt_iso"], t["_dt_source"],
                t["staleness_s"], BUCKETS[t["bucket_idx"]][2],
            ])
    return buckets_path, trades_path


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="/tmp/state.db", help="path to state.db (default /tmp/state.db)")
    p.add_argument(
        "--asset",
        default="all",
        choices=["BNB", "HYPE", "DOGE", "all"],
        help="asset to score; 'all' iterates over BNB+HYPE+DOGE",
    )
    p.add_argument(
        "--since",
        default=None,
        help="ISO date YYYY-MM-DD for trade window start (default: per-asset T4 date)",
    )
    p.add_argument(
        "--cache-dir",
        default="data/research_cache/spot_staleness",
        help="candle cache + CSV output dir",
    )
    p.add_argument("--bootstrap-n", type=int, default=1000)
    p.add_argument("--offline", action="store_true", help="forbid Coinbase REST calls")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = args.db
    if not os.path.exists(db_path):
        sys.stderr.write(f"ERROR: state.db not found at {db_path}\n")
        sys.stderr.write("       Run db-sync first: scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db\n")
        return 2
    cache_dir = Path(args.cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    assets = ["BNB", "HYPE", "DOGE"] if args.asset == "all" else [args.asset]
    conn = _connect(db_path)
    try:
        out_md: list[str] = ["# S.2 — Spot-staleness PnL attribution (raw run output)\n"]
        out_md.append(f"\nDB: `{db_path}` (mtime: {dt.datetime.fromtimestamp(os.path.getmtime(db_path), tz=dt.timezone.utc).isoformat()})")
        out_md.append(f"Bootstrap resamples: {args.bootstrap_n}")
        results: list[dict] = []
        for asset in assets:
            since = args.since or ASSET_T4_DATE[asset]
            result = run_asset(
                asset,
                conn=conn,
                since=since,
                cache_dir=cache_dir,
                bootstrap_n=args.bootstrap_n,
                offline=args.offline,
            )
            results.append(result)
            out_md.append(render_asset_md(result))
            b_path, t_path = write_csv(result, cache_dir)
            out_md.append(f"\nCSVs: `{b_path}` + `{t_path}`")
        print("\n".join(out_md))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
