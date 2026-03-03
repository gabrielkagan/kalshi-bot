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
    sport_group: str = ""       # Key into SPORT_GROUPS
    enabled: bool = True


@dataclass(frozen=True)
class SportGroupConfig:
    """Per-sport-group calibration config."""
    group_name: str
    lr_scale: float             # Replaces global CONSERVATIVE_LR_SCALE
    lr_table_type: str          # "binary" or "three_way"
    temperature: float = 1.0          # Phase 2 placeholder
    min_games_for_cal: int = 50       # Phase 2 placeholder


SPORT_GROUPS: Dict[str, SportGroupConfig] = {
    "basketball": SportGroupConfig("basketball", 0.2, "binary"),
    "hockey":     SportGroupConfig("hockey",     0.2, "binary"),
    "tennis":     SportGroupConfig("tennis",     0.2, "binary"),
    "soccer":     SportGroupConfig("soccer",     0.2, "three_way"),
    "baseball":   SportGroupConfig("baseball",   0.2, "binary"),
    "football":   SportGroupConfig("football",   0.2, "binary"),
    "mma":        SportGroupConfig("mma",        0.2, "binary"),
    "esports":    SportGroupConfig("esports",    0.2, "binary"),
}


# ── All 26 Kalshi game-level series ──────────────────────────────────────────
# Leagues without ESPN endpoints have espn_league=None — engine skips ESPN
# polling for them and only does Kalshi price monitoring.
# Off-season leagues return 0 games from ESPN and skip silently.

LEAGUES: Dict[str, LeagueConfig] = {
    # ── Binary (2 markets/game: Team A wins, Team B wins) ──
    "KXNBAGAME": LeagueConfig(
        series_ticker="KXNBAGAME", espn_sport="basketball",
        espn_league="nba", outcome_type="binary", display_name="NBA",
        sport_group="basketball",
    ),
    "KXNHLGAME": LeagueConfig(
        series_ticker="KXNHLGAME", espn_sport="hockey",
        espn_league="nhl", outcome_type="binary", display_name="NHL",
        sport_group="hockey",
    ),
    "KXMLBGAME": LeagueConfig(
        series_ticker="KXMLBGAME", espn_sport="baseball",
        espn_league="mlb", outcome_type="binary", display_name="MLB",
        sport_group="baseball",
    ),
    "KXNCAABBGAME": LeagueConfig(
        series_ticker="KXNCAABBGAME", espn_sport="basketball",
        espn_league="mens-college-basketball", outcome_type="binary",
        display_name="NCAAB", sport_group="basketball",
    ),
    "KXNCAAFGAME": LeagueConfig(
        series_ticker="KXNCAAFGAME", espn_sport="football",
        espn_league="college-football", outcome_type="binary",
        display_name="NCAAF", sport_group="football",
    ),
    "KXNFLGAME": LeagueConfig(
        series_ticker="KXNFLGAME", espn_sport="football",
        espn_league="nfl", outcome_type="binary", display_name="NFL",
        sport_group="football",
    ),
    "KXWNBAGAME": LeagueConfig(
        series_ticker="KXWNBAGAME", espn_sport="basketball",
        espn_league="wnba", outcome_type="binary", display_name="WNBA",
        sport_group="basketball",
    ),
    "KXUFCFIGHT": LeagueConfig(
        series_ticker="KXUFCFIGHT", espn_sport="mma",
        espn_league="ufc", outcome_type="binary", display_name="UFC",
        sport_group="mma",
    ),
    "KXATPMATCH": LeagueConfig(
        series_ticker="KXATPMATCH", espn_sport="tennis",
        espn_league="atp", outcome_type="binary", display_name="ATP Tennis",
        sport_group="tennis",
    ),
    "KXWTAMATCH": LeagueConfig(
        series_ticker="KXWTAMATCH", espn_sport="tennis",
        espn_league="wta", outcome_type="binary", display_name="WTA Tennis",
        sport_group="tennis",
    ),
    # Esports — no ESPN endpoint
    "KXCSGOGAME": LeagueConfig(
        series_ticker="KXCSGOGAME", espn_sport=None,
        espn_league=None, outcome_type="binary", display_name="CSGO",
        sport_group="esports",
    ),
    "KXLOLGAME": LeagueConfig(
        series_ticker="KXLOLGAME", espn_sport=None,
        espn_league=None, outcome_type="binary", display_name="LoL",
        sport_group="esports",
    ),
    "KXVALORANTGAME": LeagueConfig(
        series_ticker="KXVALORANTGAME", espn_sport=None,
        espn_league=None, outcome_type="binary", display_name="Valorant",
        sport_group="esports",
    ),

    # ── Three-way (3 markets/game: Home, Away, Draw) ──
    "KXEPLGAME": LeagueConfig(
        series_ticker="KXEPLGAME", espn_sport="soccer",
        espn_league="eng.1", outcome_type="three_way", display_name="EPL",
        sport_group="soccer",
    ),
    "KXBUNDESLIGAGAME": LeagueConfig(
        series_ticker="KXBUNDESLIGAGAME", espn_sport="soccer",
        espn_league="ger.1", outcome_type="three_way", display_name="Bundesliga",
        sport_group="soccer",
    ),
    "KXLALIGAGAME": LeagueConfig(
        series_ticker="KXLALIGAGAME", espn_sport="soccer",
        espn_league="esp.1", outcome_type="three_way", display_name="La Liga",
        sport_group="soccer",
    ),
    "KXSERIEAGAME": LeagueConfig(
        series_ticker="KXSERIEAGAME", espn_sport="soccer",
        espn_league="ita.1", outcome_type="three_way", display_name="Serie A",
        sport_group="soccer",
    ),
    "KXUCLGAME": LeagueConfig(
        series_ticker="KXUCLGAME", espn_sport="soccer",
        espn_league="uefa.champions", outcome_type="three_way", display_name="UCL",
        sport_group="soccer",
    ),
    "KXLIGUE1GAME": LeagueConfig(
        series_ticker="KXLIGUE1GAME", espn_sport="soccer",
        espn_league="fra.1", outcome_type="three_way", display_name="Ligue 1",
        sport_group="soccer",
    ),
    "KXSUPERLIGGAME": LeagueConfig(
        series_ticker="KXSUPERLIGGAME", espn_sport="soccer",
        espn_league="tur.1", outcome_type="three_way", display_name="Turkish Super Lig",
        sport_group="soccer",
    ),
    "KXMLSGAME": LeagueConfig(
        series_ticker="KXMLSGAME", espn_sport="soccer",
        espn_league="usa.1", outcome_type="three_way", display_name="MLS",
        sport_group="soccer",
    ),
    "KXUELGAME": LeagueConfig(
        series_ticker="KXUELGAME", espn_sport="soccer",
        espn_league="uefa.europa", outcome_type="three_way",
        display_name="Europa League", sport_group="soccer",
    ),
    "KXUECLGAME": LeagueConfig(
        series_ticker="KXUECLGAME", espn_sport="soccer",
        espn_league="uefa.europa.conf", outcome_type="three_way",
        display_name="Conference League", sport_group="soccer",
    ),
    "KXLIGAMXGAME": LeagueConfig(
        series_ticker="KXLIGAMXGAME", espn_sport="soccer",
        espn_league="mex.1", outcome_type="three_way", display_name="Liga MX",
        sport_group="soccer",
    ),
    "KXWCGAME": LeagueConfig(
        series_ticker="KXWCGAME", espn_sport="soccer",
        espn_league="fifa.worldcup", outcome_type="three_way",
        display_name="World Cup", sport_group="soccer",
    ),
    "KXFIFAGAME": LeagueConfig(
        series_ticker="KXFIFAGAME", espn_sport="soccer",
        espn_league="fifa.friendly", outcome_type="three_way",
        display_name="FIFA Intl", sport_group="soccer",
    ),
    "KXAFCONGAME": LeagueConfig(
        series_ticker="KXAFCONGAME", espn_sport=None,
        espn_league=None, outcome_type="three_way", display_name="AFC/Intl",
        sport_group="soccer",
    ),
    "KXEREDIVISIEGAME": LeagueConfig(
        series_ticker="KXEREDIVISIEGAME", espn_sport="soccer",
        espn_league="ned.1", outcome_type="three_way", display_name="Eredivisie",
        sport_group="soccer",
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


# ── LR Calibration Overrides ────────────────────────────────────────────────

# Conservative scaling: compress LR toward 1.0 (neutral).
# LR_scaled = 1.0 + (LR_raw - 1.0) * CONSERVATIVE_LR_SCALE
# At scale=0.5: LR=2.8 → 1.9, posterior drops from 94.5% → 92.1%
# Original table preserved for reference. Set to 1.0 to use raw values.
CONSERVATIVE_LR_SCALE = 0.2

# Reject signals where model - market > this threshold.
# 30pp: model can be at most 30pp above market (market=20% → model max=50%)
MAX_MODEL_MARKET_GAP = 0.80  # Was 0.20 — need wide gap data to calibrate model


# ── Entry Criteria ───────────────────────────────────────────────────────────

# Binary (NBA, NHL, MLB, etc.)
BINARY_ENTRY_CRITERIA = {
    "min_pregame_prob": 0.50,       # Was 0.55 — include slight favorites for data collection
    "max_kalshi_fav_price": 95,     # Was 80 — capture high-price data
    "min_time_remaining_pct": 0.10, # Was 0.50 — capture late-game data
    "max_deficit_bucket": "blowout", # Was "large" — include blowouts for calibration
}

# Three-way (soccer)
THREE_WAY_ENTRY_CRITERIA = {
    "min_pregame_prob": 0.50,       # Was 0.55 — include slight favorites
    "max_kalshi_fav_price": 95,     # Was 70 — capture high-price data
    "min_time_remaining_pct": 0.10, # Was 0.55 — capture late-game data
    "max_deficit_goals": 3,         # Was 1 — capture multi-goal data for calibration
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


def classify_deficit_tennis(deficit: int) -> str:
    """Classify tennis deficit (games within set or sets).

    When sets are even, deficit is in games (0-6 range).
    When sets are uneven, deficit is in sets (1-2 range).
    """
    if deficit <= 2:
        return "small"      # Down 1-2 games or 1 set
    elif deficit <= 4:
        return "medium"     # Down 3-4 games
    else:
        return "large"      # Down 5+ games or 2 sets


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


def get_sport_group_config(league_cfg: LeagueConfig) -> SportGroupConfig:
    """Get per-sport-group calibration config for a league."""
    if league_cfg.sport_group in SPORT_GROUPS:
        return SPORT_GROUPS[league_cfg.sport_group]
    return SportGroupConfig("unknown", CONSERVATIVE_LR_SCALE, league_cfg.outcome_type)


def lookup_lr(outcome_type: str, deficit_bucket: str,
              time_bucket: str, strength_bucket: str,
              lr_scale: Optional[float] = None) -> float:
    """Look up likelihood ratio from the appropriate table.

    Applies lr_scale (or CONSERVATIVE_LR_SCALE) to compress raw LR toward 1.0.
    """
    key = (deficit_bucket, time_bucket, strength_bucket)
    if outcome_type == "three_way":
        raw_lr = THREE_WAY_LR_TABLE.get(key, 1.0)
    else:
        raw_lr = BINARY_LR_TABLE.get(key, 1.0)
    scale = lr_scale if lr_scale is not None else CONSERVATIVE_LR_SCALE
    return 1.0 + (raw_lr - 1.0) * scale
