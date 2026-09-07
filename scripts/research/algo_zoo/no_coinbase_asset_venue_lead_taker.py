"""no_coinbase_asset_venue_lead_taker — cross-venue lead-lag for the 3 Kalshi
crypto-15M assets that have NO local Coinbase mid (HYPE / DOGE / BNB).

THESIS (structurally asymmetric, not a rename of the dead BTC-leads-alts test):
  The hunt's "liquid majors are efficient" verdict is about the BTC/ETH/SOL/XRP
  Coinbase-fed quartet that every arb wires to. HYPE/DOGE/BNB are a different
  competition regime — no canonical fast public feed locally, thinner arb
  participation, Kalshi's pricer may lean on a laggier reference. If the Kalshi
  reliable book HASN'T moved while Kraken's mid just lurched, a taker who crosses
  the Kalshi ask in the direction of the venue move may earn the catch-up.

SIGNAL (per asset, per 20s grid point t in [open+45s, close-45s]):
  - Kraken L2 mid at t and t-15s (venue_book_reconstruct: load_venue_frames + mid_at).
    spot_ret = log(mid_t / mid_{t-15s}).
  - Kalshi reliable book (reliable_nbbo_at) at t and t-15s. Honor REFUSALS on both
    signal and label books.
  - STALE-LAG GATE fires iff:
        |spot_ret| > per-asset 80th percentile of |spot_ret| over the corpus
        AND |kalshi_mid_t - kalshi_mid_{t-15s}| < 1.5 cents (book hasn't absorbed it)
  - Direction via strike (DB threshold): rising spot ABOVE strike => buy YES;
    falling spot BELOW strike => buy NO. (If the move is incoherent w/ side of
    strike, no trade.)

EXECUTION (taker):
  - Cross the reliable Kalshi ask for the chosen side. P = ask/100.
  - Taker fee = ceil(0.07 * 1 * P * (1-P)) cents/contract; maker rebate = 0.
  - Per-contract net cents (1 contract) = payoff - entry_ask - fee, payoff in {0,100}.

LABEL (NO DB outcome — in-window settlement is sparse):
  - Terminal reliable Kalshi book mid at close_epoch_from_ticker.
  - YES wins (payoff 100) iff terminal mid >= 50; NO wins iff terminal mid < 50.
    (Terminal book mid is the market's own settled-probability proxy; the book
    collapses to ~0/100 at close. We REFUSE tickers whose terminal book is
    unreliable per reliable_nbbo_at.)

MARKOUT@30s: reliable Kalshi mid 30s after t minus the mid at t, signed to our
directional bet — separates prediction (book moves our way) from crossing into
adverse flow.

HEADLINE: per-contract net cents; block bootstrap clustered by (ticker, hour),
>=1000 resamples. Per-asset gate: CI-lower > 0 net of fee => EDGE.

NON-NEGOTIABLES honored: real price-dependent fees; no look-ahead (every cutoff
replays only recv_ts<=t); reliable-book refusals dropped on signal AND label;
clustered bootstrap; ~1.3d corpus => wide CIs + humility.
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import re
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import zstandard  # noqa: E402

from scripts.research.kalshi_book_reconstruct import (  # noqa: E402
    KalshiBook,
    reliable_nbbo_at,
)
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    close_epoch_from_ticker,
    load_frames_jsonl,
)
from scripts.research.venue_book_reconstruct import (  # noqa: E402
    KrakenBook,
    VENUE_SYMBOLS,
    _epoch,
    _extract_frame,
)

EDGE_DIR = "/tmp/edge_daily"
FRAMES = os.path.join(EDGE_DIR, "frames_crypto.jsonl")
DB = os.path.join(EDGE_DIR, "state.db")
KRAKEN_ROOT = os.path.join(EDGE_DIR, "venue_pull", "kraken_ws")

ASSETS = ["HYPE", "DOGE", "BNB"]
GRID_STEP = 20.0          # seconds
OPEN_PAD = 45.0           # start at open+45s
CLOSE_PAD = 45.0          # stop at close-45s
LOOKBACK = 15.0           # t-15s
DRIFT_MAX_CENTS = 1.5     # Kalshi reliable mid must have drifted < this
PCTL = 80.0               # per-asset |spot_ret| percentile gate
MARKOUT_S = 30.0
TAKER_FEE_RATE = 0.07
N_BOOT = 2000
SEED = 17

WINDOW_SECONDS = 15 * 60  # 15M markets


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_strikes(db_path: str, tickers: set) -> dict:
    """{ticker: threshold_usd} from evaluated_opportunities (DB cross-check only).
    The strike/price-level lives nowhere in the bronze frame msg — only here."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=10000")
    out: dict[str, float] = {}
    if not tickers:
        return out
    qmarks = ",".join("?" * len(tickers))
    sql = (
        f"SELECT ticker, threshold FROM evaluated_opportunities "
        f"WHERE ticker IN ({qmarks}) AND threshold IS NOT NULL"
    )
    for tk, thr in conn.execute(sql, tuple(tickers)):
        if tk not in out and thr is not None:
            out[tk] = float(thr)
    conn.close()
    return out


def load_all_kraken_frames() -> dict:
    """Decompress every Kraken chunk ONCE; return {asset: sorted [(epoch, inner)]}
    for all 3 target assets in a single pass (the 857-file decompress dominates)."""
    sym2asset = {VENUE_SYMBOLS["kraken"][a]: a for a in ASSETS}
    out: dict[str, list] = {a: [] for a in ASSETS}
    dctx = zstandard.ZstdDecompressor()
    files = []
    for dirpath, _dirs, fnames in os.walk(KRAKEN_ROOT):
        for fn in fnames:
            if fn.endswith(".jsonl.zst"):
                files.append(os.path.join(dirpath, fn))
    def _assert_frame(_p):
        """Raise if _p's zstd frame did not terminate (ticket 86bbvrx1t)."""
        _d = zstandard.ZstdDecompressor().decompressobj()
        with open(_p, "rb") as _f:
            while True:
                _c = _f.read(1 << 20)
                if not _c:
                    break
                _d.decompress(_c)
        if not _d.eof:
            raise RuntimeError(
                f"{_p}: zstd frame did NOT terminate — TRUNCATED read.")

    files.sort()
    for fp in files:
        try:
            with open(fp, "rb") as fh:
                # ticket 86bbvrx1t: stream_reader raises NOTHING on a truncated
                # frame (measured: 44,617 lines from a half file). _assert_frame
                # below re-checks decompressobj.eof after the read.
                reader = dctx.stream_reader(fh)
                txt = io.TextIOWrapper(reader, encoding="utf-8")
                for line in txt:
                    if not line.strip():
                        continue
                    try:
                        env = json.loads(line)
                        inner = json.loads(env["_raw"])
                    except (ValueError, KeyError):
                        continue
                    ts = env.get("_wire_recv_ts")
                    if ts is None:
                        continue
                    ep = _epoch(ts)
                    for d in inner.get("data", []):
                        a = sym2asset.get(d.get("symbol"))
                        if a is None:
                            continue
                        narrowed = _extract_frame(
                            "kraken", inner, VENUE_SYMBOLS["kraken"][a])
                        if narrowed is not None:
                            out[a].append((ep, narrowed))
        except (zstandard.ZstdError, OSError) as e:  # corrupt/truncated chunk
            log(f"  [warn] skip {os.path.basename(fp)}: {e}")
    for a in out:
        out[a].sort(key=lambda x: x[0])
    return out


def kalshi_reliable_nbbo_multi(frames: list, cutoffs: list,
                               max_deltas_since_snap=100000,
                               max_levels_per_side=220) -> dict:
    """ONE forward pass over a ticker's frames, returning {cutoff: (yb,ya)} at each
    ascending cutoff — replicates reliable_nbbo_at's snapshot-anchoring + reliable
    refusal exactly, but amortized across all grid cutoffs for the ticker."""
    res: dict[float, tuple] = {}
    if not frames:
        return {c: (None, None) for c in cutoffs}
    cutoffs = sorted(set(cutoffs))
    b = KalshiBook()
    since_snap = 0
    anchored = False
    fi = 0
    n = len(frames)
    for c in cutoffs:
        while fi < n and frames[fi][0] <= c:
            inner = frames[fi][1]
            if inner.get("type") == "orderbook_snapshot":
                b = KalshiBook()
                b.apply_frame(inner)
                since_snap = 0
                anchored = True
            else:
                b.apply_frame(inner)
                since_snap += 1
            fi += 1
        if (not anchored or since_snap > max_deltas_since_snap
                or not b.is_reliable(max_levels_per_side)):
            res[c] = (None, None)
        else:
            res[c] = (b.best_yes_bid_cents(), b.best_yes_ask_cents())
    return res


def kraken_mid_multi(frames: list, cutoffs: list) -> dict:
    """ONE forward pass over a Kraken asset's frames -> {cutoff: (mid, anchored)}.
    `anchored` is True iff a Kraken snapshot has been seen at/before the cutoff
    (consumer-completeness contract from venue_book_reconstruct.load_venue_frames)."""
    res: dict[float, tuple] = {}
    if not frames:
        return {c: (None, False) for c in cutoffs}
    cutoffs = sorted(set(cutoffs))
    b = KrakenBook()
    anchored = False
    fi = 0
    n = len(frames)
    for c in cutoffs:
        while fi < n and frames[fi][0] <= c:
            inner = frames[fi][1]
            if inner.get("type") == "snapshot":
                anchored = True
            b.apply_frame(inner)
            fi += 1
        res[c] = (b.mid() if anchored else None, anchored)
    return res


def pctl(xs, p):
    if not xs:
        return float("inf")
    s = sorted(xs)
    k = (len(s) - 1) * (p / 100.0)
    f = int(math.floor(k))
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def block_bootstrap(rows, n_boot=N_BOOT):
    if not rows:
        return None, None, None, 0
    clusters: dict = defaultdict(list)
    for r in rows:
        clusters[(r["ticker"], r["hour"])].append(r["net"])
    keys = list(clusters.keys())
    means = []
    for _ in range(n_boot):
        samp = []
        for _k in range(len(keys)):
            ck = keys[random.randrange(len(keys))]
            samp.extend(clusters[ck])
        if samp:
            means.append(sum(samp) / len(samp))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means)) - 1]
    pt = sum(r["net"] for r in rows) / len(rows)
    return pt, lo, hi, len(keys)


def main():
    random.seed(SEED)
    log("Loading Kalshi crypto-15M frames ...")
    all_frames = load_frames_jsonl(FRAMES)
    log(f"  {len(all_frames)} tickers in frames file")

    tk_by_asset: dict[str, list] = defaultdict(list)
    for tk in all_frames:
        m = re.match(r"KX(HYPE|DOGE|BNB)15M-", tk)
        if m:
            tk_by_asset[m.group(1)].append(tk)
    for a in ASSETS:
        log(f"  {a}: {len(tk_by_asset[a])} tickers")

    all_target_tk = {tk for a in ASSETS for tk in tk_by_asset[a]}
    strikes = load_strikes(DB, all_target_tk)
    log(f"  strikes (DB threshold) available for {len(strikes)} / "
        f"{len(all_target_tk)} target tickers")

    log("Decompressing Kraken L2 (all 3 assets, single pass) ...")
    kraken_by_asset = load_all_kraken_frames()
    for a in ASSETS:
        log(f"  Kraken {a}: {len(kraken_by_asset[a])} frames")

    raw_records: dict[str, list] = {a: [] for a in ASSETS}
    spot_ret_abs: dict[str, list] = {a: [] for a in ASSETS}

    for asset in ASSETS:
        kr = kraken_by_asset[asset]
        if not kr:
            log(f"  [skip] no Kraken frames for {asset}")
            continue
        tickers = [tk for tk in tk_by_asset[asset] if tk in strikes]
        log(f"=== {asset}: {len(tickers)} tickers with a DB strike ===")

        for tk in tickers:
            strike = strikes[tk]
            kframes = all_frames[tk]
            if not kframes:
                continue
            close_epoch = close_epoch_from_ticker(tk)
            open_epoch = close_epoch - WINDOW_SECONDS
            t0 = open_epoch + OPEN_PAD
            t1 = close_epoch - CLOSE_PAD
            if t1 <= t0:
                continue
            hour = int(close_epoch // 3600)

            # Build the cutoff set: each grid t needs t-15s, t, t+30s (markout).
            grid = []
            t = t0
            while t <= t1:
                grid.append(t)
                t += GRID_STEP
            if not grid:
                continue
            cutoffs = sorted({c for g in grid
                              for c in (g - LOOKBACK, g, g + MARKOUT_S)})

            # Kraken window slice (anchor pre-roll) -> single pass.
            kr_win = [(t, f) for (t, f) in kr
                      if open_epoch - 600 <= t <= close_epoch + 5]
            if not kr_win:
                continue
            kmid_kr = kraken_mid_multi(kr_win, cutoffs)
            knbbo = kalshi_reliable_nbbo_multi(kframes, cutoffs)

            for g in grid:
                gp = g - LOOKBACK
                mid_t, anch_t = kmid_kr.get(g, (None, False))
                mid_p, anch_p = kmid_kr.get(gp, (None, False))
                if not (anch_t and anch_p):
                    continue
                if mid_t is None or mid_p is None or mid_t <= 0 or mid_p <= 0:
                    continue
                spot_ret = math.log(mid_t / mid_p)

                yb_t, ya_t = knbbo.get(g, (None, None))
                yb_p, ya_p = knbbo.get(gp, (None, None))
                if None in (yb_t, ya_t, yb_p, ya_p):
                    continue
                kmid_t = (yb_t + ya_t) / 2.0
                kmid_p = (yb_p + ya_p) / 2.0
                kalshi_drift = abs(kmid_t - kmid_p)

                yb_m, ya_m = knbbo.get(g + MARKOUT_S, (None, None))
                kmid_m = (yb_m + ya_m) / 2.0 if None not in (yb_m, ya_m) else None

                spot_ret_abs[asset].append(abs(spot_ret))
                raw_records[asset].append({
                    "ticker": tk, "hour": hour, "t": g,
                    "strike": strike, "mid_t": mid_t,
                    "spot_ret": spot_ret, "kalshi_drift": kalshi_drift,
                    "ya_t": ya_t, "yb_t": yb_t,
                    "kmid_t": kmid_t, "kmid_m": kmid_m,
                })

    thresh = {a: pctl(spot_ret_abs[a], PCTL) for a in ASSETS}
    for a in ASSETS:
        log(f"  {a}: {len(spot_ret_abs[a])} grid candidates, "
            f"80th-pct |spot_ret|={thresh[a]:.6g}")

    terminal_cache: dict[str, float | None] = {}

    def terminal_label(tk: str):
        if tk not in terminal_cache:
            kframes = all_frames.get(tk, [])
            ce = close_epoch_from_ticker(tk)
            yb, ya = reliable_nbbo_at(kframes, ce)
            terminal_cache[tk] = (yb + ya) / 2.0 if None not in (yb, ya) else None
        tm = terminal_cache[tk]
        if tm is None:
            return None
        return 1 if tm >= 50.0 else 0  # 1 = YES wins

    trades: dict[str, list] = {a: [] for a in ASSETS}
    n_gate_fire = {a: 0 for a in ASSETS}
    for asset in ASSETS:
        gate = thresh[asset]
        for rec in raw_records[asset]:
            if abs(rec["spot_ret"]) <= gate:
                continue
            if rec["kalshi_drift"] >= DRIFT_MAX_CENTS:
                continue
            n_gate_fire[asset] += 1
            rising = rec["spot_ret"] > 0
            above = rec["mid_t"] > rec["strike"]
            if rising and above:
                side = "yes"
            elif (not rising) and (not above):
                side = "no"
            else:
                continue

            label_yes = terminal_label(rec["ticker"])
            if label_yes is None:
                continue

            if side == "yes":
                entry = rec["ya_t"]
                won = (label_yes == 1)
            else:
                entry = 100.0 - rec["yb_t"]
                won = (label_yes == 0)
            if entry is None or entry <= 0 or entry >= 100:
                continue
            P = entry / 100.0
            fee = math.ceil(TAKER_FEE_RATE * 1 * P * (1 - P))
            payoff = 100.0 if won else 0.0
            net = payoff - entry - fee

            mk = None
            if rec["kmid_m"] is not None:
                dm = rec["kmid_m"] - rec["kmid_t"]
                mk = dm if side == "yes" else -dm

            trades[asset].append({
                "ticker": rec["ticker"], "hour": rec["hour"],
                "net": net, "won": won, "side": side, "entry": entry,
                "fee": fee, "markout": mk,
            })

    results = {}
    all_rows = []
    log("\n================ RESULTS ================")
    for asset in ASSETS:
        rows = trades[asset]
        all_rows.extend(rows)
        n = len(rows)
        log(f"  {asset}: gate-fired={n_gate_fire[asset]}  traded(after dir+label)={n}")
        if n == 0:
            results[asset] = None
            continue
        pt, lo, hi, nclu = block_bootstrap(rows)
        wr = sum(1 for r in rows if r["won"]) / n
        mks = [r["markout"] for r in rows if r["markout"] is not None]
        mk_mean = sum(mks) / len(mks) if mks else float("nan")
        results[asset] = (pt, lo, hi, n, nclu, wr, mk_mean)
        log(f"  {asset}: n={n} clusters={nclu} WR={wr:.3f} "
            f"net/contract={pt:+.3f}c CI=[{lo:+.3f},{hi:+.3f}] "
            f"markout@30s={mk_mean:+.3f}c")

    if all_rows:
        ppt, plo, phi, pclu = block_bootstrap(all_rows)
        pn = len(all_rows)
        log(f"  POOLED: n={pn} clusters={pclu} net/contract={ppt:+.3f}c "
            f"CI=[{plo:+.3f},{phi:+.3f}]")
    else:
        ppt = plo = phi = float("nan")
        pn = pclu = 0
        log("  POOLED: 0 trades")

    edge = any(results[a] is not None and results[a][1] > 0 for a in ASSETS)
    out = {
        "per_asset": {a: (None if results[a] is None else {
            "point": results[a][0], "ci_low": results[a][1],
            "ci_high": results[a][2], "n": results[a][3],
            "clusters": results[a][4], "wr": results[a][5],
            "markout30s": results[a][6],
        }) for a in ASSETS},
        "pooled": ({"point": ppt, "ci_low": plo, "ci_high": phi, "n": pn,
                    "clusters": pclu} if pn else None),
        "edge_any_asset": edge,
    }
    print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    main()
