"""Static configuration for sports comeback shadow-logging engine.

Contains league definitions, Bayesian comeback likelihood ratio tables,
and entry criteria. No runtime dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class LeagueConfig:
    """Configuration for one Kalshi game-level series."""

    series_ticker: str          # "KXNBAGAME", "KXEPLGAME", etc.
    espn_sport: Optional[str]   # "basketball", "soccer", "hockey", "baseball", None for esports
    espn_league: Optional[str]  # "nba", "eng.1", None for esports
    outcome_type: str           # "binary" or "three_way"
    display_name: str           # "NBA", "EPL", etc.
    enabled: bool = True


# ── All 26 Kalshi game-level series ──────────────────────────────────────────
# Leagues without ESPN endpoints have espn_league=None — engine skips ESPN
# polling for them and only does Kalshi price monitoring.
# Off-season leagues return 0 games from ESPN and skip silently.

LEAGUES: Dict[str, LeagueConfig] = {
    # ── Binary (2 markets/game: Team A wins, Team B wins) ──
    "KXNBAGAME": LeagueConfig(
        series_ticker="KXNBAGAME", espn_sport="basketball",
        espn_league="nba", outcome_type="binary", display_name="NBA",
    ),
    "KXNHLGAME": LeagueConfig(
        series_ticker="KXNHLGAME", espn_sport="hockey",
        espn_league="nhl", outcome_type="binary", display_name="NHL",
    ),
    "KXMLBGAME": LeagueConfig(
        series_ticker="KXMLBGAME", espn_sport="baseball",
        espn_league="mlb", outcome_type="binary", display_name="MLB",
    ),
    "KXNCAABBGAME": LeagueConfig(
        series_ticker="KXNCAABBGAME", espn_sport="basketball",
        espn_league="mens-college-basketball", outcome_type="binary",
        display_name="NCAAB",
    ),
    "KXNCAAFGAME": LeagueConfig(
        series_ticker="KXNCAAFGAME", espn_sport="football",
        espn_league="college-football", outcome_type="binary",
        display_name="NCAAF",
    ),
    "KXNFLGAME": LeagueConfig(
        series_ticker="KXNFLGAME", espn_sport="football",
        espn_league="nfl", outcome_type="binary", display_name="NFL",
    ),
    "KXWNBAGAME": LeagueConfig(
        series_ticker="KXWNBAGAME", espn_sport="basketball",
        espn_league="wnba", outcome_type="binary", display_name="WNBA",
    ),
    "KXUFCFIGHT": LeagueConfig(
        series_ticker="KXUFCFIGHT", espn_sport="mma",
        espn_league="ufc", outcome_type="binary", display_name="UFC",
    ),
    # Esports — no ESPN endpoint
    "KXCSGOGAME": LeagueConfig(
        series_ticker="KXCSGOGAME", espn_sport=None,
        espn_league=None, outcome_type="binary", display_name="CSGO",
    ),
    "KXLOLGAME": LeagueConfig(
        series_ticker="KXLOLGAME", espn_sport=None,
        espn_league=None, outcome_type="binary", display_name="LoL",
    ),
    "KXVALORANTGAME": LeagueConfig(
        series_ticker="KXVALORANTGAME", espn_sport=None,
        espn_league=None, outcome_type="binary", display_name="Valorant",
    ),

    # ── Three-way (3 markets/game: Home, Away, Draw) ──
    "KXEPLGAME": LeagueConfig(
        series_ticker="KXEPLGAME", espn_sport="soccer",
        espn_league="eng.1", outcome_type="three_way", display_name="EPL",
    ),
    "KXBUNDESLIGAGAME": LeagueConfig(
        series_ticker="KXBUNDESLIGAGAME", espn_sport="soccer",
        espn_league="ger.1", outcome_type="three_way", display_name="Bundesliga",
    ),
    "KXLALIGAGAME": LeagueConfig(
        series_ticker="KXLALIGAGAME", espn_sport="soccer",
        espn_league="esp.1", outcome_type="three_way", display_name="La Liga",
    ),
    "KXSERIEAGAME": LeagueConfig(
        series_ticker="KXSERIEAGAME", espn_sport="soccer",
        espn_league="ita.1", outcome_type="three_way", display_name="Serie A",
    ),
    "KXUCLGAME": LeagueConfig(
        series_ticker="KXUCLGAME", espn_sport="soccer",
        espn_league="uefa.champions", outcome_type="three_way", display_name="UCL",
    ),
    "KXLIGUE1GAME": LeagueConfig(
        series_ticker="KXLIGUE1GAME", espn_sport="soccer",
        espn_league="fra.1", outcome_type="three_way", display_name="Ligue 1",
    ),
    "KXSUPERLIGGAME": LeagueConfig(
        series_ticker="KXSUPERLIGGAME", espn_sport="soccer",
        espn_league="tur.1", outcome_type="three_way", display_name="Turkish Super Lig",
    ),
    "KXMLSGAME": LeagueConfig(
        series_ticker="KXMLSGAME", espn_sport="soccer",
        espn_league="usa.1", outcome_type="three_way", display_name="MLS",
    ),
    "KXUELGAME": LeagueConfig(
        series_ticker="KXUELGAME", espn_sport="soccer",
        espn_league="uefa.europa", outcome_type="three_way",
        display_name="Europa League",
    ),
    "KXUECLGAME": LeagueConfig(
        series_ticker="KXUECLGAME", espn_sport="soccer",
        espn_league="uefa.europa.conf", outcome_type="three_way",
        display_name="Conference League",
    ),
    "KXLIGAMXGAME": LeagueConfig(
        series_ticker="KXLIGAMXGAME", espn_sport="soccer",
        espn_league="mex.1", outcome_type="three_way", display_name="Liga MX",
    ),
    "KXWCGAME": LeagueConfig(
        series_ticker="KXWCGAME", espn_sport="soccer",
        espn_league="fifa.worldcup", outcome_type="three_way",
        display_name="World Cup",
    ),
    "KXFIFAGAME": LeagueConfig(
        series_ticker="KXFIFAGAME", espn_sport="soccer",
        espn_league="fifa.friendly", outcome_type="three_way",
        display_name="FIFA Intl",
    ),
    "KXAFCONGAME": LeagueConfig(
        series_ticker="KXAFCONGAME", espn_sport=None,
        espn_league=None, outcome_type="three_way", display_name="AFC/Intl",
    ),
    "KXEREDIVISIEGAME": LeagueConfig(
        series_ticker="KXEREDIVISIEGAME", espn_sport="soccer",
        espn_league="ned.1", outcome_type="three_way", display_name="Eredivisie",
    ),
}


# ── Bayesian Comeback Likelihood Ratio Tables ────────────────────────────────
#
# LR = P(observe this score state | favorite ultimately wins)
#     / P(observe this score state | favorite loses)
#
# Keys: (deficit_bucket, time_remaining_pct_bucket, pregame_strength_bucket)
# Values: LR (likelihood ratio)
#
# Initial values from research brief estimates (Choi & Hui 2014).
# Will be refined with empirical data from build_lr_tables.py.

# Binary sports (NBA, NHL, MLB, NCAAB, NCAAF, NFL, WNBA, UFC)
# deficit_bucket: "small" (1-5pts), "medium" (6-10), "large" (11-15), "blowout" (16+)
# time_remaining: ">75%", "50-75%", "25-50%", "<25%"
# strength: "strong_fav" (>=75%), "moderate" (65-75%), "slight" (55-65%)

BINARY_LR_TABLE: Dict[Tuple[str, str, str], float] = {
    # small deficit (1-5 pts)
    ("small", ">75%", "strong_fav"): 2.8,
    ("small", ">75%", "moderate"): 2.2,
    ("small", ">75%", "slight"): 1.6,
    ("small", "50-75%", "strong_fav"): 2.4,
    ("small", "50-75%", "moderate"): 1.8,
    ("small", "50-75%", "slight"): 1.3,
    ("small", "25-50%", "strong_fav"): 1.8,
    ("small", "25-50%", "moderate"): 1.3,
    ("small", "25-50%", "slight"): 0.9,
    ("small", "<25%", "strong_fav"): 1.1,
    ("small", "<25%", "moderate"): 0.8,
    ("small", "<25%", "slight"): 0.5,

    # medium deficit (6-10 pts)
    ("medium", ">75%", "strong_fav"): 2.0,
    ("medium", ">75%", "moderate"): 1.5,
    ("medium", ">75%", "slight"): 1.1,
    ("medium", "50-75%", "strong_fav"): 1.5,
    ("medium", "50-75%", "moderate"): 1.1,
    ("medium", "50-75%", "slight"): 0.7,
    ("medium", "25-50%", "strong_fav"): 0.9,
    ("medium", "25-50%", "moderate"): 0.6,
    ("medium", "25-50%", "slight"): 0.4,
    ("medium", "<25%", "strong_fav"): 0.5,
    ("medium", "<25%", "moderate"): 0.3,
    ("medium", "<25%", "slight"): 0.2,

    # large deficit (11-15 pts)
    ("large", ">75%", "strong_fav"): 1.3,
    ("large", ">75%", "moderate"): 0.9,
    ("large", ">75%", "slight"): 0.6,
    ("large", "50-75%", "strong_fav"): 0.8,
    ("large", "50-75%", "moderate"): 0.5,
    ("large", "50-75%", "slight"): 0.3,
    ("large", "25-50%", "strong_fav"): 0.4,
    ("large", "25-50%", "moderate"): 0.2,
    ("large", "25-50%", "slight"): 0.1,
    ("large", "<25%", "strong_fav"): 0.2,
    ("large", "<25%", "moderate"): 0.1,
    ("large", "<25%", "slight"): 0.05,

    # blowout (16+ pts)
    ("blowout", ">75%", "strong_fav"): 0.7,
    ("blowout", ">75%", "moderate"): 0.4,
    ("blowout", ">75%", "slight"): 0.2,
    ("blowout", "50-75%", "strong_fav"): 0.3,
    ("blowout", "50-75%", "moderate"): 0.2,
    ("blowout", "50-75%", "slight"): 0.1,
    ("blowout", "25-50%", "strong_fav"): 0.1,
    ("blowout", "25-50%", "moderate"): 0.05,
    ("blowout", "25-50%", "slight"): 0.02,
    ("blowout", "<25%", "strong_fav"): 0.05,
    ("blowout", "<25%", "moderate"): 0.02,
    ("blowout", "<25%", "slight"): 0.01,
}

# Three-way soccer: deficit_goals (1, 2, 3+)
# time_remaining: ">75%", "50-75%", "25-50%", "<25%"
# strength: "strong_fav" (>=75%), "moderate" (60-75%), "slight" (50-60%)

THREE_WAY_LR_TABLE: Dict[Tuple[str, str, str], float] = {
    # 1 goal deficit
    ("1_goal", ">75%", "strong_fav"): 2.5,
    ("1_goal", ">75%", "moderate"): 1.9,
    ("1_goal", ">75%", "slight"): 1.4,
    ("1_goal", "50-75%", "strong_fav"): 2.0,
    ("1_goal", "50-75%", "moderate"): 1.5,
    ("1_goal", "50-75%", "slight"): 1.1,
    ("1_goal", "25-50%", "strong_fav"): 1.3,
    ("1_goal", "25-50%", "moderate"): 0.9,
    ("1_goal", "25-50%", "slight"): 0.6,
    ("1_goal", "<25%", "strong_fav"): 0.7,
    ("1_goal", "<25%", "moderate"): 0.4,
    ("1_goal", "<25%", "slight"): 0.2,

    # 2 goal deficit
    ("2_goal", ">75%", "strong_fav"): 1.2,
    ("2_goal", ">75%", "moderate"): 0.8,
    ("2_goal", ">75%", "slight"): 0.5,
    ("2_goal", "50-75%", "strong_fav"): 0.6,
    ("2_goal", "50-75%", "moderate"): 0.4,
    ("2_goal", "50-75%", "slight"): 0.2,
    ("2_goal", "25-50%", "strong_fav"): 0.3,
    ("2_goal", "25-50%", "moderate"): 0.15,
    ("2_goal", "25-50%", "slight"): 0.08,
    ("2_goal", "<25%", "strong_fav"): 0.1,
    ("2_goal", "<25%", "moderate"): 0.05,
    ("2_goal", "<25%", "slight"): 0.02,

    # 3+ goal deficit
    ("3_goal", ">75%", "strong_fav"): 0.4,
    ("3_goal", ">75%", "moderate"): 0.2,
    ("3_goal", ">75%", "slight"): 0.1,
    ("3_goal", "50-75%", "strong_fav"): 0.15,
    ("3_goal", "50-75%", "moderate"): 0.08,
    ("3_goal", "50-75%", "slight"): 0.03,
    ("3_goal", "25-50%", "strong_fav"): 0.05,
    ("3_goal", "25-50%", "moderate"): 0.02,
    ("3_goal", "25-50%", "slight"): 0.01,
    ("3_goal", "<25%", "strong_fav"): 0.02,
    ("3_goal", "<25%", "moderate"): 0.01,
    ("3_goal", "<25%", "slight"): 0.005,
}


# ── Entry Criteria ───────────────────────────────────────────────────────────

# Binary (NBA, NHL, MLB, etc.)
BINARY_ENTRY_CRITERIA = {
    "min_pregame_prob": 0.65,       # Pregame favorite must be >= 65%
    "max_kalshi_fav_price": 38,     # Kalshi fav price must be <= 38c (deep underdog territory)
    "min_time_remaining_pct": 0.50, # At least 50% of game remaining
    "max_deficit_bucket": "large",  # Skip blowouts
}

# Three-way (soccer)
THREE_WAY_ENTRY_CRITERIA = {
    "min_pregame_prob": 0.60,       # Lower threshold for soccer (draws exist)
    "max_kalshi_fav_price": 35,     # Tighter for soccer (3-way pricing)
    "min_time_remaining_pct": 0.55, # Soccer: at least 55% (40 min+)
    "max_deficit_goals": 1,         # 1 goal ONLY — 2+ goal comebacks are rare
}


def classify_deficit_binary(deficit: int) -> str:
    """Classify point deficit into bucket for binary sports."""
    if deficit <= 5:
        return "small"
    elif deficit <= 10:
        return "medium"
    elif deficit <= 15:
        return "large"
    else:
        return "blowout"


def classify_deficit_three_way(deficit: int) -> str:
    """Classify goal deficit into bucket for soccer."""
    if deficit == 1:
        return "1_goal"
    elif deficit == 2:
        return "2_goal"
    else:
        return "3_goal"


def classify_time_remaining(pct: float) -> str:
    """Classify time remaining percentage into bucket."""
    if pct > 0.75:
        return ">75%"
    elif pct > 0.50:
        return "50-75%"
    elif pct > 0.25:
        return "25-50%"
    else:
        return "<25%"


def classify_strength(pregame_prob: float, outcome_type: str) -> str:
    """Classify pregame favorite strength into bucket."""
    if outcome_type == "three_way":
        if pregame_prob >= 0.75:
            return "strong_fav"
        elif pregame_prob >= 0.60:
            return "moderate"
        else:
            return "slight"
    else:
        if pregame_prob >= 0.75:
            return "strong_fav"
        elif pregame_prob >= 0.65:
            return "moderate"
        else:
            return "slight"


def lookup_lr(outcome_type: str, deficit_bucket: str,
              time_bucket: str, strength_bucket: str) -> float:
    """Look up likelihood ratio from the appropriate table."""
    key = (deficit_bucket, time_bucket, strength_bucket)
    if outcome_type == "three_way":
        return THREE_WAY_LR_TABLE.get(key, 1.0)
    else:
        return BINARY_LR_TABLE.get(key, 1.0)
