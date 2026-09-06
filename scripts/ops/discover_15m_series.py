#!/usr/bin/env python3
"""Daily 15M-series discovery — alert when Kalshi launches a 15-minute series
we are not tracking (ticket 86bbvdc8y, 2026-09-05).

WHY: on 2026-09-05 the operator noticed, by hand, that Kalshi had launched
KXNEAR15M / KXZEC15M (2026-06-30), KXGOLD/WTI/SILVER15M (2026-07-31),
KXCOPPER/NATGAS15M (2026-08-27) and KXCRYPTOLEAD15M (2026-08-20). The bot did
not know them and — because the collector's fast discovery poll was scoped to
a hand-mirrored crypto series list — the order-book bronze for all of them was
~zero for 2+ months. The collector side is now series-agnostic (close-horizon
sweep, same ticket); THIS script closes the human side: it runs daily on the
Mac (VPS-compute-isolation rule), diffs the venue's 15M series against a local
known-set, and alerts on anything new so onboarding never again depends on
someone noticing.

SOURCES (both public / unauthenticated; no Kalshi key on the Mac):
  1. ``GET /trade-api/v2/series`` (~16 MB, one call) → every series with
     ``frequency == "fifteen_min"`` OR ticker ending in ``15M``. PRIMARY —
     the venue's own catalog, complete by construction.
  2. The LATEST ``kalshi_rest/markets`` bronze chunk in S3 (as the ticket
     specified) → distinct ``KX*15M`` ticker prefixes among the open markets
     in that chunk. A chunk is 100 REST pages (~20K markets) of a ~950K-market
     page-through, so it is a PARTIAL view: it can only ADD prefixes (a series
     that appears there but not in /series is itself an anomaly worth an
     alert); absence from the chunk means nothing. Best-effort — any rclone /
     zstd failure is logged and skipped (``--no-bronze`` disables it).

FIRST-SEEN (corpus length per family): for every NEW series (not on the
bootstrap run) the script pages
``/markets?series_ticker=<S>&status=settled`` back to the earliest
``close_time`` (bounded to ``--max-first-seen-pages``) and records it in the
state file, so the alert says how much history already exists. Bronze
``market_lifecycle_v2`` cross-checks for the 2026-09-05 batch agreed with REST
to the day (numbers in the ``collector/rest_snapshot.py`` GENERALIZED note and
the ``agent_docs/bot_layout.md`` collector entries).

STATE: ``<state-dir>/known_series.json`` — ``{"series": {ticker: {...}},
"updated": iso}``. The FIRST run (no state file) is a bootstrap: it records
the current set and does NOT alert (otherwise every install would page on 27
"new" series). ``--dry-run`` never writes.

ALERT: Telegram if ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` are set (the
wrapper sources the repo ``.env`` so launchd sees them), else a macOS
notification; ALWAYS a durable sentinel ``<state-dir>/NEW_15M_SERIES_<stamp>.txt``
+ ``latest_report.json`` (monitor-the-monitor: launchd stdout/err go to
``<state-dir>/`` too, never ``/tmp``). The report also lists 15M series the BOT
does not trade (``bot.constants.SERIES_TICKERS``) — informational every run,
alert only on NEW-vs-state.

FAILURE IS LOUD (monitor-the-monitor): any exception in the run — catalog
fetch error, an empty/reshaped ``/series`` payload (0 fifteen-minute series is
never legitimate), a corrupt state file — writes ``<state-dir>/LAST_FAILURE.txt``,
raises the same alert channel with a ``FAILED`` prefix and exits 1. A corrupt
state file is renamed ``known_series.json.corrupt-<stamp>`` and is a FAILURE,
not a re-bootstrap (re-bootstrapping would swallow the next real alert).

Usage (launchd wrapper: scripts/ops/discover_15m_series.sh, plist:
scripts/ops/launchd/io.kalshi.15m-discovery.plist):
  python3 -m scripts.ops.discover_15m_series [--state-dir ~/kalshi-15m-discovery]
      [--no-bronze] [--dry-run] [--rclone-remote kalshi-restore:kalshi-bot-archive/bronze]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Self-bootstrap sys.path so ``from bot.constants import ...`` works when
# launched by launchd as a file path (memory: feedback_monitor_the_monitor).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_STATE_DIR = os.path.expanduser("~/kalshi-15m-discovery")
DEFAULT_RCLONE_REMOTE = "kalshi-restore:kalshi-bot-archive/bronze"
STATE_FILENAME = "known_series.json"
REPORT_FILENAME = "latest_report.json"

# ``KX...15M`` series prefix — the segment before the first ``-`` of a market
# ticker. ``KXGBPUSD15MTEST`` is a real (test) series and matches too; the
# /series source is authoritative on membership, this regex only extracts.
_SERIES_15M_RE = re.compile(r"^(KX[A-Z0-9]*15M[A-Z]*)(?:-|$)")


# ─── pure helpers (TDD-pinned in tests/contracts/test_discover_15m_series.py) ──


def fifteen_min_series_from_series_payload(payload: dict) -> Dict[str, dict]:
    """``{ticker: {title, category, frequency}}`` for every 15M series in a
    ``/series`` response. Membership = ``frequency == "fifteen_min"`` OR the
    ticker matches the ``KX…15M[suffix]`` shape (belt + braces: a mis-tagged
    frequency or a ``15MTEST`` suffix should still surface)."""
    out: Dict[str, dict] = {}
    for row in payload.get("series", []) or []:
        if not isinstance(row, dict):
            continue
        tk = row.get("ticker")
        if not isinstance(tk, str) or not tk:
            continue
        if row.get("frequency") == "fifteen_min" or _SERIES_15M_RE.match(tk):
            out[tk] = {
                "title": row.get("title"),
                "category": row.get("category"),
                "frequency": row.get("frequency"),
            }
    return out


def series_prefixes_from_rest_chunk_lines(lines: Iterable[str]) -> Set[str]:
    """Distinct ``KX*15M`` series prefixes across the ``response.markets``
    of every D1.9 ``kalshi_rest/markets`` bronze record (envelope ``_raw`` is
    a JSON string of ``{http_status, ..., response: {markets: [...]}}``).
    Malformed lines are skipped."""
    out: Set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            env = json.loads(line)
            raw = env.get("_raw") if isinstance(env, dict) else None
            inner = json.loads(raw) if isinstance(raw, str) else raw
            resp = inner.get("response") if isinstance(inner, dict) else None
            markets = resp.get("markets") if isinstance(resp, dict) else None
        except (ValueError, AttributeError):
            continue
        if not isinstance(markets, list):
            continue
        for m in markets:
            tk = m.get("ticker") if isinstance(m, dict) else None
            if isinstance(tk, str):
                mt = _SERIES_15M_RE.match(tk)
                if mt:
                    out.add(mt.group(1))
    return out


def diff_new_series(current: Iterable[str], known: Iterable[str]) -> List[str]:
    """Series present now that the state file has never recorded (sorted)."""
    return sorted(set(current) - set(known))


def bot_unknown_series(current: Iterable[str], bot_series: Iterable[str]) -> List[str]:
    """15M series the bot does not trade — informational, NOT an alert trigger."""
    return sorted(set(current) - set(bot_series))


def should_alert(new_series: Sequence[str], first_run: bool) -> bool:
    """Alert only on NEW series after bootstrap. The first run seeds the state
    file silently — paging on 27 'new' series at install time is noise."""
    return bool(new_series) and not first_run


def format_report(
    *, new_series: Sequence[str], current: Dict[str, dict],
    bot_unknown: Sequence[str], first_seen: Dict[str, dict], first_run: bool,
    bronze_only: Sequence[str] = (),
) -> str:
    lines: List[str] = []
    if first_run:
        lines.append(f"[15m-discovery] BOOTSTRAP — recorded {len(current)} 15M series; no alert.")
    elif new_series:
        lines.append(f"[15m-discovery] NEW 15M SERIES on Kalshi: {', '.join(new_series)}")
        for s in new_series:
            meta = current.get(s, {})
            fs = first_seen.get(s, {})
            lines.append(
                f"  - {s}: {meta.get('title')!s} [{meta.get('category')}] "
                f"first settled close={fs.get('earliest_close')} "
                f"settled_n={fs.get('settled_n')} days={fs.get('distinct_days')}"
            )
    else:
        lines.append(f"[15m-discovery] no new series ({len(current)} tracked).")
    if bronze_only:
        lines.append(
            "  ! prefixes in latest kalshi_rest bronze chunk but NOT in /series: "
            + ", ".join(bronze_only)
        )
    if bot_unknown:
        lines.append(f"  bot does not trade {len(bot_unknown)}: {', '.join(bot_unknown)}")
    return "\n".join(lines)


# ─── I/O helpers ────────────────────────────────────────────────────────────


def _get_json(url: str, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-bot-15m-discovery/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_series_catalog() -> dict:
    return _get_json(f"{KALSHI_API}/series")


def fetch_first_seen(series: str, *, max_pages: int = 20, sleep_s: float = 0.15) -> dict:
    """Earliest settled ``close_time`` for a series via public REST paging."""
    cursor: Optional[str] = None
    n = 0
    earliest: Optional[str] = None
    latest: Optional[str] = None
    days: Set[str] = set()
    for _ in range(max_pages):
        q = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        d = _get_json(f"{KALSHI_API}/markets?{urllib.parse.urlencode(q)}")
        ms = d.get("markets") or []
        for m in ms:
            ct = m.get("close_time")
            if not isinstance(ct, str):
                continue
            n += 1
            days.add(ct[:10])
            earliest = ct if earliest is None or ct < earliest else earliest
            latest = ct if latest is None or ct > latest else latest
        cursor = d.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(sleep_s)
    return {
        "earliest_close": earliest, "latest_close": latest,
        "settled_n": n, "distinct_days": len(days),
        "truncated": bool(cursor),
    }


def latest_rest_chunk_prefixes(remote: str) -> Tuple[Set[str], Optional[str]]:
    """(prefixes, chunk_path) from the lexicographically-latest
    ``kalshi_rest/markets`` chunk. Best-effort: returns (set(), None) on any
    rclone/zstd failure."""
    base = f"{remote.rstrip('/')}/kalshi_rest/markets"
    try:
        def _last(path: str) -> Optional[str]:
            out = subprocess.run(["rclone", "lsf", "--dirs-only", path],
                                 capture_output=True, text=True, timeout=120)
            items = sorted(x.strip("/") for x in out.stdout.split() if x.strip())
            return items[-1] if items else None
        y = _last(base)
        mo = _last(f"{base}/{y}") if y else None
        d = _last(f"{base}/{y}/{mo}") if mo else None
        if not d:
            return set(), None
        files = subprocess.run(
            ["rclone", "lsf", "-R", "--files-only", f"{base}/{y}/{mo}/{d}"],
            capture_output=True, text=True, timeout=300,
        ).stdout.split()
        files = sorted(f for f in files if f.endswith(".zst"))
        if not files:
            return set(), None
        chunk = f"{base}/{y}/{mo}/{d}/{files[-1]}"
        cat = subprocess.Popen(["rclone", "cat", chunk], stdout=subprocess.PIPE)
        try:
            zst = subprocess.Popen(["zstd", "-dc"], stdin=cat.stdout,
                                   stdout=subprocess.PIPE, text=True)
        except Exception:
            cat.kill()
            cat.wait(timeout=5)
            raise
        finally:
            # Parent must not hold the read end: zstd owns it now, and if zstd
            # failed to spawn rclone would otherwise block on a full pipe.
            if cat.stdout is not None:
                cat.stdout.close()
        assert zst.stdout is not None
        try:
            prefixes = series_prefixes_from_rest_chunk_lines(zst.stdout)
            zst.wait(timeout=300)
            cat.wait(timeout=300)
        except Exception:
            zst.kill()
            cat.kill()
            zst.wait(timeout=5)
            cat.wait(timeout=5)
            raise
        return prefixes, chunk
    except Exception as exc:  # best-effort secondary source
        print(f"[15m-discovery] bronze cross-check skipped: {exc!r}", file=sys.stderr)
        return set(), None


class StateCorrupt(RuntimeError):
    """Raised when the known-set file exists but cannot be parsed."""


def load_state(path: str) -> Optional[dict]:
    """``None`` when absent (bootstrap); ``StateCorrupt`` when unreadable —
    the corrupt file is moved aside to ``.corrupt-<stamp>`` and THIS run fails
    loudly. NOTE: the next scheduled run (24h later) WILL re-bootstrap silently
    from the venue catalog — act on the FAILED alert before then (restore the
    ``.corrupt-<stamp>`` file, or accept the reseed knowing any series that
    launched in between will not be flagged)."""
    try:
        with open(path) as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            os.replace(path, f"{path}.corrupt-{stamp}")
        except OSError:
            pass
        raise StateCorrupt(f"{path} unreadable ({exc!r}); moved aside as .corrupt-{stamp}")
    if not isinstance(state, dict) or not isinstance(state.get("series"), dict):
        raise StateCorrupt(f"{path} has unexpected shape (top-level {type(state).__name__})")
    return state


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _send_telegram(text: str) -> bool:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    try:
        data = json.dumps({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def raise_alert(text: str, state_dir: str, *, sentinel_prefix: str = "NEW_15M_SERIES") -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sentinel = os.path.join(state_dir, f"{sentinel_prefix}_{stamp}.txt")
    try:
        with open(sentinel, "w") as fh:
            fh.write(text + "\n")
    except OSError:
        pass
    if not _send_telegram(text):
        first_line = text.splitlines()[0].replace('"', "'") if text else "15m discovery"
        try:
            subprocess.run(["osascript", "-e",
                            f'display notification "{first_line} — see {state_dir}" '
                            'with title "Kalshi 15M discovery"'],
                           timeout=10, check=False)
        except Exception:
            pass
    print(f"[alert] sentinel={sentinel}")


# ─── main ───────────────────────────────────────────────────────────────────


def _parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    ap.add_argument("--rclone-remote", default=DEFAULT_RCLONE_REMOTE)
    ap.add_argument("--no-bronze", action="store_true", help="skip the S3 kalshi_rest chunk cross-check")
    ap.add_argument("--dry-run", action="store_true", help="never write state / sentinel")
    ap.add_argument("--max-first-seen-pages", type=int, default=20)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    """Failure wrapper: any exception → LAST_FAILURE.txt + FAILED alert + exit 1
    (monitor-the-monitor). ``run()`` is the happy path."""
    args = _parse_args(argv)
    os.makedirs(args.state_dir, exist_ok=True)
    try:
        return run(args)
    except Exception as exc:
        text = f"[15m-discovery] FAILED: {exc!r}"
        print(text, file=sys.stderr)
        try:
            with open(os.path.join(args.state_dir, "LAST_FAILURE.txt"), "w") as fh:
                fh.write(f"{datetime.now(timezone.utc).isoformat()} {text}\n")
        except OSError:
            pass
        if not args.dry_run:
            raise_alert(text, args.state_dir, sentinel_prefix="DISCOVERY_FAILED")
        return 1


def run(args) -> int:
    state_path = os.path.join(args.state_dir, STATE_FILENAME)
    state = load_state(state_path)  # raises StateCorrupt → main() alerts
    first_run = state is None
    known: Dict[str, dict] = dict((state or {}).get("series", {}))

    current = fifteen_min_series_from_series_payload(fetch_series_catalog())
    if not current:
        raise RuntimeError(
            "/series returned 0 fifteen-minute series — payload reshaped or empty; "
            "refusing to record an empty known-set")

    bronze_prefixes: Set[str] = set()
    chunk: Optional[str] = None
    if not args.no_bronze:
        bronze_prefixes, chunk = latest_rest_chunk_prefixes(args.rclone_remote)
    bronze_only = sorted(bronze_prefixes - set(current))
    for s in bronze_only:  # a series in bronze but not in /series is still a series
        current[s] = {"title": None, "category": None, "frequency": None, "source": "bronze_only"}

    new_series = diff_new_series(current, known)
    first_seen: Dict[str, dict] = {}
    # First-seen paging is a per-NEW-series cost (≤20 public requests each).
    # Skipped on the bootstrap run, where EVERY catalog series is "new" and
    # ~27 × 20 pages would be a pointless 3-minute fan-out; the corpus-length
    # numbers for the 2026-09-05 batch live in the finding doc.
    for s in ([] if first_run else new_series):
        try:
            first_seen[s] = fetch_first_seen(s, max_pages=args.max_first_seen_pages)
        except Exception as exc:
            first_seen[s] = {"error": repr(exc)}

    try:
        from bot.constants import SERIES_TICKERS
        bot_series = list(SERIES_TICKERS.values())
    except Exception:  # never let a bot import failure kill the discovery job
        bot_series = []
    bot_unknown = bot_unknown_series(current, bot_series) if bot_series else []

    report = format_report(new_series=new_series, current=current, bot_unknown=bot_unknown,
                           first_seen=first_seen, first_run=first_run, bronze_only=bronze_only)
    print(report)

    now_iso = datetime.now(timezone.utc).isoformat()
    for s in new_series:
        known[s] = {**current[s], "first_detected": now_iso, **first_seen.get(s, {})}
    if not args.dry_run:
        save_state(state_path, {"series": known, "updated": now_iso,
                                "bronze_chunk_checked": chunk})
        with open(os.path.join(args.state_dir, REPORT_FILENAME), "w") as fh:
            json.dump({"run_at": now_iso, "new_series": new_series, "bot_unknown": bot_unknown,
                       "bronze_only": bronze_only, "n_tracked": len(known),
                       "first_seen": first_seen}, fh, indent=1)
        if should_alert(new_series, first_run):
            raise_alert(report, args.state_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
