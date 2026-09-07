#!/usr/bin/env python3
"""Corpus-readiness gate helper: normalize coinbase ticker bronze -> spot mid jsonl.

Reads decompressed coinbase_ws ticker bronze chunks pulled into /tmp/edge_daily/cb_pull,
filters to BTC/ETH/SOL/XRP product_ids, and writes a flat normalized spot file:
  /tmp/edge_daily/coinbase_spot.jsonl
each line: {"ts": <_wire_recv_ts>, "product_id": "BTC-USD", "mid": <float>, "bid": .., "ask": .., "last": ..}

mid = (best_bid + best_ask)/2 when both present, else falls back to price (last trade).
Window: 2026-05-30T21:06Z onward (matches frames corpus floor stated by orchestrator;
the actual pulled partitions are 5/30 h>=21 + all 5/31, so a couple pre-21:06 frames may slip in
on the 21:00 hour boundary -- harmless, downstream filters on its own window).
"""
import json
import os
import subprocess
import glob

CB_DIR = "/tmp/edge_daily/cb_pull"
OUT = "/tmp/edge_daily/coinbase_spot.jsonl"
WANT = {"BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"}


def iter_frames():
    for path in sorted(glob.glob(os.path.join(CB_DIR, "**", "*.zst"), recursive=True)):
        try:
            proc = subprocess.run(["zstd", "-dc", path], capture_output=True, check=True)
        except Exception:
            continue
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            yield d


def main():
    n_in = 0
    n_out = 0
    by_prod = {}
    with open(OUT, "w") as fout:
        for d in iter_frames():
            n_in += 1
            raw = d.get("_raw")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    continue
            if not isinstance(raw, dict):
                continue
            pid = raw.get("product_id")
            if pid not in WANT:
                continue
            try:
                bid = float(raw["best_bid"]) if raw.get("best_bid") else None
                ask = float(raw["best_ask"]) if raw.get("best_ask") else None
            except Exception:
                bid = ask = None
            try:
                last = float(raw["price"]) if raw.get("price") else None
            except Exception:
                last = None
            if bid is not None and ask is not None and ask > 0:
                mid = (bid + ask) / 2.0
            else:
                mid = last
            if mid is None:
                continue
            rec = {
                "ts": d.get("_wire_recv_ts"),
                "product_id": pid,
                "mid": mid,
                "bid": bid,
                "ask": ask,
                "last": last,
            }
            fout.write(json.dumps(rec) + "\n")
            n_out += 1
            by_prod[pid] = by_prod.get(pid, 0) + 1
    print(f"frames_in={n_in} spot_rows_out={n_out}")
    print("by_product:", by_prod)


if __name__ == "__main__":
    main()
