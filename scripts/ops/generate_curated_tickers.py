#!/usr/bin/env python3
"""Generate curated tickers JSON for the kalshi-collector (RCA-G `86ba12xyq`).

Reads from Kalshi REST ``/markets?status=open`` (the same endpoint the
collector uses at boot), filters to the bot's analyzed series prefixes,
writes JSON to disk in the ``{TIER_ALL: [...]}`` shape that
``collector.main_loop._load_tickers_by_tier`` accepts via the
``COLLECTOR_TICKERS_FILE`` env var.

Background: the collector's default REST-snapshot mode subscribes to all
754K+ open Kalshi markets, of which ~99.8% are esports/cross-category
markets the bot never analyzes. Curated mode drops the WS subscribe
universe to ~10K tickers (crypto 15M+hourly + SPX + weather cities),
which collapses boot time 17min → ~30s and drop rate ~33% → ~0%.

Operator runbook (RCA-G plan doc
``kb/decisions/collector-curated-mitigation-plan.md``):

  1. On the VPS:
     ``cd ~/kalshi-bot-repo && . venv/bin/activate``
     ``set -a && . ~/.env.collector && set +a``
     ``python3 scripts/ops/generate_curated_tickers.py``
     # writes ``/home/botuser/curated_tickers.json``

  2. Add to ``/home/botuser/.env.collector``:
     ``COLLECTOR_TICKERS_FILE=/home/botuser/curated_tickers.json``

  3. ``sudo systemctl restart kalshi-collector``

  4. (OPTIONAL) Add a cron entry to refresh the file periodically.
     IMPORTANT: ``collector/main_loop.py:686`` reads the file ONCE at
     boot — there is no live reload. A cron-refresh ONLY freshens the
     file for the NEXT operator-issued ``systemctl restart``. New 15M
     market windows opening after boot will NOT be auto-subscribed.
     The bot itself re-fetches via its own KalshiFeed at scan time, so
     trading is unaffected — but the COLLECTOR's bronze coverage for
     newly-opened windows requires either (a) periodic operator
     restart, or (b) extending the collector with a file-watcher (out
     of scope for this Bit; see plan doc Risk #4).
     Recommended cadence if cron-refreshing for next-restart staleness:
     ``0 */6 * * * cd ~/kalshi-bot-repo && . venv/bin/activate
       && set -a && . ~/.env.collector && set +a
       && python3 scripts/ops/generate_curated_tickers.py
       >> ~/curated_tickers_refresh.log 2>&1``

Reversal: ``sed -i /COLLECTOR_TICKERS_FILE/d ~/.env.collector
           && sudo systemctl restart kalshi-collector`` — collector
reverts to REST-snapshot universal mode at next boot.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ── Bot universe series prefixes ────────────────────────────────────────────
#
# Each entry matches tickers of shape ``<PREFIX><MARKET_ID>``. Trailing
# hyphen ensures exact series matching (e.g. ``KXBTC15M-`` does NOT
# match a hypothetical ``KXBTC15MFOO`` lookalike series).
#
# Drift-pinned by ``tests/unit/test_generate_curated_tickers.py``:
# every entry MUST be derivable from ``bot/constants.py::SERIES_TICKERS``
# + ``HOURLY_SERIES_TICKERS`` + ``bot/engines/weather_engine.py`` city
# config + ``bot/engines/spx_engine.py::SPX_SERIES_TICKER``. A drift
# between this list and the bot's actual analyzed universe causes the
# test to fail RED.
BOT_SERIES_PREFIXES: List[str] = [
    # Crypto 15M (from bot/constants.py::SERIES_TICKERS)
    "KXBTC15M-",
    "KXETH15M-",
    "KXSOL15M-",
    "KXXRP15M-",
    "KXHYPE15M-",
    "KXDOGE15M-",
    "KXBNB15M-",
    "KXADA15M-",
    "KXBCH15M-",
    # Crypto hourly daily settlement (from bot/constants.py::HOURLY_SERIES_TICKERS)
    "KXBTCD-",
    "KXETHD-",
    "KXSOLD-",
    "KXXRPD-",
    "KXHYPED-",
    "KXDOGED-",
    "KXBNBD-",
    "KXADAD-",
    "KXBCHD-",
    # SPX hourly — bot/engines/spx_engine.py:27 SPX_SERIES_TICKER="KXINXU"
    # is the LIVE Kalshi series the bot queries (see :1107-1123). The
    # legacy ``KXSPX*`` LIKE patterns at bot/state.py:988/:1014 are
    # historical backfill classifiers on settled_trades and capture
    # zero current rows since Kalshi renamed SPX → KXINXU.
    "KXINXU-",
    # Weather high-temp cities (from bot/engines/weather_engine.py)
    "KXHIGHNY-",
    "KXHIGHCHI-",
    "KXHIGHMIA-",
    "KXHIGHDEN-",
    "KXHIGHLAX-",
    "KXHIGHAUS-",
    "KXHIGHTATL-",
    "KXHIGHTSFO-",
    "KXHIGHTDAL-",
    "KXHIGHTPHX-",
    "KXHIGHPHIL-",
    "KXHIGHTMIN-",
    "KXHIGHTSEA-",
    "KXHIGHTHOU-",
    "KXHIGHTBOS-",
    "KXHIGHTLV-",
    "KXHIGHTOKC-",
    "KXHIGHTDC-",
    "KXHIGHTNOLA-",
]


# Matches ``collector/rest_snapshot.py::TIER_ALL`` — single-tier
# classification (D0.2 scope-map authoritative).
TIER_ALL: str = "1"


def filter_curated(all_tickers, prefixes):
    """Return sorted list of tickers matching ANY prefix.

    Set-comprehension + ``sorted`` mirrors ``fetch_tickers_by_tier``'s
    ``sorted(set(...))`` output shape so the curated file slots into the
    same downstream consumer (``collector.main_loop._load_tickers_by_tier``)
    without further normalization.
    """
    return sorted(
        {t for t in all_tickers if any(t.startswith(p) for p in prefixes)}
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate curated tickers JSON for kalshi-collector "
            "(RCA-G 86ba12xyq)."
        ),
    )
    parser.add_argument(
        "--output",
        default=os.environ.get(
            "CURATED_TICKERS_OUTPUT", "/home/botuser/curated_tickers.json",
        ),
        help="Output JSON path (default: /home/botuser/curated_tickers.json).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("KALSHI_COLLECTOR_KEY_ID"),
        help="Kalshi API key id (default: env KALSHI_COLLECTOR_KEY_ID).",
    )
    parser.add_argument(
        "--key-path",
        default=os.environ.get("KALSHI_COLLECTOR_KEY_PATH"),
        help="RSA-PSS PEM path (default: env KALSHI_COLLECTOR_KEY_PATH).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "KALSHI_REST_BASE_URL", "https://api.elections.kalshi.com",
        ),
        help="Kalshi REST base URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the first 50 curated tickers to stdout WITHOUT writing "
            "the output file. Useful for verifying prefixes match real "
            "tickers before flipping the collector to curated mode."
        ),
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.api_key or not args.key_path:
        print(
            "ERROR: KALSHI_COLLECTOR_KEY_ID and KALSHI_COLLECTOR_KEY_PATH "
            "must be set (via env or --api-key/--key-path).",
            file=sys.stderr,
        )
        return 2

    # Lazy imports — keep argparse/help cheap.
    from kalshi_wire.auth import load_private_key
    from collector.rest_snapshot import fetch_tickers_by_tier

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    private_key = load_private_key(args.key_path)
    result = fetch_tickers_by_tier(
        api_key=args.api_key,
        private_key=private_key,
        base_url=args.base_url,
    )
    if result is None:
        print(
            "ERROR: fetch_tickers_by_tier returned None (Kalshi REST "
            "transport/auth failure). NOT writing output to preserve "
            "the prior curated_tickers.json — collector reads it on next "
            "boot.",
            file=sys.stderr,
        )
        return 3

    all_tickers = result.get(TIER_ALL, [])
    curated = filter_curated(all_tickers, BOT_SERIES_PREFIXES)
    pct = (100.0 * len(curated) / max(1, len(all_tickers)))
    logging.info(
        "Fetched %d open markets; curated to %d (%.2f%%) matching %d prefixes.",
        len(all_tickers), len(curated), pct, len(BOT_SERIES_PREFIXES),
    )

    if args.dry_run:
        sample = curated[:50]
        print(json.dumps({TIER_ALL: sample}, indent=2))
        logging.info(
            "DRY RUN — printed first %d of %d curated tickers; "
            "would write to %s.", len(sample), len(curated), args.output,
        )
        return 0

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps({TIER_ALL: curated}))
    os.replace(tmp_path, output_path)
    logging.info("Wrote %s (%d tickers).", output_path, len(curated))
    return 0


if __name__ == "__main__":
    sys.exit(main())
