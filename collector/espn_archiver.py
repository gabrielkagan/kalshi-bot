"""ESPN bronze archiver — D1.11.a (ticket 86ba0ppy0, 2026-05-19).

Second non-WS bronze source after D1.8 weather. Mirrors the structural
shape of ``WeatherArchiver`` adapted for the ESPN deltas:

  - **No persistent connection.** HTTP-poll loop fires one cycle per
    ``poll_once()`` call (driven by ``espn_main_loop`` on a 60-second
    cadence by default). ``_conn=None`` on every envelope (writer.py:245
    emits ``conn=none`` in the partition string for HTTP-polled sources).
  - **One channel per enabled non-esports league.** Channel name is
    the ESPN league slug verbatim (e.g. ``"nba"``, ``"eng.1"``,
    ``"uefa.champions"``). The per-channel partition path is
    ``bronze/espn/<league_slug>/year=YYYY/...``. Dots in path segments
    are legal under S3 + Linux + DuckDB Hive partitioning.
  - **Source name** ``espn`` (NEW slot — D0.3 §1 amended at D1.11.a
    ship; previously had no sports source reserved).
  - **Envelope via** ``kalshi_wire.ws_client.build_envelope`` per
    D0.3 §2 + the 2026-05-16 §5 AMENDMENT. Single source of truth
    for the 6-field envelope shape.
  - **Non-200 responses are bronze.** ESPN rate-limit responses (when
    they happen) get written with the diagnostic shape (``http_status``
    + ``error``); silver D3.x can model quota dynamics + correlate
    against the bot's poll bursts.
  - **Independent poller** — does NOT tee from
    ``bot/engines/sports_engine.py::ESPNLiveFeed``. D0.3 §10 invariant:
    bot failure ⇒ collector keeps capturing.

Per CLAUDE.md anti-patterns: synchronous (NO asyncio), threading-safe
via the ``BronzeWriter`` contract.

LEAGUES_ESPN drift discipline: the league_slug → sport map below
mirrors ``bot.engines.sports_data.LEAGUES`` for enabled
non-esports entries. The contract test
``tests/contracts/test_collector_espn_archiver.py::test_leagues_espn_mirrors_bot_leagues``
fails RED whenever the bot enables/disables a league or adds a new
one. Operator must update both sides + restart kalshi-espn-collector
on the VPS to pick up the new channel as a BronzeWriter.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from typing import Dict, Mapping, Optional, Sequence

import requests

from kalshi_wire.ws_client import build_envelope

logger = logging.getLogger(__name__)

# Bronze ``_source`` value pinned at module level. Mirrors the
# ``_BRONZE_SOURCE`` constant in weather_main_loop.py + bronze
# partition prefix at ``bronze/espn/``. D0.3 §1 amended at D1.11.a.
SOURCE: str = "espn"

# ESPN public site API base — SAME endpoint the bot polls in
# ``bot/engines/sports_engine.py:139``. Pinning the URL here (rather
# than reading from the bot) preserves the D0.3 §10 no-bot-import
# contract; the contract test pins the string so a future bot-side
# URL change surfaces as a contract failure that the operator must
# resolve in lock-step.
ESPN_BASE: str = "https://site.api.espn.com/apis/site/v2/sports"

# HTTP timeout for each per-league poll. Matches the bot's ESPN_TIMEOUT.
ESPN_TIMEOUT_SECONDS: float = 10.0

# Sleep between intra-cycle league calls to be polite + spread load
# across the 60s tick. Default 0.2s × 24 leagues = ~4.8s of inter-call
# sleep per cycle, plus per-call HTTP latency (~150ms each → ~3.6s);
# ~8.4s total cycle leaves ~52s idle within the 60s tick. The kwarg
# is plumbed test-injectable (pass 0.0 in unit-test fixtures to skip
# the polite-spread sleep when you want a deterministic poll_once()
# in <10ms).
DEFAULT_INTER_LEAGUE_SLEEP_SECONDS: float = 0.2

# League slug → sport mapping. Mirrors the enabled+espn-eligible
# subset of bot.engines.sports_data.LEAGUES (24 entries at D1.11.a
# ship; CSGO/LoL/Valorant/AFC-Intl have espn_league=None so are
# excluded). Drift-pinned by
# tests/contracts/test_collector_espn_archiver.py::test_leagues_espn_mirrors_bot_leagues.
LEAGUES_ESPN: Mapping[str, str] = {
    "atp": "tennis",
    "college-football": "football",
    "eng.1": "soccer",
    "esp.1": "soccer",
    "fifa.friendly": "soccer",
    "fifa.worldcup": "soccer",
    "fra.1": "soccer",
    "ger.1": "soccer",
    "ita.1": "soccer",
    "mens-college-basketball": "basketball",
    "mex.1": "soccer",
    "mlb": "baseball",
    "nba": "basketball",
    "ned.1": "soccer",
    "nfl": "football",
    "nhl": "hockey",
    "tur.1": "soccer",
    "uefa.champions": "soccer",
    "uefa.europa": "soccer",
    "uefa.europa.conf": "soccer",
    "ufc": "mma",
    "usa.1": "soccer",
    "wnba": "basketball",
    "wta": "tennis",
}

# Default channels list = sorted LEAGUES_ESPN keys. Exported as a
# tuple so silver D3.x parsers + the main loop can enumerate channels
# without instantiating the archiver. Sorted for diff stability.
DEFAULT_CHANNELS: tuple[str, ...] = tuple(sorted(LEAGUES_ESPN.keys()))


def _iso_utc_microsecond_z(ts: _dt.datetime) -> str:
    """Format a tz-aware UTC datetime as ISO-8601 with µs + trailing Z."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    else:
        ts = ts.astimezone(_dt.timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ESPNArchiver:
    """Polls ESPN per-league scoreboard endpoints and writes bronze
    envelopes.

    One ``ESPNArchiver`` instance is constructed by ``espn_main_loop.run``
    with a per-channel ``BronzeWriter`` map. ``poll_once()`` drives one
    cycle (all configured leagues); the main loop sleeps between
    cycles.
    """

    def __init__(
        self,
        *,
        writers_by_channel: Mapping[str, "object"],
        leagues: Optional[Sequence[str]] = None,
        inter_league_sleep_seconds: float = DEFAULT_INTER_LEAGUE_SLEEP_SECONDS,
        request_timeout_seconds: float = ESPN_TIMEOUT_SECONDS,
        now_fn=lambda: _dt.datetime.now(_dt.timezone.utc),
    ) -> None:
        self._writers_by_channel = dict(writers_by_channel)
        self._leagues: tuple[str, ...] = tuple(
            leagues if leagues is not None else DEFAULT_CHANNELS
        )
        for league in self._leagues:
            assert league in self._writers_by_channel, (
                f"ESPNArchiver: league {league!r} requested but no "
                f"writer in writers_by_channel "
                f"(keys: {list(self._writers_by_channel)})"
            )
            assert league in LEAGUES_ESPN, (
                f"ESPNArchiver: league {league!r} not in LEAGUES_ESPN — "
                f"add its sport mapping to "
                f"collector.espn_archiver.LEAGUES_ESPN (lock-step with "
                f"bot.engines.sports_data.LEAGUES)."
            )
        self._inter_league_sleep_seconds = inter_league_sleep_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._now_fn = now_fn
        self._session = requests.Session()
        # Mirror bot/engines/sports_engine.py:148 User-Agent. ESPN
        # returns 403 for some clients without a UA; keep parity with
        # the bot so a server-side allow/deny list applied to the bot
        # also applies to us.
        self._session.headers["User-Agent"] = "KalshiBot/1.0"
        # Monotone per-archiver collector_seq per D0.3 §2; resets on
        # process restart (silver QA cross-references against a boot
        # record stream — out of scope at D1.11.a).
        self._collector_seq: int = 0

    # ── Public API ────────────────────────────────────────────────────

    def poll_once(self) -> None:
        """Run one full poll cycle across all configured leagues.

        Best-effort posture: per-call exceptions are swallowed +
        WARN-logged so one league's failure doesn't abort the rest of
        the cycle. Rate-limit + connection errors get written as
        bronze records (with ``http_status`` or ``error`` diagnostic
        fields) — diagnostic data is itself signal.
        """
        for league in self._leagues:
            sport = LEAGUES_ESPN[league]
            self._poll_one(sport=sport, league=league)
            if self._inter_league_sleep_seconds > 0:
                time.sleep(self._inter_league_sleep_seconds)

    # ── Internals ─────────────────────────────────────────────────────

    def _poll_one(self, *, sport: str, league: str) -> None:
        """Fetch one ``/scoreboard`` and write an envelope.

        URL shape matches ``bot/engines/sports_engine.py::_poll_league``:
        ``{ESPN_BASE}/{sport}/{league}/scoreboard`` with no query params.
        """
        url = f"{ESPN_BASE}/{sport}/{league}/scoreboard"
        self._fetch_and_write(sport=sport, league=league, url=url)

    def _fetch_and_write(
        self,
        *,
        sport: str,
        league: str,
        url: str,
    ) -> None:
        """Fire one HTTP request + write one envelope, regardless of outcome.

        Builds the diagnostic-envelope payload (``sport`` + ``league`` +
        ``poll_ts`` + ``http_status`` + ``elapsed_ms`` + either ``response``
        on success or ``error`` on failure) and wraps it in the 6-field
        bronze envelope.

        Bronze captures the failure modes too — quota storms, connection
        errors, and parse errors are all signal for silver D3.x analysis.
        """
        poll_ts = self._now_fn()
        t0 = time.time()
        diag: Dict[str, object] = {
            "sport": sport,
            "league": league,
            "poll_ts": _iso_utc_microsecond_z(poll_ts),
        }
        try:
            resp = self._session.get(
                url, timeout=self._request_timeout_seconds,
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            diag["http_status"] = resp.status_code
            diag["elapsed_ms"] = elapsed_ms
            if resp.status_code == 200:
                try:
                    diag["response"] = resp.json()
                except (ValueError, json.JSONDecodeError) as exc:
                    diag["error"] = f"json_parse_error: {exc!r}"
            else:
                diag["error"] = f"http_{resp.status_code}"
        except requests.RequestException as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            diag["http_status"] = None
            diag["elapsed_ms"] = elapsed_ms
            diag["error"] = f"request_exception: {type(exc).__name__}: {exc!r}"

        self._write_envelope(channel=league, diag=diag, wire_recv_ts=poll_ts)

    def _write_envelope(
        self,
        *,
        channel: str,
        diag: Dict[str, object],
        wire_recv_ts: _dt.datetime,
    ) -> None:
        """Wrap the diagnostic payload in the bronze envelope + write."""
        self._collector_seq += 1
        # _raw is a STRING per D0.3 §2.
        raw_str = json.dumps(diag, separators=(",", ":"), ensure_ascii=False)
        envelope = build_envelope(
            raw=raw_str,
            source=SOURCE,
            channel=channel,
            conn=None,
            collector_seq=self._collector_seq,
            wire_recv_ts=wire_recv_ts,
        )
        writer = self._writers_by_channel[channel]
        writer.write(envelope)
