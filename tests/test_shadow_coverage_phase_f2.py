"""Phase F-2 (Shadow Coverage Expansion): maker counterfactual snapshot.

Phase F-2 populates the snapshot half of Phase B's maker counterfactual:
  - `maker_price_cents` = best_yes_bid + 1 (1-cent improve maker post).
    NULL if best_yes_bid is unavailable OR best_yes_bid + 1 >= best_yes_ask
    (a maker post that crosses the spread is mathematically a taker — skip).
  - `maker_depth_at_post` = depth currently at maker_price_cents in the
    YES bid ladder. 0 if no level there (the typical case for an "improve"
    maker post — creates a new level). NULL if ladder unavailable.

DEFERRED to a future Phase F-2b: `maker_would_fill_within_30s` requires a
post-hoc fillability daemon that tracks ask depletion + trade prints over
the 30s window after the row is written. Out of scope for this commit.

Master plan: kb/decisions/shadow-coverage-expansion-may01.md.
"""

import json
import os
import sys

import pytest
import bot.scanner  # noqa: F401
import bot.state  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestPhaseF2MakerHelper:
    """Direct unit tests for the new helper that derives the maker
    counterfactual snapshot from (best_yes_bid, best_yes_ask, ladder_json)."""

    def test_improve_maker_in_wide_spread(self):
        """Bid 75, ask 80 → maker post at 76, depth 0 (new level)."""
        import bot
        ladder_json = json.dumps({
            "yes_bids": [[75, 100], [74, 50]],
            "yes_asks": [[80, 200], [81, 50]],
        })
        out = bot.scanner.OpportunityScanner._compute_maker_counterfactual(
            best_yes_bid=75, best_yes_ask=80, ladder_json=ladder_json,
        )
        assert out["maker_price_cents"] == 76
        assert out["maker_depth_at_post"] == 0

    def test_one_cent_spread_returns_null_price(self):
        """Bid 79, ask 80 → maker post at 80 would CROSS the ask (taker, not
        maker). Helper returns NULL price + NULL depth — capturing the
        operational truth that a maker post is impossible at this spread."""
        import bot
        ladder_json = json.dumps({
            "yes_bids": [[79, 100]],
            "yes_asks": [[80, 200]],
        })
        out = bot.scanner.OpportunityScanner._compute_maker_counterfactual(
            best_yes_bid=79, best_yes_ask=80, ladder_json=ladder_json,
        )
        assert out["maker_price_cents"] is None
        assert out["maker_depth_at_post"] is None

    def test_no_bid_returns_null(self):
        """No best bid → can't compute maker price."""
        import bot
        out = bot.scanner.OpportunityScanner._compute_maker_counterfactual(
            best_yes_bid=None, best_yes_ask=80, ladder_json=None,
        )
        assert out["maker_price_cents"] is None
        assert out["maker_depth_at_post"] is None

    def test_no_ask_returns_null(self):
        """No best ask → can't verify the post wouldn't cross. NULL safer
        than guessing."""
        import bot
        ladder_json = json.dumps({"yes_bids": [[75, 100]], "yes_asks": []})
        out = bot.scanner.OpportunityScanner._compute_maker_counterfactual(
            best_yes_bid=75, best_yes_ask=None, ladder_json=ladder_json,
        )
        assert out["maker_price_cents"] is None
        assert out["maker_depth_at_post"] is None

    def test_ladder_with_existing_level_at_maker_price(self):
        """Bid 75 with also a level at 76 (someone else already bidding
        between standard bid and ask) → maker would JOIN that level;
        depth = sum of qty at 76."""
        import bot
        ladder_json = json.dumps({
            "yes_bids": [[76, 50], [75, 100]],
            "yes_asks": [[80, 200]],
        })
        out = bot.scanner.OpportunityScanner._compute_maker_counterfactual(
            best_yes_bid=75, best_yes_ask=80, ladder_json=ladder_json,
        )
        # Maker price = best_bid + 1 = 76; depth there = 50.
        assert out["maker_price_cents"] == 76
        assert out["maker_depth_at_post"] == 50

    def test_malformed_ladder_treated_as_no_data(self):
        """Bad JSON → maker_price still computable (just bid+1), but
        depth lookup returns 0 (no level found vs True NULL — depth=0 is
        the conservative answer for 'level didn't exist in the ladder')."""
        import bot
        out = bot.scanner.OpportunityScanner._compute_maker_counterfactual(
            best_yes_bid=75, best_yes_ask=80, ladder_json="{not_valid_json",
        )
        # Price still derivable (no ladder needed).
        assert out["maker_price_cents"] == 76
        # Depth: 0 (level absent from a malformed/missing ladder).
        assert out["maker_depth_at_post"] == 0

    def test_no_ladder_provided(self):
        """ladder_json=None → maker_price computable; depth=0 fallback."""
        import bot
        out = bot.scanner.OpportunityScanner._compute_maker_counterfactual(
            best_yes_bid=75, best_yes_ask=80, ladder_json=None,
        )
        assert out["maker_price_cents"] == 76
        assert out["maker_depth_at_post"] == 0


class TestPhaseF2EndToEndInsert:
    """End-to-end: when scan-tick caches `_scan_ms_cache[ticker]` includes
    `maker_price_cents` + `maker_depth_at_post`, an insert_evaluated_opportunity
    call auto-fills both into the row."""

    def test_insert_picks_up_maker_fields_from_ms_cache(self):
        import bot
        sm = bot.state.StateManager(":memory:")
        sm._scan_ms_cache["TEST15M-X"] = {
            "yes_spread_cents": 5,
            "bid_depth": 100,
            "maker_price_cents": 76,
            "maker_depth_at_post": 0,
        }
        sm.insert_evaluated_opportunity(
            ticker="TEST15M-X", event_ticker="E", asset="BTC",
            filter_stage="low_price_shadow", product_type="15m",
            market_price=80,
        )
        row = sm.conn.execute(
            "SELECT maker_price_cents, maker_depth_at_post "
            "FROM evaluated_opportunities WHERE ticker = 'TEST15M-X'"
        ).fetchone()
        assert row is not None
        assert row["maker_price_cents"] == 76
        assert row["maker_depth_at_post"] == 0

    def test_insert_explicit_kwargs_override_cache(self):
        """Caller-provided maker fields take precedence over cache values."""
        import bot
        sm = bot.state.StateManager(":memory:")
        sm._scan_ms_cache["TEST15M-Y"] = {
            "maker_price_cents": 76, "maker_depth_at_post": 0,
        }
        sm.insert_evaluated_opportunity(
            ticker="TEST15M-Y", event_ticker="E", asset="BTC",
            filter_stage="low_price_shadow", product_type="15m",
            market_price=80,
            maker_price_cents=99, maker_depth_at_post=42,
        )
        row = sm.conn.execute(
            "SELECT maker_price_cents, maker_depth_at_post "
            "FROM evaluated_opportunities WHERE ticker = 'TEST15M-Y'"
        ).fetchone()
        assert row["maker_price_cents"] == 99
        assert row["maker_depth_at_post"] == 42

    def test_no_cache_entry_writes_null(self):
        import bot
        sm = bot.state.StateManager(":memory:")
        # No cache entry for this ticker.
        sm.insert_evaluated_opportunity(
            ticker="TEST15M-Z", event_ticker="E", asset="BTC",
            filter_stage="low_price_shadow", product_type="15m",
            market_price=80,
        )
        row = sm.conn.execute(
            "SELECT maker_price_cents, maker_depth_at_post "
            "FROM evaluated_opportunities WHERE ticker = 'TEST15M-Z'"
        ).fetchone()
        assert row["maker_price_cents"] is None
        assert row["maker_depth_at_post"] is None
