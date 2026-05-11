"""One-shot diagnostic for sports engine match failure.

Answers three questions without deploying anything:
  Q1 (Layer 2): What status values does Kalshi return for in-progress sports
      events? Where do MLB games-started-hours-ago live?
  Q2 (Layer 3a): For KXNHLGAME-26APR19LACOL, does the event response include
      a populated markets array (the sports engine skips events with markets=[])?
  Q3 (Layer 3b): Do the market orderbooks actually have no-side bids?

Run on the VPS:  cd /opt/kalshi-bot && source venv/bin/activate && \
    source .env && python3 scripts/sports_diagnose.py
"""
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.kalshi_client import KalshiClient  # noqa: E402


SERIES_TO_PROBE = ["KXNHLGAME", "KXNBAGAME", "KXMLBGAME"]
STATUSES_TO_TRY = [None, "open", "active", "closed", "settled", "determined"]


def build_client() -> KalshiClient:
    api_key = os.environ.get("KALSHI_API_KEY") or os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not api_key or not private_key_path:
        raise RuntimeError("KALSHI_API_KEY[_ID] and KALSHI_PRIVATE_KEY_PATH must be set")
    return KalshiClient(api_key, private_key_path)


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def q1_status_sweep(client: KalshiClient) -> None:
    """For each status filter, count events per series + dump any for today."""
    import datetime
    today_prefix = datetime.datetime.utcnow().strftime("%y%b%d").upper()
    for series in SERIES_TO_PROBE:
        section(f"Q1: status sweep for {series}  (today_prefix={today_prefix})")
        for status in STATUSES_TO_TRY:
            try:
                resp = client.get_events(
                    series_ticker=series, status=status,
                    with_nested_markets=False, limit=100,
                )
            except Exception as exc:
                print(f"  status={status!r:<12} ERROR: {exc}")
                continue
            events = (resp or {}).get("events") or []
            today = [e for e in events
                     if e.get("event_ticker", "").split("-", 1)[-1].startswith(today_prefix)]
            raw_statuses = sorted({e.get("status", "<missing>") for e in events})
            print(f"  status={status!r:<12} total={len(events):>3} today={len(today):>3} "
                  f"raw_statuses={raw_statuses}")
            for e in today[:3]:
                print(f"      - {e.get('event_ticker')} status={e.get('status')!r} "
                      f"sub_title={e.get('sub_title', '')!r}")


def q2_markets_shape(client: KalshiClient, event_tickers: List[str]) -> None:
    """For each event_ticker, dump markets array to check if live games return markets=[]."""
    for et in event_tickers:
        section(f"Q2: markets shape for {et}")
        # Try each status — the event might only appear under one
        for status in [None, "open", "active"]:
            parts = et.split("-")
            series = parts[0] if parts else ""
            try:
                resp = client.get_events(
                    series_ticker=series, status=status,
                    with_nested_markets=True, limit=100,
                )
            except Exception as exc:
                print(f"  status={status!r} ERROR: {exc}")
                continue
            events = (resp or {}).get("events") or []
            matches = [e for e in events if e.get("event_ticker") == et]
            if not matches:
                print(f"  status={status!r:<10} NOT FOUND in response")
                continue
            e = matches[0]
            mkts = e.get("markets") or []
            print(f"  status={status!r:<10} event.status={e.get('status')!r}  "
                  f"markets_count={len(mkts)}")
            for m in mkts[:4]:
                print(f"      ticker={m.get('ticker')!r} "
                      f"status={m.get('status')!r} "
                      f"subtitle={m.get('subtitle') or m.get('title')!r} "
                      f"yes_bid={m.get('yes_bid')} yes_ask={m.get('yes_ask')} "
                      f"no_bid={m.get('no_bid')} no_ask={m.get('no_ask')}")


def q3_orderbook_shape(client: KalshiClient, market_tickers: List[str]) -> None:
    """Dump orderbook for each ticker — check if no_bids is populated."""
    for ticker in market_tickers:
        section(f"Q3: orderbook for {ticker}")
        try:
            ob = client.get_orderbook(ticker)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue
        if not ob:
            print("  empty response")
            continue
        book = (ob or {}).get("orderbook") or {}
        yes_bids = book.get("yes") or []
        no_bids = book.get("no") or []
        print(f"  yes_bids_count={len(yes_bids)} no_bids_count={len(no_bids)}")
        print(f"  yes_bids top 3: {yes_bids[:3]}")
        print(f"  no_bids  top 3: {no_bids[:3]}")


def pick_live_event_tickers(client: KalshiClient) -> List[str]:
    """Ask the user which event tickers to probe — or auto-pick today's for each series."""
    import datetime
    today_prefix = datetime.datetime.utcnow().strftime("%y%b%d").upper()
    picks: List[str] = []
    for series in SERIES_TO_PROBE:
        # Merge open + active
        got = set()
        for status in ("open", "active"):
            try:
                resp = client.get_events(
                    series_ticker=series, status=status,
                    with_nested_markets=False, limit=100,
                )
            except Exception:
                continue
            for e in (resp or {}).get("events") or []:
                et = e.get("event_ticker", "")
                if et.split("-", 1)[-1].startswith(today_prefix):
                    got.add(et)
        # Pick up to 2 per series — prefer ones that look "earlier today" (live)
        for et in sorted(got)[:2]:
            picks.append(et)
    return picks


def pick_market_tickers_for_events(
    client: KalshiClient, event_tickers: List[str]
) -> List[str]:
    """For each event, pull its markets and return their tickers."""
    market_tickers: List[str] = []
    for et in event_tickers:
        parts = et.split("-")
        series = parts[0] if parts else ""
        for status in ("active", "open"):
            try:
                resp = client.get_events(
                    series_ticker=series, status=status,
                    with_nested_markets=True, limit=100,
                )
            except Exception:
                continue
            for e in (resp or {}).get("events") or []:
                if e.get("event_ticker") != et:
                    continue
                for m in e.get("markets") or []:
                    t = m.get("ticker")
                    if t and t not in market_tickers:
                        market_tickers.append(t)
                break
            if market_tickers:
                break
    return market_tickers


def main() -> None:
    client = build_client()
    q1_status_sweep(client)

    event_tickers = pick_live_event_tickers(client)
    section(f"Auto-picked event_tickers to probe: {event_tickers}")
    if not event_tickers:
        print("No today's events found — aborting Q2/Q3.")
        return
    q2_markets_shape(client, event_tickers)

    market_tickers = pick_market_tickers_for_events(client, event_tickers)
    section(f"Auto-picked market_tickers to probe: {market_tickers[:6]}")
    q3_orderbook_shape(client, market_tickers[:6])


if __name__ == "__main__":
    main()
