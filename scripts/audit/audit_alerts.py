#!/usr/bin/env python3
"""Audit invariant checker + Telegram alerter.

Reads JSON artifacts produced by the 5 audit scripts and checks
health invariants. Sends Telegram alerts on violations.

Usage:
    python3 scripts/audit/audit_alerts.py --json-dir /tmp/audit_artifacts/ --dry-run
    python3 scripts/audit/audit_alerts.py --json-dir /tmp/audit_artifacts/ --telegram
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Telegram config (from env, same as watchdog.py)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(msg: str) -> bool:
    """Send Telegram message. Returns True on success."""
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[audit_alerts] No Telegram config, skipping: {msg[:80]}")
        return False
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
        return True
    except Exception as e:
        print(f"[audit_alerts] Telegram send failed: {e}")
        return False


# ── Invariant definitions ───────────────────────────────────────

def check_15m(data: dict) -> list:
    """Check 15M live trading invariants."""
    violations = []
    trades = data.get("trades", 0)
    wins = data.get("wins", 0)
    pnl = data.get("pnl_cents", 0)
    wr = data.get("win_rate", 0)

    if trades < 1:
        violations.append("15M: zero trades in period")
    if wr < 0.80 and trades >= 5:
        violations.append(f"15M: win rate {wr:.1%} < 80% (n={trades})")
    if pnl < -5000:  # -$50
        violations.append(f"15M: PnL ${pnl/100:.2f} < -$50")

    return violations


def check_hourly(data: dict) -> list:
    """Check hourly observation invariants."""
    violations = []

    # Hourly uses different JSON structures depending on audit script
    # hourly_shadow_audit.py: top-level keys
    settled = data.get("settled", 0)
    signals = data.get("signals", data.get("total_evals", 0))
    wins = data.get("wins", 0)
    losses = data.get("losses", 0)

    if signals < 1:
        violations.append("Hourly: zero signals — data pipeline may be broken")

    # Brier and overconfidence come from the performance_summary stats
    # These aren't in the hourly JSON artifact by default, so check if present
    brier = data.get("brier")
    if brier is not None and brier > 0.25 and settled >= 20:
        violations.append(f"Hourly: Brier {brier:.3f} > 0.25")

    overconfidence = data.get("overconfidence_pp")
    if overconfidence is not None and overconfidence > 5.0 and settled >= 20:
        violations.append(
            f"Hourly: overconfidence {overconfidence:.1f}pp > 5pp")

    return violations


def check_observation(module: str, data: dict) -> list:
    """Check observation-mode modules (SPX, Weather, Sports).
    Primary invariant: data is flowing (observations >= 1)."""
    violations = []

    # Different audit scripts use different key names
    # SPX: performance.total_evals or top-level total_evals
    perf = data.get("performance", data.get("overview", data))
    evals = perf.get("total_evals", perf.get("total", 0))
    signals = perf.get("signals", 0)

    if evals < 1:
        violations.append(f"{module}: zero evaluations — engine may be down")
    elif signals < 1:
        violations.append(f"{module}: zero signals in {evals} evals")

    return violations


# ── Main ────────────────────────────────────────────────────────

MODULE_CHECKERS = {
    "15m": ("15m_audit.json", check_15m),
    "hourly": ("hourly_audit.json", check_hourly),
    "spx": ("spx_audit.json", lambda d: check_observation("SPX", d)),
    "weather": ("weather_audit.json", lambda d: check_observation("Weather", d)),
    "sports": ("sports_audit.json", lambda d: check_observation("Sports", d)),
}


def main():
    parser = argparse.ArgumentParser(
        description="Audit invariant checker + Telegram alerter")
    parser.add_argument("--json-dir", default="/tmp/audit_artifacts/",
                        help="Directory containing audit JSON artifacts")
    parser.add_argument("--telegram", action="store_true",
                        help="Send Telegram alerts on violations")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be alerted without sending")
    args = parser.parse_args()

    json_dir = Path(args.json_dir)
    if not json_dir.exists():
        print(f"ERROR: JSON directory not found: {json_dir}")
        sys.exit(1)

    all_violations = []
    modules_checked = 0
    modules_missing = []

    for module, (filename, checker) in MODULE_CHECKERS.items():
        path = json_dir / filename
        if not path.exists():
            modules_missing.append(module)
            continue

        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            all_violations.append(f"{module}: failed to read JSON: {e}")
            continue

        modules_checked += 1
        violations = checker(data)
        all_violations.extend(violations)

    # Report
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'=' * 50}")
    print(f"  Audit Invariant Check — {now}")
    print(f"{'=' * 50}")
    print(f"\n  Modules checked: {modules_checked}/5")
    if modules_missing:
        print(f"  Missing artifacts: {', '.join(modules_missing)}")

    if not all_violations:
        print("\n  All invariants passed.")
        print(f"{'=' * 50}")
        return

    print(f"\n  VIOLATIONS ({len(all_violations)}):")
    for v in all_violations:
        print(f"    - {v}")
    print(f"{'=' * 50}")

    # Telegram alert
    if args.telegram and not args.dry_run:
        msg_lines = [f"*Audit Alert* — {now}", ""]
        for v in all_violations:
            msg_lines.append(f"- {v}")
        msg_lines.append(f"\n{modules_checked}/5 modules checked")
        send_telegram("\n".join(msg_lines))
        print("\n  Telegram alert sent.")
    elif args.dry_run:
        print("\n  [DRY RUN] Would send Telegram alert with above violations.")

    sys.exit(1 if all_violations else 0)


if __name__ == "__main__":
    main()
