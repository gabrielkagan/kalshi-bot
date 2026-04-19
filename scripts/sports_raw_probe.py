"""Dump RAW API responses — no parsing, just JSON pretty-print.

Compares:
  - A live sports market orderbook (expected: populated)
  - A live 15m crypto orderbook (control — known working)
  - The event response shape for a sports event

Usage on VPS:
  cd /home/botuser/kalshi-bot-repo && set -a && source .env && set +a && \
    ./venv/bin/python3 scripts/sports_raw_probe.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import KalshiClient  # noqa: E402


def build_client() -> KalshiClient:
    api_key = os.environ.get("KALSHI_API_KEY") or os.environ.get("KALSHI_API_KEY_ID", "")
    pk = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    return KalshiClient(api_key, pk)


def dump(title, obj):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    print(json.dumps(obj, indent=2, default=str)[:4000])


def main():
    c = build_client()

    sports_tickers = [
        "KXNHLGAME-26APR19LACOL-LA",
        "KXNHLGAME-26APR19LACOL-COL",
        "KXNHLGAME-26APR19BOSBUF-BOS",
        "KXNBAGAME-26APR19PHIBOS-BOS",
    ]
    for t in sports_tickers:
        dump(f"orderbook raw: {t}", c.get_orderbook(t, depth=10))

    # Crypto control — find any live 15m market
    dump("events KXBTC15M (sample)", c.get_events(
        series_ticker="KXBTC15M", status="open",
        with_nested_markets=True, limit=3))

    # Sports event raw (so we see ALL keys/nested shape)
    dump("events KXNHLGAME raw (first event only)", {
        "first_event": (
            c.get_events(series_ticker="KXNHLGAME", status="open",
                         with_nested_markets=True, limit=1)
            or {}
        ).get("events", [None])[0]
    })

    # Try a /markets/{ticker} direct fetch — different endpoint
    for t in sports_tickers[:2]:
        dump(f"get_market {t}", c.get_market(t))


if __name__ == "__main__":
    main()
