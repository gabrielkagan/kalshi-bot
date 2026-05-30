"""Daily automated edge tracker — pull clean bronze, run the markout MM evaluator,
alert the moment a cell clears the pre-registered gate.

"We need to know when we've found alpha." This runs daily (launchd on the Mac —
Mac-local compute per the VPS-isolation rule), pulls the last N hours of clean
crypto-15M bronze (orderbook + trade) + a fresh state.db, runs
`mm_markout_evaluator.evaluate`, writes a dated report, and:
  - if ANY cell SURVIVES the gate  → ALERT (Telegram if creds present; ALWAYS a
    local sentinel file + macOS notification + loud stdout) recommending an
    adversarial-review pass before believing it.
  - else → log the report quietly (the expected default while markets are efficient).

Detection ≠ belief: a survivor is a CANDIDATE that still must clear a 2-zero
adversarial gate (and, the first time, human sign-off) before any live sizing.

Usage / cron:  python3 -m scripts.research.edge_daily_run [--hours 30] [--workdir /tmp/edge_daily]
Env (optional): TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID for push alerts; VPS_HOST for state.db.

Parent: kb/decisions/settlement-convergence-worklist.md (markout MM evaluator + daily tracker)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

# Clean-data epoch: the collector reconnect-storm fix (86ba76adw) landed + the
# collector restarted 2026-05-30T21:06:54Z. Books before this are the corrupted /
# snapshot-only era — reliable_nbbo_at refuses them anyway, but we don't even pull
# them. Bump this only if a later data-quality fix supersedes it.
CLEAN_DATA_START = "2026-05-30T21:06:54Z"
S3_BRONZE = "kalshi-restore:kalshi-bot-archive/bronze/kalshi_ws"
VPS_HOST = os.environ.get("VPS_HOST", "botuser@45.55.181.30")
CRYPTO_RE = r"KX(BTC|ETH|SOL|XRP|HYPE|DOGE|BNB|ADA|BCH)15M-"


# ----- alert decision (pure; TDD-pinned) -----------------------------------


def should_alert(survivors) -> bool:
    """Alert iff at least one cell cleared the gate. (Kept pure + trivial so the
    decision is testable and the I/O-heavy runner can't accidentally suppress an
    alpha hit behind a bug.)"""
    return bool(survivors)


def format_alert(survivors) -> str:
    lines = [f"🟢 ALPHA CANDIDATE: {len(survivors)} cell(s) cleared the markout gate "
             f"({datetime.now(timezone.utc).isoformat(timespec='seconds')})"]
    for s in survivors:
        lines.append(
            f"  {s['cell']}: settleNet={s['settle_mean_net']:+.2f}c "
            f"CI={tuple(round(x, 1) for x in s['settle_ci'])} "
            f"mk30={s['markout_30s_net']:+.2f}c n={s['n_fills']} latency={s['attribution']}")
    lines.append("→ NOT yet an edge: dispatch adversarial review (2-zero gate) + "
                 "human sign-off before any live sizing.")
    return "\n".join(lines)


# ----- alerting (Telegram if creds; always sentinel + macOS notice) --------


def _send_telegram(text: str) -> bool:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def raise_alert(text: str, workdir: str) -> None:
    print("\n" + "=" * 70 + f"\n{text}\n" + "=" * 70, flush=True)
    # durable sentinel — survives even if Telegram/notification fail
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sentinel = os.path.join(workdir, f"ALPHA_CANDIDATE_{stamp}.txt")
    try:
        with open(sentinel, "w") as fh:
            fh.write(text + "\n")
    except OSError:
        pass
    tg = _send_telegram(text)
    if not tg:  # best-effort macOS notification fallback
        try:
            subprocess.run(["osascript", "-e",
                            'display notification "ALPHA CANDIDATE cleared the markout '
                            'gate — see edge_daily report" with title "Kalshi edge hunt"'],
                           timeout=10, check=False)
        except Exception:
            pass
    print(f"[alert] telegram={'sent' if tg else 'unavailable'}  sentinel={sentinel}")


# ----- data pull ------------------------------------------------------------


def _recent_hours(hours_back: int):
    """List of (day, hour) UTC partitions for the last `hours_back` hours.
    Uses the wall clock — fine for a cron job (NOT a determinism-sensitive path)."""
    now = datetime.now(timezone.utc)
    out = []
    base = now.replace(minute=0, second=0, microsecond=0)
    for h in range(hours_back + 1):
        t = base.timestamp() - h * 3600
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        out.append((dt.year, dt.month, dt.day, dt.hour))
    return out


def pull_clean(workdir: str, hours_back: int) -> tuple:
    """Pull recent crypto-15M orderbook + trade bronze (grepped small) + fresh
    state.db. Returns (frames_file, trades_file, db_file). Best-effort per hour."""
    os.makedirs(workdir, exist_ok=True)
    frames_f = os.path.join(workdir, "frames_crypto.jsonl")
    trades_f = os.path.join(workdir, "trades_crypto.jsonl")
    db_f = os.path.join(workdir, "state.db")
    for chan, out_f in (("orderbook_delta", frames_f), ("trade", trades_f)):
        open(out_f, "w").close()
        for (y, m, d, h) in _recent_hours(hours_back):
            part = f"year={y}/month={m:02d}/day={d:02d}/hour={h:02d}"
            dest = os.path.join(workdir, "pull", chan, part)
            subprocess.run(["rclone", "copy", f"{S3_BRONZE}/{chan}/{part}", dest,
                            "--transfers=8"], capture_output=True, timeout=600)
            # grep crypto-15M out of the pulled .zst into the cumulative file
            subprocess.run(
                f'find "{dest}" -name "*.zst" 2>/dev/null | while read f; do '
                f'zstd -dc "$f" 2>/dev/null; done | grep -E "{CRYPTO_RE}" >> "{out_f}"',
                shell=True, capture_output=True, timeout=600)
    # fresh state.db for outcomes
    subprocess.run(["ssh", "-o", "ConnectTimeout=20", VPS_HOST,
                    "cd ~/kalshi-bot-repo && rm -f /tmp/sd_edge.db && "
                    "sqlite3 state.db '.backup /tmp/sd_edge.db'"],
                   capture_output=True, timeout=120)
    subprocess.run(["scp", "-o", "ConnectTimeout=20",
                    f"{VPS_HOST}:/tmp/sd_edge.db", db_f], capture_output=True, timeout=300)
    return frames_f, trades_f, db_f


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=30, help="hours of bronze to pull")
    ap.add_argument("--workdir", default="/tmp/edge_daily")
    ap.add_argument("--no-pull", action="store_true",
                    help="reuse existing files in workdir (skip the rclone/ssh pull)")
    args = ap.parse_args(argv)

    from scripts.research import mm_markout_evaluator as mm
    from scripts.research.phase1b_real_price_economics import (
        load_frames_jsonl, load_outcomes_db)
    from scripts.research.phase1b_live_shadow import load_trades_by_ticker

    if args.no_pull:
        frames_f = os.path.join(args.workdir, "frames_crypto.jsonl")
        trades_f = os.path.join(args.workdir, "trades_crypto.jsonl")
        db_f = os.path.join(args.workdir, "state.db")
    else:
        frames_f, trades_f, db_f = pull_clean(args.workdir, args.hours)

    frames = load_frames_jsonl(frames_f)
    trades = load_trades_by_ticker(trades_f)
    outcomes = load_outcomes_db(db_f, set(frames))
    res = mm.evaluate(frames, trades, outcomes)
    survivors = res["survivors"]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = os.path.join(args.workdir, f"report_{stamp}.txt")
    summary = (f"windows={len(frames)} survivors={len(survivors)} "
               f"clean_since={CLEAN_DATA_START}")
    with open(report, "w") as fh:
        fh.write(summary + "\n")
        for key in sorted(res["cells"]):
            c = res["cells"][key]
            fh.write(f"{key} fills={c['filled']}/{c['posted']} "
                     f"settleNet={mm._mean(c['settle']):+.2f} "
                     f"attribution={c.get('attribution')} "
                     f"survives={c['gate']['survives']}\n")
    print(summary + f"  report={report}")

    if should_alert(survivors):
        raise_alert(format_alert(survivors), args.workdir)
    else:
        print("⚪ no gate survivor — efficient/insufficient (expected). logged only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
