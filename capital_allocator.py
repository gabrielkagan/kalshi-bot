"""Cross-strategy bankroll allocation and correlation risk management."""

import json
import math
import time
import logging
import datetime
from datetime import timezone
from typing import Dict, Optional

# ─── Static Strategy Weights ─────────────────────────────────────────────────

STRATEGY_CONFIGS = {
    # max_cents high for crypto_15m: per-trade risk already bounded by MAX_RISK_PER_TRADE.
    # When it's the sole active strategy, it must get full balance — not an artificial cap.
    "crypto_15m": {"weight": 0.50, "max_cents": 1000000, "min_cents": 5000},  # $10K ceiling (was $1K)
    "crypto_hourly": {"weight": 0.15, "max_cents": 8000, "min_cents": 2000},
    "spx_hourly": {"weight": 0.25, "max_cents": 12000, "min_cents": 3000},
    "weather": {"weight": 0.10, "max_cents": 5000, "min_cents": 1000},
}

# ─── Correlation Risk Thresholds ─────────────────────────────────────────────

REGIME_BUDGET_CAPS_CENTS = {
    "RED": 12000,      # $120
    "ORANGE": 20000,   # $200
    "YELLOW": 30000,   # $300
    "GREEN": 40000,    # $400
}

CORRELATION_STATE_FILE = "correlation_state.json"

# FOMC meeting dates for 2026 (dates the statement is released)
FOMC_DATES_2026 = {
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-10",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
}


class CapitalAllocator:
    """Allocates bankroll across strategies with static weights.

    Observation-mode strategies release their budget to the active pool.
    Per-strategy min/max bounds prevent starvation or overallocation.
    """

    def __init__(self, observation_strategies: Optional[set] = None):
        self._observation_strategies = observation_strategies or set()
        self._correlation_mgr = CorrelationRiskManager()

    def get_budget_cents(self, strategy: str, total_balance_cents: int,
                         locked_by_strategy: Optional[Dict[str, int]] = None) -> int:
        """Compute available budget in cents for a strategy.

        Args:
            strategy: One of the keys in STRATEGY_CONFIGS (e.g., "crypto_15m").
            total_balance_cents: Total bankroll in cents.
            locked_by_strategy: Dict mapping strategy -> cents currently locked in positions.
        """
        if strategy not in STRATEGY_CONFIGS:
            return total_balance_cents  # unknown strategy gets full balance (backward compat)

        locked = locked_by_strategy or {}

        # Regime cap removed — was permanently GREEN/$400, throttling all trades.
        # CorrelationRiskManager.update_daily() was never called, making the regime
        # static. Full balance now flows to sizing; per-asset caps in bot.py control risk.
        effective_total = total_balance_cents

        # Redistribute observation-mode budgets to active strategies
        active_weight_sum = sum(
            cfg["weight"] for s, cfg in STRATEGY_CONFIGS.items()
            if s not in self._observation_strategies
        )
        if active_weight_sum <= 0:
            active_weight_sum = 1.0  # safety

        cfg = STRATEGY_CONFIGS[strategy]

        if strategy in self._observation_strategies:
            return 0  # observation strategies don't get real budget

        # Scale weight proportionally among active strategies
        scaled_weight = cfg["weight"] / active_weight_sum
        raw_budget = int(effective_total * scaled_weight)

        # Subtract already-locked capital for this strategy
        already_locked = locked.get(strategy, 0)
        available = raw_budget - already_locked

        # Enforce min/max bounds
        available = max(cfg["min_cents"], min(cfg["max_cents"], available))

        # Don't exceed total balance
        return max(0, min(available, total_balance_cents))

    def get_all_budgets(self, total_balance_cents: int,
                        locked_by_strategy: Optional[Dict[str, int]] = None) -> Dict[str, int]:
        """Get budgets for all strategies."""
        return {
            s: self.get_budget_cents(s, total_balance_cents, locked_by_strategy)
            for s in STRATEGY_CONFIGS
        }

    def get_regime(self) -> str:
        return self._correlation_mgr.get_regime()

    def get_ewma_correlation(self) -> Optional[float]:
        return self._correlation_mgr._ewma_corr

    def get_vix(self) -> Optional[float]:
        return self._correlation_mgr._last_vix

    def get_composite_score(self) -> int:
        return self._correlation_mgr._composite_score

    def update_observation_strategies(self, obs_set: set):
        """Update which strategies are in observation mode."""
        self._observation_strategies = obs_set

    def update_daily(self, btc_daily_return: Optional[float] = None,
                     spx_daily_return: Optional[float] = None,
                     vix_level: Optional[float] = None):
        """Update correlation risk manager with daily data (call at 4:01 PM ET)."""
        self._correlation_mgr.update_daily(btc_daily_return, spx_daily_return, vix_level)


class CorrelationRiskManager:
    """EWMA correlation tracker between BTC and SPX with regime-based budget caps.

    Composite score (0-12):
      - EWMA corr:      <0.40→0, 0.40-0.60→1, 0.60-0.75→3, >0.75→4
      - VIX level:       <20→0, 20-25→1, 25-30→3, >30→4
      - Same-day decline: both down >3%→4, >2%→3, >1%→1, else→0
      - FOMC day:        auto-add 2 points

    Regime: ≥7→RED, ≥4→ORANGE, ≥2→YELLOW, else→GREEN
    Escalation is immediate; de-escalation requires 3 consecutive GREEN days.
    """

    def __init__(self):
        self._ewma_corr: Optional[float] = None
        self._ewma_lambda = 0.94  # ~11-day half-life
        self._last_vix: Optional[float] = None
        self._composite_score: int = 0
        self._regime: str = "GREEN"
        self._consecutive_green_days: int = 0
        self._prev_regime: str = "GREEN"

        # EWMA internals
        self._ewma_cov: float = 0.0
        self._ewma_var_btc: float = 0.0
        self._ewma_var_spx: float = 0.0
        self._ewma_mean_btc: float = 0.0
        self._ewma_mean_spx: float = 0.0
        self._initialized: bool = False

        self._load_state()

    def _load_state(self):
        try:
            with open(CORRELATION_STATE_FILE, "r") as f:
                state = json.load(f)
            self._ewma_corr = state.get("ewma_corr")
            self._ewma_cov = state.get("ewma_cov", 0.0)
            self._ewma_var_btc = state.get("ewma_var_btc", 0.0)
            self._ewma_var_spx = state.get("ewma_var_spx", 0.0)
            self._ewma_mean_btc = state.get("ewma_mean_btc", 0.0)
            self._ewma_mean_spx = state.get("ewma_mean_spx", 0.0)
            self._last_vix = state.get("last_vix")
            self._composite_score = state.get("composite_score", 0)
            self._regime = state.get("regime", "GREEN")
            self._consecutive_green_days = state.get("consecutive_green_days", 0)
            self._prev_regime = state.get("prev_regime", "GREEN")
            self._initialized = state.get("initialized", False)
            logging.info("CorrelationRiskManager: loaded state (regime=%s, score=%d, corr=%s)",
                         self._regime, self._composite_score,
                         f"{self._ewma_corr:.3f}" if self._ewma_corr is not None else "N/A")
        except FileNotFoundError:
            logging.info("CorrelationRiskManager: no saved state, starting fresh")
        except Exception as e:
            logging.warning("CorrelationRiskManager: failed to load state: %s", e)

    def _save_state(self):
        try:
            state = {
                "ewma_corr": self._ewma_corr,
                "ewma_cov": self._ewma_cov,
                "ewma_var_btc": self._ewma_var_btc,
                "ewma_var_spx": self._ewma_var_spx,
                "ewma_mean_btc": self._ewma_mean_btc,
                "ewma_mean_spx": self._ewma_mean_spx,
                "last_vix": self._last_vix,
                "composite_score": self._composite_score,
                "regime": self._regime,
                "consecutive_green_days": self._consecutive_green_days,
                "prev_regime": self._prev_regime,
                "initialized": self._initialized,
                "updated_at": datetime.datetime.now(timezone.utc).isoformat(),
            }
            with open(CORRELATION_STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logging.warning("CorrelationRiskManager: failed to save state: %s", e)

    def get_regime(self) -> str:
        return self._regime

    def update_daily(self, btc_return: Optional[float] = None,
                     spx_return: Optional[float] = None,
                     vix_level: Optional[float] = None):
        """Update with daily returns and recompute regime.

        Call once per day at market close (4:01 PM ET).
        btc_return and spx_return are log returns (e.g., 0.02 = +2%).
        """
        if vix_level is not None:
            self._last_vix = vix_level

        # Update EWMA correlation if both returns available
        if btc_return is not None and spx_return is not None:
            lam = self._ewma_lambda
            if not self._initialized:
                # Seed with first observation
                self._ewma_mean_btc = btc_return
                self._ewma_mean_spx = spx_return
                self._ewma_var_btc = btc_return ** 2
                self._ewma_var_spx = spx_return ** 2
                self._ewma_cov = btc_return * spx_return
                self._initialized = True
            else:
                # EWMA update
                self._ewma_mean_btc = lam * self._ewma_mean_btc + (1 - lam) * btc_return
                self._ewma_mean_spx = lam * self._ewma_mean_spx + (1 - lam) * spx_return
                dev_btc = btc_return - self._ewma_mean_btc
                dev_spx = spx_return - self._ewma_mean_spx
                self._ewma_var_btc = lam * self._ewma_var_btc + (1 - lam) * dev_btc ** 2
                self._ewma_var_spx = lam * self._ewma_var_spx + (1 - lam) * dev_spx ** 2
                self._ewma_cov = lam * self._ewma_cov + (1 - lam) * dev_btc * dev_spx

            # Compute correlation
            denom = math.sqrt(max(self._ewma_var_btc, 1e-12) * max(self._ewma_var_spx, 1e-12))
            self._ewma_corr = self._ewma_cov / denom if denom > 0 else 0.0
            self._ewma_corr = max(-1.0, min(1.0, self._ewma_corr))

        # Compute composite score
        score = 0

        # 1. EWMA correlation component
        if self._ewma_corr is not None:
            c = abs(self._ewma_corr)
            if c > 0.75:
                score += 4
            elif c > 0.60:
                score += 3
            elif c > 0.40:
                score += 1

        # 2. VIX component
        if self._last_vix is not None:
            if self._last_vix > 30:
                score += 4
            elif self._last_vix > 25:
                score += 3
            elif self._last_vix > 20:
                score += 1

        # 3. Same-day co-decline
        if btc_return is not None and spx_return is not None:
            if btc_return < -0.03 and spx_return < -0.03:
                score += 4
            elif btc_return < -0.02 and spx_return < -0.02:
                score += 3
            elif btc_return < -0.01 and spx_return < -0.01:
                score += 1

        # 4. FOMC day
        today_str = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today_str in FOMC_DATES_2026:
            score += 2

        self._composite_score = score

        # Determine raw regime
        if score >= 7:
            raw_regime = "RED"
        elif score >= 4:
            raw_regime = "ORANGE"
        elif score >= 2:
            raw_regime = "YELLOW"
        else:
            raw_regime = "GREEN"

        # Regime transition: escalate immediately, de-escalate after 3 GREEN days
        regime_order = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}

        if regime_order.get(raw_regime, 0) >= regime_order.get(self._regime, 0):
            # Escalation: immediate
            self._regime = raw_regime
            self._consecutive_green_days = 0
        elif raw_regime == "GREEN":
            self._consecutive_green_days += 1
            if self._consecutive_green_days >= 3:
                # Step down one level
                current_idx = regime_order.get(self._regime, 0)
                if current_idx > 0:
                    for name, idx in regime_order.items():
                        if idx == current_idx - 1:
                            self._regime = name
                            break
                    self._consecutive_green_days = 0
        else:
            self._consecutive_green_days = 0

        logging.info(
            "CorrelationRiskManager: score=%d regime=%s corr=%s vix=%s",
            score, self._regime,
            f"{self._ewma_corr:.3f}" if self._ewma_corr is not None else "N/A",
            f"{self._last_vix:.1f}" if self._last_vix is not None else "N/A",
        )

        self._save_state()

    def is_fomc_day(self) -> bool:
        today_str = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return today_str in FOMC_DATES_2026
