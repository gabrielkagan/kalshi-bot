#!/usr/bin/env python3
"""Build empirical likelihood ratio tables from NBA historical data.

Uses nba_api to fetch play-by-play data and compute comeback rates
by (deficit, time_remaining, pregame_strength) buckets.

Runs locally, not on VPS. Requires: pip install nba_api

Usage:
    python scripts/ops/build_lr_tables.py [--seasons 5] [--output sports_data_lr.py]
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from typing import Dict, Tuple

try:
    from nba_api.stats.endpoints import leaguegamefinder, playbyplayv2
    from nba_api.stats.static import teams as nba_teams
except ImportError:
    print("ERROR: nba_api not installed. Run: pip install nba_api")
    sys.exit(1)


def get_season_games(season: str) -> list:
    """Get all regular season games for a season (e.g. '2023-24')."""
    finder = leaguegamefinder.LeagueGameFinder(
        season_nullable=season,
        season_type_nullable="Regular Season",
        league_id_nullable="00",
    )
    time.sleep(1)  # Rate limit
    df = finder.get_data_frames()[0]
    # Each game appears twice (once per team) — deduplicate by GAME_ID
    game_ids = df["GAME_ID"].unique().tolist()
    return game_ids


def compute_comeback_rates(game_ids: list, max_games: int = 2000) -> Dict:
    """Analyze play-by-play to compute comeback rates.

    Returns dict of bucket → {total: int, comebacks: int, rate: float}
    """
    buckets = defaultdict(lambda: {"total": 0, "comebacks": 0})

    for i, game_id in enumerate(game_ids[:max_games]):
        if i % 50 == 0:
            print(f"  Processing game {i+1}/{min(len(game_ids), max_games)}...")
        try:
            pbp = playbyplayv2.PlayByPlayV2(game_id=game_id)
            time.sleep(0.6)  # Rate limit
            df = pbp.get_data_frames()[0]
            process_game_pbp(df, game_id, buckets)
        except Exception as e:
            print(f"  Skipping game {game_id}: {e}")
            continue

    # Compute rates
    results = {}
    for key, val in buckets.items():
        rate = val["comebacks"] / val["total"] if val["total"] > 0 else 0
        results[key] = {
            "total": val["total"],
            "comebacks": val["comebacks"],
            "rate": round(rate, 4),
        }
    return results


def process_game_pbp(df, game_id: str, buckets: dict) -> None:
    """Process a single game's play-by-play data."""
    if df.empty:
        return

    # Get final score to determine winner
    last_row = df.iloc[-1]
    # SCOREMARGIN format: "TIE" or "+5" or "-3" (home perspective)
    margin = last_row.get("SCOREMARGIN", "TIE")
    if margin == "TIE" or not margin:
        return  # Skip ties/OT for now

    # Track score states throughout the game
    total_periods = 4  # Regulation
    for _, row in df.iterrows():
        period = row.get("PERIOD", 0)
        if period > 4:
            continue  # Skip OT

        score_margin = row.get("SCOREMARGIN", "TIE")
        if score_margin == "TIE" or not score_margin:
            continue

        try:
            margin_val = int(score_margin)
        except (ValueError, TypeError):
            continue

        if margin_val == 0:
            continue

        # Determine if the team that's behind ultimately wins
        deficit = abs(margin_val)
        trailing_team_won = (margin_val > 0 and int(margin) < 0) or (
            margin_val < 0 and int(margin) > 0
        )

        # Classify deficit
        if deficit <= 5:
            deficit_bucket = "small"
        elif deficit <= 10:
            deficit_bucket = "medium"
        elif deficit <= 15:
            deficit_bucket = "large"
        else:
            deficit_bucket = "blowout"

        # Time remaining
        time_remaining_pct = 1.0 - (period - 1) / total_periods
        if time_remaining_pct > 0.75:
            time_bucket = ">75%"
        elif time_remaining_pct > 0.50:
            time_bucket = "50-75%"
        elif time_remaining_pct > 0.25:
            time_bucket = "25-50%"
        else:
            time_bucket = "<25%"

        # For now, use "moderate" strength bucket (would need pregame odds)
        strength_bucket = "moderate"

        key = (deficit_bucket, time_bucket, strength_bucket)
        buckets[key]["total"] += 1
        if trailing_team_won:
            buckets[key]["comebacks"] += 1


def rates_to_lr(rates: Dict) -> Dict[str, float]:
    """Convert comeback rates to likelihood ratios.

    LR = P(observe state | trailing team wins) / P(observe state | trailing team loses)
       ≈ comeback_rate / (1 - comeback_rate)
    """
    lr_table = {}
    for key, val in rates.items():
        rate = val["rate"]
        if rate > 0 and rate < 1:
            lr = rate / (1 - rate)
        elif rate >= 1:
            lr = 10.0  # Cap
        else:
            lr = 0.01  # Floor
        lr_table[str(key)] = round(lr, 3)
    return lr_table


def main():
    parser = argparse.ArgumentParser(description="Build LR tables from NBA data")
    parser.add_argument("--seasons", type=int, default=3, help="Number of seasons")
    parser.add_argument("--max-games", type=int, default=500, help="Max games per season")
    parser.add_argument("--output", default="sports_lr_empirical.json", help="Output file")
    args = parser.parse_args()

    # Generate season strings (e.g. "2023-24", "2022-23", ...)
    current_year = 2025
    seasons = [f"{current_year - i}-{str(current_year - i + 1)[2:]}"
               for i in range(1, args.seasons + 1)]

    all_games = []
    for season in seasons:
        print(f"Fetching games for {season}...")
        games = get_season_games(season)
        print(f"  Found {len(games)} games")
        all_games.extend(games)

    print(f"\nTotal games: {len(all_games)}")
    print(f"Processing play-by-play (max {args.max_games} per season)...")

    rates = compute_comeback_rates(all_games, max_games=args.max_games * args.seasons)

    print(f"\nComputed rates for {len(rates)} buckets:")
    for key in sorted(rates.keys()):
        val = rates[key]
        print(f"  {key}: {val['comebacks']}/{val['total']} = {val['rate']:.1%}")

    lr_table = rates_to_lr(rates)
    print(f"\nLR table:")
    for key in sorted(lr_table.keys()):
        print(f"  {key}: LR = {lr_table[key]}")

    # Save
    output = {
        "seasons": seasons,
        "total_games": len(all_games),
        "comeback_rates": {str(k): v for k, v in rates.items()},
        "lr_table": lr_table,
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
