"""Sports comeback shadow-logging engine.

Fully independent from crypto trading. Runs as a daemon thread.
- Polls ESPN for live scores across all configured leagues
- Discovers Kalshi game-level markets and captures pregame prices
- Computes Bayesian comeback probabilities
- Logs everything to sports_shadow_log + evaluated_opportunities

NO reference to OrderExecutor. NO code path to place orders.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import timezone
from typing import Dict, List, Optional, Set, Tuple

import requests

from sports_data import (
    BINARY_ENTRY_CRITERIA,
    BINARY_LR_TABLE,
    CONSERVATIVE_LR_SCALE,
    LEAGUES,
    MAX_MODEL_MARKET_GAP,
    SPORT_GROUPS,
    SPORTS_EXCLUDED_GROUPS,
    SPORTS_SHADOW_MIN_LR,
    SPORTS_SHADOW_MIN_PRICE,
    SPORTS_SHADOW_SIGNAL_GROUPS,
    THREE_WAY_ENTRY_CRITERIA,
    THREE_WAY_LR_TABLE,
    LeagueConfig,
    SportGroupConfig,
    classify_deficit_binary,
    classify_deficit_tennis,
    classify_deficit_three_way,
    classify_strength,
    classify_time_remaining,
    get_sport_group_config,
    lookup_lr,
)


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class GameState:
    """Live game state from ESPN."""

    game_id: str
    league: str                 # series_ticker, e.g. "KXNBAGAME"
    home_team: str
    away_team: str
    home_code: str              # ESPN abbreviation, e.g. "LAL"
    away_code: str
    home_score: int
    away_score: int
    period: int
    clock: str                  # "5:30" or "45:00+2" etc.
    time_remaining_pct: float   # 0.0 to 1.0
    game_status: str            # "pre", "live", "final"
    scheduled_start: str        # ISO timestamp
    red_cards_home: int = 0
    red_cards_away: int = 0


@dataclass
class KalshiGameMarkets:
    """Kalshi market info for a game."""

    event_ticker: str
    # ticker → side ("home", "away", "draw")
    market_tickers: Dict[str, str] = field(default_factory=dict)
    # Pregame prices (captured before game start)
    pregame_price_home: Optional[float] = None
    pregame_price_away: Optional[float] = None
    pregame_price_draw: Optional[float] = None
    # Closing prices (captured at game end for CLV)
    closing_price_home: Optional[float] = None
    closing_price_away: Optional[float] = None


@dataclass
class ComebackSignal:
    """Output of the Bayesian comeback model."""

    comeback_prob: float
    prior: float
    likelihood_ratio: float
    edge: float                 # vs current Kalshi price
    fee_adjusted_edge: float
    deficit_bucket: str
    time_bucket: str
    strength_bucket: str
    signal_fired: bool
    filter_stage: str
    rejection_reason: Optional[str] = None
    simulated_contracts: int = 0
    simulated_risk: float = 0.0
    # Counterfactual fields for multi-threshold analysis
    would_signal_50c: bool = False
    would_signal_60c: bool = False
    would_signal_70c: bool = False
    would_signal_80c: bool = False
    would_signal_pregame_55: bool = False
    would_signal_pregame_65: bool = False
    shadow_lr_scale_50_posterior: float = 0.0
    shadow_lr_scale_50_signal: bool = False
    market_implied_prob: float = 0.0
    sport_group: str = ""
    sport_lr_scale: float = 0.2
    is_strong_config: bool = False


# Approximate game duration (seconds) for seconds_to_close estimation
_SPORT_DURATION_SEC = {
    "basketball": 2880, "hockey": 3600, "soccer": 5400,
    "baseball": 10800, "football": 3600, "tennis": 7200,
    "mma": 1500, "esports": 3600,
}

# ── ESPN Live Feed ───────────────────────────────────────────────────────────

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_TIMEOUT = 10  # seconds


class ESPNLiveFeed:
    """Polls ESPN scoreboard API for live game data across all leagues."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "KalshiBot/1.0"
        self._lock = threading.Lock()
        self._games: Dict[str, GameState] = {}  # game_id → GameState

    def poll_all_leagues(self) -> Dict[str, GameState]:
        """Poll ESPN for all enabled leagues. Returns game_id → GameState."""
        results: Dict[str, GameState] = {}
        for series, league_cfg in LEAGUES.items():
            if not league_cfg.enabled or not league_cfg.espn_league:
                continue
            try:
                games = self._poll_league(league_cfg)
                for g in games:
                    results[g.game_id] = g
            except Exception:
                logging.debug("ESPN poll failed for %s", league_cfg.display_name,
                              exc_info=True)
        with self._lock:
            self._games = results
        return results

    def _poll_league(self, cfg: LeagueConfig) -> List[GameState]:
        """Poll a single league's scoreboard."""
        url = f"{ESPN_BASE}/{cfg.espn_sport}/{cfg.espn_league}/scoreboard"
        t0 = time.time()
        resp = self._session.get(url, timeout=ESPN_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        elapsed_ms = (time.time() - t0) * 1000

        games: List[GameState] = []
        for event in data.get("events", []):
            try:
                if cfg.espn_sport == "tennis":
                    # ESPN returns all genders + doubles at a tournament.
                    # Filter to singles matching this series' gender.
                    want_type = ("Men's Singles" if cfg.espn_league == "atp"
                                 else "Women's Singles")
                    for grouping in event.get("groupings", []):
                        for comp in grouping.get("competitions", []):
                            comp_type = comp.get("type", {}).get("text", "")
                            if comp_type and comp_type != want_type:
                                continue
                            game = self._parse_tennis_match(comp, cfg, event)
                            if game:
                                games.append(game)
                else:
                    game = self._parse_event(event, cfg)
                    if game:
                        games.append(game)
            except Exception:
                logging.debug("Failed to parse ESPN event %s",
                              event.get("id", "?"), exc_info=True)
        return games

    def _parse_event(self, event: dict, cfg: LeagueConfig) -> Optional[GameState]:
        """Parse a single ESPN event into GameState."""
        competitions = event.get("competitions", [])
        if not competitions:
            return None
        comp = competitions[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        # ESPN lists home first, away second (usually)
        home = away = None
        for c in competitors:
            if c.get("homeAway") == "home":
                home = c
            elif c.get("homeAway") == "away":
                away = c
        if not home or not away:
            # Fallback: first = home, second = away
            home, away = competitors[0], competitors[1]

        home_team = home.get("team", {}).get("displayName", "Unknown")
        away_team = away.get("team", {}).get("displayName", "Unknown")
        home_code = home.get("team", {}).get("abbreviation", "???")
        away_code = away.get("team", {}).get("abbreviation", "???")
        home_score = int(home.get("score", "0"))
        away_score = int(away.get("score", "0"))

        # Status
        status_obj = comp.get("status", {})
        status_type = status_obj.get("type", {})
        state = status_type.get("state", "pre")  # "pre", "in", "post"
        game_status = {"pre": "pre", "in": "live", "post": "final"}.get(state, state)

        # Period/clock
        period = status_obj.get("period", 0)
        clock_str = status_obj.get("displayClock", "0:00")

        # Time remaining percentage
        time_pct = self._estimate_time_remaining(cfg, period, clock_str, game_status)

        # Scheduled start
        scheduled = event.get("date", "")

        # Red cards (soccer only)
        red_home = red_away = 0
        if cfg.espn_sport == "soccer":
            for stat in home.get("statistics", []):
                if stat.get("name") == "redCards":
                    red_home = int(stat.get("displayValue", "0"))
            for stat in away.get("statistics", []):
                if stat.get("name") == "redCards":
                    red_away = int(stat.get("displayValue", "0"))

        return GameState(
            game_id=str(event.get("id", "")),
            league=cfg.series_ticker,
            home_team=home_team,
            away_team=away_team,
            home_code=home_code,
            away_code=away_code,
            home_score=home_score,
            away_score=away_score,
            period=period,
            clock=clock_str,
            time_remaining_pct=time_pct,
            game_status=game_status,
            scheduled_start=scheduled,
            red_cards_home=red_home,
            red_cards_away=red_away,
        )

    @staticmethod
    def _estimate_time_remaining(cfg: LeagueConfig, period: int,
                                 clock: str, status: str) -> float:
        """Estimate fraction of game remaining (0.0 = over, 1.0 = not started)."""
        if status == "pre":
            return 1.0
        if status == "final":
            return 0.0

        sport = cfg.espn_sport
        try:
            # Parse clock "MM:SS", "M:SS", or soccer "36'" / "90'+4'"
            parts = clock.replace("'", "").replace("+", ":").split(":")
            minutes = int(parts[0]) if parts else 0
            seconds = int(parts[1]) if len(parts) > 1 else 0
            clock_secs = minutes * 60 + seconds
        except (ValueError, IndexError):
            clock_secs = 0

        if sport == "basketball":
            if cfg.espn_league in ("nba", "wnba"):
                # 4 x 12min quarters = 48 min total. Clock counts down.
                total_secs = 48 * 60
                elapsed = (period - 1) * 12 * 60 + (12 * 60 - clock_secs)
            else:
                # College: 2 x 20min halves = 40 min. Clock counts down.
                total_secs = 40 * 60
                elapsed = (period - 1) * 20 * 60 + (20 * 60 - clock_secs)
        elif sport == "hockey":
            # 3 x 20min periods. Clock counts down.
            total_secs = 60 * 60
            elapsed = (period - 1) * 20 * 60 + (20 * 60 - clock_secs)
        elif sport == "soccer":
            # 2 x 45min halves = 90 min. Clock counts UP.
            total_secs = 90 * 60
            elapsed = clock_secs
            if period >= 2:
                elapsed = max(elapsed, 45 * 60)  # At least into second half
        elif sport == "football":
            # 4 x 15min quarters. Clock counts down.
            total_secs = 60 * 60
            elapsed = (period - 1) * 15 * 60 + (15 * 60 - clock_secs)
        elif sport == "baseball":
            # ~9 innings, estimate 18 half-innings
            total_half_innings = 18
            # period = inning, top/bottom encoded in status
            elapsed_half = max(0, (period - 1) * 2)
            return max(0.0, 1.0 - elapsed_half / total_half_innings)
        elif sport == "mma":
            # UFC: 3 or 5 round fights, 5 min per round. Clock counts down.
            total_secs = 15 * 60  # Assume 3 rounds
            elapsed = (period - 1) * 5 * 60 + (5 * 60 - clock_secs)
        elif sport == "tennis":
            # Fallback: period-based estimate (best-of-3 default)
            max_sets = 3
            return max(0.0, min(1.0, 1.0 - (period - 1) / max_sets))
        else:
            return 0.5  # Unknown sport — conservative estimate

        return max(0.0, min(1.0, 1.0 - elapsed / total_secs))

    def _parse_tennis_match(self, comp: dict, cfg: LeagueConfig,
                            event: dict) -> Optional[GameState]:
        """Parse a single tennis competition (match) into GameState.

        Tennis ESPN structure differs from team sports:
        - Events are tournaments, competitions are individual matches
        - Players accessed via competitor["athlete"], not competitor["team"]
        - Score = sets won (count linescores with winner=True)
        - No clock — time estimated from sets + games in current set
        """
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        home = away = None
        for c in competitors:
            if c.get("homeAway") == "home":
                home = c
            elif c.get("homeAway") == "away":
                away = c
        if not home or not away:
            home, away = competitors[0], competitors[1]

        # Player names and derived codes
        home_name = home.get("athlete", {}).get("displayName", "Unknown")
        away_name = away.get("athlete", {}).get("displayName", "Unknown")
        # Skip TBD/unknown players — bracket placeholders can't match Kalshi
        if home_name in ("TBD", "Unknown") or away_name in ("TBD", "Unknown"):
            return None
        home_code = _tennis_player_code(home_name)
        away_code = _tennis_player_code(away_name)

        # Score = sets won (count linescores with winner=True)
        home_ls = home.get("linescores", [])
        away_ls = away.get("linescores", [])
        home_sets = sum(1 for s in home_ls if s.get("winner", False))
        away_sets = sum(1 for s in away_ls if s.get("winner", False))

        # When sets are even, use current set games for granular deficit.
        # Sets even + down 2-5 in games → deficit=3, generates signals.
        # Sets uneven → set deficit drives (existing behavior).
        if home_sets == away_sets and home_ls:
            last_home = home_ls[-1] if home_ls else {}
            last_away = away_ls[-1] if away_ls else {}
            h_games = int(last_home.get("value", "0") or "0")
            a_games = int(last_away.get("value", "0") or "0")
            home_score = h_games
            away_score = a_games
        else:
            home_score = home_sets
            away_score = away_sets

        # Status
        status_obj = comp.get("status", {})
        status_type = status_obj.get("type", {})
        state = status_type.get("state", "pre")
        game_status = {"pre": "pre", "in": "live", "post": "final"}.get(state, state)

        period = status_obj.get("period", 0)

        # Best-of: ATP slams are best-of-5, everything else best-of-3
        max_sets = 3

        # Time remaining from set/game progress
        time_pct = self._tennis_time_remaining(
            status=game_status, home_sets=home_sets, away_sets=away_sets,
            home_ls=home_ls, away_ls=away_ls, max_sets=max_sets,
        )

        # Use competition ID (unique match), not event ID (tournament-level)
        comp_id = str(comp.get("id", event.get("id", "")))

        return GameState(
            game_id=comp_id,
            league=cfg.series_ticker,
            home_team=home_name,
            away_team=away_name,
            home_code=home_code,
            away_code=away_code,
            home_score=home_score,
            away_score=away_score,
            period=period,
            clock="",
            time_remaining_pct=time_pct,
            game_status=game_status,
            scheduled_start=event.get("date", ""),
        )

    @staticmethod
    def _tennis_time_remaining(status: str, home_sets: int, away_sets: int,
                               home_ls: list, away_ls: list,
                               max_sets: int) -> float:
        """Estimate fraction of tennis match remaining.

        Uses completed sets + games in current set as progress indicator.
        No clock in tennis — this is a coarse estimate.
        """
        if status == "pre":
            return 1.0
        if status == "final":
            return 0.0

        completed_sets = home_sets + away_sets
        # Games in current set (last linescore entry)
        current_set_games = 0
        if home_ls:
            last_home = home_ls[-1] if home_ls else {}
            last_away = away_ls[-1] if away_ls else {}
            h_games = int(last_home.get("value", "0") or "0")
            a_games = int(last_away.get("value", "0") or "0")
            current_set_games = h_games + a_games

        # Progress: completed sets + partial credit for current set games
        # Assume ~10 games per set as denominator (covers tiebreaks)
        progress = (completed_sets + current_set_games / 10.0) / max_sets
        return max(0.0, min(1.0, 1.0 - progress))


# ── Kalshi Sports Discovery ─────────────────────────────────────────────────

class KalshiSportsDiscovery:
    """Discovers and caches Kalshi game-level markets."""

    def __init__(self, kalshi_client):
        self._client = kalshi_client
        self._lock = threading.Lock()
        # event_ticker → KalshiGameMarkets
        self._markets: Dict[str, KalshiGameMarkets] = {}
        self._last_refresh: float = 0.0
        self._refresh_interval: float = 300.0  # 5 minutes
        self._last_api_call: float = 0.0

    def _throttled_api_call(self, func, *args, **kwargs):
        """Ensure at most 2 API calls per second for sports."""
        now = time.time()
        elapsed = now - self._last_api_call
        if elapsed < 0.5:
            time.sleep(0.5 - elapsed)
        self._last_api_call = time.time()
        return func(*args, **kwargs)

    def refresh_if_needed(self) -> None:
        """Refresh market discovery if stale."""
        now = time.time()
        if now - self._last_refresh < self._refresh_interval:
            return
        self._last_refresh = now

        for series, league_cfg in LEAGUES.items():
            if not league_cfg.enabled:
                continue
            try:
                resp = self._throttled_api_call(
                    self._client.get_events,
                    series_ticker=league_cfg.series_ticker,
                    status="open",
                    with_nested_markets=True,
                )
                if not resp or "events" not in resp:
                    continue

                for event in resp["events"]:
                    event_ticker = event.get("event_ticker", "")
                    if not event_ticker:
                        continue

                    markets = event.get("markets", [])
                    if not markets:
                        continue

                    game_markets = KalshiGameMarkets(event_ticker=event_ticker)
                    for mkt in markets:
                        ticker = mkt.get("ticker", "")
                        # Parse team side from ticker or subtitle
                        subtitle = mkt.get("subtitle", "") or mkt.get("title", "")
                        side = self._classify_market_side(ticker, subtitle, league_cfg)
                        if side:
                            game_markets.market_tickers[ticker] = side

                    with self._lock:
                        if event_ticker not in self._markets:
                            self._markets[event_ticker] = game_markets
                        else:
                            # Merge tickers (don't overwrite pregame prices)
                            existing = self._markets[event_ticker]
                            existing.market_tickers.update(game_markets.market_tickers)

            except Exception:
                logging.debug("Kalshi discovery failed for %s",
                              league_cfg.series_ticker, exc_info=True)

    def _classify_market_side(self, ticker: str, subtitle: str,
                              cfg: LeagueConfig) -> Optional[str]:
        """Classify market as home/away/draw from ticker or title text."""
        lower = subtitle.lower()
        if "draw" in lower or "tie" in lower:
            return "draw"
        # For binary: first market = usually home, second = away
        # This is a heuristic — team codes in tickers are more reliable
        if "win" in lower or "yes" in lower:
            return "home"  # Default; will be refined by matching to ESPN data
        return "home"  # Fallback

    def get_markets_for_event(self, event_ticker: str) -> Optional[KalshiGameMarkets]:
        """Get cached market info for an event."""
        with self._lock:
            return self._markets.get(event_ticker)

    def get_all_markets(self) -> Dict[str, KalshiGameMarkets]:
        """Get all cached markets."""
        with self._lock:
            return dict(self._markets)

    def capture_pregame_price(self, event_ticker: str, side: str,
                              price: float) -> None:
        """Store pregame price snapshot for a market side."""
        with self._lock:
            mkts = self._markets.get(event_ticker)
            if not mkts:
                return
            if side == "home" and mkts.pregame_price_home is None:
                mkts.pregame_price_home = price
            elif side == "away" and mkts.pregame_price_away is None:
                mkts.pregame_price_away = price
            elif side == "draw" and mkts.pregame_price_draw is None:
                mkts.pregame_price_draw = price

    def capture_closing_price(self, event_ticker: str, side: str,
                              price: float) -> None:
        """Store closing price for CLV computation."""
        with self._lock:
            mkts = self._markets.get(event_ticker)
            if not mkts:
                return
            if side == "home":
                mkts.closing_price_home = price
            elif side == "away":
                mkts.closing_price_away = price

    def get_orderbook_snapshot(self, ticker: str) -> Optional[Dict]:
        """Fetch orderbook for a specific market ticker."""
        try:
            return self._throttled_api_call(self._client.get_orderbook, ticker)
        except Exception:
            logging.debug("Orderbook fetch failed for %s", ticker, exc_info=True)
            return None


# ── Bayesian Comeback Model ─────────────────────────────────────────────────

class BayesianComebackModel:
    """Computes posterior comeback probability using Bayesian update."""

    # Fee formula: ceil(0.07 * C * P * (1-P)) — same as crypto taker
    FEE_MULTIPLIER = 0.07

    def evaluate(self, game: GameState, league_cfg: LeagueConfig,
                 pregame_fav_prob: float, current_kalshi_price: float,
                 deficit: int,
                 sport_group_cfg: Optional[SportGroupConfig] = None) -> ComebackSignal:
        """Evaluate a comeback opportunity.

        Args:
            game: Current game state
            league_cfg: League configuration
            pregame_fav_prob: Pregame probability of favorite winning (0-1)
            current_kalshi_price: Current Kalshi price for favorite in cents (0-100)
            deficit: How many points/goals favorite is trailing by (positive)
        """
        outcome_type = league_cfg.outcome_type

        # Classify into buckets
        if outcome_type == "three_way":
            deficit_bucket = classify_deficit_three_way(deficit)
        elif league_cfg.espn_sport == "tennis":
            deficit_bucket = classify_deficit_tennis(deficit)
        else:
            deficit_bucket = classify_deficit_binary(deficit)

        time_bucket = classify_time_remaining(game.time_remaining_pct)
        strength_bucket = classify_strength(pregame_fav_prob, outcome_type)

        # Look up LR (per-sport scale)
        sgc = sport_group_cfg or get_sport_group_config(league_cfg)
        lr = lookup_lr(outcome_type, deficit_bucket, time_bucket, strength_bucket,
                       lr_scale=sgc.lr_scale)

        # Bayesian update: posterior = (prior * LR) / (prior * LR + (1 - prior))
        prior = pregame_fav_prob
        numerator = prior * lr
        denominator = numerator + (1 - prior)
        posterior = numerator / denominator if denominator > 0 else prior

        # Edge vs current Kalshi price
        current_prob = current_kalshi_price / 100.0
        edge = posterior - current_prob

        # Fee-adjusted edge
        p = current_kalshi_price / 100.0
        fee_cents = math.ceil(self.FEE_MULTIPLIER * 1 * p * (1 - p))
        fee_pct = fee_cents / 100.0
        fee_adjusted_edge = edge - fee_pct

        # Entry criteria check
        criteria = (THREE_WAY_ENTRY_CRITERIA if outcome_type == "three_way"
                    else BINARY_ENTRY_CRITERIA)

        signal_fired = False
        filter_stage = "sports_observation"
        rejection_reason = None

        # Check all criteria
        if pregame_fav_prob < criteria["min_pregame_prob"]:
            filter_stage = "sports_weak_favorite"
            rejection_reason = (
                f"pregame_prob={pregame_fav_prob:.3f} < "
                f"{criteria['min_pregame_prob']}")
        elif current_kalshi_price > criteria["max_kalshi_fav_price"]:
            filter_stage = "sports_price_too_high"
            rejection_reason = (
                f"kalshi_price={current_kalshi_price}c > "
                f"{criteria['max_kalshi_fav_price']}c")
        elif game.time_remaining_pct < criteria["min_time_remaining_pct"]:
            filter_stage = "sports_late_game"
            rejection_reason = (
                f"time_remaining={game.time_remaining_pct:.2f} < "
                f"{criteria['min_time_remaining_pct']}")
        elif outcome_type == "three_way" and deficit > criteria["max_deficit_goals"]:
            filter_stage = "sports_deficit_too_large"
            rejection_reason = f"deficit={deficit} > {criteria['max_deficit_goals']} goals"
        elif outcome_type == "binary" and deficit > 15:
            filter_stage = "sports_deficit_too_large"
            rejection_reason = f"deficit={deficit} > 15 (blowout)"
        elif fee_adjusted_edge <= 0:
            filter_stage = "sports_insufficient_edge"
            rejection_reason = (
                f"fee_adj_edge={fee_adjusted_edge:.4f} <= 0 "
                f"(raw_edge={edge:.4f}, fee={fee_pct:.4f})")
        elif posterior - current_prob > MAX_MODEL_MARKET_GAP:
            filter_stage = "sports_model_overconfident"
            rejection_reason = (
                f"model_market_gap={posterior - current_prob:.4f} > "
                f"{MAX_MODEL_MARKET_GAP}")
        elif sgc.group_name not in SPORTS_SHADOW_SIGNAL_GROUPS:
            filter_stage = "sports_excluded_sport"
            rejection_reason = (
                f"sport_group={sgc.group_name} not in "
                f"{SPORTS_SHADOW_SIGNAL_GROUPS}")
        elif current_kalshi_price < SPORTS_SHADOW_MIN_PRICE:
            filter_stage = "sports_excluded_price"
            rejection_reason = (
                f"kalshi_price={current_kalshi_price}c < "
                f"{SPORTS_SHADOW_MIN_PRICE}c min")
        elif lr < SPORTS_SHADOW_MIN_LR:
            filter_stage = "sports_excluded_lr"
            rejection_reason = (
                f"lr={lr:.4f} < {SPORTS_SHADOW_MIN_LR} min")
        else:
            signal_fired = True
            filter_stage = "sports_signal"

        # Simulated sizing (observation only — never executed)
        sim_contracts = 0
        sim_risk = 0.0
        if signal_fired:
            # Simple fixed sizing for shadow: 10 contracts at current ask
            sim_contracts = 10
            sim_risk = sim_contracts * current_kalshi_price

        # ── Counterfactual analysis ──────────────────────────────────────
        # Base checks that are common to all price thresholds
        _time_ok = game.time_remaining_pct >= criteria["min_time_remaining_pct"]
        _deficit_ok = True
        if outcome_type == "three_way" and deficit > criteria.get("max_deficit_goals", 99):
            _deficit_ok = False
        elif outcome_type == "binary" and deficit > 15:
            _deficit_ok = False
        _base_ok = _time_ok and _deficit_ok and fee_adjusted_edge > 0 and (
            posterior - current_prob <= MAX_MODEL_MARKET_GAP)

        # Would signal fire at each price ceiling?
        would_signal_50c = _base_ok and current_kalshi_price <= 50 and pregame_fav_prob >= criteria["min_pregame_prob"]
        would_signal_60c = _base_ok and current_kalshi_price <= 60 and pregame_fav_prob >= criteria["min_pregame_prob"]
        would_signal_70c = _base_ok and current_kalshi_price <= 70 and pregame_fav_prob >= criteria["min_pregame_prob"]
        would_signal_80c = _base_ok and current_kalshi_price <= 80 and pregame_fav_prob >= criteria["min_pregame_prob"]

        # Would signal fire at each pregame threshold?
        would_signal_pregame_55 = _base_ok and current_kalshi_price <= criteria["max_kalshi_fav_price"] and pregame_fav_prob >= 0.55
        would_signal_pregame_65 = _base_ok and current_kalshi_price <= criteria["max_kalshi_fav_price"] and pregame_fav_prob >= 0.65

        # Shadow LR scale=0.5 posterior (compare against live scale=0.2)
        shadow_lr_raw = 1.0
        key = (deficit_bucket, time_bucket, strength_bucket)
        if outcome_type == "three_way":
            shadow_lr_raw = THREE_WAY_LR_TABLE.get(key, 1.0)
        else:
            shadow_lr_raw = BINARY_LR_TABLE.get(key, 1.0)
        shadow_lr_50 = 1.0 + (shadow_lr_raw - 1.0) * 0.5
        shadow_num = prior * shadow_lr_50
        shadow_den = shadow_num + (1 - prior)
        shadow_lr_scale_50_posterior = shadow_num / shadow_den if shadow_den > 0 else prior
        shadow_edge_50 = shadow_lr_scale_50_posterior - current_prob - fee_pct
        shadow_lr_scale_50_signal = (
            _base_ok and shadow_edge_50 > 0
            and current_kalshi_price <= criteria["max_kalshi_fav_price"]
            and pregame_fav_prob >= criteria["min_pregame_prob"]
            and (shadow_lr_scale_50_posterior - current_prob) <= MAX_MODEL_MARKET_GAP
        )

        market_implied_prob = current_kalshi_price / 100.0

        # Composite flag: pregame >= 60%, price <= 70c, time > 25%
        is_strong_config = (
            pregame_fav_prob >= 0.60
            and current_kalshi_price <= 70
            and game.time_remaining_pct > 0.25
        )

        return ComebackSignal(
            comeback_prob=posterior,
            prior=prior,
            likelihood_ratio=lr,
            edge=edge,
            fee_adjusted_edge=fee_adjusted_edge,
            deficit_bucket=deficit_bucket,
            time_bucket=time_bucket,
            strength_bucket=strength_bucket,
            signal_fired=signal_fired,
            filter_stage=filter_stage,
            rejection_reason=rejection_reason,
            simulated_contracts=sim_contracts,
            simulated_risk=sim_risk,
            would_signal_50c=would_signal_50c,
            would_signal_60c=would_signal_60c,
            would_signal_70c=would_signal_70c,
            would_signal_80c=would_signal_80c,
            would_signal_pregame_55=would_signal_pregame_55,
            would_signal_pregame_65=would_signal_pregame_65,
            shadow_lr_scale_50_posterior=shadow_lr_scale_50_posterior,
            shadow_lr_scale_50_signal=shadow_lr_scale_50_signal,
            market_implied_prob=market_implied_prob,
            sport_group=sgc.group_name,
            sport_lr_scale=sgc.lr_scale,
            is_strong_config=is_strong_config,
        )


class PlattCalibrator:
    """Platt scaling (logistic recalibration) for sports comeback probabilities.

    Fits a 2-parameter logistic: P(win | raw_prob) = 1 / (1 + exp(-(a*x + b)))
    where x = raw comeback_prob, target = fav_won (0/1).

    - Fits on H1 (first half of settled data by evaluation_time)
    - Validates on H2 (second half)
    - Logs both raw and calibrated probs for comparison
    - No sklearn dependency — manual MLE via gradient descent
    """

    MIN_TRAINING_ROWS = 30
    REFIT_INTERVAL_SEC = 6 * 3600  # Refit every 6 hours

    def __init__(self):
        self._a: float = 1.0  # slope (start at identity-ish)
        self._b: float = 0.0  # intercept
        self._fitted: bool = False
        self._n_train: int = 0
        self._h1_brier: float = 0.0
        self._h2_brier_raw: float = 0.0
        self._h2_brier_cal: float = 0.0
        self._last_fit_time: float = 0.0

    def fit_from_db(self, conn: sqlite3.Connection) -> bool:
        """Fit on settled sports_shadow_log rows with H1/H2 split."""
        try:
            rows = conn.execute(
                "SELECT comeback_prob, fav_won FROM sports_shadow_log "
                "WHERE fav_won IS NOT NULL AND comeback_prob IS NOT NULL "
                "AND filter_stage NOT IN ('sports_fav_leading') "
                "ORDER BY evaluation_time"
            ).fetchall()
        except Exception:
            logging.warning("PlattCalibrator: DB query failed", exc_info=True)
            return False

        n = len(rows)
        if n < self.MIN_TRAINING_ROWS:
            logging.info("PlattCalibrator: only %d rows, need %d — skipping fit",
                         n, self.MIN_TRAINING_ROWS)
            return False

        probs = [float(r[0]) for r in rows]
        targets = [int(r[1]) for r in rows]

        # H1/H2 split
        mid = n // 2
        x_h1, y_h1 = probs[:mid], targets[:mid]
        x_h2, y_h2 = probs[mid:], targets[mid:]

        # Fit logistic on H1 via gradient descent
        a, b = self._fit_logistic(x_h1, y_h1)
        if a is None:
            logging.warning("PlattCalibrator: fitting failed")
            return False

        self._a = a
        self._b = b
        self._fitted = True
        self._n_train = len(x_h1)
        self._last_fit_time = time.time()

        # Compute Brier scores for validation
        self._h1_brier = self._brier(x_h1, y_h1, a, b)
        self._h2_brier_raw = sum(
            (p - y) ** 2 for p, y in zip(x_h2, y_h2)) / len(x_h2)
        self._h2_brier_cal = self._brier(x_h2, y_h2, a, b)

        logging.info(
            "PlattCalibrator: fitted on %d H1 rows (a=%.3f, b=%.3f). "
            "H2 Brier raw=%.4f → cal=%.4f (Δ=%+.4f, n=%d)",
            len(x_h1), a, b,
            self._h2_brier_raw, self._h2_brier_cal,
            self._h2_brier_cal - self._h2_brier_raw, len(x_h2))

        # Persist params for dashboard snapshot
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sports_platt_params "
                "(id, a, b, n_train, h1_brier, h2_brier_raw, h2_brier_cal, "
                "fitted, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, 1, ?)",
                (a, b, len(x_h1), self._h1_brier, self._h2_brier_raw,
                 self._h2_brier_cal, datetime.utcnow().isoformat()))
            conn.commit()
        except Exception:
            logging.warning("PlattCalibrator: failed to persist params", exc_info=True)

        return True

    @staticmethod
    def _fit_logistic(x: list, y: list,
                      lr: float = 0.5, n_iter: int = 200) -> Tuple:
        """Fit 2-param logistic via gradient descent with MLE.

        Returns (a, b) or (None, None) on failure.
        """
        a, b = 1.0, 0.0  # Start near identity
        n = len(x)
        if n == 0:
            return None, None
        eps = 1e-12
        for _ in range(n_iter):
            grad_a, grad_b = 0.0, 0.0
            for xi, yi in zip(x, y):
                logit = a * xi + b
                # Clip to avoid overflow
                logit = max(-20.0, min(20.0, logit))
                pred = 1.0 / (1.0 + math.exp(-logit))
                pred = max(eps, min(1.0 - eps, pred))
                err = yi - pred
                grad_a += err * xi
                grad_b += err
            a += lr * grad_a / n
            b += lr * grad_b / n
        return a, b

    @staticmethod
    def _brier(x: list, y: list, a: float, b: float) -> float:
        """Compute Brier score using calibrated probs."""
        n = len(x)
        if n == 0:
            return 0.0
        total = 0.0
        for xi, yi in zip(x, y):
            logit = max(-20.0, min(20.0, a * xi + b))
            pred = 1.0 / (1.0 + math.exp(-logit))
            total += (pred - yi) ** 2
        return total / n

    def calibrate(self, raw_prob: float) -> float:
        """Apply Platt scaling. Returns raw_prob if not fitted."""
        if not self._fitted:
            return raw_prob
        logit = self._a * raw_prob + self._b
        logit = max(-20.0, min(20.0, logit))
        return 1.0 / (1.0 + math.exp(-logit))

    def needs_refit(self) -> bool:
        """Whether enough time has passed to refit."""
        return time.time() - self._last_fit_time > self.REFIT_INTERVAL_SEC

    @property
    def diagnostics(self) -> Dict:
        """Return diagnostics for logging/dashboard."""
        return {
            "fitted": self._fitted,
            "a": round(self._a, 4),
            "b": round(self._b, 4),
            "n_train": self._n_train,
            "h1_brier": round(self._h1_brier, 4),
            "h2_brier_raw": round(self._h2_brier_raw, 4),
            "h2_brier_cal": round(self._h2_brier_cal, 4),
        }


def _tennis_player_code(display_name: str) -> str:
    """Derive 3-letter code from player name for Kalshi ticker matching.

    Kalshi uses first 3 letters of last name (uppercased), alpha only:
      "Jannik Sinner"         → "SIN"
      "Alex de Minaur"        → "DEM"  (multi-word surnames concatenated)
      "Coco Gauff"            → "GAU"
      "Christopher O'Connell" → "OCO"  (apostrophe stripped)
    """
    parts = display_name.strip().split()
    last_name = "".join(parts[1:]) if len(parts) > 1 else parts[0]
    # Strip non-alpha characters (apostrophes, hyphens, periods, etc.)
    alpha_only = re.sub(r"[^A-Za-z]", "", last_name)
    return alpha_only[:3].upper()


# ESPN uses short 2-letter codes for some teams; Kalshi uses 3-letter codes.
# Without expansion, "NY" fails word-boundary matching against "NYK" in tickers,
# and "LA" false-positives as substring of "COLA" in event tickers.
_ESPN_TO_KALSHI: Dict[str, str] = {
    "GS": "GSW",   # Golden State Warriors (NBA)
    "NY": "NYK",   # New York Knicks (NBA) — NYR/NYI handled by series_ticker filter
    "NO": "NOP",   # New Orleans Pelicans (NBA)
    "LA": "LAK",   # Los Angeles Kings (NHL)
    "SA": "SAS",   # San Antonio Spurs (NBA)
}


def _expand_code(code: str) -> str:
    """Expand short ESPN codes to Kalshi equivalents for matching."""
    return _ESPN_TO_KALSHI.get(code.upper(), code.upper())


def _team_code_in_ticker(code: str, ticker_upper: str) -> bool:
    """Check if a team code appears in a ticker with word-boundary matching.

    Prevents false positives like "NY" matching "ANYTOWN".
    Team codes are delimited by non-alpha chars or string boundaries in Kalshi tickers.
    Works for MARKET tickers (e.g. "KXNBAGAME-26MAR03BKNMIA-BKN" — hyphen separator).
    Does NOT work for EVENT tickers where codes are concatenated ("BKNMIA" has no separator).
    Use _event_matches_game() for event-level matching instead.

    Tries expanded Kalshi code (e.g. NY→NYK) if direct match fails.
    """
    expanded = _expand_code(code)
    if bool(re.search(rf'(?:^|[^A-Z]){re.escape(expanded)}(?:[^A-Z]|$)', ticker_upper)):
        return True
    # Also try original code if expansion changed it (handles unknown Kalshi codes)
    if expanded != code.upper():
        return bool(re.search(rf'(?:^|[^A-Z]){re.escape(code.upper())}(?:[^A-Z]|$)', ticker_upper))
    return False


def _event_matches_game(home_code: str, away_code: str, event_ticker: str,
                         series_ticker: str = "") -> bool:
    """Check if a Kalshi event ticker matches an ESPN game.

    Kalshi event tickers concatenate both team codes: "KXNBAGAME-26MAR03BKNMIA".
    Requires BOTH team codes present (substring match) to avoid false positives
    where one team appears in a different game's event ticker.

    Uses expanded Kalshi codes (e.g. NO→NOP) to prevent false positives from
    short codes matching inside other words (e.g. "LA" in "COLA").

    When series_ticker is provided, the event ticker must start with it to prevent
    cross-series matches (e.g., MLS PHI matching NBA KXNBAGAME).
    """
    et_upper = event_ticker.upper()
    if series_ticker and not et_upper.startswith(series_ticker.upper()):
        return False
    home_exp = _expand_code(home_code)
    away_exp = _expand_code(away_code)
    return home_exp in et_upper and away_exp in et_upper


# ── Main Sports Engine ───────────────────────────────────────────────────────

class SportsEngine:
    """Orchestrates sports comeback shadow logging.

    Runs as daemon thread, 30-second tick cycle.
    Completely independent from crypto trading — no OrderExecutor reference.
    """

    TICK_INTERVAL = 30  # seconds

    def __init__(self, kalshi_client=None, state_manager=None, db_path: str = "state.db"):
        self._client = kalshi_client
        self._state = state_manager
        self._db_path = db_path
        self._espn = ESPNLiveFeed()
        self._discovery = KalshiSportsDiscovery(kalshi_client) if kalshi_client else None
        self._model = BayesianComebackModel()
        self._platt = PlattCalibrator()
        self._thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        # Time-based dedup: log every 60s (trailing) or 120s (leading/tied)
        self._last_logged_time: Dict[str, float] = {}
        self._last_logged_score: Dict[str, Tuple[int, int]] = {}
        # Track pregame favorites: game_id → (fav_code, fav_prob)
        self._pregame_favs: Dict[str, Tuple[str, float]] = {}
        # Per-game signal dedup: only fire one signal per game
        self._signaled_games: Set[str] = set()
        # Settlement tracking
        self._settled_games: Set[str] = set()
        self._startup_loaded: bool = False
        # DB connection (separate for thread safety)
        self._db_conn: Optional[sqlite3.Connection] = None

    def start(self) -> None:
        """Start the sports engine daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="SportsEngine"
        )
        self._thread.start()
        logging.info("SportsEngine daemon thread started")

    def _get_db_conn(self) -> sqlite3.Connection:
        """Get or create thread-local DB connection."""
        if self._db_conn is None:
            self._db_conn = sqlite3.connect(self._db_path)
            self._db_conn.row_factory = sqlite3.Row
            self._db_conn.execute("PRAGMA journal_mode=WAL")
            self._db_conn.execute("PRAGMA busy_timeout=30000")
        return self._db_conn

    def _run_loop(self) -> None:
        """Main event loop."""
        logging.info("SportsEngine starting tick loop (interval=%ds)", self.TICK_INTERVAL)
        # Initial Platt calibrator fit from historical data
        try:
            self._platt.fit_from_db(self._get_db_conn())
        except Exception:
            logging.warning("SportsEngine Platt initial fit failed", exc_info=True)
        while not self._shutdown.is_set():
            try:
                self._tick()
            except Exception:
                logging.warning("SportsEngine tick failed", exc_info=True)
            self._shutdown.wait(self.TICK_INTERVAL)
        logging.info("SportsEngine shutting down")

    def _tick(self) -> None:
        """One scan cycle."""
        # 1. Refresh Kalshi market discovery
        if self._discovery:
            try:
                self._discovery.refresh_if_needed()
            except Exception:
                logging.debug("SportsEngine discovery refresh failed", exc_info=True)

        # 2. Poll ESPN for all live games
        games = self._espn.poll_all_leagues()
        if not games:
            return

        # 2b. One-time load of already-settled game IDs from DB
        if not self._startup_loaded:
            self._load_settled_games()
            self._startup_loaded = True

        # 2c. Settle any completed games
        self._settle_completed_games(games)

        live_count = 0
        signal_count = 0

        # Periodic Platt refit (every 6 hours)
        if self._platt.needs_refit():
            try:
                self._platt.fit_from_db(self._get_db_conn())
            except Exception:
                logging.debug("SportsEngine Platt refit failed", exc_info=True)

        for game_id, game in games.items():
            if game.game_status != "live":
                # For pre-game: try to capture pregame prices (skip excluded groups)
                if game.game_status == "pre" and self._discovery:
                    _pre_cfg = LEAGUES.get(game.league)
                    if _pre_cfg and _pre_cfg.sport_group not in SPORTS_EXCLUDED_GROUPS:
                        self._try_capture_pregame(game)
                continue

            league_cfg = LEAGUES.get(game.league)
            if not league_cfg:
                continue

            # Skip excluded sport groups entirely (e.g., tennis)
            if league_cfg.sport_group in SPORTS_EXCLUDED_GROUPS:
                continue

            live_count += 1

            # 3. Identify pregame favorite (with retry chain)
            fav_info = self._pregame_favs.get(game_id)
            pregame_capture_method = "pre_game" if fav_info else ""

            if not fav_info:
                # Retry: attempt pregame capture for games that went live
                self._try_capture_pregame(game)
                fav_info = self._pregame_favs.get(game_id)
                if fav_info:
                    pregame_capture_method = "first_live_retry"

            if not fav_info:
                # Last resort: infer from current live Kalshi prices
                fav_info = self._infer_favorite(game)
                if not fav_info:
                    # Log discovery miss (once per game via _last_logged_time)
                    if game_id not in self._last_logged_time:
                        all_mkts = (self._discovery.get_all_markets()
                                    if self._discovery else {})
                        logging.info(
                            "SportsEngine: no Kalshi market match for %s (%s) "
                            "%s vs %s (discovery cache: %d events, series matches: %d)",
                            game.league, league_cfg.display_name,
                            game.home_code, game.away_code,
                            len(all_mkts),
                            sum(1 for et in all_mkts if et.upper().startswith(game.league.upper())))
                        self._last_logged_time[game_id] = time.time()
                    continue
                self._pregame_favs[game_id] = fav_info
                pregame_capture_method = "live_infer"

            fav_code, pregame_fav_prob = fav_info

            # 4. Is favorite currently trailing?
            if fav_code == game.home_code:
                fav_score = game.home_score
                underdog_score = game.away_score
            else:
                fav_score = game.away_score
                underdog_score = game.home_score

            deficit = underdog_score - fav_score

            # 5. Time-based dedup: 60s for trailing, 120s for leading/tied
            _now = time.time()
            _last = self._last_logged_time.get(game_id, 0)
            _score_key = (game.home_score, game.away_score)
            _prev_score = self._last_logged_score.get(game_id)
            # score_changed: True if score differs from last observation.
            # On first observation (_prev_score is None), set False to avoid
            # restart false positives — we don't know WHEN the score changed.
            _score_changed = (_prev_score is not None and _prev_score != _score_key)
            _is_trailing = deficit > 0
            _interval = 60 if _is_trailing else 120
            if not _score_changed and (_now - _last) < _interval:
                continue
            self._last_logged_time[game_id] = _now
            self._last_logged_score[game_id] = _score_key

            # 5b. Leading/tied: log minimal state for calibration baseline
            if not _is_trailing:
                current_price = self._get_current_kalshi_price(game, fav_code)
                if current_price is None:
                    current_price = 0.0
                ob_data = self._get_orderbook_data(game, fav_code)
                _leading_sgc = get_sport_group_config(league_cfg)
                leading_signal = ComebackSignal(
                    comeback_prob=pregame_fav_prob,
                    prior=pregame_fav_prob,
                    likelihood_ratio=1.0,
                    edge=0.0,
                    fee_adjusted_edge=0.0,
                    deficit_bucket="leading",
                    time_bucket=classify_time_remaining(game.time_remaining_pct),
                    strength_bucket=classify_strength(pregame_fav_prob, league_cfg.outcome_type),
                    signal_fired=False,
                    filter_stage="sports_fav_leading",
                    rejection_reason=None,
                    simulated_contracts=0,
                    simulated_risk=0.0,
                    market_implied_prob=current_price / 100.0 if current_price else 0.0,
                    sport_group=_leading_sgc.group_name,
                    sport_lr_scale=_leading_sgc.lr_scale,
                )
                self._insert_shadow_log(
                    game=game, league_cfg=league_cfg,
                    fav_code=fav_code, pregame_fav_prob=pregame_fav_prob,
                    fav_score=fav_score, underdog_score=underdog_score,
                    deficit=deficit, signal=leading_signal, ob_data=ob_data,
                    pregame_capture_method=pregame_capture_method,
                    market_implied_prob=current_price / 100.0 if current_price else 0.0,
                    score_changed=_score_changed,
                )
                continue

            # 6. Get current Kalshi price
            raw_kalshi_price = self._get_current_kalshi_price(game, fav_code)
            current_price = raw_kalshi_price if raw_kalshi_price is not None else 0.0

            # 7. Compute Bayesian posterior + edge
            sport_group_cfg = get_sport_group_config(league_cfg)
            signal = self._model.evaluate(
                game=game,
                league_cfg=league_cfg,
                pregame_fav_prob=pregame_fav_prob,
                current_kalshi_price=current_price,
                deficit=deficit,
                sport_group_cfg=sport_group_cfg,
            )

            # 7b. Platt scaling: calibrate raw posterior
            platt_prob = self._platt.calibrate(signal.comeback_prob)
            current_prob = current_price / 100.0
            platt_edge = platt_prob - current_prob
            # Fee: same as model uses (edge - fee_adjusted_edge = fee_pct)
            fee_pct = signal.edge - signal.fee_adjusted_edge
            platt_fee_adj_edge = platt_edge - fee_pct

            if signal.signal_fired:
                if game_id in self._signaled_games:
                    # Already fired for this game — mark as duplicate
                    signal.signal_fired = False
                    signal.filter_stage = "sports_signal_dup"
                    signal.rejection_reason = (
                        "duplicate signal for game "
                        "(first signal already fired)")
                else:
                    self._signaled_games.add(game_id)
                    signal_count += 1

            # 8. Get orderbook snapshot for logging
            ob_data = self._get_orderbook_data(game, fav_code)

            # 9. Insert to sports_shadow_log
            if pregame_capture_method == "live_infer" and signal.signal_fired:
                logging.warning(
                    "SportsEngine signal fired with live_infer fav for %s "
                    "(pregame prices not captured)", game_id)
            self._insert_shadow_log(
                game=game, league_cfg=league_cfg,
                fav_code=fav_code, pregame_fav_prob=pregame_fav_prob,
                fav_score=fav_score, underdog_score=underdog_score,
                deficit=deficit, signal=signal, ob_data=ob_data,
                pregame_capture_method=pregame_capture_method,
                market_implied_prob=current_price / 100.0 if current_price else 0.0,
                score_changed=_score_changed,
                platt_prob=platt_prob,
                platt_edge=platt_edge,
                platt_fee_adj_edge=platt_fee_adj_edge,
            )

            # 10. Insert to evaluated_opportunities (skip dups)
            if signal.filter_stage != "sports_signal_dup":
                self._insert_evaluated_opportunity(
                    game=game, league_cfg=league_cfg,
                    signal=signal, current_price=current_price,
                    ob_data=ob_data, raw_kalshi_price=raw_kalshi_price,
                )

        if live_count > 0:
            logging.debug("SportsEngine: %d live games, %d signals",
                          live_count, signal_count)

    def _try_capture_pregame(self, game: GameState) -> None:
        """Capture pregame Kalshi prices before game starts.

        Matches individual market tickers against ESPN team codes to correctly
        identify home vs away markets (instead of relying on _classify_market_side
        which always returns "home").
        """
        if not self._discovery:
            return
        all_markets = self._discovery.get_all_markets()
        home_upper = game.home_code.upper()
        away_upper = game.away_code.upper()

        for event_ticker, mkts in all_markets.items():
            if not _event_matches_game(game.home_code, game.away_code, event_ticker,
                                       series_ticker=game.league):
                continue

            home_price = None
            away_price = None
            for ticker, _side in list(mkts.market_tickers.items()):
                ticker_upper = ticker.upper()
                # Determine which team this market is for
                is_home = _team_code_in_ticker(home_upper, ticker_upper)
                is_away = _team_code_in_ticker(away_upper, ticker_upper)
                if not is_home and not is_away:
                    continue  # Draw market or unrecognized

                ob = self._discovery.get_orderbook_snapshot(ticker)
                if not ob or "orderbook" not in ob:
                    continue
                book = ob["orderbook"]
                no_bids = book.get("no", [])
                if not no_bids:
                    continue
                best_no_bid = max((b[0] for b in no_bids if b), default=None)
                if best_no_bid is None:
                    continue
                yes_ask = 100 - best_no_bid

                if is_home:
                    mkts.market_tickers[ticker] = "home"
                    self._discovery.capture_pregame_price(
                        event_ticker, "home", yes_ask)
                    home_price = yes_ask
                elif is_away:
                    mkts.market_tickers[ticker] = "away"
                    self._discovery.capture_pregame_price(
                        event_ticker, "away", yes_ask)
                    away_price = yes_ask

            # Determine favorite from prices
            if game.game_id not in self._pregame_favs:
                if home_price is not None and away_price is not None:
                    if home_price >= away_price:
                        self._pregame_favs[game.game_id] = (
                            game.home_code, home_price / 100.0)
                    else:
                        self._pregame_favs[game.game_id] = (
                            game.away_code, away_price / 100.0)
                elif home_price is not None and home_price > 50:
                    self._pregame_favs[game.game_id] = (
                        game.home_code, home_price / 100.0)
                elif away_price is not None and away_price > 50:
                    self._pregame_favs[game.game_id] = (
                        game.away_code, away_price / 100.0)
            break  # Only match first event

    def _infer_favorite(self, game: GameState) -> Optional[Tuple[str, float]]:
        """Try to infer favorite from current Kalshi prices.

        Matches individual market tickers against ESPN team codes directly
        instead of relying on pregame_price_home/away (which may not be
        populated if the game started before the bot).
        """
        if not self._discovery:
            return None

        home_upper = game.home_code.upper()
        away_upper = game.away_code.upper()
        all_markets = self._discovery.get_all_markets()

        for event_ticker, mkts in all_markets.items():
            if not _event_matches_game(game.home_code, game.away_code, event_ticker,
                                       series_ticker=game.league):
                continue

            # First try cached pregame prices
            if mkts.pregame_price_home and mkts.pregame_price_away:
                if mkts.pregame_price_home >= mkts.pregame_price_away:
                    return (game.home_code, mkts.pregame_price_home / 100.0)
                else:
                    return (game.away_code, mkts.pregame_price_away / 100.0)

            # Fallback: fetch live orderbook and match by team code
            home_price = None
            away_price = None
            for ticker, _side in mkts.market_tickers.items():
                ticker_upper = ticker.upper()
                is_home = _team_code_in_ticker(home_upper, ticker_upper)
                is_away = _team_code_in_ticker(away_upper, ticker_upper)
                if not is_home and not is_away:
                    continue

                ob = self._discovery.get_orderbook_snapshot(ticker)
                if not ob or "orderbook" not in ob:
                    continue
                book = ob["orderbook"]
                no_bids = book.get("no", [])
                if not no_bids:
                    continue
                best_no_bid = max((b[0] for b in no_bids if b), default=None)
                if best_no_bid is None:
                    continue
                yes_ask = 100 - best_no_bid

                if is_home:
                    home_price = yes_ask
                    mkts.market_tickers[ticker] = "home"
                elif is_away:
                    away_price = yes_ask
                    mkts.market_tickers[ticker] = "away"

            if home_price is not None and away_price is not None:
                if home_price >= away_price:
                    return (game.home_code, home_price / 100.0)
                else:
                    return (game.away_code, away_price / 100.0)
            elif home_price is not None and home_price > 50:
                return (game.home_code, home_price / 100.0)
            elif away_price is not None and away_price > 50:
                return (game.away_code, away_price / 100.0)

        return None

    def _get_current_kalshi_price(self, game: GameState,
                                  fav_code: str) -> Optional[float]:
        """Get current Kalshi price for the favorite's market."""
        if not self._discovery:
            return None

        all_markets = self._discovery.get_all_markets()
        fav_upper = fav_code.upper()
        for event_ticker, mkts in all_markets.items():
            if not _event_matches_game(game.home_code, game.away_code, event_ticker,
                                       series_ticker=game.league):
                continue
            fav_side = "home" if fav_code == game.home_code else "away"
            # Primary: match by side label (works when _try_capture_pregame ran)
            for ticker, side in mkts.market_tickers.items():
                if side == fav_side:
                    ob = self._discovery.get_orderbook_snapshot(ticker)
                    if ob and "orderbook" in ob:
                        book = ob["orderbook"]
                        no_bids = book.get("no", [])
                        if no_bids:
                            best_no_bid = max((b[0] for b in no_bids if b), default=None)
                            if best_no_bid is not None:
                                return 100 - best_no_bid
            # Fallback: match team code directly in ticker
            for ticker in mkts.market_tickers:
                if _team_code_in_ticker(fav_upper, ticker.upper()):
                    ob = self._discovery.get_orderbook_snapshot(ticker)
                    if ob and "orderbook" in ob:
                        book = ob["orderbook"]
                        no_bids = book.get("no", [])
                        if no_bids:
                            best_no_bid = max((b[0] for b in no_bids if b), default=None)
                            if best_no_bid is not None:
                                # Fix side label for future lookups
                                mkts.market_tickers[ticker] = fav_side
                                return 100 - best_no_bid
            logging.debug(
                "SportsEngine: event %s matched game %s but no fav ticker "
                "for %s (sides: %s)", event_ticker, game.game_id, fav_code,
                dict(mkts.market_tickers))
            break
        return None

    def _get_orderbook_data(self, game: GameState,
                            fav_code: str) -> Dict:
        """Get orderbook snapshot data for logging."""
        default = {
            "ticker": "", "event_ticker": "",
            "yes_bid": None, "yes_ask": None,
            "mid_price": None, "spread": None,
            "ask_depth": None, "bid_depth": None,
        }
        if not self._discovery:
            return default

        all_markets = self._discovery.get_all_markets()
        fav_upper = fav_code.upper()
        for event_ticker, mkts in all_markets.items():
            if not _event_matches_game(game.home_code, game.away_code, event_ticker,
                                       series_ticker=game.league):
                continue
            fav_side = "home" if fav_code == game.home_code else "away"

            # Primary: match by side label
            matched_ticker = None
            for ticker, side in mkts.market_tickers.items():
                if side == fav_side:
                    matched_ticker = ticker
                    break

            # Fallback: match team code directly in ticker
            if matched_ticker is None:
                for ticker in mkts.market_tickers:
                    if _team_code_in_ticker(fav_upper, ticker.upper()):
                        matched_ticker = ticker
                        # Fix side label for future lookups
                        mkts.market_tickers[ticker] = fav_side
                        break

            if matched_ticker is None:
                logging.debug(
                    "SportsEngine: _get_orderbook_data no fav ticker for %s "
                    "in event %s (sides: %s)", fav_code, event_ticker,
                    dict(mkts.market_tickers))
                break

            ob = self._discovery.get_orderbook_snapshot(matched_ticker)
            if ob and "orderbook" in ob:
                book = ob["orderbook"]
                yes_bids = book.get("yes", [])
                no_bids = book.get("no", [])

                yes_bid = max((b[0] for b in yes_bids if b), default=None)
                yes_ask = None
                if no_bids:
                    best_no = max((b[0] for b in no_bids if b), default=None)
                    if best_no is not None:
                        yes_ask = 100 - best_no

                mid = None
                spread = None
                if yes_bid is not None and yes_ask is not None:
                    mid = (yes_bid + yes_ask) / 2.0
                    spread = yes_ask - yes_bid

                ask_depth = sum(b[1] for b in no_bids if b) if no_bids else 0
                bid_depth = sum(b[1] for b in yes_bids if b) if yes_bids else 0

                return {
                    "ticker": matched_ticker,
                    "event_ticker": event_ticker,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "mid_price": mid,
                    "spread": spread,
                    "ask_depth": ask_depth,
                    "bid_depth": bid_depth,
                }
            break

        # No event matched — log once per game for monitoring
        if not any(
            _event_matches_game(game.home_code, game.away_code, et,
                                series_ticker=game.league)
            for et in all_markets
        ):
            _ob_miss_key = f"{game.game_id}_ob_miss"
            if _ob_miss_key not in self._signaled_games:
                self._signaled_games.add(_ob_miss_key)
                logging.info(
                    "SportsEngine: no Kalshi event found for %s %s vs %s "
                    "(%s/%s) — %d events in cache",
                    game.league, game.home_team, game.away_team,
                    game.home_code, game.away_code, len(all_markets))

        return default

    def _load_settled_games(self) -> None:
        """One-time load of already-settled game IDs and signaled games from DB."""
        try:
            conn = self._get_db_conn()
            rows = conn.execute(
                "SELECT DISTINCT game_id FROM sports_shadow_log "
                "WHERE fav_won IS NOT NULL"
            ).fetchall()
            self._settled_games = {r[0] for r in rows}

            # Restore _signaled_games — unsettled games with signal_fired
            sig_rows = conn.execute(
                "SELECT DISTINCT game_id FROM sports_shadow_log "
                "WHERE signal_fired=1 AND fav_won IS NULL"
            ).fetchall()
            self._signaled_games = {r[0] for r in sig_rows} - self._settled_games
            logging.info("SportsEngine: loaded %d settled, %d signaled games",
                         len(self._settled_games), len(self._signaled_games))

            # Sweep for stale unsettled games (>6 hours old, not in current ESPN poll)
            self._settle_stale_games(conn)
        except Exception:
            logging.warning("SportsEngine: failed to load settled games",
                            exc_info=True)

    def _settle_stale_games(self, conn: sqlite3.Connection) -> None:
        """Settle games that were never caught as 'final' by ESPN polling.

        This handles the case where the bot restarted after a game ended and
        ESPN no longer returns it in the scoreboard. Uses the last-known
        scores from sports_shadow_log to determine the winner.
        """
        try:
            stale_rows = conn.execute(
                "SELECT DISTINCT game_id, home_code, away_code, "
                "pregame_fav_code, MAX(home_score) AS last_home, "
                "MAX(away_score) AS last_away, MAX(evaluation_time) AS last_eval "
                "FROM sports_shadow_log "
                "WHERE fav_won IS NULL "
                "AND evaluation_time < datetime('now', '-6 hours') "
                "GROUP BY game_id"
            ).fetchall()

            if not stale_rows:
                return

            settled_count = 0
            for srow in stale_rows:
                game_id = srow[0]
                # Don't skip games in _settled_games — they may have
                # unsettled rows added after initial settlement (race condition
                # where evals arrive between settlement and game_id being
                # added to _settled_games). The SQL already filters fav_won IS NULL.

                home_code = srow[1]
                away_code = srow[2]
                fav_code = srow[3]
                last_home = srow[4]
                last_away = srow[5]

                if last_home is None or last_away is None:
                    continue
                if last_home == last_away:
                    # Can't determine winner from tied score
                    continue

                # Determine winner
                if fav_code == home_code:
                    fav_won = 1 if last_home > last_away else 0
                else:
                    fav_won = 1 if last_away > last_home else 0

                market_result = "yes" if fav_won else "no"

                # Get rows to update
                rows = conn.execute(
                    "SELECT id, signal_fired, simulated_contracts, yes_ask "
                    "FROM sports_shadow_log "
                    "WHERE game_id = ? AND fav_won IS NULL",
                    (game_id,)
                ).fetchall()

                # Closing price: use last known yes_ask
                closing_price = None
                cp_row = conn.execute(
                    "SELECT yes_ask FROM sports_shadow_log "
                    "WHERE game_id = ? AND yes_ask IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (game_id,)
                ).fetchone()
                if cp_row:
                    closing_price = cp_row[0]

                update_params = []
                for row in rows:
                    row_id, sig_fired, sim_contracts, entry_price = row
                    sim_contracts = sim_contracts or 0

                    pnl_cents = None
                    if sig_fired and sim_contracts > 0 and entry_price:
                        cost_cents = sim_contracts * entry_price
                        p = entry_price / 100.0
                        fee_cents = math.ceil(
                            0.07 * sim_contracts * p * (1 - p))
                        if fav_won:
                            pnl_cents = sim_contracts * 100 - cost_cents - fee_cents
                        else:
                            pnl_cents = -cost_cents - fee_cents

                    update_params.append((
                        last_home, last_away,
                        fav_won, market_result, pnl_cents,
                        closing_price, row_id))

                if update_params:
                    conn.executemany(
                        "UPDATE sports_shadow_log SET "
                        "final_home_score=?, final_away_score=?, "
                        "fav_won=?, market_result=?, pnl_cents=?, "
                        "closing_price=? WHERE id=?",
                        update_params
                    )
                    conn.commit()
                    self._settled_games.add(game_id)
                    self._signaled_games.discard(game_id)
                    settled_count += 1

                    # Also settle evaluated_opportunities
                    try:
                        eo_rows = conn.execute(
                            "SELECT id, market_price, position_size "
                            "FROM evaluated_opportunities "
                            "WHERE status='pending' AND product_type='sports' "
                            "AND (ticker = ? OR ticker LIKE ?)",
                            (f"SPORTS-{game_id}", f"%{game_id}%")
                        ).fetchall()
                        _settle_now = datetime.datetime.now(
                            datetime.timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ")
                        for erow in eo_rows:
                            _eid, _ep, _cnt = erow[0], erow[1], erow[2] or 1
                            _cf_pnl = None
                            if _ep and _ep > 0:
                                _tf = math.ceil(
                                    0.07 * _cnt * (_ep / 100) * (1 - _ep / 100))
                                if fav_won:
                                    _cf_pnl = int((100 - _ep) * _cnt - _tf)
                                else:
                                    _cf_pnl = int(-(_ep * _cnt + _tf))
                            conn.execute(
                                "UPDATE evaluated_opportunities SET "
                                "status='settled', market_result=?, "
                                "counterfactual_pnl=?, settled_time=? WHERE id=?",
                                ("yes" if fav_won else "no",
                                 _cf_pnl, _settle_now, _eid))
                        if eo_rows:
                            conn.commit()
                    except Exception:
                        logging.warning(
                            "SportsEngine stale settle EO failed for %s",
                            game_id, exc_info=True)

            if settled_count > 0:
                logging.info("SportsEngine: settled %d stale games on startup",
                             settled_count)
        except Exception:
            logging.warning("SportsEngine: stale game sweep failed",
                            exc_info=True)

    def _settle_completed_games(self, games: Dict[str, GameState]) -> None:
        """Settle completed games — populate fav_won, pnl_cents, scores."""
        for game_id, game in games.items():
            if game.game_status != "final":
                continue
            # Don't skip games in _settled_games — late-arriving eval rows
            # may have fav_won=NULL even after initial settlement.

            try:
                conn = self._get_db_conn()
                rows = conn.execute(
                    "SELECT id, pregame_fav_code, signal_fired, "
                    "simulated_contracts, yes_ask "
                    "FROM sports_shadow_log "
                    "WHERE game_id = ? AND fav_won IS NULL",
                    (game_id,)
                ).fetchall()
                if not rows:
                    self._settled_games.add(game_id)
                    continue

                # Pre-compute closing price ONCE per game (same for all rows)
                # MUST happen BEFORE DB writes to avoid holding write lock
                # during discovery iteration (caused 10s+ lock, Mar 3 2026)
                fav_code_0 = rows[0][1]
                closing_price = self._get_closing_price(game, fav_code_0)

                # Build all update params, then batch-write
                update_params = []
                for row in rows:
                    row_id = row[0]
                    fav_code = row[1]
                    signal_fired = row[2]
                    sim_contracts = row[3] or 0
                    entry_price = row[4]  # yes_ask at time of evaluation

                    # Determine if favorite won from final scores
                    if fav_code == game.home_code:
                        fav_won = 1 if game.home_score > game.away_score else 0
                    else:
                        fav_won = 1 if game.away_score > game.home_score else 0

                    market_result = "yes" if fav_won else "no"

                    # Compute simulated PnL for signal rows
                    pnl_cents = None
                    if signal_fired and sim_contracts > 0 and entry_price:
                        cost_cents = sim_contracts * entry_price
                        p = entry_price / 100.0
                        fee_cents = math.ceil(
                            0.07 * sim_contracts * p * (1 - p))
                        if fav_won:
                            revenue = sim_contracts * 100
                            pnl_cents = revenue - cost_cents - fee_cents
                        else:
                            pnl_cents = -cost_cents - fee_cents

                    update_params.append((
                        game.home_score, game.away_score,
                        fav_won, market_result, pnl_cents,
                        closing_price, row_id))

                # Single batch write — minimizes lock hold time
                conn.executemany(
                    "UPDATE sports_shadow_log SET "
                    "final_home_score=?, final_away_score=?, "
                    "fav_won=?, market_result=?, pnl_cents=?, "
                    "closing_price=? WHERE id=?",
                    update_params
                )
                conn.commit()

                # Settle evaluated_opportunities for this game
                try:
                    eo_rows = conn.execute(
                        "SELECT id, market_price, position_size "
                        "FROM evaluated_opportunities "
                        "WHERE status='pending' AND product_type='sports' "
                        "AND (ticker = ? OR ticker LIKE ?)",
                        (f"SPORTS-{game_id}",
                         f"%{game_id}%")
                    ).fetchall()
                    _settle_now = datetime.datetime.now(
                        datetime.timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ")
                    for erow in eo_rows:
                        _eid, _ep, _cnt = erow[0], erow[1], erow[2] or 1
                        _cf_pnl = None
                        if _ep and _ep > 0:
                            _tf = math.ceil(
                                0.07 * _cnt * (_ep / 100) * (1 - _ep / 100))
                            if fav_won:
                                _cf_pnl = int((100 - _ep) * _cnt - _tf)
                            else:
                                _cf_pnl = int(-(_ep * _cnt + _tf))
                        conn.execute(
                            "UPDATE evaluated_opportunities SET "
                            "status='settled', market_result=?, "
                            "counterfactual_pnl=?, settled_time=? WHERE id=?",
                            ("yes" if fav_won else "no",
                             _cf_pnl, _settle_now, _eid))
                    if eo_rows:
                        conn.commit()
                except Exception:
                    logging.warning(
                        "SportsEngine eval_opp settle failed for %s",
                        game_id, exc_info=True)

                self._settled_games.add(game_id)
                self._signaled_games.discard(game_id)
                self._last_logged_time.pop(game_id, None)
                self._last_logged_score.pop(game_id, None)
                logging.debug("SportsEngine: settled game %s (%s vs %s) "
                              "fav_won=%s, %d rows",
                              game_id, game.home_team, game.away_team,
                              fav_won, len(rows))
            except Exception:
                logging.warning("SportsEngine: failed to settle game %s",
                                game_id, exc_info=True)

    def _get_closing_price(self, game: GameState,
                           fav_code: str) -> Optional[float]:
        """Get closing price for a completed game's favorite market."""
        # Try live orderbook first
        price = self._get_current_kalshi_price(game, fav_code)
        if price is not None:
            return price

        # Fallback: last logged yes_ask from sports_shadow_log
        try:
            conn = self._get_db_conn()
            row = conn.execute(
                "SELECT yes_ask FROM sports_shadow_log "
                "WHERE game_id = ? AND yes_ask IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (game.game_id,)
            ).fetchone()
            if row:
                return row[0]
        except Exception:
            pass
        return None

    def _insert_shadow_log(self, game: GameState, league_cfg: LeagueConfig,
                           fav_code: str, pregame_fav_prob: float,
                           fav_score: int, underdog_score: int,
                           deficit: int, signal: ComebackSignal,
                           ob_data: Dict,
                           pregame_capture_method: str = "",
                           market_implied_prob: float = 0.0,
                           score_changed: bool = False,
                           platt_prob: Optional[float] = None,
                           platt_edge: Optional[float] = None,
                           platt_fee_adj_edge: Optional[float] = None) -> None:
        """Insert record into sports_shadow_log."""
        try:
            conn = self._get_db_conn()

            # Get pregame prices from discovery cache
            pregame_home = pregame_away = pregame_draw = None
            if self._discovery:
                for et, mkts in self._discovery.get_all_markets().items():
                    if _event_matches_game(game.home_code, game.away_code, et,
                                           series_ticker=game.league):
                        pregame_home = mkts.pregame_price_home
                        pregame_away = mkts.pregame_price_away
                        pregame_draw = mkts.pregame_price_draw
                        break

            conn.execute("""
                INSERT INTO sports_shadow_log (
                    game_id, sport, league, home_team, away_team,
                    home_code, away_code, pregame_fav_code, pregame_fav_prob,
                    pregame_price_home, pregame_price_away, pregame_price_draw,
                    scheduled_start, outcome_type,
                    home_score, away_score, fav_score, underdog_score,
                    deficit, period, clock, time_remaining_pct,
                    game_status, red_cards_fav, red_cards_underdog,
                    ticker, event_ticker, yes_bid, yes_ask,
                    mid_price, spread, ask_depth, bid_depth,
                    comeback_prob, prior, likelihood_ratio,
                    edge, fee_adjusted_edge,
                    signal_fired, filter_stage, rejection_reason,
                    simulated_contracts, simulated_risk,
                    would_signal_50c, would_signal_60c,
                    would_signal_70c, would_signal_80c,
                    would_signal_pregame_55, would_signal_pregame_65,
                    market_implied_prob, pregame_capture_method,
                    shadow_lr_scale_50_posterior, shadow_lr_scale_50_signal,
                    score_changed,
                    sport_group, sport_lr_scale,
                    is_strong_config,
                    platt_prob, platt_edge, platt_fee_adj_edge
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?
                )
            """, (
                game.game_id, league_cfg.espn_sport, league_cfg.display_name,
                game.home_team, game.away_team,
                game.home_code, game.away_code, fav_code, pregame_fav_prob,
                pregame_home, pregame_away, pregame_draw,
                game.scheduled_start, league_cfg.outcome_type,
                game.home_score, game.away_score, fav_score, underdog_score,
                deficit, game.period, game.clock, game.time_remaining_pct,
                game.game_status,
                game.red_cards_home if fav_code == game.home_code else game.red_cards_away,
                game.red_cards_away if fav_code == game.home_code else game.red_cards_home,
                ob_data.get("ticker", ""), ob_data.get("event_ticker", ""),
                ob_data.get("yes_bid"), ob_data.get("yes_ask"),
                ob_data.get("mid_price"), ob_data.get("spread"),
                ob_data.get("ask_depth"), ob_data.get("bid_depth"),
                signal.comeback_prob, signal.prior, signal.likelihood_ratio,
                signal.edge, signal.fee_adjusted_edge,
                1 if signal.signal_fired else 0,
                signal.filter_stage, signal.rejection_reason,
                signal.simulated_contracts, signal.simulated_risk,
                1 if signal.would_signal_50c else 0,
                1 if signal.would_signal_60c else 0,
                1 if signal.would_signal_70c else 0,
                1 if signal.would_signal_80c else 0,
                1 if signal.would_signal_pregame_55 else 0,
                1 if signal.would_signal_pregame_65 else 0,
                signal.market_implied_prob,
                pregame_capture_method,
                signal.shadow_lr_scale_50_posterior,
                1 if signal.shadow_lr_scale_50_signal else 0,
                1 if score_changed else 0,
                signal.sport_group,
                signal.sport_lr_scale,
                1 if signal.is_strong_config else 0,
                platt_prob, platt_edge, platt_fee_adj_edge,
            ))
            conn.commit()
        except Exception:
            logging.warning("SportsEngine shadow log insert failed", exc_info=True)

    def _insert_evaluated_opportunity(self, game: GameState,
                                      league_cfg: LeagueConfig,
                                      signal: ComebackSignal,
                                      current_price: float,
                                      ob_data: Optional[Dict] = None,
                                      raw_kalshi_price: Optional[float] = None,
                                      ) -> None:
        """Insert to evaluated_opportunities for settlement tracking.

        Uses own DB connection to avoid cross-thread writes to StateManager.
        Inserts YES-side row, then a NO-side shadow row for data collection.
        """
        try:
            conn = self._get_db_conn()
            # Use real Kalshi ticker if available, else synthetic for dedup
            real_ticker = ob_data.get("ticker") if ob_data else ""
            real_event = ob_data.get("event_ticker") if ob_data else ""
            ticker = real_ticker or f"SPORTS-{game.game_id}"
            event_ticker = real_event or game.league
            now = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")
            _dur = _SPORT_DURATION_SEC.get(league_cfg.sport_group, 3600)
            _stc = (int(game.time_remaining_pct * _dur)
                    if game.time_remaining_pct is not None else None)
            # Use raw_kalshi_price for DB (None when no Kalshi market)
            _db_price = (int(raw_kalshi_price)
                         if raw_kalshi_price is not None else None)
            conn.execute("""
                INSERT OR REPLACE INTO evaluated_opportunities
                    (ticker, event_ticker, asset, filter_stage, rejection_reason,
                     evaluation_time, market_price, calibrated_prob, raw_prob, edge,
                     fee_adjusted_edge, product_type, status,
                     seconds_to_close, spot_price, position_size, kelly_f,
                     z_score, vol_regime, calibration_method, counterfactual,
                     ask_depth, side)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, event_ticker, league_cfg.display_name,
                  signal.filter_stage, signal.rejection_reason, now,
                  _db_price,
                  signal.comeback_prob, signal.comeback_prob, signal.edge,
                  signal.fee_adjusted_edge, "sports", "pending",
                  _stc,
                  round(signal.prior * 100, 2) if signal.prior else None,
                  signal.simulated_contracts,
                  signal.simulated_risk,
                  signal.likelihood_ratio,
                  signal.sport_group,
                  "bayesian_comeback",
                  1 if signal.signal_fired else 0,
                  ob_data.get("ask_depth") if ob_data else None,
                  "yes"))
            conn.commit()
        except Exception:
            logging.debug("SportsEngine eval_opp insert failed", exc_info=True)

        # ── NO-side shadow row ──
        # Collect raw NO-side data: NO prob = 1 - comeback_prob, NO price from orderbook
        try:
            if raw_kalshi_price is not None and raw_kalshi_price > 0:
                _no_price = 100 - int(raw_kalshi_price)
                if _no_price >= 5:  # match NO_SIDE_MIN_ENTRY_PRICE
                    _no_prob = 1.0 - signal.comeback_prob
                    _no_edge = _no_prob - _no_price / 100.0
                    # Taker fee for NO side (sports uses 0.07 crypto taker fee)
                    _no_fee = math.ceil(0.07 * 1 * _no_price * (100 - _no_price) / 100) / 100.0
                    _no_fee_edge = _no_edge - _no_fee
                    _no_stage = "no_side_shadow"
                    conn = self._get_db_conn()
                    conn.execute("""
                        INSERT OR REPLACE INTO evaluated_opportunities
                            (ticker, event_ticker, asset, filter_stage, rejection_reason,
                             evaluation_time, market_price, calibrated_prob, raw_prob, edge,
                             fee_adjusted_edge, product_type, status,
                             seconds_to_close, spot_price, position_size, kelly_f,
                             z_score, vol_regime, calibration_method, counterfactual,
                             ask_depth, side)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (ticker, event_ticker, league_cfg.display_name,
                          _no_stage, None, now,
                          _no_price,
                          _no_prob, 1.0 - signal.comeback_prob, _no_edge,
                          _no_fee_edge, "sports", "pending",
                          _stc,
                          round(signal.prior * 100, 2) if signal.prior else None,
                          None, None,  # no sizing for NO shadow
                          signal.likelihood_ratio,
                          signal.sport_group,
                          "bayesian_comeback_no",
                          0,  # not a signal
                          ob_data.get("bid_depth") if ob_data else None,
                          "no"))
                    conn.commit()
        except Exception:
            logging.warning("SportsEngine NO-side eval_opp insert failed", exc_info=True)

    def stop(self) -> None:
        """Signal shutdown."""
        self._shutdown.set()
        if self._db_conn:
            try:
                self._db_conn.close()
            except Exception:
                pass
