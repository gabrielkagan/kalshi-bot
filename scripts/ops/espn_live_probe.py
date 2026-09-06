#!/usr/bin/env python3
"""ESPN scoreboard live probe — ticket 86bbvqhyr (2026-09-06).

One GET against a single ``site.api.espn.com`` scoreboard endpoint using
the SAME User-Agent the bot + collector send
(``bot.engines.sports_engine.ESPN_USER_AGENT``). Exit status:

  0  HTTP 200 (prints event count)
  1  any other HTTP status (ESPN is rejecting us — 403 = UA filter class)
  2  transport error (DNS / TLS / timeout)

Run after any collector or bot deploy, and whenever the
``d1_11_http_errors`` / ``b3_fu3_sports_eval_silence`` alerts fire:

    python3 scripts/ops/espn_live_probe.py
    python3 scripts/ops/espn_live_probe.py --sport basketball --league nba
    python3 scripts/ops/espn_live_probe.py --ua "curl/8.7.1"   # RCA: swap UA

Why: ESPN's Akamai edge started 403-ing ``KalshiBot/1.0`` ~2026-08-05
and both pollers kept writing well-formed 403 rows for ~5 weeks. A
one-shot probe with a non-zero exit is the cheapest end-to-end canary
(L-espn-2). Postmortem: kb/failures/espn-403-user-agent-silent-outage-sep06.md
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

# Cron / operator invocation has no editable install — bootstrap BEFORE
# any `from bot.*` import (feedback_monitor_the_monitor; same pattern as
# scripts/ops/collector_health_monitor.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import requests  # noqa: E402

from bot.engines.sports_engine import (  # noqa: E402
    ESPN_BASE, ESPN_TIMEOUT, ESPN_USER_AGENT,
)


def _parse(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sport", default="football",
                    help="ESPN sport path segment (default: football)")
    ap.add_argument("--league", default="college-football",
                    help="ESPN league slug (default: college-football)")
    ap.add_argument("--ua", default=None,
                    help="Override User-Agent (RCA only; default = the "
                         "shared ESPN_USER_AGENT constant)")
    ap.add_argument("--timeout", type=float, default=float(ESPN_TIMEOUT))
    return ap.parse_args(list(argv) if argv is not None else None)


def main(
    argv: Optional[Sequence[str]] = None,
    get_fn: Optional[Callable[..., object]] = None,
) -> int:
    args = _parse(argv)
    get = get_fn if get_fn is not None else requests.get
    ua = args.ua if args.ua is not None else ESPN_USER_AGENT
    url = f"{ESPN_BASE}/{args.sport}/{args.league}/scoreboard"
    t0 = time.time()
    try:
        resp = get(url, headers={"User-Agent": ua}, timeout=args.timeout)
    except requests.RequestException as exc:
        print(f"ESPN_PROBE FAIL transport url={url} ua={ua!r} "
              f"error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    elapsed_ms = int((time.time() - t0) * 1000)
    status = getattr(resp, "status_code", None)
    if status != 200:
        print(f"ESPN_PROBE FAIL http_status={status} url={url} ua={ua!r} "
              f"elapsed_ms={elapsed_ms}", file=sys.stderr)
        return 1
    try:
        n_events = len(resp.json().get("events", []))  # type: ignore[attr-defined]
    except Exception as exc:  # non-JSON 200 is still a failure of content
        print(f"ESPN_PROBE FAIL http_status=200 but body not JSON "
              f"({type(exc).__name__}) url={url} ua={ua!r}", file=sys.stderr)
        return 1
    print(f"ESPN_PROBE OK http_status=200 events={n_events} url={url} "
          f"ua={ua!r} elapsed_ms={elapsed_ms}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
