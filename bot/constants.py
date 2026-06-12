"""bot.constants — module-level configuration constants extracted from
bot/_impl.py per Sprint 3 Bit 3.1.

Re-exported into bot._impl via `from bot.constants import *` near the top
of bot/_impl.py. External consumers (market_config.py, bot/snapshots/dashboard_snapshot.py,
postdeploy_verify.py, etc.) reach these via `import bot.constants; bot.constants.X`
directly post-Bit-9.3-iii.b (2026-05-11 — `_BotProxy` retired). Pre-retirement
the same callers used `import bot; bot.X` via the proxy → bot._impl.X →
star-imported binding chain; that path is gone. Consumers still doing
`import bot._impl as _bot_mod; getattr(_bot_mod, NAME, default)` work via the
residual shim's star-imports until Bit 9.3-iii.c deletes bot/_impl.py.

ZERO-DEPS by contract: only `os` is imported (for `os.environ.get(...)`
env-flag constants like HIGH_PRICE_STC_BLOCK_ENABLED). DO NOT add numpy /
scipy / torch / sklearn / pandas — that defeats bot._thread_env's
pre-numerical-import contract. Regression test:
tests/contracts/test_constants_extraction.py::test_bot_constants_imports_only_os.
"""
import os

# ─── Trading Configuration ───────────────────────────────────────────────────
OBSERVATION_MODE = False           # False = LIVE TRADING with real money

INITIAL_DEPOSIT_CENTS = 63479     # $634.79 — bot starting capital Feb 24 2026 (derived: balance - cumulative PnL)

SERIES_TICKERS = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "SOL": "KXSOL15M",
    "XRP": "KXXRP15M",
    "HYPE": "KXHYPE15M",      # T4 LIVE 2026-05-14 (P2.3, 86b9xv66a)
    "DOGE": "KXDOGE15M",      # T4 LIVE 2026-05-14 (P2.3, 86b9xv66a)
    "BNB": "KXBNB15M",        # T4 LIVE 2026-05-19 (P2.4, 86b9zmj37)
    "ADA": "KXADA15M",        # T1 15M shadow 2026-05-30 (ada-bch-15m-shadow-t1)
    "BCH": "KXBCH15M",        # T1 15M shadow 2026-05-30 (ada-bch-15m-shadow-t1)
}

MIN_ENTRY_PRICE = 75              # cents (global floor — lowered from 80 for ETH 75-79c; SOL uses this, BTC/XRP overridden below)

MAX_ENTRY_PRICE = 99              # cents

BTC_MIN_ENTRY_PRICE = 88          # cents (data: 88c = 96.2% WR on n=53 shadow, 96.3% on n=27 recent)

ETH_MIN_ENTRY_PRICE = 90          # cents (raised from 85 — data: ETH 85-89c is 86.2% WR on 65 trades, -$23.76 PnL; 90c+ is 95.2% WR)

SOL_MIN_ENTRY_PRICE = 86          # cents (raised from 80: SOL@85c 68.2% WR -$496 on 22 trades vs 86c 94.4% WR +$375 on 36 trades)

ETH_SUB80_POSITION_CAP = 50      # Half-Kelly at 75c/87% WR = 322-645 contracts; cap to 50 (ceil), floor 20

XRP_MIN_ENTRY_PRICE = 92          # cents (data: XRP PnL negative at every floor <90c, PF=1.68 at >=92c)

BTC_MAX_RISK_PER_TRADE = 0.15    # BTC: 15% per-trade (was 12% — regime cap removal gives full balance to sizing)

ETH_MAX_RISK_PER_TRADE = 0.20    # ETH: 20% per-trade (new — was uncapped beyond generic 25%)

SOL_MAX_RISK_PER_TRADE = 0.15    # SOL: 15% per-trade (raised from 12% — data: 43.9% trades capped at 12%, +$26 PnL)

XRP_MAX_RISK_PER_TRADE = 0.15    # XRP: 15% per-trade (was 12% — regime cap removal gives full balance)

SOL_MIN_EDGE = 0.010             # SOL-specific edge floor (reverted to 1.0% — prior 1.8% based on pre-BLR data, invalid under passthrough cal)

SOL_HIGH_EDGE_SHADOW = 0.05     # SOL edge ceiling shadow: log evaluations with edge > 5% for analysis (5%+ band is 80% WR, PnL-negative)

SOL_LOW_ENTRY_STC_GATE = True    # Block SOL ≤85c at STC≥300s (data: 78.3% WR -$289, vs <300s 100% WR +$228)

XRP_15M_SHADOW = False            # XRP 15M promoted to live at 92c+ (data: 41W/2L 95.3% WR at >=92c)

# T1 onboarding (2026-05-10, ticket 86b9vecw9): HYPE + DOGE 15M shadow observation.
# T4 live promotion (2026-05-14, ClickUp 86b9xv66a): per-asset MIN_ENTRY_PRICE +
# MAX_RISK_PER_TRADE constants wired into the elif chains (scanner ~:2680-2693 +
# ~:4707-4742 main-path sizing + ~:4144-4171 DC sizing; executor mirrors at
# ~:2090-2103 escalation + ~:3187-3201 maker + ~:4571-4575 sub-floor-fill
# telemetry map). NBBO_FALLBACK_GATES INTENTIONALLY
# OMITS HYPE/DOGE — orderbook-only first-step; widen later from observed
# spread distribution. Live signal: raw_prob + conservative per-asset
# MARKET_BLEND_W (DOGE 0.60, HYPE 0.80 from B.1 sweep on n=1710/1469 shadow
# samples; +7.0%/+4.4% Brier improvement over coinflip on replay corpus per
# P2.3.b-fu2). Lock-step tests: tests/contracts/test_p2_3_live_promotion_constants.py +
# tests/integration/test_doge_hype_onboarding_t1.py::TestAtomicActivationSafety
# (both flipped atomically with this flag). cal_mlp retrain from live
# evaluated_opportunities deferred 2-4 weeks post-promote (separate Bit).
HYPE_15M_SHADOW = False           # HYPE 15M live (T4 promoted 2026-05-14, floor 90c, max_risk 0.10)
DOGE_15M_SHADOW = False           # DOGE 15M live (T4 promoted 2026-05-14, floor 85c, max_risk 0.10)
# T1 onboarding (2026-05-17, ticket 86b9zmj0c — umbrella 86b9zmhyk): BNB 15M shadow.
# T4 LIVE PROMOTION (2026-05-19, ticket 86b9zmj37 / P2.4 — sibling to P2.3 HYPE/DOGE
# 86b9xv66a, 2026-05-14). 5 T4 prereqs wired: BNB_MIN_ENTRY_PRICE=90 (per-tier WR
# analysis n=272 at 90c+ 100% WR; 85-89c is sub-fee EV at +0.08c naive),
# BNB_MAX_RISK_PER_TRADE=0.10 (HYPE/DOGE conservative-new-asset precedent),
# TM_ASSET_RISK_CAPS["BNB"] (mechanical mirror), MARKET_BLEND_W_BY_ASSET["BNB"]=0.20
# (B.1-equivalent Brier sweep argmin at n=721 — matches ETH pattern; raw model beats
# market by ~10% Brier), NBBO_FALLBACK_GATES["BNB"]=(90, 99, 300.0) (analog default
# mirror of ETH; post-T4 refinement follow-up when NBBO-fallback observations accumulate).
# MIN_ENTRY_PRICE + MAX_RISK_PER_TRADE constants wired into the elif chains
# (scanner per-asset floor + 15M sizer cap + DC asset cap; executor mirrors at
# escalation floor ~:2097 + maker floor ~:3197 + sub-floor-fill telemetry map
# ~:4638 — same shape as HYPE/DOGE P2.3 lockstep). Kill-switch clauses
# preserved at TM/WKND/OVN/DC strategy eligibility sites in bot/scanner/__init__.py —
# flipping BNB_15M_SHADOW=True reverts to shadow in lock-step. Hourly stays in
# HOURLY_EXCLUDED_ASSETS (15M-only promotion; matches HYPE/DOGE P2.3 post-T4
# state). Plan: kb/decisions/p2-4-bnb-live-promotion-plan.md. Regression lock:
# tests/integration/test_bnb_onboarding_t1.py.
BNB_15M_SHADOW = False            # BNB 15M LIVE (P2.4 promotion 2026-05-19); flip to True for kill-switch revert

# ── Trading mode: modular global + per-asset live/shadow control ──────────────
# Single source of truth for "should this asset place REAL orders right now?".
# Adds a modular gate ALONGSIDE (defense-in-depth with) the still-present
# scattered inline `_15M_SHADOW` checks (~6 candidate-append sites,
# 4 assets hardcoded) with ONE gate consulted at the order chokepoints
# (`bot/executor.py::execute` + `bot/kalshi_client.py::place_order`) via
# `bot/trading_mode.py::is_live`. Read live → flipping a flag is a runtime
# kill-switch (no restart). SHIPPED OFF per operator directive 2026-05-30:
# revert EVERYTHING to shadow after a verified ~80% account drawdown on a
# structurally-losing strategy (settlement-convergence edge hunt: high-tail
# favorite-buying is -3.2c/contract; lifetime fees $651 > +$379 gross). To put an
# asset back live you must flip BOTH GLOBAL_LIVE_TRADING=True AND
# ASSET_LIVE_TRADING["<ASSET>"]=True (double fail-safe) — and only after a
# strategy clears the adversarial gate. Pinned by tests/unit/test_trading_mode.py.
GLOBAL_LIVE_TRADING = False        # master kill — False = entire bot shadow (no real orders)
ASSET_LIVE_TRADING_DEFAULT = False  # unknown/unlisted asset -> shadow (fail-safe)
ASSET_LIVE_TRADING = {              # per-asset live enable (ALL 9 crypto 15M series, explicit)
    "BTC": False, "ETH": False, "SOL": False, "XRP": False,
    "HYPE": False, "DOGE": False, "BNB": False, "ADA": False, "BCH": False,
}

# ── Longshot premium-harvest maker strategy (Bit L-1, 2026-06-11) ─────────────
# Validated via scripts/research/genhunt/02b_longshot_fillable_validation.py:
# fillable-only +4.58c/ct, day-bootstrap CI [+2.82, +6.26], 12/12 days positive,
# all 6 assets positive, conditioning gap +4.32c, 10s-cancel pickoff -0.16c.
# Plan: kb/decisions/longshot-twap-live-small-plan.md. Mechanics: for each open
# 15M crypto window at STC 180..720s, for each side whose executable ask is in
# 4-15c, if p_normal(side) <= ask * LONGSHOT_EDGE_RATIO we SELL that side as a
# maker (post the opposite side's bid at 100-ask) and hold to settlement.
# Live/shadow control stays with the trading_mode gate at executor.execute()
# (single chokepoint — never duplicated here). Engine: bot/longshot.py.
# Regression lock: tests/integration/test_longshot_strategy.py.
LONGSHOT_ENABLED = True            # LIVE since 2026-06-12 (operator go-live, $400 deposit; was default OFF)
LONGSHOT_MIN_ASK_CENTS = 4         # sold-side executable ask band lower edge (validated 4-15c)
LONGSHOT_MAX_ASK_CENTS = 15        # sold-side executable ask band upper edge
LONGSHOT_MIN_STC_SECONDS = 180.0   # T-3min: stop quoting / cancel resting below this STC
LONGSHOT_MAX_STC_SECONDS = 720.0   # T-12min: earliest entry
LONGSHOT_EDGE_RATIO = 0.5          # condition: p_normal <= ask * ratio (ask in prob units, i.e. ask_cents/100)
LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE = 3   # live-small sizing (plan doc, $400-500 bankroll)
LONGSHOT_MAX_CONCURRENT_COLLATERAL_DOLLARS = 150.0  # resting quotes + open longshot positions
LONGSHOT_CLIENT_OID_PREFIX = "ls-"  # client_order_id prefix on every longshot maker: boot orphan reconciliation + per-strategy live-gate recognition (R1-M1/M4)
LONGSHOT_LIVE_OVERRIDE = False     # PAUSED 2026-06-12 ~12:05Z (operator): live loss rate 4/11 windows (36%) vs ~6% backtest, p~0.3-3% — adverse-selection signature; autopsy in flight. Engine stays ENABLED in shadow (free would-be-fill measurement). Was LIVE 10:51-12:05Z.
# Longshot live universe (R4-M1 mechanism): the ONLY assets longshot may ever
# trade live — gates the WHOLE strategy branch in trading_mode.strategy_is_live
# (override leg AND any future GLOBAL+asset dual-live flip). Evidence = the 02b
# validation run (scripts/research/genhunt/02b_longshot_fillable_validation.py,
# committed in this branch): its "all 6 assets positive" headline covers
# the 6 pre-directive assets (all below except BNB). BNB is EXCLUDED from the 02b UNIVERSE tuple by construction
# ("BNB excluded (no replayable spot source — same honest subset as #02)", per
# the script's pre-registration docstring), so longshot has ZERO evidence on
# BNB. ADA/BCH sat in the 02b UNIVERSE (Coinbase spot replays exist) but
# contributed ZERO windows — no KXADA15M/KXBCH15M markets existed in the
# 2026-05-30..06-10 corpus (GENHUNT report: "ADA/BCH listing day-zero ...
# waiting on markets that don't exist yet") — AND they carry the T1
# zero-live-orders shadow designation (ADA_15M_SHADOW/BCH_15M_SHADOW=True,
# 2026-05-30): excluded on BOTH grounds.
# OPERATOR DIRECTIVE (2026-06-12, go-live scoping): "when we do a go live it
# should be everything available" — BNB is INCLUDED below despite the 02b
# evidence gap (the gap is a corpus artifact: no replayable spot source in
# the research corpus; the LIVE engine computes p_normal from the bot's own
# feeds, which cover BNB — it trades live in the main pipeline). Risk is
# bounded by the live-small rails (3ct/window, $150 collateral, combined
# $20/day cap); per-asset evidence accrues from the live evaluation. ADA/BCH
# remain excluded: their Kalshi 15M series do not exist yet (zero corpus
# windows) — add when listed, with the directive standing.
LONGSHOT_LIVE_ASSETS = frozenset(
    {"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"})
# Frozen/unmeasured-spot gate (R1-M1 fix round, Bit V.1 — mirror of
# TWAPLOCK_MAX_SPOT_STALENESS_SECONDS below; longshot shipped WITHOUT one).
# 30.0s = the validated backtest's abstention horizon: 02_longshot_tick_floor
# sets STALE_S=30.0 and its decision-point reads (_at / _rv) return None —
# i.e. the backtest ABSTAINED — whenever the spot was >30s event-stale
# (tracked in-repo equivalent: scripts/research/genhunt/
# 02b_longshot_fillable_validation.py::_at/_rv_pure, same STALE_S=30.0 —
# the 02 script itself is not committed).
# Live, CoinbaseFeed's sampler re-stamps a frozen price with fresh
# timestamps every 1s, so only the Bit-S.1 event-time staleness reading
# (state._scan_spot_staleness_cache) can see the freeze; evaluate_market
# emits NO SIGNAL + writes NO eval rows when the reading is missing or
# > this (log: LONGSHOT_SPOT_STALE, info, 60s/asset throttle — BNB gaps
# 34% of 1-min intervals, S.2 RCA). Lockstep with
# bot.helpers.tape_rv.TAPE_RV_MAX_STALENESS_S (pinned by
# tests/contracts/test_tape_rv_estimator_parity.py).
LONGSHOT_MAX_SPOT_STALENESS_SECONDS = 30.0
# Stale-episode quote-down grace (R2-M1 fix round, Bit V.1). When the
# Bit-S.1 event-time reading stays missing/stale past the gate above for
# at least this many wall-clock seconds on a ticker, evaluate_market
# CANCELS that ticker's resting quotes (reason spot_stale; cancel_order is
# intentionally ungated — cancels only reduce exposure) instead of leaving
# them up un-refreshed through the episode (observed up to ~12 min). The
# validated +4.58c economics never priced stale-episode fills — the 02b
# fill model (scripts/research/genhunt/02b_longshot_fillable_validation.py)
# drops them via zscore -> None on a >30s-stale spot at print time — and
# the ONLY measured tolerance for a quote lingering past signal death is
# 02b's 10s cancel-latency arm (LATENCY_S=10.0 at that script's constants
# block; fills_latency10 pickoff -0.16c/ct), so the grace must stay
# <= 10.0. Grace > 0 absorbs flickery staleness (no cancel churn — BNB
# gaps 34% of 1-min intervals, S.2 RCA); a fresh eval clears the per-ticker
# episode clock (bot/longshot.py::_stale_first_seen).
LONGSHOT_STALE_CANCEL_GRACE_SECONDS = 10.0

# ── TWAP-lock endgame taker strategy (Bit T-1, 2026-06-11) ────────────────────
# Validated via scripts/research/genhunt/01b_twap_lock_validation.py:
# +14.4c/ct, day-bootstrap CI [+11.1, +17.7], n=359 over 12 days, 29.9
# locks/day on the honest 4-venue index, print cross-check 99.2%, all 7
# assets positive. Plan: kb/decisions/longshot-twap-live-small-plan.md.
# Mechanics: in the final ~2min of a 15M crypto window Kalshi settles on a
# 60s TWAP of its reference index. Compute the accrued TWAP fraction from
# live Coinbase spot; once the locked side's probability p_lock clears
# TWAPLOCK_P_LOCK_THRESHOLD (remaining variance cannot flip the outcome),
# BUY that side as a TAKER (IOC) if the executable ask leaves
# >= fee + TWAPLOCK_MIN_EDGE_CENTS vs ~100c settlement; hold to settlement.
# Live/shadow control stays with the trading_mode gate at executor.execute()
# (single chokepoint — never duplicated here). Engine: bot/twaplock.py.
# Regression lock: tests/integration/test_twaplock_strategy.py.
TWAPLOCK_ENABLED = True            # LIVE since 2026-06-12 (operator go-live, $400 deposit; was default OFF)
TWAPLOCK_P_LOCK_THRESHOLD = 0.99   # STRICTER than the validated 0.95: Coinbase-anchored MVP index adds proxy error vs the honest 4-venue index; undercounting costs frequency, not correctness (degraded-index lesson)
TWAPLOCK_TWAP_WINDOW_SECONDS = 60.0  # Kalshi settles on a 60s TWAP of its reference index
TWAPLOCK_ENTRY_WINDOW_SECONDS = 90.0  # only act in the final 90s — the validated decision grid starts at DEC_FROM=90 (01b_twap_lock_validation.py); no backtest evidence for (90, 120], so we don't trade it (R1-MN1)
TWAPLOCK_MAX_CONTRACTS_PER_ENTRY = 2   # live-small sizing (plan doc: 1-2 ct/entry)
# One entry per window per asset is STRUCTURAL, not a knob: the engine's
# in-memory latch is binary and ANY tw- pending_orders row on the ticker
# consumes the shot. The former TWAPLOCK_MAX_ENTRIES_PER_WINDOW constant
# was RETIRED at R1-MN4 (a value other than 1 could never be honored);
# pinned-absent by tests/integration/test_twaplock_strategy.py.
TWAPLOCK_MIN_EDGE_CENTS = 3        # executable ask must be <= 100 - taker_fee(1ct) - this margin
# Frozen-spot false-lock gate (R2-MN1): a frozen Coinbase WS price keeps
# feeding the engine's ring buffer with FRESH receive timestamps, so the
# accrued TWAP freezes at a stale price and p_lock can clear the threshold
# spuriously (the absent-sample -> None layer in bot/twaplock.py::
# _accrued_mean does NOT catch this — samples keep arriving, they're just
# stale). The scanner's per-asset WS staleness reading (Bit S.1 cache,
# bot/state.py _scan_spot_staleness_cache) must exist and be <= this many
# seconds or the engine emits NO SIGNAL (log: TWAPLOCK_SPOT_STALE — info,
# not warning: it fires routinely on thin assets). The S.2 RCA pre-flight
# (ticket 86ba1wrh7, 2026-05-21) measured Coinbase 1-min candle coverage
# May 9-21 at BNB 65.9% / HYPE 89.5% / DOGE 99.2% (BTC/ETH/SOL/XRP ~100%)
# — so this gate trades frequency on thin assets for signal integrity,
# the same direction as the stricter-than-validated 0.99 p_lock threshold.
TWAPLOCK_MAX_SPOT_STALENESS_SECONDS = 5.0
TWAPLOCK_CLIENT_OID_PREFIX = "tw-"  # client_order_id prefix on every twaplock taker: reconciler carve-outs + per-strategy live-gate recognition (mirrors ls-)
TWAPLOCK_LIVE_OVERRIDE = False     # PAUSED 2026-06-12 12:57Z (operator; PR #164 merge 040fe826): longshot autopsy found blended_rv running 1.4-4x BELOW tape vol on alts — p_lock consumes the SAME input, so "0.99 locked" may be ~0.9. Zero fills while live (3 IOC misses). Re-arm only after vol-engine RCA + honest-vol rewire + shadow soak.
# Twaplock validated live universe (R4-M1): the ONLY assets twaplock may ever
# trade live — gates the WHOLE strategy branch in trading_mode.strategy_is_live
# (override leg AND any future GLOBAL+asset dual-live flip). Mirrors the
# TRACKED tuple in scripts/research/genhunt/01b_twap_lock_validation.py
# (committed in this branch) — the 7 assets the +14.4c/ct "all 7 assets
# positive" verdict covers (BNB on a single-venue Kraken index, flagged in the
# per-asset breakdown but positive). ADA/BCH are NOT in 01b TRACKED (no
# KXADA15M/KXBCH15M markets existed in the 2026-05-30..06-10 corpus — GENHUNT
# report "ADA/BCH listing day-zero") AND carry the T1 zero-live-orders shadow
# designation (ADA_15M_SHADOW/BCH_15M_SHADOW=True, 2026-05-30): excluded on
# BOTH grounds.
TWAPLOCK_LIVE_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"})

# ── Live-small shared risk rails (longshot + twaplock COMBINED; Bit T-1) ──────
# Single source of truth consumed by BOTH engines' disable latches via
# bot/strategy_caps.py (plan-doc requirement: "$20/day cap, both strategies
# combined, realized+marked"; the Bit L-1 per-strategy cap constant
# LONGSHOT_DAILY_LOSS_CAP_DOLLARS was retired into this combined rail).
LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS = 20.0  # combined realized+marked PnL today across ('longshot','twaplock') <= -cap -> same-day auto-disable of BOTH
LIVE_SMALL_CONSECUTIVE_LOSING_DAYS_DISABLE = 3  # N consecutive completed COMBINED losing days -> persistent disable of BOTH
LIVE_SMALL_STREAK_RESET_UTC_DATE = ""  # operator re-enable: combined losing days on/before this UTC date are ignored ("" = never reset)

# Engine-owned client_order_id prefixes — the reconciler carve-outs in
# bot/state.py (_reconcile_orders / cleanup_expired_resting_orders /
# RECONCILE_IMPORT stamping) and trading_mode.strategy_from_client_order_id
# key off this map. Extend it when a new engine-owned strategy lands.
ENGINE_OWNED_OID_PREFIX_TO_STRATEGY = {
    LONGSHOT_CLIENT_OID_PREFIX: "longshot",
    TWAPLOCK_CLIENT_OID_PREFIX: "twaplock",
}
ENGINE_OWNED_CLIENT_OID_PREFIXES = tuple(ENGINE_OWNED_OID_PREFIX_TO_STRATEGY)

# T1 onboarding (2026-05-30, branch ada-bch-15m-shadow-t1): ADA + BCH 15M
# SHADOW observation. Bot subscribes to Coinbase ADA-USD/BCH-USD + Kalshi
# KXADA15M/KXBCH15M, runs the full scan→evaluate pipeline, writes
# filter_stage='ada_shadow'/'bch_shadow' rows to evaluated_opportunities, but
# submits ZERO live orders. Mirrors the SHADOW half of BNB T1 (NOT the T4/P2.4
# live half — no per-asset MIN_ENTRY_PRICE/MAX_RISK constants, no
# MARKET_BLEND_W_BY_ASSET/TM_ASSET_RISK_CAPS/NBBO_FALLBACK_GATES entries; those
# are T3/T4 surface and never reached under shadow=True). Kill-switch clauses
# wired at the TM/WKND/OVN/DC strategy eligibility sites in
# bot/scanner/__init__.py so an asset in shadow cannot route live through any
# strategy. Hourly stays in HOURLY_EXCLUDED_ASSETS (15M-only; ADA has no hourly
# series, BCH's KXBCHD is not subscribed). Plan:
# kb/decisions/ada-bch-15m-shadow-t1-plan.md. Regression lock:
# tests/integration/test_ada_bch_onboarding_t1.py.
ADA_15M_SHADOW = True             # ADA 15M shadow observation (T1 2026-05-30); flip to False at T4 live promotion
BCH_15M_SHADOW = True             # BCH 15M shadow observation (T1 2026-05-30); flip to False at T4 live promotion

# T4 live-promotion per-asset floors (2026-05-14). Data: B.1b post-blend
# edge-gated subset since 2026-05-10. HYPE conservative pick (borderline EV
# even at floor 90 in raw shadow data; maker-fill discount of ~1-3c/trade
# expected to lift to positive). DOGE clean pick (PnL +1.93c/trade at 85+,
# n=445, WR 95.5%). See kb/findings/p2-3-b-live-promotion-price-tier-analysis-may14.md.
HYPE_MIN_ENTRY_PRICE = 90         # cents (data: 90+ WR 94.4% n=250 post-blend; conservative borderline-EV pick)
DOGE_MIN_ENTRY_PRICE = 85         # cents (data: 85+ WR 95.5% n=445 PnL +1.93c/trade; matches SOL floor pattern)
# P2.4 BNB live promotion 2026-05-19 (ticket 86b9zmj37). Per-tier WR analysis on
# evaluated_opportunities n=564 YES-side settled: 85-89c is +0.08c naive (sub-fee EV);
# 90c+ is +7.60c at 100% WR n=272 (exceeds HYPE n=250 precedent). 100% WR is
# directional-regime-conditioned; 14d post-promotion soak monitor in plan doc rollback rule.
# See kb/decisions/p2-4-bnb-live-promotion-plan.md § RCA #1.
BNB_MIN_ENTRY_PRICE = 90          # cents (data: 90+ WR 100% n=272 P2.4 sweep; 85-89c is sub-fee EV)

HYPE_MAX_RISK_PER_TRADE = 0.10    # HYPE: conservative new-asset start (below live 4 at 0.15-0.20)
DOGE_MAX_RISK_PER_TRADE = 0.10    # DOGE: conservative new-asset start
BNB_MAX_RISK_PER_TRADE = 0.10     # BNB: conservative new-asset start (P2.4 2026-05-19, matches HYPE/DOGE precedent)

XRP_SHADOW_MIN_PRICE = 88         # Shadow tier: 88c+ subset (86-87c is 84% WR but PnL-negative)

MIN_SECONDS_BEFORE_CLOSE = 0

MAX_SECONDS_BEFORE_CLOSE = 900    # scan 15 min before close (600-900s is shadow data collection)

STC_SHADOW_THRESHOLD = 600        # 15M trades above this STC are shadow-only

STC_EXTENDED_LIVE_FLOOR = 300     # 300-600s zone: per-asset higher floors apply (model 9pp overconfident at low prices)

STC_EXTENDED_BUFFER_RESCUE = 0.25  # Buffer >= this bypasses extended floor (data: 21/21 100% WR, Wilson LB 88.6% > 87% BE)

SOL_RESCUE_CONTRACT_CAP = 25       # SOL rescue sizing tail cap (Apr 19: 5 losses avg 56ct × 89c = −$50/loss; capped: −$22/loss)

STC_EXTENDED_BTC_MIN_PRICE = 93   # BTC floor for 300-600s (data: 93c+ = 98.1% WR, n=52)

STC_EXTENDED_ETH_MIN_PRICE = 90   # ETH floor for 300-600s (data: 90c+ = 100% WR, n=31; same as main floor)

STC_EXTENDED_SOL_MIN_PRICE = 95   # SOL floor for 300-600s (data: 95c+ = 100% WR, n=14)

STC_EXTENDED_XRP_MIN_PRICE = 92   # XRP floor for 300-600s (data: 92c+ = 100% WR, n=15; same as main floor)

# ─── 96¢ STC Danger-Band Block (SOL/XRP) ─────────────────────────────────
# 30-day forensic on 2026-04-26: YES entries on SOL or XRP at exactly 96¢ in the 2-5min
# STC band lost -$974 across 98 trades (88W/10L). Adjacent cells profitable: BTC/ETH 96¢
# (+$72), SOL/XRP 95¢ (+$199), SOL 97-99¢ (+$217 at 99.5% WR), and SOL/XRP 96¢ outside
# this STC band (0-2min, 5+min). Wilson 95% CI on loss-rate [5.7%, 17.8%] entirely
# exceeds the ~5-6% breakeven loss-rate at 96¢ (24:1 loss/win ratio).
# Underlying cause: calibrator over-confidence on thin-buffer/short-horizon entries
# (see kb/findings/proximity-calibration-miss-eth-2026-04-26.md). ML fix deferred;
# this is the interim config gate.
# DO NOT widen to >=96 — backtester confirmed 97-99¢ band is profitable, blocking it
# costs ~$161/30d in foregone profit.
# Decision doc: kb/decisions/96c-sol-xrp-2to5min-block-2026-04-26.md
# HWM spike-rejection Telegram alert toggle. Muted 2026-05-31 (default OFF):
# bot not trading, balance bounces (56c <-> 30055c) re-trip the 3-consecutive-
# rejection counter and spam the channel. logging.warning still fires regardless
# for journal observability. Set HWM_SPIKE_ALERT_ENABLED=1 on the VPS to restore.
HWM_SPIKE_ALERT_ENABLED = os.environ.get("HWM_SPIKE_ALERT_ENABLED", "0") == "1"

HIGH_PRICE_STC_BLOCK_ENABLED = os.environ.get("HIGH_PRICE_STC_BLOCK_ENABLED", "0") == "1"

HIGH_PRICE_STC_BLOCK_ASSETS = frozenset({"SOL", "XRP"})

HIGH_PRICE_STC_BLOCK_PRICE_CENTS = 96      # exact match — DO NOT widen, see comment above

HIGH_PRICE_STC_BLOCK_STC_LO_S = 121         # inclusive lower bound (seconds_to_close)

HIGH_PRICE_STC_BLOCK_STC_HI_S = 300         # inclusive upper bound (seconds_to_close)

HIGH_PRICE_STC_BLOCK_FILTER_STAGE = "96C_SOL_XRP_STC_DANGER_BAND"

# Strategy-aware: only block bleeder strategies in the cell. Wins (TM-96, TM-untagged,
# TAKER_NOW, MAKER_AGGRESSIVE, decided_t1*, weekend_discount, PANIC_CAPTURE) pass through
# untouched. Saves +$895/30d vs +$753/30d for a crude block-everything gate.
# Bleeder evidence (30d, scan-time strategy field, n / W-L / net PnL):
#   decided_t2_z2:    8 / 6-2 / -$508.74
#   decided_t2_z25:   8 / 6-2 / -$165.20
#   decided_t2:       6 / 5-1 /  -$76.24
#   MAKER_PATIENT:    5 / 3-2 / -$142.56
HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES = frozenset({
    "decided_t2", "decided_t2_z2", "decided_t2_z25",
    "MAKER_PATIENT",
})

# ─── Additional bleed-cell blocks (R-bleed-1, 2026-04-30) ──────────────────
#
# Two cells identified in 7d post-WS-fix data as catastrophic-tail dominators.
# Mirror HIGH_PRICE_STC_BLOCK_* pattern: env-flag controlled, default OFF,
# blocked candidates STILL get a shadow row written so v2/v3 training data
# continues flowing.
#
# Cell 1: TM-98 high-price 2-5min STC bleed
#   {BTC, ETH, XRP} × terminal_momentum_98 × 97-98¢ × 121-300s STC
#   7d data: BTC -$28 / ETH -$154 / XRP -$47 → -$980/30d projected
#   Pattern: high-WR (93-95%) but ONE catastrophic loss per asset (-$50 to
#   -$179) erases dozens of small wins. Same shape across all 3 assets.
#   SOL TM-98 NOT included: -$23/14d, not catastrophic.
#
# Price band 97-98¢ rationale: TM-98 strategy is TRIGGERED when best_ask=98
# (per `f"terminal_momentum_{best_ask}"` at scan-time), but the actual
# ENTRY price can be 97 (maker fill 1c below ask) or 98 (taker/escalated).
# Both fill modes carry the same catastrophic-tail risk. PRICE_HI = 98
# (not 99 — TM-99 is profitable per 14d data, 100% WR).
TM98_HIGHPRICE_BLEED_BLOCK_ENABLED = os.environ.get(
    "TM98_HIGHPRICE_BLEED_BLOCK_ENABLED", "0") == "1"

TM98_HIGHPRICE_BLEED_BLOCK_ASSETS = frozenset({"BTC", "ETH", "XRP"})

TM98_HIGHPRICE_BLEED_BLOCK_PRICE_LO = 97

TM98_HIGHPRICE_BLEED_BLOCK_PRICE_HI = 98

TM98_HIGHPRICE_BLEED_BLOCK_STC_LO_S = 121

TM98_HIGHPRICE_BLEED_BLOCK_STC_HI_S = 300

TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE = "TM98_97_98C_2_5MIN_BLEED"

TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES = frozenset({"terminal_momentum_98"})

# Cell 2: SOL TAKER low-price 2-5min STC bleed
#   SOL × TAKER_NOW × 85-89¢ × 121-300s STC
#   7d: -$182, worst single -$213. Projects -$782/30d.
#   Pattern: SOL near-asset-floor IOC fills with thin buffer; one bad
#   downward move while bot is sized 200+ contracts wipes 17 small wins.
SOL_TAKER_LOWPRICE_BLEED_BLOCK_ENABLED = os.environ.get(
    "SOL_TAKER_LOWPRICE_BLEED_BLOCK_ENABLED", "0") == "1"

SOL_TAKER_LOWPRICE_BLEED_BLOCK_ASSETS = frozenset({"SOL"})

SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_LO = 85

SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_HI = 89

SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_LO_S = 121

SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_HI_S = 300

SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE = "SOL_TAKER_85_89C_2_5MIN_BLEED"

SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES = frozenset({"TAKER_NOW"})

# Cell 3: SOL_BLEED_V2 — supersedes SOL_TAKER_LOWPRICE (May 10, 2026)
#   SOL × {TAKER_NOW, MAKER_PATIENT} × 88-93¢ × 121-300s STC
#
# RCA (kb/findings/sol-bleed-v2-rca-may10.md): the v1 SOL_TAKER_LOWPRICE
# gate is netting -$102/30d (40 blocks counterfactual +$102 — productive
# cell) AND missed three catastrophic May-6→May-10 losses totalling -$338.
# Two defects in the v1 gate: (a) strategy filter `{TAKER_NOW}` is too
# narrow because bot/executor.py force-routes EVERY SOL candidate through
# `sol_taker_override` regardless of scan-time strategy label — MAKER_PATIENT
# becomes taker at execution, slipping past the v1 strategy filter;
# (b) price band 85-89¢ is too low for the post-v2 (May 5 cross-asset
# deploy) regime — the bleed cell drifted up to 90-92¢ where a single
# -$176 today (5/10 KXSOL101615 MAKER_PATIENT 89→90¢ STC 299.8s) and
# -$128 yesterday (5/9 KXSOL091145 TAKER_NOW 92¢ STC 292.5s) settled NO.
#
# Strategy filter `{TAKER_NOW, MAKER_PATIENT}` is intentionally tight:
# MAKER_AGGRESSIVE was +$106 pre-v2 (n=14) in the same 88-93¢ × 2-5min cell;
# weekend_discount/overnight_discount/decided_t1/t2 are all net positive.
# Blocking those would kill productive trades and net-cost us money.
#
# Default OFF — operator flips on VPS post-ship. The v1 gate
# (SOL_TAKER_LOWPRICE_BLEED_BLOCK_ENABLED) is intentionally left in
# place for rollback; ops will flip its env to 0 on deploy.
SOL_BLEED_V2_BLOCK_ENABLED = os.environ.get(
    "SOL_BLEED_V2_BLOCK_ENABLED", "0") == "1"

SOL_BLEED_V2_BLOCK_ASSETS = frozenset({"SOL"})

SOL_BLEED_V2_BLOCK_PRICE_LO = 88

SOL_BLEED_V2_BLOCK_PRICE_HI = 93

SOL_BLEED_V2_BLOCK_STC_LO_S = 121

SOL_BLEED_V2_BLOCK_STC_HI_S = 300

SOL_BLEED_V2_BLOCK_FILTER_STAGE = "SOL_BLEED_V2_88_93C_2_5MIN"

SOL_BLEED_V2_BLOCK_STRATEGIES = frozenset({"TAKER_NOW", "MAKER_PATIENT"})

# ─── Binance feed kill-switch ──────────────────────────────────────────────
# US-VPS deploys are geoblocked from Binance.com WebSocket (HTTP 451). The
# feed reconnects every ~70s in a tight loop forever, adding event-loop noise
# and log spam. Coinbase + Kraken still feed BTC; Binance was tertiary.
# Default OFF — operator must opt in if running outside the US.
BINANCE_FEED_ENABLED = os.environ.get("BINANCE_FEED_ENABLED", "0") == "1"

ONE_ASSET_PER_WINDOW = False

# ─── Hourly Live Trading (sub-60c, BTC+ETH only) ────────────────────────────
HOURLY_OBSERVATION_ENABLED = True     # Master switch for hourly data collection

HOURLY_LIVE_ENABLED = os.environ.get("HOURLY_LIVE_ENABLED", "0") == "1"  # Kill switch: must be set on VPS

HOURLY_OBSERVATION_ONLY = not HOURLY_LIVE_ENABLED  # Derived from kill switch

HOURLY_SERIES_TICKERS = {
    "BTC": "KXBTCD",
    "ETH": "KXETHD",
    "SOL": "KXSOLD",
    "XRP": "KXXRPD",
    "HYPE": "KXHYPED",        # T1 (2026-05-10): shadow via HOURLY_EXCLUDED_ASSETS
    "DOGE": "KXDOGED",        # T1 (2026-05-10): shadow via HOURLY_EXCLUDED_ASSETS
    "BNB": "KXBNBD",          # T1 (2026-05-17, 86b9zmj0c): shadow via HOURLY_EXCLUDED_ASSETS
    # ADA hourly (KXADAD) is NOT live on Kalshi as of 2026-05-30 — entry kept
    # for the cross-registry invariant (every ASSET ∈ all 3 registries). The
    # hourly REST lookup returns 0 markets until/if Kalshi launches it; ADA is
    # in HOURLY_EXCLUDED_ASSETS so it stays shadow-only even then.
    "ADA": "KXADAD",          # T1 (2026-05-30): inert until Kalshi launches; shadow via HOURLY_EXCLUDED_ASSETS
    "BCH": "KXBCHD",          # T1 (2026-05-30): KXBCHD live; not subscribed (15M-only), shadow via HOURLY_EXCLUDED_ASSETS
}

HOURLY_MAX_SECONDS_BEFORE_CLOSE = 1800  # 30 min before close

HOURLY_MIN_SECONDS_BEFORE_CLOSE = 0

HOURLY_MARKET_BLEND_W = 0.40            # Optimal Brier per 134K simulation (0.70 was second-worst)

HOURLY_MIN_ENTRY_PRICE = 50            # Floor for data collection

HOURLY_MAX_ENTRY_PRICE = 59            # Sub-60c only — edge lives at low prices, 70-79c is death zone

HOURLY_MAX_RISK_PER_TRADE = 0.15       # Conservative start (60% of 15M's 0.25)

HOURLY_BANKROLL_FRACTION = 0.10        # Hourly sizes off 10% of balance (like SPX's 0.15)

HOURLY_FIXED_CONTRACTS = 25            # Fixed sizing — bypass Kelly entirely (raised from 10)

HOURLY_MAX_EDGE = 0.05                 # Reject >5% edge (10%+ zone has 24.2% WR — edge inversion)

HOURLY_TAKER_ONLY = True               # IOC only — no maker orders, no per-asset lock contention with 15M

# ─── Hourly NO-Side Verification ──────────────────────────────────────────
# Data: model-flagged BTC NO at 40-54c has 53.9% WR (n=1,113, z=2.61, p=0.005).
# Model adds value: rejected NO at same prices has 47.0% WR (loses money).
# Time-split stable: both halves 54.8% WR. Multi-asset: BTC/ETH/SOL/XRP all positive.
# Structural thesis: crypto long bias overprices YES, underprices NO.
# Flat 1-contract verification to measure fill rates and live edge persistence.
HOURLY_NO_SIDE_LIVE = os.environ.get("HOURLY_NO_SIDE_LIVE", "0") == "1"

HOURLY_NO_MIN_PRICE = 40               # Minimum NO entry price (cents)

HOURLY_NO_MAX_PRICE = 54               # Maximum NO entry price (cents)

HOURLY_NO_FIXED_CONTRACTS = 1          # Flat 1-contract — verification mode

HOURLY_NO_KILL_THRESHOLD = -2000       # Auto-disable if cumulative NET NO PnL < -$20 (R-p7-deploy-r9 fee-fix changed comparison from gross to net; kill now fires marginally sooner under fee burden — safer)

# ─── Hourly Decided Contracts (separate from sub-60c, separate kill switch) ──
# Same DC thesis as 15M but on hourly BTC tickers. Conservative: z≤-4, 93-96c,
# BTC only, sigma gate blocks cold-RK false signals, 1 trade per window.
# Data: 97.9% WR on 94 shadow signals at z≤-3 (7 days). We use z≤-4 for safety.
HOURLY_DC_ENABLED = os.environ.get("HOURLY_DC_ENABLED", "1") == "1"

HOURLY_DC_Z_THRESHOLD = -4.0           # Stricter than 15M T2 (z≤-3)

HOURLY_DC_MIN_PRICE = 93               # Same as 15M DC floor

HOURLY_DC_MAX_PRICE = 96               # Conservative ceiling (97c+ has 6.5% loss rate)

HOURLY_DC_ASSUMED_PROB = 0.97          # Same as 15M T2

HOURLY_DC_CONTRACTS = 25               # Fixed sizing

HOURLY_DC_MIN_SIGMA = 0.000250         # Blocks cold-RK false signals

HOURLY_DC_ASSETS = {"BTC"}             # BTC only (SOL had legitimate loss)

HOURLY_DC_MAX_PER_WINDOW = 1           # Single best strike per window

# ─── SPX Decided Contracts Shadow (1-week validation before promotion) ────
# 112/112 Mon-Wed at z≤-3 93-96c. All 6 losses on Thu-Fri. Sigma=0 on 97%
# of signals (SPX EGARCH broken) but z-score still works via spot-vs-strike.
# Shadow-only until: (a) 1 week of forward data confirms WR, (b) EGARCH fixed.
SPX_DC_SHADOW_ENABLED = True

SPX_DC_Z_THRESHOLD = -3.0

SPX_DC_MIN_PRICE = 93

SPX_DC_MAX_PRICE = 96

SPX_DC_VALID_DAYS = {0, 1, 2}  # Mon=0, Tue=1, Wed=2 (Python weekday())

# ─── Hourly Three-Layer Optimization (Researcher Recommendations) ─────────
HOURLY_TEMPERATURE_T = 1.45           # Temperature scaling: softens overconfident probs (T>1 = less confident)

HOURLY_TEMPERATURE_ENABLED = True     # Toggle for temperature scaling

HOURLY_CALIBRATION_ENABLED = False    # Disabled: hourly beta_cal is +44pp overconfident (93.2% predicted vs 49.2% actual, n=455). Passthrough+T=1.45 is nearly perfect (-2pp OC).

HOURLY_MIN_STC_ENTRY = 600             # 10 min minimum (5-10m zone is 56.5% WR — too thin)

HOURLY_MAX_STC_ENTRY = 1800            # 30 min maximum (25-30m is the sweet spot at 69.4% WR)

HOURLY_EXCLUDED_ASSETS = {"SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH"}  # YES-side: BTC+ETH only — XRP/SOL data-driven; HYPE/DOGE/BNB hourly excluded per 15M-only promotion design (HYPE/DOGE T4 P2.3 2026-05-14, BNB T4 P2.4 2026-05-19); ADA/BCH hourly excluded per 15M-shadow-only design (T1 2026-05-30 — safety belt: ADA has no hourly series, BCH's KXBCHD is not subscribed)

# NO-side asymmetry (Apr 15 data, model-flagged hourly candidates in 40-54c range):
#   BTC NO: 51.5% WR @ 47.1c avg (+3.9pp vs BE, model adds +7.3pp, n=1041)
#   ETH NO: 54.7% WR @ 48.4c avg (+5.6pp vs BE, model adds +11.5pp, n=137)
#   SOL NO: 62.2% WR @ 49.2c avg (+13.0pp vs BE, model adds +17.8pp, n=37)
#   XRP NO: 53.1% WR @ 50.7c avg (+2.4pp vs BE, model adds +1.7pp, n=32)
# All four assets show positive model-filtered edge on NO-side — the YES-side toxicity
# (XRP 42.2% YES WR) is precisely the asymmetry that creates NO-side edge. Structural
# thesis: crypto long bias overprices YES → NO underpriced. -$20 kill switch bounds
# downside. Revisit per-asset if fills produce divergent live PnL.
HOURLY_NO_EXCLUDED_ASSETS = {"HYPE", "DOGE", "BNB", "ADA", "BCH"}  # NO-side safety belt: HYPE/DOGE/BNB hourly excluded per 15M-only promotion design (HYPE/DOGE T4 P2.3 2026-05-14, BNB T4 P2.4 2026-05-19; hourly path not yet promoted); ADA/BCH per 15M-shadow-only T1 2026-05-30. Existing BTC/ETH/SOL/XRP unblocked per data above.

HOURLY_MAX_POSITIONS_PER_WINDOW = 2   # Max concurrent hourly positions per time window (ENB ~1.3)

# ─── Hourly Config A (shadow promotion candidate) ────────────────────────────
# Filters applied as a SECOND insert (filter_stage='hourly_config_a') alongside
# the unfiltered baseline ('hourly_observation'). Does NOT affect live trading.
# Graduation criteria (all must hold for 7+ days post-filter):
#   - WR ≥ 78%
#   - Wilson 95% CI lower bound ≥ 72%
#   - Brier < 0.25
#   - No single day with WR < 60%
#   - Flat sim PnL positive
HOURLY_CONFIG_A_EXCLUDED = {'XRP'}    # XRP: 42% WR, -$89 sim PnL, 12-33pp below non-XRP every UTC bucket

HOURLY_CONFIG_A_MAX_EDGE = 0.007      # Edge ≤ 0.7%: filters out overconfident high-edge noise (8-15% edge = 32% WR)

HOURLY_MAX_WINDOW_RISK = 0.15         # Max aggregate risk across all hourly positions per window

# ─── BTC 70-89c wl2 Variant (promotion candidate) ───────────────────────────
# Backtest: 67t, 62W/5L, 92.5% WR, flat $7.47, Kelly $92.48, Brier 0.076
# 70-89c tier has 11pp margin over breakeven vs 2.7pp for 86c+
# Graduation: n>=100, WR>=88%, positive PnL, Wilson CI lower >= 3pp above breakeven
HOURLY_CONFIG_B_ASSET = 'BTC'

HOURLY_CONFIG_B_MIN_PRICE = 70

HOURLY_CONFIG_B_MAX_PRICE = 89

HOURLY_CONFIG_B_MAX_PER_WINDOW = 2    # Price-sorted: top 2 by price within window

# ─── Hourly Configs C–G (shadow promotion candidates, Mar 12 2026) ──────────
# Five diverse configs from hourly alpha research. All shadow-only —
# insert as filter_stage='hourly_config_X' alongside the unfiltered baseline.
# Graduation: WR≥72%, Wilson LB≥65%, Brier<0.30, 7+ days, PnL positive.
HOURLY_SHADOW_CONFIGS = [
    {"name": "hourly_config_c", "included_assets": {"BTC", "ETH"}, "min_stc": 600, "max_stc": 1800},
    {"name": "hourly_config_d", "excluded_assets": {"XRP"}, "max_edge": 0.05},
    {"name": "hourly_config_e", "included_assets": {"BTC", "ETH"}, "min_stc": 1200, "max_stc": 1800},
    {"name": "hourly_config_f", "max_edge": 0.012},
    {"name": "hourly_config_g", "included_assets": {"BTC"}, "min_stc": 900, "max_stc": 1800},
    # Killed configs h, j, k — 55% WR, deeply negative PnL, wasting DB writes
    {"name": "hourly_config_i", "included_assets": {"BTC", "ETH"}, "min_stc": 600, "max_stc": 1800, "temperature": 2.0, "blend_w": 0.0},
    {"name": "hourly_config_l", "excluded_assets": {"XRP"}, "temperature": 2.0, "blend_w": 0.0},
    {"name": "hourly_config_m", "included_assets": {"BTC", "ETH"}, "min_stc": 600, "max_stc": 1800, "temperature": 2.5, "blend_w": 0.0},
]

HOURLY_KELLY_FRACTION = 0.25          # Quarter-Kelly: 44% of growth rate, ~3% halving probability

# ─── SPX Hourly — LIVE TRADING ────────────────────────────────────────────────
# Promoted Mar 17 2026: SPX-D CalEngine (post_temp), 472 settled at 85.2% WR,
# Brier 0.138, forward confirmed 45 signals at 86.7% WR / 0.147 Brier.
SPX_HOURLY_ENABLED = True

SPX_HOURLY_OBSERVATION_ONLY = True       # Reverted — Polygon 403 breaks vol engine

SPX_HOURLY_MIN_ENTRY_PRICE = 90          # 90c+ floor (SPX-C: 90.9% WR at 90c+)

SPX_HOURLY_MAX_ENTRY_PRICE = 99

SPX_HOURLY_MAX_SECONDS_BEFORE_CLOSE = 1800

SPX_HOURLY_MIN_SECONDS_BEFORE_CLOSE = 300

SPX_HOURLY_MARKET_BLEND_W = 0.00         # No blend — CalEngine calibration only (SPX-D)

SPX_HOURLY_MAX_RISK_PER_TRADE = 0.10     # Conservative (down from 0.15)

SPX_HOURLY_TEMPERATURE_T = 1.0           # CalEngine handles temperature internally

SPX_HOURLY_KELLY_FRACTION = 0.125        # Eighth-Kelly: ultra-conservative for new live system

SPX_HOURLY_FEE_MULTIPLIER_TAKER = 0.035  # Finance category: half of crypto's 0.07

SPX_HOURLY_FEE_MULTIPLIER_MAKER = 0.0  # Kalshi charges $0 on maker fills

SPX_HOURLY_MAX_POSITIONS_PER_WINDOW = 2  # Max concurrent SPX positions per hourly window

SPX_HOURLY_MAX_WINDOW_RISK = 0.15        # Max aggregate risk across SPX positions per window

SPX_HOURLY_BANKROLL_FRACTION = 0.15      # SPX sizes off 15% of total balance — crypto unaffected

# ─── Weather Observation Mode ─────────────────────────────────────────────────
WEATHER_ENABLED = True

WEATHER_OBSERVATION_ONLY = True

WEATHER_MIN_ENTRY_PRICE = 10

WEATHER_MAX_ENTRY_PRICE = 99

WEATHER_MAX_SECONDS_BEFORE_CLOSE = 86400  # Weather settles daily — always eligible

WEATHER_MIN_SECONDS_BEFORE_CLOSE = 3600   # At least 1 hour before settlement

WEATHER_MIN_STC_ENTRY = 3600.0           # 1h min for shadow trade signals

WEATHER_MAX_STC_ENTRY = 43200.0          # 12h max — audit: 4-12h calibrated, 12h+ catastrophic

WEATHER_MAX_RISK_PER_TRADE = 0.10

WEATHER_KELLY_FRACTION = 0.25

WEATHER_MARKET_BLEND_W = 0.20            # 80% model, 20% market (ensemble is primary signal)

WEATHER_MIN_EDGE_PCT = 0.001             # 0.1% — very low for max signal collection (observation-only)

WEATHER_CAL_ENGINE_ENABLED = True        # Per-city CalEngines learning in shadow

# ─── Weather Shadow Variants (Mar 12 2026) ──────────────────────────────────
# Two focused shadow configs alongside the uncapped baseline (weather_observation).
# Research: model well-calibrated <25% predicted (≤30c), catastrophically overconfident >40%.
#   - Capped30: price ≤30c — restricts to calibrated regime (+0.8pp to +4.3pp gap)
#   - ShortSTC: STC ≤8h — ensemble freshest, 46.7% WR vs 18.4% for 16-24h
# Both insert as filter_stage='weather_shadow_X' alongside uncapped baseline.
# Graduation: WR above breakeven, Wilson CI lower > BE, 30+ days, PnL positive.
WEATHER_SHADOW_CONFIGS = [
    {"name": "weather_shadow_capped30", "max_price": 30},
    {"name": "weather_shadow_short_stc", "max_stc": 28800},  # 8 hours
    {"name": "weather_shadow_capped30_short_stc", "max_price": 30, "max_stc": 28800},  # both filters
]

# Weather NO-side shadow: model overconfident on YES (+25.5pp at 75-90% bucket) → strong NO signal.
# Signal fires when YES prob ≥ 55% (cheap NO contracts) and NO edge after fees is positive.
# Fixed 1-contract sizing (Kelly oversizes on low-edge NO signals).
WEATHER_NO_SHADOW_MIN_YES_PROB = 0.55  # Only shadow when model is confident YES (NO is cheap)

# Weather NO-side live execution — bypasses WEATHER_OBSERVATION_ONLY for NO-side only.
# YES-side remains fully gated by WEATHER_OBSERVATION_ONLY = True.
# Data: 397 settled, 73.6% WR, +$181 sim PnL, 40pp+ cushion above breakeven.
# Gate: STC >= 8h (short STC NO loses), fixed 1-contract sizing, all 19 cities.
WEATHER_NO_SIDE_LIVE = False             # KILLED 2026-05-16: lifetime n=167, 38.3% WR vs 70% assumed prior (Wilson 95% CI [23.6%, 47.0%], far below 70%). Near-ATM zone (NO 39-40c ↔ YES 60-61c) is market-maker zone with no edge. Bracket_no_live (far-ITM NO 4-12c, 91.7% WR) is where NO edge actually lives. See ClickUp Weather Initiative folder (90149436180) for first-principles re-research plan.

WEATHER_NO_SIDE_MIN_STC = 57600.0        # 16 hours — tightened from 8h (data: 77.1% WR at 16-24h, 32.5% at 0-8h)

WEATHER_NO_MIN_PRICE = 39                # Tightened from 37 May 2: 37c=25%WR -$1.44 (n=12), 38c=14.3%WR -$1.66 (n=7) bleeding harder than the 36c tier. 39-40c band profitable (39c +$1.54 n=14, 40c +$6.20 n=47).

WEATHER_NO_MAX_PRICE = 40                # Only buy NO contracts priced ≤ 40c (YES ≥ 60c)

WEATHER_NO_ASSUMED_PROB = 0.70           # Bypass model (structurally wrong on NO). Shadow: 79.7% WR, worst week 74%

WEATHER_NO_KILL_THRESHOLD = -2000        # Auto-disable if cumulative NET NO PnL < -$20 (R-p7-deploy-r9 fee-fix changed comparison from gross to net; safer)

WEATHER_NO_CONTRACT_COUNT = 2            # Sized 1→2 May 4: 39c+ ex-LAS n=56, 57.1% WR, Wilson 95% LB 44.1% > 40c BE; PF 2.02; max DD historical $1.57 → ~$3.14 at 2x.

WEATHER_NO_EXCLUDED_CITY_PREFIXES = frozenset({"KXHIGHTLV"})  # LAS bleeds within 39c+ band: -$1.19 on 8 trades, 25% WR (May 4). Kalshi event_ticker family for Las Vegas high-temp markets.

# ─── Weather Bracket NO-Side ────────────────────────────────────────────
# Brackets at YES 88-96c settle NO 91.7% of the time (157 single-strike, Wilson CI 86.3-95.1%).
# Breakeven is only 4-12%. Mechanism: narrow 5°F brackets overprice YES because 2-3°F forecast
# misses in either direction push actual temp outside the bracket.
BRACKET_NO_ENABLED = os.environ.get("BRACKET_NO_ENABLED", "1") == "1"

BRACKET_NO_YES_MIN = 88                    # Min YES price to trigger

BRACKET_NO_YES_MAX = 96                    # Max YES price — 97-99c excluded (dead zone: 47.8% NO rate)

BRACKET_NO_FIXED_CONTRACTS = 5             # Fixed sizing — start small, verify execution, scale to 25 later

BRACKET_NO_ASSUMED_PROB = 0.92             # NO probability (91.7% actual, conservative)

BRACKET_NO_MIN_STC = 28800                 # 8 hours minimum STC (data: 84.8% NO at 8-16h, 92.6% at 16h+)

BRACKET_NO_MAX_CONCURRENT = 6             # Max simultaneous bracket NO positions

BRACKET_NO_KILL_THRESHOLD = -2000          # -$20 cumulative NET PnL kill switch (R-p7-deploy-r9 fee-fix changed comparison from gross to net; safer)

# ─── Stacking Infrastructure ────────────────────────────────────────────
STACKING_ENABLED = os.environ.get("STACKING_ENABLED", "0") == "1"

MAX_TICKER_RISK = 0.25    # 25% of balance per ticker across all strategies (was 20%)

MAX_WINDOW_RISK = 0.30    # 30% of balance per settlement window, CROSS-ASSET (was 25%)

HOURLY_MIN_EDGE_PCT = 0.001              # 0.1% — low for max signal collection (observation-only)

# ─── Sports Comeback Observation Mode ────────────────────────────────────
SPORTS_ENABLED = True

SPORTS_OBSERVATION_ONLY = True         # HARDCODED — never live without explicit promotion

# ─── API Configuration ───────────────────────────────────────────────────────
BASE_URL = ("https://api.elections.kalshi.com" if os.environ.get("KALSHI_ENV") == "production"
            else "https://demo-api.kalshi.co")

API_PATH_PREFIX = "/trade-api/v2"

READ_RATE_LIMIT = 30              # per second (Advanced tier)

WRITE_RATE_LIMIT = 30             # per second (Advanced tier)

# ─── Orderbook depth logging cache ───────────────────────────────────────────
# How fresh a cached ladder must be to auto-fill into evaluated_opportunities.
# Scanner ticks ~every 1-2s per ticker; 10s gives us 5-10 ticks of grace
# before forensic data is suspect. Beyond this we write NULL (honest) instead
# of a stale ladder labeled as "now" (forensic poisoning).
OB_CACHE_FRESHNESS_SECONDS = 10

# Entries older than this are dropped on opportunistic eviction. Bounds
# memory growth from quiet/closed-market tickers — failure_15m_silence_apr24_second
# was a similar "WS cache phantom state" leak.
OB_CACHE_EVICT_AGE_SECONDS = 30

# ─── File Paths ──────────────────────────────────────────────────────────────
DB_PATH = "state.db"

SCAN_JOURNAL = "scan_journal.jsonl"

TRADE_JOURNAL = "trade_journal.jsonl"

SETTLEMENT_JOURNAL = "settlement_journal.jsonl"

ORDER_JOURNAL = "order_journal.jsonl"

REJECTION_JOURNAL = "rejection_journal.jsonl"

OPPORTUNITY_JOURNAL = "opportunity_journal.jsonl"

EXECUTION_JOURNAL = "execution_journal.jsonl"

PERFORMANCE_JOURNAL = "performance_journal.jsonl"

FILL_MODEL_JOURNAL = "fill_model_journal.jsonl"

# ─── Loop Timing ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 1.0

MARKET_REFRESH_SECONDS = 30.0

# Staleness budget for the active_windows cache (Step #5 watchdog).
# Tied to MARKET_REFRESH_SECONDS so the relationship is explicit:
# allow up to 3 consecutive missed refreshes before failing closed.
# 4× refresh interval = 120s tolerates ~90s of Kalshi /events
# unavailability (refresh worker call blocks, then completes, timestamp
# updates) without nuking scan, while still detecting a silently-dead
# worker in ~2 minutes — well under "indefinite stale". A tighter
# budget (e.g., 60s = 2×) caused false-trips when a single REST call
# hit Kalshi's 30-50s timeout.
ACTIVE_WINDOWS_STALENESS_BUDGET_S = MARKET_REFRESH_SECONDS * 4

SETTLEMENT_CHECK_SECONDS = 30.0

# B4 (ticket 86b9zudcc, 2026-05-18): defense-in-depth threshold for the
# settlement Telegram alert's WIN-side balance-delta cross-check. When
# the cash delta observed across `client.get_balance()` pre/post the
# settlement diverges from the locally-expected credit (= `aggregate_count
# × 100¢` on WIN, `0` on LOSS — only `revenue` actually moves cash at
# settle time; cost/fee were debited at fill) by more than this many
# cents, SettlementTracker._process_settlement logs a
# SETTLEMENT_PNL_DIVERGENCE WARNING and appends a ⚠️ KALSHI_DELTA tag
# to the alert. 50¢ tolerates per-row rounding while still catching the
# HYPE-scale ($114) overcount class (B1). LOSS-side phantoms are
# structurally invisible to this surface (cash moves $0 at LOSS settle)
# and are caught by `scripts/audit/phantom_pnl_audit.py` instead.
SETTLEMENT_PNL_DIVERGENCE_THRESHOLD_CENTS = 50

# ─── WS Cache Reconciliation (Phase 2 of silent-scan fix, Apr 25 2026) ──
# WS orderbook cache accumulates phantom state over time (5-10x REST
# divergence observed; flips direction within 1 min). H3 (seq-tracked
# message loss) DISPROVEN — drift happens with seq=monotonic. H3' (msg
# loss without seq increment on server) OPEN — cannot be detected by
# client. Industry standard (Binance, Bybit, Kraken, Polymarket): when
# in doubt, re-snapshot. The only thing that DEFINITIVELY clears
# accumulated state is a fresh server snapshot.
#
# Force-resubscribe: queue an unsubscribe + a re-subscribe. Server
# responds to subscribe with a fresh orderbook_snapshot, which
# `_handle_ob_snapshot` replaces the cache with atomically.
# (TODO: optimize to `update_subscription` with `action: get_snapshot`
# per Kalshi docs — preserves subscription, no gap. Requires sid
# tracking; ship unsub+resub first as safe fallback.)
WS_FORCE_RESUB_COOLDOWN_S = 30.0      # rate-limit per ticker; prevents loops

WS_PERIODIC_RESNAPSHOT_INTERVAL_S = 300.0  # 5 min — full sweep of 15M tickers

# Apr 26 2026 incident
# (kb/failures/scan-loop-stall-window-rotation-2026-04-26.md):
# at the 09:30 UTC 15M settlement boundary, worker thread unsubscribed
# settled tickers while main-thread scan body's stale `_local_windows`
# snapshot was still iterating those same tickers — `_get_orderbook_cached`
# called `subscribe_ticker(OLD_t)`, undoing the worker's cleanup. Result:
# 1Hz subscribe→delete→subscribe→delete loop on KXSOL15M-26APR260530-30
# for 7 seconds, followed by 10 minutes of cache-recovery thrash and zero
# 15M scan output.
#
# Fix: post-unsubscribe blacklist. `unsubscribe_ticker` records
# `ticker -> monotonic() + WS_UNSUBSCRIBE_BLACKLIST_S`. Both
# `subscribe_ticker` and `force_resubscribe` consult the map and silent-skip
# while the entry is fresh. 30s is well above the observed thrash window
# (~10 min was pathological; normal stale-window observation is sub-second
# to ~5s) and well below the 15-min 15M cycle so legitimate next-window
# subscriptions aren't blocked.
WS_UNSUBSCRIBE_BLACKLIST_S = 30.0

# Hybrid snapshot strategy: try `update_subscription` with
# `action: get_snapshot` (per Kalshi docs — preserves subscription,
# no gap). If no snapshot arrives within this timeout, fall back to
# unsubscribe + resubscribe (definitively works; uses existing code
# paths). 5s is enough for normal RTT + processing; if Kalshi accepts
# the get_snapshot command at all, the response is much faster.
WS_SNAPSHOT_REQUEST_TIMEOUT_S = 5.0

# R1 / A1 [P0]: if Kalshi doesn't honor update_subscription/get_snapshot
# the primary path silently fails 100%. Track consecutive timeouts;
# after this threshold, auto-disable the primary path (skip directly
# to unsub+resub). One LOUD warning is emitted on disable so the
# behavior change is visible in logs.
WS_GET_SNAPSHOT_DISABLE_AFTER = 3

# R1 / A5 [P1]: post-resub recovery watchdog. If a ticker stays out
# of `_orderbooks` longer than this after force_resubscribe, log a
# WARNING — the resubscribe didn't take effect (Kalshi never sent a
# new snapshot). The watchdog runs inline in _check_snapshot_timeouts.
WS_FORCE_RESUB_RECOVERY_TIMEOUT_S = 30.0

# Phase 2.6 R2 / B2 (R3 → 60s, Phase 2.7 → 180s): watchdog for
# outstanding subscribe responses. If type=subscribed never arrives
# within this timeout, pop the entry + log WARNING.
#
# Phase 2.6 deploy verification observed 692 WS_SUBSCRIBE_STUCK
# events in 7 minutes at 60s — Kalshi simply takes longer than 60s
# under our subscribe flood (~100 tickers across 15M+weather+sports+
# spx). False-positive watchdog firings were triggering 3
# WS_FORCE_RECONNECT events / 7min — reconnect storms. Bumping to
# 180s gives Kalshi headroom and reduces churn dramatically.
#
# A late-arriving subscribed after pop is unrecoverable (the
# response has no market_ticker — we cannot bind ticker→sid) so we
# accept "data flows but no drift-recovery" until next WS reconnect
# rather than risk a dual-subscription leak from re-queueing.
WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S = 180.0

# Phase 2.9 — raw WS frame logging for diagnostic trace.
# Phase 2.8 deploy showed WS_FORCE_RECONNECT firing every ~3 min
# even after stale-cleanup fix — live tickers' subscribes don't
# get type=subscribed acks, watchdog fires, reconnect, repeat.
# We can't diagnose ack-reliability without seeing the actual wire
# frames. WS_RAW_OUT/IN logs everything for the first 120s of each
# session (captures the initial subscribe burst + any acks/errors)
# plus all command-response types regardless of time (subscribed,
# unsubscribed, ok, error — all low volume).
WS_RAW_LOG_DURATION_S = 120.0

WS_RAW_LOG_TRUNCATE = 800

# Phase 2.9 R-review A3: hard cap on raw log lines per session
# (reset on reconnect). With ~100 tickers and chatty deltas + a
# reconnect-every-3min regime, an uncapped 120s window could emit
# tens of thousands of log lines per reconnect → multi-GB/day,
# blowing past journalctl's RuntimeMaxUse default. Cap protects
# against runaway log growth while still providing diagnostic
# coverage of the initial subscribe burst.
WS_RAW_LOG_MAX_PER_SESSION = 3000

# ─── Coinbase WebSocket ──────────────────────────────────────────────────────
COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"

COINBASE_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "HYPE": "HYPE-USD",       # T1 (2026-05-10): shadow observation (verified live + online on Coinbase Exchange)
    "DOGE": "DOGE-USD",       # T1 (2026-05-10): shadow observation (verified live + online on Coinbase Exchange)
    "BNB": "BNB-USD",         # T1 (2026-05-17, 86b9zmj0c): shadow observation (verified status=online, trading_disabled=false on Coinbase Exchange)
    "ADA": "ADA-USD",         # T1 (2026-05-30): shadow observation (verified status=online, trading_disabled=false on Coinbase Exchange)
    "BCH": "BCH-USD",         # T1 (2026-05-30): shadow observation (verified status=online, trading_disabled=false on Coinbase Exchange)
}

PRICE_BUFFER_SIZE = 1800          # 30 minutes of 1-second snapshots (extended Apr 19 for Phase 2 features)

SPOT_BUFFER_PERSIST_PATH = "state/spot_buffer.json"  # R-p7-deploy-r11: persist

                                   # 30-min buffer across restarts so cal_mlp
                                   # 5m/30m momentum features don't go NULL
                                   # for the first 5-30 min after every deploy.
                                   # See kb/concepts/calibrator-data-hygiene-apr29.md.
SPOT_BUFFER_PERSIST_INTERVAL_S = 30  # flush cadence

# ─── Volatility Engine ───────────────────────────────────────────────────────
# VOL_RETURN_INTERVAL → bot/config.py (relocated from repo root in Bit 12.1)
VOL_WINDOW_1MIN = 12              # 60s / 5s = 12 returns

VOL_WINDOW_5MIN = 60              # 300s / 5s = 60 returns

VOL_WINDOW_15MIN = 180            # 900s / 5s = 180 returns

VOL_BLEND_WEIGHTS = (0.5, 0.3, 0.2)  # 1min, 5min, 15min

RK_TV_SHADOW_MODE = False             # PROMOTED: time-varying RK weights drive live blend

JUMP_THRESHOLD_MULTIPLIER = 3.0   # return > 3x RV = jump

JUMP_DECAY_TAU = 432.7               # 300/ln(2), half-life = 300s

JUMP_DECAY_MAX_BOOST = 1.0           # boost starts at 1.0 (total = 2.0×)

JUMP_DECAY_MIN_BOOST = 0.01          # below this = regime "normal"

JUMP_MAX_HISTORY = 10                # max jump events per asset

# ─── Adaptive Jump Detection (Tier System) ────────────────────────────────
JUMP_ADAPTIVE_SHADOW_MODE = False       # False = adaptive drives regime, legacy at DEBUG

JUMP_ADAPTIVE_SUBSAMPLE = 3             # Every 3rd 5s tick = 15s returns

JUMP_ADAPTIVE_EWMA_LAMBDA = 0.94       # EWMA decay for variance

JUMP_ADAPTIVE_EWMA_INIT_RETURNS = 10   # Min 15s returns before EWMA trusted

JUMP_ADAPTIVE_PCTILE_WINDOW = 180      # 180 × 15s = 45 min rolling window

JUMP_ADAPTIVE_PCTILE_LEVEL = 0.995     # 99.5th percentile

JUMP_ADAPTIVE_SIGMA_MULT = 4.0         # |r| > 4σ_EWMA threshold

JUMP_ADAPTIVE_PCTILE_MIN_OBS = 30      # Min obs before percentile trusted

JUMP_ADAPTIVE_DECAY_TAU = 64.93        # 45/ln(2), half-life = 45s

JUMP_ADAPTIVE_DECAY_MAX_BOOST = 1.5    # Base boost per jump (magnitude-scaled)

JUMP_ADAPTIVE_DECAY_MIN_BOOST = 0.01   # Below this = "normal"

JUMP_ADAPTIVE_DECAY_CAP = 5.0          # Max total multiplier

JUMP_ADAPTIVE_MAG_SCALE_BASE = 4.0     # Magnitude scaling denominator

JUMP_ADAPTIVE_MAG_CAP = 3.0            # Cap magnitude ratio at 3x

JUMP_ADAPTIVE_MAX_HISTORY = 10         # Max events per asset

JUMP_ADAPTIVE_STATE_PATH = "jump_adaptive_state.json"

JUMP_ADAPTIVE_SAVE_INTERVAL = 300.0    # Save EWMA/percentile state every 5 min

# ─── Adaptive RK Bandwidth (BN 2008/2009) ─────────────────────────────────
RK_ADAPTIVE_SHADOW_MODE = False          # False = adaptive H* drives blended_rv

RK_CSTAR_FLAT_TOP_PARZEN = 3.5134       # c* for flat-top Parzen kernel (BN 2009 Table 2)

RK_NOISE_VAR_FLOOR = 1e-20              # ω² floor (prevents zero/negative)

RK_BANDWIDTH_MAX_FRACTION = 1 / 3       # H* cap as fraction of n

RK_MIN_RETURNS_FOR_ADAPTIVE = 20        # need ≥20 returns for reliable γ̂(1)

# ─── Deribit DVOL Integration ────────────────────────────────────────────────
DERIBIT_DVOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"

DERIBIT_DVOL_CURRENCIES = {"BTC": "BTC", "ETH": "ETH"}

DVOL_FETCH_INTERVAL = 60.0        # seconds between DVOL fetches

DVOL_CACHE_TTL = 120.0            # stale after 2 min

DVOL_HOURLY_AVG_MAXLEN = 60       # 60 fetches × 60s = ~1h rolling window

DVOL_HOURLY_AVG_MIN = 3           # Need ≥3 samples for meaningful average

DVOL_REQUEST_TIMEOUT = 5.0

# ─── IV-RV Regime Detection ──────────────────────────────────────────────────
IV_RV_SPREAD_THRESHOLD = 0.50     # if IV > RV by 50%, shift toward IV

BETA_LOOKBACK_RETURNS = 60        # 5 min of returns for cross-asset beta

# ─── Multi-venue synthetic RTI (B2b-1 shadow; RTI-6 per-asset go-live) ──
# Kill-switch for the in-bot 4-venue (Coinbase/Kraken/Bitstamp/Gemini) L2 →
# CFB-shape synthetic RTI feed (bot/feeds/synthetic_rti_feed.py). SHADOW by
# default: the synthetic is logged to evaluated_opportunities.rti_* for the
# Bit-3 retrain corpus and feeds a trade decision ONLY for assets in
# SYNTHETIC_RTI_LIVE_ASSETS (RTI-6 per-asset go-live gate, defined below;
# default EMPTY ⇒ shadow for every asset). Default OFF — flipping to
# True opens 4 extra L2 WS connections + a sampler thread (off the scan hot
# path; see the feed's _SAMPLE_INTERVAL_SECONDS). Ticket 86ba64h2w; plan
# kb/decisions/b2b-1-core-shadow-plan.md. NEVER flip the live signal before
# the Bit-4 shadow-validation gate (that is a separate, future change).
SYNTHETIC_RTI_ENABLED = os.environ.get("SYNTHETIC_RTI_ENABLED", "0") == "1"

# ─── RTI go-live (RTI-6, per-asset promotion) ───────────────────────────
# Per-asset gate for FEEDING the synthetic RTI into the LIVE decision spot (vs
# the B2b-1 shadow logging above). Default EMPTY => no asset uses RTI for its
# decision; zero behavior change, and the shadow invariant still holds for
# every asset. Promote an asset (add its symbol) ONLY after it clears the
# RTI-3 beats-market Brier gate AND the RMSE gate (umbrella 86ba6hdqr / ticket
# 86ba6hf2y; plan kb/decisions/rti-go-live-plan.md). Mirrors the per-asset
# MARKET_BLEND_W_BY_ASSET pattern — going live = a one-line edit here plus the
# re-fit blend weights, shipped same commit, after the data gate + approval.
SYNTHETIC_RTI_LIVE_ASSETS: set = set()
# Minimum rti_confidence (contributed venues / expected, the CFB-shape
# denominator) for a synthetic value to be trusted as the decision spot. Below
# this the scanner falls back to the Coinbase spot — never trade on a
# low-confidence synthetic. UNVALIDATED placeholder — MUST be tuned from the
# RTI-3 corpus before any asset is promoted; 0.75 is a guess, not a result.
RTI_LIVE_MIN_CONFIDENCE = 0.75

# ─── Cross-Exchange Order Flow ──────────────────────────────────────────
CROSS_EXCHANGE_ENABLED = True

CROSS_EXCHANGE_SYMBOLS = {
    "BTC": {"binance": "btcusdt", "kraken": "BTC/USD", "bybit": "BTCUSDT"},
    "ETH": {"binance": "ethusdt", "kraken": "ETH/USD", "bybit": "ETHUSDT"},
    "SOL": {"binance": "solusdt", "kraken": "SOL/USD", "bybit": "SOLUSDT"},
    "XRP": {"binance": "xrpusdt", "kraken": "XRP/USD", "bybit": "XRPUSDT"},
    "DOGE": {"binance": "dogeusdt", "kraken": "XDG/USD", "bybit": "DOGEUSDT"},
    # HYPE: only on Binance.US, NOT on Binance.com (the bot connects to
    # stream.binance.com). The "binance" key is intentionally absent —
    # CrossExchangeFeed uses v.get("binance") with skip-if-falsy semantics
    # so the entry resolves to "Kraken + Bybit only" cleanly. Documented
    # gap per the T1.5 verify-first contract; not a silent NULL.
    "HYPE": {"kraken": "HYPE/USD", "bybit": "HYPEUSDT"},
    # BNB: present on Binance.com (BNB is Binance's native token),
    # Kraken, Bybit. T1.5 (2026-05-17, ticket 86b9zmj15). The "binance"
    # key IS included matching the BTC/ETH/SOL/XRP/DOGE pattern — BNB
    # is listed on Binance.com. The US-VPS geo-block (HTTP 451 from
    # api.binance.com + stream.binance.com) is gated at module level
    # via BINANCE_FEED_ENABLED=0, NOT by per-asset key absence.
    # Separate EU-proxy spike: ticket 86b9zn45p.
    # Verifications:
    #   Binance: bnbusdt (listed; bot can't reach from US-IP, gated)
    #   Kraken : BNB/USD (api.kraken.com/0/public/AssetPairs?pair=BNBUSD,
    #             status=online, wsname=BNB/USD)
    #   Bybit  : BNBUSDT (Mac CloudFront-blocked artifact; VPS log
    #             '[INFO] Bybit feed connected' 21:27:48 confirms prod reach)
    "BNB": {"binance": "bnbusdt", "kraken": "BNB/USD", "bybit": "BNBUSDT"},
}

BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"

KRAKEN_WS_URL = "wss://ws.kraken.com/v2"

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/spot"

CROSS_EXCHANGE_BUFFER_SIZE = 15

CROSS_EXCHANGE_LEAD_THRESHOLD = 0.002     # 0.2% for single-exchange lead

CROSS_EXCHANGE_CONSENSUS_THRESHOLD = 0.003  # 0.3% for consensus

# R-bleed-1 R1-H1: when BINANCE_FEED_ENABLED=0 (US-VPS deploys), only
# Kraken+Bybit feed prices. Static MIN=3 silently makes consensus branches
# unreachable, killing the OFA_CONSENSUS_BOOST signal. Adapt threshold to
# the number of effectively-feeding exchanges.
#
# R2-M1: this evaluates ONCE at module import. Tests that mutate
# `bot.BINANCE_FEED_ENABLED` at runtime must ALSO patch
# `CROSS_EXCHANGE_CONSENSUS_MIN` — this derivation does NOT re-evaluate.
#
# R2-M2: lowering MIN from 3→2 keeps the consensus branch reachable but
# does NOT restore the original signal frequency. With N=2 feeds requiring
# both above 0.3% threshold, consensus events fire roughly 1/3 as often
# as the original "any 2 of 3" calibration. OFA_CONSENSUS_BOOST=+2pp is
# not load-bearing for trading decisions; "fires rarely" is acceptable
# vs the prior "never fires". Revisit calibration after 30d of N=2 logs.
#
# Bit B (2026-05-11, ClickUp 86b9vrr9h): HYPE dev-env asymmetry.
# Per CROSS_EXCHANGE_SYMBOLS + T1.5 verification (bf8b9a3, 2026-05-10
# for DOGE/HYPE; T1.5 for BNB 2026-05-17, ticket 86b9zmj15):
# - BTC/ETH/SOL/XRP/DOGE/BNB: present on Binance + Kraken + Bybit (max 3)
# - HYPE: present on Kraken + Bybit only — Binance.com does NOT list
#   HYPE (Binance.US only; the bot connects to stream.binance.com)
# Prod (BINANCE_FEED_ENABLED=0): MIN=2, HYPE reaches consensus normally.
# Dev (BINANCE_FEED_ENABLED=1): MIN=3, HYPE max-consensus=2 < MIN=3 →
# consensus branches unreachable for HYPE only; signal silently never
# fires. Engineers flipping BINANCE_FEED_ENABLED=1 locally for testing
# should remember this asymmetry. DOGE (Kraken symbol XDG/USD) is on all
# 3 exchanges so reaches MIN=3; BNB (Kraken symbol BNB/USD, Binance.com
# native token) is on all 3 as well. See
# kb/decisions/asset-onboarding-doge-hype-bit-1-5-shipped-may10.md
# "Per-exchange optionality" section for the canonical narrative.
_CROSS_EXCHANGE_FEEDS_ACTIVE = 3 if BINANCE_FEED_ENABLED else 2

CROSS_EXCHANGE_CONSENSUS_MIN = _CROSS_EXCHANGE_FEEDS_ACTIVE

CROSS_EXCHANGE_STALE_SECONDS = 30.0

# ─── CoinGlass Derivatives ─────────────────────────────────────────────
COINGLASS_API_URL = "https://open-api-v3.coinglass.com/api"

COINGLASS_FETCH_INTERVAL = 600.0          # 10 min (100 calls/day budget)

COINGLASS_CACHE_TTL = 900.0               # stale after 15 min

COINGLASS_REQUEST_TIMEOUT = 10.0

COINGLASS_SYMBOLS = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP", "DOGE": "DOGE", "HYPE": "HYPE", "BNB": "BNB"}

FUNDING_RATE_EXTREME = 0.0005             # 0.05%/8h

FUNDING_RATE_ELEVATED = 0.0003            # 0.03%/8h

# ─── Order Flow Adjustments ────────────────────────────────────────────
OFA_CONSENSUS_BOOST = 0.02                # +2pp when 3+ exchanges confirm direction

OFA_CONSENSUS_REDUCE = -0.02              # -2pp when 3+ exchanges oppose direction

OFA_LEAD_BOOST = 0.01                     # +1pp for weaker single-exchange lead

OFA_EXTREME_FUNDING_REDUCE = -0.015       # -1.5pp for extreme funding

OFA_ELEVATED_FUNDING_REDUCE = -0.005      # -0.5pp for elevated funding

OFA_MAX_ADJUSTMENT = 0.03                 # cap total at +/-3pp

# ─── Kalshi Orderbook Flow Tracking ──────────────────────────────────────
KALSHI_OFT_ENABLED = True

KALSHI_OFT_SHADOW_MODE = True          # True = compute & log, don't affect prob_adjustment

KALSHI_OFT_BUFFER_SIZE = 60            # 60 snapshots × ~1s = ~1 minute history per ticker

KALSHI_OFT_MIN_SNAPSHOTS = 5           # Need ≥5 snapshots before computing signals

KALSHI_OFT_STALE_SECONDS = 120.0       # Evict tickers inactive for 2 minutes

KALSHI_OFT_IMBALANCE_STRONG = 0.7      # bid_qty / total_qty ≥ 0.7 = strong buy pressure

KALSHI_OFT_IMBALANCE_WEAK = 0.3        # bid_qty / total_qty ≤ 0.3 = strong sell pressure

KALSHI_OFT_DEPTH_DRAIN_PCT = -0.5      # Depth shrinking >50% over window = drain signal

KALSHI_OFT_LOG_INTERVAL = 300.0        # Log OFT diagnostics every 5 min

# Kalshi OFT probability adjustments (shadow mode initially)
OFA_KALSHI_IMBALANCE_BOOST = 0.01      # +1pp for strong buy imbalance

OFA_KALSHI_IMBALANCE_REDUCE = -0.01    # -1pp for strong sell imbalance

OFA_KALSHI_DEPTH_DRAIN_BOOST = 0.005   # +0.5pp when depth draining (convergence signal)

OFA_KALSHI_CONVERGENCE_BOOST = 0.005   # +0.5pp for rapid ask convergence (>0.5¢/s)

# ─── Calibration Engine ─────────────────────────────────────────────────────
CALIBRATION_STATE_PATH = "calibration_state.json"

HOURLY_CALIBRATION_STATE_PATH = "hourly_calibration_state.json"

CALIBRATION_MIN_SAMPLES_PLATT = 200

CALIBRATION_MIN_SAMPLES_BETA = 350   # lowered from 500 (we have 370+ obs)

CALIBRATION_MIN_SAMPLES_BLR = 50

FIFTEEN_M_CALIBRATION_ENABLED = False  # BLR bypass: raw_prob more accurate (w=0.11 collapsed, Brier 0.0648 vs 0.0634 raw)

CALIBRATION_RETRAIN_INTERVAL = 3600    # seconds between retrain checks

CALIBRATION_BRIER_WINDOW = 500         # rolling Brier over last N outcomes

# Dynamic probability cap schedule (keyed by seconds_remaining)
# As expiry approaches, allow higher confidence from the model
DYNAMIC_CAP_SCHEDULE = [
    (600, 0.93),   # > 10 min: status quo cap
    (300, 0.95),   # 5–10 min: slightly relaxed
    (120, 0.97),   # 2–5 min: moderately relaxed
    (60,  0.985),  # 1–2 min: high confidence allowed
    (0,   0.995),  # < 1 min: near-certain allowed
]

# Hourly markets: 60-min windows. 1800s = 30 min out (scan start).
# At 30 min, deep-ITM hourly strikes are genuinely 95%+ likely.
# The 15M caps (0.93 at >10min) are too conservative for hourly.
# Data: 40 settled hourly insufficient_edge trades, 40W/0L (100% WR).
HOURLY_DYNAMIC_CAP_SCHEDULE = [
    (1800, 0.97),  # > 30 min: allow up to 97%
    (900,  0.98),  # 15-30 min
    (300,  0.99),  # 5-15 min
    (60,   0.995), # 1-5 min
    (0,    0.999), # < 1 min
]

MARKET_BLEND_W = 0.40             # legacy 15M scalar — kept as fallback for non-15M product types and any unknown asset (via `.get(asset, MARKET_BLEND_W)`). P2.1.d (2026-05-13) + P2.3 (2026-05-14, 86b9xv66a) + P2.4 (2026-05-19, 86b9zmj37) superseded for all 7 production 15M assets via MARKET_BLEND_W_BY_ASSET.

# ─── 15M per-asset market blend weights (P2.1.d, 2026-05-13) ────────────────
# Each value chosen from the 4-asset × 6-weight sim PnL sweep in P2.1.c-fu1
# against cal_mlp v1.1 candidate bundles. BTC + XRP pulled off corner argmaxes
# (0.0 → 0.10; 1.0 → 0.90) for robustness; ETH + SOL kept at interior argmaxes
# (0.20 and 0.80 respectively). Rationale + raw sweep data:
#   kb/findings/p2-1-c-fu1-blend-weight-sweep-resolves-eth-may13.md
#   .p2_1_c_run/cross_sweep_summary.txt
# P2.3 live promotion 2026-05-14 (86b9xv66a) added HYPE 0.80 + DOGE 0.60
# from B.1 Brier sweep on T1 shadow data. Fallback for unknown assets +
# non-15M product types is MARKET_BLEND_W above (legacy 0.40).
MARKET_BLEND_W_BY_ASSET: dict = {
    "BTC": 0.10,
    "ETH": 0.20,
    "SOL": 0.80,
    "XRP": 0.90,
    # P2.3 live promotion (2026-05-14, ClickUp 86b9xv66a). Both interior
    # argmins from B.1 full-population Brier sweep on shadow data since
    # 2026-05-10 (DOGE n=1710, HYPE n=1469). DOGE plateau 0.55-0.70;
    # HYPE plateau 0.70-0.85. Heavy market blend tempers model
    # overconfidence (HYPE mean_raw 0.77 vs win_rate 0.66; DOGE
    # mean_raw 0.75 vs win_rate 0.70). Sweep doc:
    # kb/findings/p2-3-b-live-promotion-blend-weights-may14.md.
    "HYPE": 0.80,
    "DOGE": 0.60,
    # P2.4 BNB live promotion (2026-05-19, ClickUp 86b9zmj37). B.1-equivalent
    # full-population Brier sweep on shadow data since 2026-05-17 (n=721).
    # Interior argmin at w=0.20 (matches ETH pattern); plateau 0.10-0.30.
    # OPPOSITE of HYPE/DOGE shape — BNB's raw model is well-calibrated
    # (mean_pred 0.818 vs win_rate 0.812), so low market blend preserves
    # the model's edge. Plan doc: kb/decisions/p2-4-bnb-live-promotion-plan.md.
    "BNB": 0.20,
}

ENDGAME_BLEND_PRICE = 96         # don't blend at or above this price (preserve endgame edge)

# ─── Shadow Calibration Pipeline ──────────────────────────────────────────────
SHADOW_CAL_PIPELINE = True   # REVERTED: no-blend system runs in shadow for monitoring

SHADOW_BLEND_W = 0.50        # Counterfactual: old system used 50% market blend

SHADOW_TEMP_SCALE = True     # Use temperature scaling instead of Beta Cal

Z_SCORE_MAX = 25.0                # refuse to trade if |z| > 25 (data: 0 losses in tradeable range up to z=25)

DISCREPANCY_PROB = 0.90           # model says >90% but...

DISCREPANCY_PRICE = 75            # ...market is below 75¢ → refuse

# ─── Opportunity Scanner ────────────────────────────────────────────────────
MIN_EDGE_PCT = 0.25               # flat fallback — matches lowest MIN_EDGE_BY_PRICE tier (was 0.7)

# Weekend Edge Discount — shadow-only counterfactual for Sat/Sun quiet markets
# When RV drops on weekends, model edges shrink below thresholds even though WR stays high.
# This logs what WOULD have traded at relaxed thresholds for graduation analysis.
# Graduation criteria (shadow → live):
#   - 4-6 weekends of data (~80-120 settled signals)
#   - WR >= 85% on settled markets
#   - No single asset dragging below 75% WR
#   - No edge inversion (lower tiers not dragging overall)
WEEKEND_EDGE_DISCOUNT = 0.60      # multiply MIN_EDGE_BY_PRICE by this on Sat/Sun

WEEKEND_DISCOUNT_LIVE = True      # Promote weekend discount to live trading (kill switch)

WEEKEND_DISCOUNT_MIN_PRICE = 90   # 90c+ only (raised from 89 to match ETH floor; data: 163 cands at 90c+ weekends, 95.6% WR)

WEEKEND_DISCOUNT_MAX_STC = 600    # STC gate — 600-900s is 57% WR, kills PnL

WEEKEND_EDGE_FLOOR = 0.0          # Allow zero-edge trades on weekends (data: 95.5% WR at 0% threshold, +$9.43/wknd-day)

WEEKEND_FIXED_RISK = 0.07         # 7% bankroll when positive-but-tiny Kelly rounds to 0 contracts (gated on Kelly>0 since 86b9zjx7r)

OVERNIGHT_EDGE_DISCOUNT = 0.60    # multiply MIN_EDGE_BY_PRICE by this during overnight quiet hours (04-11 UTC)

OVERNIGHT_QUIET_START = 4         # UTC hour — quiet zone starts (inclusive)

OVERNIGHT_QUIET_END = 11          # UTC hour — quiet zone ends (inclusive)

OVERNIGHT_DISCOUNT_LIVE = True    # Promote overnight discount to live trading (kill switch)

OVERNIGHT_DISCOUNT_MIN_PRICE = 89 # 89c+ only (data: 75/80 = 93.8% WR at 89c+, taker-sim +$208)

OVERNIGHT_DISCOUNT_MAX_STC = 600  # STC gate — match weekend discount STC cap

# ─── Overnight Low-Price Shadow ──────────────────────────────────────────
# Thesis: overnight market makers are slow/absent, so 50-85c YES contracts
# have stale pricing — model correctly puts outcomes at 90%+ probability.
# Settlement data: 50-69c overnight @ cal_prob>0.80 → 89.9% WR (n=325).
# Shadow-only — collects data for graduation analysis before live trading.
# Graduation criteria:
#   - ≥80 settled signals
#   - ≥85% WR on taker simulation
#   - No single asset below 75% WR (n≥10)
#   - Positive Kelly-sized sim PnL on taker
#   - No edge inversion by price tier
#   - ≥10 overnight sessions of data
OVERNIGHT_LP_SHADOW = True

OVERNIGHT_LP_MIN_ENTRY_PRICE = 50   # Lowest YES price to shadow-evaluate

OVERNIGHT_LP_MAX_ENTRY_PRICE = 85   # Highest (live 86+ pipeline unchanged)

OVERNIGHT_LP_MIN_CAL_PROB = 0.82    # Higher than live — forces high model confidence in untested price territory

OVERNIGHT_LP_MIN_EDGE_PCT = 0.10    # 10% min — symmetric payoffs need bigger edge than 86+c asymmetric

OVERNIGHT_LP_MAX_RISK_PER_TRADE = 0.10  # 10% (vs 25% live) — tighter for symmetric payoff

OVERNIGHT_LP_KELLY_FRACTION = 0.125 # Eighth-Kelly — extra conservative (calibration trained on 86-99c)

OVERNIGHT_LP_MIN_STC = 120         # At least 2 min to close (avoid scramble)

OVERNIGHT_LP_MAX_STC = 600         # Max 10 min — trade when outcome is nearly decided

OVERNIGHT_LP_HOURS_START = 0       # UTC hour — overnight LP window start (inclusive)

OVERNIGHT_LP_HOURS_END = 12        # UTC hour — overnight LP window end (exclusive)

OVERNIGHT_LP_VOL_SPIKE_MULT = 2.0  # Circuit breaker: skip if trailing vol > 2x overnight median

OVERNIGHT_LP_VOL_HISTORY_DAYS = 7  # Days of overnight vol history for median computation

# ─── Low-STC Sizing Cap (Fix #3) ─────────────────────────────────────────
# Data: 0-100s STC is -$84/14d (12W/2L). Catastrophic losses at very short STC
# wipe all gains. Halve position to limit downside on last-second reversals.
LOW_STC_SIZING_CAP = 0.50           # position multiplier when STC < threshold

LOW_STC_SIZING_CAP_THRESHOLD = 100  # seconds — apply cap below this STC

STC_SIZING_SCALER_KNEE = 300        # seconds — start scaling down above this (data: 5m+ WR drops from 94.6% to 87.5%)

STC_SIZING_SCALER_ENABLED = True    # universal STC size scaler: contracts *= 300/STC for STC>300

# ─── Loss Burst Cooldown (Apr 11 2026) ────────────────────────────────────
# After any 15M loss, pause new 15M entries for that asset for N seconds.
# Data (30d): 53/82 losses (65%) happen in temporal bursts — correlated macro
# moves that hit multiple 15M windows in the same regime. First loss is
# unavoidable; subsequent losses in the same 2h window are preventable.
# Counterfactual: per-asset 2h lockout = +$441/30d vs. −$70 30d baseline.
# See: kb/failures/loss-clustering.md
LOSS_COOLDOWN_ENABLED = True

LOSS_COOLDOWN_SECONDS = 7200        # 2 hours — per-asset 15M lockout after any loss

# ─── Decided Contract Shadow (Fix #2) ─────────────────────────────────────
# When z-score is very negative (spot far above strike) with short STC,
# the contract is essentially decided but the EGARCH pipeline can't compute
# edge because calibration squashes probability below market price.
# T1: z ≤ -5 → 32/32 = 100% WR. T2: z ≤ -3 at 93-96c → 52/54 = 96.3% WR.
DECIDED_CONTRACT_SHADOW = os.environ.get("DECIDED_CONTRACT_SHADOW", "1") == "1"

DECIDED_CONTRACT_Z_T1 = -5.0       # Tier 1 z threshold

DECIDED_CONTRACT_Z_T2 = -3.0       # Tier 2 z threshold (narrower price range)

DECIDED_CONTRACT_MIN_PRICE = 93     # Minimum ask price (cents) for decided signal

DECIDED_CONTRACT_T2_MAX_PRICE = 96  # T2 only applies up to 96c

DECIDED_CONTRACT_MAX_STC = 300      # Only within 5 minutes of close

# ── Decided Contract LIVE overlay ──
# Incremental strategy on top of main pipeline. Env-var kill switches (no deploy needed).
DECIDED_CONTRACT_Z_T1B = -4.0      # Tier 1B z threshold (relaxed T1 with higher price floor)

DECIDED_CONTRACT_T1B_MIN_PRICE = 95 # T1B only at 95c+ (40/40=100% WR at -5<z≤-4, 95c+)

DECIDED_T1_ENABLED = os.environ.get("DECIDED_T1_ENABLED", "1") == "1"

DECIDED_T1B_ENABLED = os.environ.get("DECIDED_T1B_ENABLED", "1") == "1"

DECIDED_T2_ENABLED = os.environ.get("DECIDED_T2_ENABLED", "1") == "1"

DECIDED_T2_Z25_ENABLED = os.environ.get("DECIDED_T2_Z25_ENABLED", "1") == "1"

DECIDED_T2_Z2_ENABLED = os.environ.get("DECIDED_T2_Z2_ENABLED", "0") == "1"  # Shadowed: -$313 net on 47 trades, no edge in z=-2.5 to -1.75

DECIDED_CONTRACT_Z_T2_Z25 = -2.5            # Tier 2-Z25: -3 < z ≤ -2.5, 93-96c (data: 7/7 = 100% WR)

DECIDED_CONTRACT_Z_T2_Z2 = -1.75            # Tier 2-Z2: -2.5 < z ≤ -1.75, 93-96c (expanded from -2.0 — data: 114/115 = 99.1% WR in -2.0 to -1.75 zone, Wilson LB 95.2%)

DECIDED_CONTRACT_T2_Z25_RISK = 0.10         # 10% fixed sizing (cut from 0.20 Apr 21 — 14d -$95 on 17 trades, 2 losses: Apr 10 SOL -$58, Apr 21 XRP -$132)

DECIDED_CONTRACT_T2_Z2_RISK = 0.20          # 20% fixed sizing (was 12.5% — data: 24/25 WR, 96%)

DECIDED_CONTRACT_RISK = 0.20                # Fixed 20% bankroll per signal (was 12.5% — data: 56/56 WR on T1+T1B+T2)

# SOL DC price-tiered risk: contain high-price loss asymmetry.
# SOL is the only asset with DC losses (2 losses, 28 wins). Both losses are SOL-specific.
# At 96c: win=$4/ct, loss=$96/ct → need 96% WR to break even. 20% risk at 96c = $284 max loss.
SOL_DC_RISK_TIERS = [(97, 0.05), (95, 0.10)]  # (price_floor, risk). Below 95c: default tier risk.

DECIDED_CONTRACT_MAX_WINDOW_RISK = 0.35     # 35% bankroll cap per window (was 25% — raised to accommodate 20% per-signal)

DC_IOC_RETRY_DELAY = 8                      # DEFAULT seconds between DC IOC retry attempts (used as fallback)

DC_IOC_MAX_RETRIES = 10                     # max retry attempts per DC ticker (initial + 10 = 11 total)

DC_PRICE_TOLERANCE_START_RETRY = 3          # retry number at which price widening begins (0-indexed from retries, not attempts)

DC_PRICE_TOLERANCE_MAX = 3                  # max cents above original target price

# ── Decided Contract Shadow Expansion ──
# Six shadow variants to evaluate expansion candidates. None place orders.
DC_SHADOW_STAGES = frozenset({
    "dc_shadow_t1b_93c",    # T1B at 93-94c (13/14, 1 loss at small n)
    "dc_shadow_t2_z25",     # T2 relaxed to z≤-2.5 (47/48)
    "dc_shadow_t2_90c",     # T2 price floor 90c BTC/ETH/SOL (29/29)
    "dc_shadow_t2_90c_xrp", # T2 price floor 90c XRP only (10/11)
    "dc_shadow_t2_z2",      # T2 relaxed to z≤-2 (105/111)
    "dc_shadow_no_side",    # NO-side decided (z≥5, 166/166)
    "dc_t2_z2_phase1_shadow", # T2-Z2 Phase 1 sim: BTC+ETH only, 10% sizing (re-promotion candidate — see kb/decisions/t2-z2-shadowed.md Apr 22 section)
})

DC_T2_Z2_PHASE1_RISK = 0.10  # Proposed Phase 1 sizing for T2-Z2 re-promotion shadow. NOT the live risk — live remains DECIDED_CONTRACT_T2_Z2_RISK=0.20 (shadowed via env var).

# ─── Relaxed Edge Shadow (Fix #1) ──────────────────────────────────────
# Edge thresholds at 88-93c may be too conservative. Data shows rejected trades
# at these prices win well above breakeven: 88c=97.2% WR, 89c=93.3%, 91c=94.4%.
# Shadow with halved thresholds to validate before promoting.
# ─── Terminal Momentum Strategy ──────────────────────────────────────────
# Trades 15M contracts at extreme prices (95-99c) in the final 1-5 minutes.
# These are contracts the main pipeline rejects as insufficient_edge but that
# settle YES at 98.99% WR (496 observations). Fixed 50-contract sizing, direct taker.
TERMINAL_MOMENTUM_ENABLED = os.environ.get("TERMINAL_MOMENTUM_ENABLED", "1") == "1"

TM_PRICE_SET = frozenset({96, 98, 99})    # Valid entry prices (95c/97c removed: 94.5% WR vs 95-97% BE = negative EV, -$980/2wk on 347 trades). frozenset (not mutable set) so TM_LIVE_STRATEGIES — derived eagerly at import — can't drift from runtime mutations.

TM_MIN_PROB = 0.93                        # Model confirmation threshold

TM_MIN_STC = 61                           # Minimum seconds to close

TM_MAX_STC = 300                          # Maximum seconds to close

TM_BASE_CONTRACTS = 100                   # Base multiplier for margin-proportional sizing

TM_STC_SAFE_THRESHOLD = 180               # STC below this → boost (100% WR zone)

TM_STC_DANGER_LO = 180                    # STC danger zone lower bound

TM_STC_DANGER_HI = 240                    # STC danger zone upper bound (210-240s has ALL losses)

TM_STC_SAFE_MULT = 1.5                    # Multiplier for STC < safe threshold

TM_STC_DANGER_MULT = 0.5                  # Multiplier for danger zone

TM_STC_NORMAL_MULT = 1.0                  # Multiplier for STC >= danger_hi

TM_MIN_CONTRACTS = 25                     # Floor (always collect data)

TM_MAX_CONTRACTS = 500                    # Hard cap

TM_MAX_CONCURRENT = 8                     # Max simultaneous TM positions (raised for stacking — multiple price levels on same ticker)

TM_NEGATIVE_EV_TIERS = set()              # Cleared — 95c removed from TM_PRICE_SET entirely (was min-sizing, now fully blocked)

TM_NBBO_MIN_BUFFER_PCT = 0.10            # NBBO-sourced TM at 98-99c requires >= 0.10% buffer

TM_NBBO_BLOCKED_PRICES = {96}            # Block TM at 96c when NBBO (data: 96c NBBO -$326, orderbook 21/21 +$60). 97c removed from TM_PRICE_SET entirely.

# R-p7-deploy-r10: cal_mlp lower-bound gate for TM-96. ALWAYS evaluated for
# shadow logging; only BLOCKS the trade when env var is "1". Default 0 so
# operators can ship the wiring shadow-only first, then flip live after
# observing the gate's would-block / would-allow rate over a few days.
# Motivating loss: ETH terminal_momentum_96 trade 2026-04-29 -$140 had
# raw_prob 0.93 (negative edge at 96¢) AND cal_mlp_final_lo 0.86 (well
# below 96¢). cal_mlp catches this class of overconfident high-price
# entries; gate provides the wiring without committing to full v1
# promotion across all strategies.
TM96_CALMLP_GATE_ENABLED = os.environ.get(
    'TM96_CALMLP_GATE_ENABLED', '0').strip().lower() in ('1', 'true', 'yes')

TM_THIN_BUFFER_PCT = 0.20                # Below this buffer %, apply TM_THIN_BUFFER_CONTRACT_CAP (all sources)

TM_THIN_BUFFER_CONTRACT_CAP = 50         # Contract cap when buf_pct < TM_THIN_BUFFER_PCT. Apr 1-23: 10/14 TM losses

                                         # (-$578) had buf_pct<0.20% avg 114ct; capping bounds each to ~-$50.
                                         # Kelly-sized backtest: Strategy B (cap) +$638 vs baseline, beats hard gate (+$577).

# Buffer-size multiplier (Sim B, ticket 86ba0v6z1, 2026-05-19). Wide-buffer TM trades
# are systematically under-sized: 30d phantom-corrected data shows $/contract is
# 30-50× higher at buf_pct≥0.40% than in the thin-buffer band, but sizing is roughly
# flat (~60 ct avg). The multiplier scales the BASE × margin × stc_mult formula:
# thin buffer keeps 1.0× (TM_THIN_BUFFER_CONTRACT_CAP=50 still binds as backstop);
# 0.40-0.80% scales 2× ($+1.40/ct realized); ≥0.80% scales 3× ($+1.67/ct realized).
# Per-asset risk caps and TM_MAX_CONTRACTS still bound the upside. Sorted ascending
# by buf_pct floor — last entry whose floor ≤ buf_pct wins.
# Sister mirror: scripts/cal_mlp/sim_pnl.py (lockstep — see test_tm_buffer_multiplier.py).
TM_BUFFER_SIZE_MULTIPLIER = (
    # (buf_pct_floor, multiplier)
    (0.00, 1.0),   # thin buffer — preserved; 50-ct cap is the bound
    (0.20, 1.0),   # already-profitable middle band; no change
    (0.40, 2.0),   # +1.40¢/ct realized → 2× scale
    (0.80, 3.0),   # +1.67¢/ct realized → 3× scale
)

# Per-asset risk caps for TM (same as main pipeline — TM no longer bypasses these)
TM_ASSET_RISK_CAPS = {
    "BTC": BTC_MAX_RISK_PER_TRADE,        # 0.15
    "ETH": ETH_MAX_RISK_PER_TRADE,        # 0.20
    "SOL": SOL_MAX_RISK_PER_TRADE,        # 0.15
    "XRP": XRP_MAX_RISK_PER_TRADE,        # 0.15
    "HYPE": HYPE_MAX_RISK_PER_TRADE,      # 0.10 (P2.3 live promotion 2026-05-14)
    "DOGE": DOGE_MAX_RISK_PER_TRADE,      # 0.10 (P2.3 live promotion 2026-05-14)
    "BNB": BNB_MAX_RISK_PER_TRADE,        # 0.10 (P2.4 live promotion 2026-05-19)
}

# ── TM half-Kelly cal_mlp shadow (Sim C, ticket 86ba0v7fc, 2026-05-19) ─────
# Shadow-only Kelly sizing on cal_mlp_p_mean (with raw_prob fallback) — logged
# to evaluated_opportunities.tm_shadow_kelly_* columns; NEVER consumed by
# production sizing. Sim C validates the Kelly-on-cal_mlp framework against
# realized TM PnL before any promotion. See
# kb/decisions/tm-half-kelly-shadow-plan.md.
#
# Half-Kelly: quarter-Kelly was over-conservative ($-49 vs actual $+125 in
# 30d counterfactual); half-Kelly was the data-justified choice ($+183 vs
# actual $+125 +$57 delta).
TM_SHADOW_KELLY_FRACTION = 0.50

# $100 absolute-loss bound — caps catastrophic-tail at any single TM trade
# matching the empirical loss-distribution constraint the TM_THIN_BUFFER
# cap was originally designed around (Apr 1-23: 8/14 TM losses ≥100ct were
# at sub-0.20% buffer). At 99c entry, 10000/99 ≈ 101 ct.
TM_SHADOW_KELLY_ABS_LOSS_BOUND_CENTS = 10000

# ── TM Sweep Shadow ────────────────────────────────────────────────────────
# Captures pre/post-fill orderbook depths at TM-relevant tiers (96/97/98/99)
# every TM execution. On settlement, computes counterfactual sweep PnL
# assuming sequential IOCs into 98 then 99 (skipping 97 entirely — 97 is
# the known negative-EV tier; sweeping past it is allowed, taking it is not).
# Shadow-only — answers "would a sweep into 98/99 after a TM-96 partial
# fill have been profitable?" Decision pending data accumulation.
#
# Analysis hygiene (adversary C1, C3): segment by entry_price_cents when
# aggregating cf_pnl — entry=98 rows sweep only 1 tier (99c) and aren't
# directly comparable to entry=96 rows that sweep 2 tiers. On stacked-TM
# tickers (multiple status='open' rows), don't sum cf_pnl — the snapshots
# overlap; only one sweep could have actually fired.
TM_SWEEP_SHADOW_ENABLED = os.environ.get("TM_SWEEP_SHADOW_ENABLED", "1") == "1"

TM_SWEEP_CAPTURE_TIERS = (96, 97, 98, 99)        # snapshot all four for analysis

TM_SWEEP_COUNTERFACTUAL_TIERS = (98, 99)         # 97 excluded by design (TM_NEGATIVE_EV)

# ── TM Sweep LIVE promotion (Apr 28 2026) ─────────────────────────────────
# Promoted from shadow on n=115 unique tickers, 115/115 wins, +$49.11
# cf_with_97 over ~38h. ADVERSARY A1 SURFACING: the 0-loss sample is a
# degenerate Wilson distribution — variance anchored to 0; one observed
# loss drops the LB from 96.8% to 93.5%, where 99c-tier sweep is −5.5¢
# per contract = ~−$2.75 per 50ct IOC. User opt-in only via env var.
#
# Mechanism: when enabled, _edge_ceiling override in _submit_taker lifts
# to MAX_ENTRY_PRICE for the EXACT terminal_momentum_{96,98,99} strategies
# (adversary A2 — startswith was a footgun against re-adding 95/97).
# The smart IOC picker can then bump 96→99. Position size is recomputed
# using worst-case fill price = MAX_ENTRY_PRICE so per-asset risk caps
# respect the actual capital-at-risk after a sweep (adversary A6).
#
# Kill switch: `TM_SWEEP_LIVE_ENABLED=0` env var → service restart.
# Shadow capture continues regardless so we can monitor realized vs
# counterfactual fills.
#
# Decision rationale + monitoring plan in kb/decisions/tm-sweep-live-promotion.md.
TM_SWEEP_LIVE_ENABLED = os.environ.get("TM_SWEEP_LIVE_ENABLED", "1") == "1"

# Exact-set strategy match — derived from TM_PRICE_SET so re-adding 95/97
# to TM_PRICE_SET requires explicit re-validation here.
#
# Asymmetric-coverage flag (adversary R2 A1): terminal_momentum_96 is in
# TM_LIVE_STRATEGIES but NOT in MAKER_TAIL_ELIGIBLE_STRATEGIES or
# LADDER_ESCALATION_ELIGIBLE_STRATEGIES. tm_98/tm_99 partial-fills get
# a maker-tail safety net for the unfilled remainder; tm_96 does not.
# Pre-promotion behavior was the same (tm_96 IOCs that partialed died
# without retry), so this isn't a regression — but a future change
# that adds tm_96 to either eligibility set should bring its own data.
TM_LIVE_STRATEGIES = frozenset(f"terminal_momentum_{p}" for p in TM_PRICE_SET)

# ─── Buffer-Aware Sizing (Infrastructure — DISABLED until data matures) ────
# PPO data (Apr 7, 63 tickers) shows entry buffer predicts main-pipeline outcomes:
# - Losses: 63% of observations have negative buffer, avg -0.023%
# - Wins: 0.7% negative, avg +0.209%
# Buffer does NOT predict TM outcomes (price dominates), but DOES for main pipeline.
# When enabled, multiplies Kelly-derived position_size by a buffer factor.
# Needs 10+ main-pipeline losses with buffer data to calibrate thresholds.
BUFFER_SIZING_ENABLED = False              # Feature flag — activate when data matures

BUFFER_SIZING_FAT = 0.20                   # Buffer >= this → boost ×1.25

BUFFER_SIZING_NORMAL = 0.10               # Buffer >= this → standard ×1.0

BUFFER_SIZING_THIN = 0.05                 # Buffer >= this → reduce ×0.5

BUFFER_SIZING_CRITICAL = 0.05             # Buffer < this → minimum ×0.25

# ─── Low-Price Near-Expiry (LPNE) Strategy ──────────────────────────────
# Trades BTC 15M at 80-87c in the final 10-120s before expiry. These are contracts
# the price floor rejects but that settle YES at 97.6% WR (42 obs, STC<=120s).
# Fixed 50-contract sizing, direct taker. BTC ONLY — ETH 80% WR, XRP 88.5%, SOL marginal.
LPNE_ENABLED = os.environ.get("LPNE_ENABLED", "1") == "1"

LPNE_ASSETS = {"BTC"}                     # BTC only — other assets don't have the WR

LPNE_MIN_PRICE = 80                       # Lowest eligible price

LPNE_MAX_PRICE = 87                       # Highest (88c+ is main pipeline BTC floor)

LPNE_MIN_STC = 10                         # Avoid last-second settlement noise

LPNE_MAX_STC = 120                        # Data: STC<=120s is the validated zone

LPNE_FIXED_CONTRACTS = 50                 # Fixed sizing, bypasses Kelly entirely

LPNE_MAX_CONCURRENT = 2                   # Conservative — new strategy

POSITION_PRICE_MONITOR_ENABLED = True       # Log yes_ask/bid for held positions (WS, zero API cost)

POSITION_PRICE_MONITOR_WS_STALE_SEC = 120.0  # Accept WS data up to 2min old (thin books don't update often)

RELAXED_EDGE_SHADOW = os.environ.get("RELAXED_EDGE_SHADOW", "1") == "1"

RELAXED_EDGE_DISCOUNT = 0.50        # 50% of normal edge threshold (halved)

RELAXED_EDGE_MIN_PRICE = 88         # Lower bound of relaxed range

RELAXED_EDGE_MAX_PRICE = 96         # Upper bound (exclusive). Data: 94-96c near-misses 96.8-100% WR (n=106)

# Price-dependent minimum edge: higher prices have worse asymmetry
# At 95c: 1 loss = 19 wins. At 87c: 1 loss = 6.7 wins.
MIN_EDGE_BY_PRICE = [
    (97, 0.010),   # 97-99c: need 1.0% edge (was 2.0% — data: 4 incremental signals, all wins)
    (95, 0.0075),  # 95-96c: need 0.75% edge (was 1.25% — data: 72 incremental signals, 97.2% WR, time-stable)
    (93, 0.005),   # 93-94c: need 0.5% edge (was 0.9% — data: near-misses at 93-95c have 95.5% WR, old 0.9% rejected them)
    (91, 0.0020),  # 91-92c: need 0.20% edge (was 0.35% — data: 193 settled at 94.3% WR, Wilson LB 90.1%)
    (89, 0.0025),  # 89-90c: need 0.25% edge (was 0.5% — halved: 2 rejected winners at 0.31-0.48%)
    (0,  0.0025),  # 80-88c: need 0.25% edge (floor lowered to 80c for ETH/SOL)
]

# FOMC rate decision announcement days (day 2 of each meeting). Fed publishes
# schedule annually — update this set each year. Source: federalreserve.gov.
FOMC_ANNOUNCEMENT_DATES = frozenset([
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
])

# CPI monthly release dates (BLS, usually 08:30 ET mid-month). Source: bls.gov.
CPI_RELEASE_DATES = frozenset([
    # 2025
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10",
    "2025-05-13", "2025-06-11", "2025-07-15", "2025-08-12",
    "2025-09-11", "2025-10-15", "2025-11-13", "2025-12-10",
    # 2026
    "2026-01-14", "2026-02-11", "2026-03-11", "2026-04-14",
    "2026-05-13", "2026-06-10", "2026-07-15", "2026-08-12",
    "2026-09-10", "2026-10-15", "2026-11-12", "2026-12-10",
])

ORDERBOOK_CACHE_TTL = 5.0         # seconds to cache orderbook responses

MAX_OB_FETCHES_PER_TICK = 6       # cap API calls for orderbooks per tick (Advanced tier)

BALANCE_CACHE_TTL = 10.0          # seconds to cache balance

# ─── Order Execution ──────────────────────────────────────────────────────
MAKER_PRICE_OFFSET = 1            # cents below fair value for maker orders

MAKER_POLL_INTERVAL = 2.0         # poll for maker fills every 2 seconds

ESCALATION_MAX_ENTRY = 99         # taker price cap during escalation (cents)

CONVERGENCE_WINDOW_SECONDS = 30.0 # seconds to measure price velocity

MAKER_TIMEOUT_SECONDS = 30.0     # hard timeout for maker orders

# ─── Maker tail after IOC partial fill ──────────────────────────────────
# After an IOC partially fills (e.g. wanted 50ct, got 9 because top of
# book was thin), instead of cancelling the unfilled remainder, post it
# as a post_only=True GTC limit at the IOC price for a short TTL. The
# remainder fills if benign rotation/inventory flow arrives at our
# bid; we eat adverse selection if the price moves against us. Live
# IOC strategies only — disabled/observation paths are excluded.
# Decision: kb/decisions/maker-tail-after-ioc-partial.md (TBD).
# Shipped straight to prod (no shadow) on Apr 25 2026 with TDD +
# adversarial review; risk capped via min STC + min remainder + per-
# asset and global concurrency caps.
# Pre-submit STC gate — kill 409 market_closed / 404 market_not_found
# settlement race. Apr 26 forensic: ETH ~106 api_errors / 3d at avg
# 98.8¢ near settlement (~30% of submissions in this zone), BTC 31,
# SOL/XRP 20-28 each. Mechanism: candidate fires at STC≤2s, network
# round-trip + Kalshi processing ~200ms-2s, by the time the order
# hits the matching engine the window has settled. 3.0s buffer covers
# typical Kalshi processing latency + clock-drift margin. STC value
# read off candidate dict (scan-time snapshot) — accepts a small
# residual race on the scan→submit gap (typically <1s). Set env to
# 0 to disable the gate live without redeploy. See
# kb/failures/order-submit-settlement-race-2026-04-26.md
MIN_ORDER_SUBMIT_STC_S = float(os.environ.get("MIN_ORDER_SUBMIT_STC_S", "3.0"))

MAKER_TAIL_AFTER_IOC_PARTIAL = (
    os.environ.get("MAKER_TAIL_AFTER_IOC_PARTIAL", "1") == "1")

MAKER_TAIL_TTL_SECONDS = 60        # cancel any tail older than this on tick()

MAKER_TAIL_MIN_REMAINDER = 5       # below this, API + state overhead > expected EV

MAKER_TAIL_MIN_STC_SECONDS = 60    # near-expiry zombie risk; skip

MAKER_TAIL_MAX_PER_ASSET = 2       # bound capital escrow per asset

MAKER_TAIL_MAX_GLOBAL = 5          # bound total escrow across the bot

# 8 currently-live IOC-firing strategies. Excluded by design: lpne
# (STC<120s already gated), weather_no_live (small fixed sizing, ex-LAS), hourly* (env
# kill switches). Expand only with data.
MAKER_TAIL_ELIGIBLE_STRATEGIES = frozenset({
    "decided_t1", "decided_t1b",
    "decided_t2", "decided_t2_z25",
    "terminal_momentum_98", "terminal_momentum_99",
    "weekend_discount", "overnight_discount",
})

# ─── Ladder escalation after IOC partial fill ──────────────────────────
# When an IOC partial-fills (e.g., wanted 50ct, got 2 because the real
# top of book was thin), retry ONCE at +1¢ for the remainder. Captures
# the dominant pattern of "thin top, deeper next level" which the
# passive maker-tail at the original price misses. Runs BEFORE the
# maker-tail so coexistence is layered: active reach first, then
# passive rest at original. Hard caps:
#   - LADDER_ESCALATION_MAX_STEPS = 1 (single retry only)
#   - LADDER_ESCALATION_OFFSET = 1 (one tick up per step)
#   - escalated price ≤ strategy MAX_ENTRY_PRICE (e.g., decided_t2
#     stops at 96¢) and ≤ MAX_ENTRY_PRICE (99¢ global).
#   - candidate must have ioc_filled > 0 (no escalation into phantom
#     books — same logic as MAKER_TAIL_AFTER_IOC_PARTIAL Gate 1).
#   - candidate carries _is_ladder_retry=True after the first step;
#     the helper refuses to escalate again on retries (recursion guard).
# See kb/decisions/ladder-escalation-after-ioc-partial.md.
LADDER_ESCALATION_ENABLED = (
    os.environ.get("LADDER_ESCALATION_ENABLED", "0") == "1")

LADDER_ESCALATION_MAX_STEPS = 1     # single retry only — N=2 needs data

LADDER_ESCALATION_OFFSET = 1        # cents per step

LADDER_ESCALATION_MIN_REMAINDER = 5 # mirror MAKER_TAIL_MIN_REMAINDER

# Eligible set mirrors MAKER_TAIL — these 8 strategies are vetted as
# "we want more size on partial fills". Excludes lpne (STC too tight),
# weather/hourly (different mechanics or disabled), TM_95/96/97
# (off / loss-making per Apr 26 30d analysis).
LADDER_ESCALATION_ELIGIBLE_STRATEGIES = MAKER_TAIL_ELIGIBLE_STRATEGIES

# ─── Direct Taker Threshold ──────────────────────────────────────────────
DIRECT_TAKER_THRESHOLD = 180.0    # seconds_to_close below this → skip maker, go IOC directly

                                  # Raised 75→180: 0% maker fill rate (26/26 escalated to taker), 9 missed candidates/day
MAKER_ONLY_THRESHOLD = 0.0        # seconds_to_close below this → maker only, no taker escalation

# ─── Per-Asset Taker Override ──────────────────────────────────────────
SOL_TAKER_FIRST = True            # SOL: bypass maker entirely, go direct IOC at all STC

                                  # Data: 44.7% maker fill rate, $101/wk missed, 95% unfilled WR
                                  # Taker fee delta ~$2/wk vs $101 missed — clear win
TAKER_FIRST_ASSETS = {"SOL"} if SOL_TAKER_FIRST else set()

SOL_EMPTY_BOOK_MAKER_MIN_PRICE = 87  # SOL maker fallback: only on empty books at 87c+ (data: 400 unfilled at depth=0, 95% WR)

SOL_EMPTY_BOOK_MIN_STC = 60.0        # SOL maker fallback: skip if STC < 60s (too tight for maker rest)

IOC_TICKER_COOLDOWN = 15          # seconds cooldown after IOC attempt per ticker (was 60 — too long for 15min windows)

IOC_RETRY_OFFSET = 1              # cents above ask for taker-first IOC (1c worse entry, much higher fill rate)

# ─── IOC Size-Clamp Policy (per-strategy) ──────────────────────────────
# Option X originally clamped every orderbook-source IOC to top-of-book depth
# to prevent Variant B ladder sweeps on sub-floor phantom asks. Post-WS-fix
# (0ddcaf8), top-of-book is frequently 1-2ct on TM_99/TM_98 markets, which
# killed strategies whose profit came from Kalshi's multi-level IOC matching
# sizing larger than the thin top level (pre-fix, NBBO fallback → blind IOC
# for 100ct was matched by the exchange against its real book, typically ~70ct).
#
# Per-strategy policy:
#   "top_of_book" — cap count at depth of quoted best ask (current Option X).
#                   Correct for strategies with sub-floor risk: prevents
#                   Kalshi from sweeping to rejected prices below floor.
#   "no_clamp"    — submit full Kelly count. Kalshi IOC auto-cancels
#                   unfilled remainder ($0 charge), so over-sizing is free.
#                   PHANTOM_ABORT (ask_depth=0) still fires as the tail guard.
#                   Correct for strategies where any sub-limit fill is
#                   strictly better (ceiling-triggered) or where empirical
#                   data shows sweeping essentially never happens.
#
# Decision criteria (see kb/decisions/no-floor-relaxation-on-ws-fix.md):
#   Ceiling-triggered (TM_99/98/96): limit IS the top of the strategy's
#     valid range. Any sub-limit fill strictly improves the trade.
#     30d empirical: TM_99 had 1/452 sweeps >3c, net +$196.
#   Floor-triggered discounts (overnight/weekend): have a floor but only
#     fire when the market is already at a discount — deeper asks below
#     don't exist in practice. 30d empirical: 1/52 and 1/77 sweeps >3c,
#     zero sub-floor losses.
#   Generic candidate paths (TAKER_NOW, MAKER_*, TM_95, TM_97): either
#     have active sub-floor risk (TAKER_NOW: 20/315 sweeps >3c, 11 losses
#     ≥$50) or are historical losers regardless of sizing.
STRATEGY_CLAMP_POLICY = {
    # Ceiling-triggered TM variants — sub-limit sweeps are strictly better
    "terminal_momentum": "no_clamp",       # legacy generic TM path
    "terminal_momentum_96": "no_clamp",
    "terminal_momentum_98": "no_clamp",
    "terminal_momentum_99": "no_clamp",
    # Floor-triggered discounts that empirically never cross their floor
    "overnight_discount": "no_clamp",
    "weekend_discount": "no_clamp",
    # Dead paths — TM_PRICE_SET = {96, 98, 99} so these never fire under
    # current config (removed from TM_PRICE_SET around Apr 9 after 30d data
    # showed TM_95 -$237/12 and TM_97 -$244/15 were net losers). Entries
    # kept as forward-compat defense: if 95/97 are ever re-added to
    # TM_PRICE_SET without reviewing clamp policy, this gives them a
    # conservative top_of_book fallback instead of slipping through to
    # STRATEGY_CLAMP_DEFAULT (also top_of_book, but explicit > implicit).
    # Any re-enable should ship with a fresh sweep/tail analysis at those
    # prices — the prior loss history is the real reason to leave them off.
    "terminal_momentum_95": "top_of_book",
    "terminal_momentum_97": "top_of_book",
    # Apr 25 2026: switched 15M direct/escalation paths from
    # top_of_book to no_clamp. Single-level clamp was capping orders
    # at top-of-book qty (avg ~3-15ct on 96c+ markets), driving
    # 15M avg fill from 64ct pre-clamp to 33ct post-clamp — a 50%
    # size drop on the dominant 15M IOC paths (MAKER_AGGRESSIVE
    # post_only escalation, PANIC_CAPTURE, TAKER_NOW direct entry).
    # Variant B (sub-floor IOC sweep) risk is documented in
    # memory/bug_ioc_subfloor_fill.md as cohort net +$178/22d
    # (positive EV historically), so re-enabling that path is a
    # feature not a bug. Catastrophic-case safety nets remain:
    #   - PHANTOM_ABORT on _rest_fresh==0 (real-time empty book)
    #   - IOC_DRIFT_CHECK rolling-window REST smoothed-peak clamp
    #     (catches sustained WS-vs-REST phantom — the Apr 24
    #     incident pattern WS=765/REST=1)
    #   - Circuit breakers on REST GETs
    # Those upstream defenses fire regardless of strategy policy.
    # `low_price_near_expiry`: price-floor sensitive (LPNE enters
    # at the floor; sub-floor sweep would breach the floor gate).
    # `bracket_no`: observation-only with fixed 1ct sizing where
    # the clamp is never binding.
    "TAKER_NOW": "no_clamp",
    "MAKER_PATIENT": "no_clamp",
    "MAKER_AGGRESSIVE": "no_clamp",
    "PANIC_CAPTURE": "no_clamp",
    "CONFIRMATION_ADDON": "no_clamp",
    "DIP_ADDON": "no_clamp",
    "low_price_near_expiry": "top_of_book",
    "bracket_no": "top_of_book",
}

STRATEGY_CLAMP_DEFAULT = "top_of_book"  # conservative fallback for unrecognized strategies

# ─── Smart IOC Limit Picker ──────────────────────────────────────────
# Operational ceiling: the most cents above best_yes_ask the picker
# is allowed to walk, regardless of edge headroom. 3c is a balance:
# enough to unlock typical level-2/3 deep liquidity (production sample
# Apr 25: 151ct sat 3c above best), small enough that even a stray
# bump on a thin-edge candidate is bounded. Note: this is SEPARATE
# from IOC_RETRY_OFFSET (which controls cancel-replace retry pricing,
# not depth walking — conflating them couples unrelated behaviors).
IOC_LIMIT_MAX_BUMP_CENTS = 3

# ─── Smart IOC Limit Picker — per-strategy edge reserve ──────────────
# `edge_ceiling_price = floor(prob*100) - fee_1c - reserve_cents`
# At the limit, worst-case fill has exactly `reserve_cents` of edge.
#
# Default = 0 (B): break-even after fee on worst fill. Most fills come
# at cheaper offer prices (Kalshi price-time priority), so AVERAGE
# edge is much better than the worst-case.
#
# Per-strategy override < 0 ("aggressive"): tolerate marginal negative
# edge on worst-filled contracts. ONLY justified for high-conviction
# strategies whose WR is high enough that the rare loss is bounded:
# decided contracts (z-score-confirmed near-certainty) and addons
# (extending bets we already trusted). reserve = -1 means worst-fill
# edge = -1c ≈ -fee_1c — paying fee-cost on margin.
#
# Hard floor: never go below -1. Worst-fill edge < -fee is structurally
# unprofitable regardless of WR (loss case = full contract value lost).
STRATEGY_LIMIT_BUMP_DEFAULT_RESERVE = 0

STRATEGY_LIMIT_BUMP_RESERVE_CENTS = {
    # Decided contracts — z-score-confirmed near-certain settlement
    # (T1 z≤-5, T1B z≤-4, T2 z≤-3, T2_Z25 z≤-2.5). Documented WR 95-100%.
    # Strategy strings match the candidate's `strategy` field (short
    # form), set in scan() via the _dc_strat = {long: short} mapping
    # at the DC entry path. Round 3 [A1] regression: long-form keys
    # (`decided_contract_t1`) silently miss the lookup.
    "decided_t1":      -1,
    "decided_t1b":     -1,
    "decided_t2":      -1,
    "decided_t2_z25":  -1,
    # NOTE: `decided_t2_z2` is INTENTIONALLY OMITTED. T2-Z2 was
    # shadowed Apr 1 2026 (DECIDED_T2_Z2_ENABLED=False) after
    # -$313 on 47 trades — no structural edge in z∈[-2.5,-1.75].
    # If re-enabled by env var without re-validation, it should
    # NOT inherit the aggressive reserve from the other tiers.
    # See memory/project_t2_z2_apr22_rejection.md.
    # Addons — extend positions we already committed to entering
    "CONFIRMATION_ADDON":       -1,
    "DIP_ADDON":                -1,
}

# ─── WS Cache-Drift Defense (pre-IOC REST verification) ──────────────────
# Evidence (Apr 24 2026): post-WS-fix TM_96 fill on KXSOL15M-26APR241245-45 —
# WS cache reported ask_depth=765 at 96c, IOC for 50ct submitted, Kalshi
# matched only 1ct. Confirms WS cache can diverge from real Kalshi book at
# top-of-book, not just deep levels (per the still-open H3 hypothesis in
# kb/failures/kalshi-ws-schema-drift.md § "WS delta underflow"). The delta
# underflow warnings (5-10k/day) are the same phenomenon.
#
# Mitigation: for any IOC where WS-cached ask_depth looks deep (≥20ct),
# fetch a fresh REST /orderbook right before submit and use the smaller of
# (cached_depth, rest_depth) as the effective clamp cap. Applies regardless
# of STRATEGY_CLAMP_POLICY — defense against a data-layer bug, not a policy
# change.
#
# Cost: one REST /orderbook per qualifying IOC (~30-50ms latency, negligible
# rate-limit impact at current volumes). Rejected from generic Option Y (in
# ioc-subfloor-fill.md) because that applied to ALL IOCs including the thin
# NBBO path; this version only fires when cache claims depth is present.
IOC_DRIFT_CHECK_ENABLED = os.environ.get("IOC_DRIFT_CHECK_ENABLED", "1") == "1"

IOC_DRIFT_CHECK_MIN_CACHED_DEPTH = 20   # skip REST if cached depth already thin

# Clamp if REST windowed-peak < ratio * cached. "rest" here is the
# rolling-window peak from `_rest_best_ask_depth_smoothed`, not a
# single REST sample (see IOC_DRIFT_CHECK_REST_WINDOW_S below).
IOC_DRIFT_CHECK_DIVERGENCE_RATIO = 0.5

# Cold-start catastrophic-drift escape hatch. When the rolling buffer
# has <_REST_DEPTH_MIN_SAMPLES_FOR_CLAMP samples (every freshly-discovered
# ticker), the smoothed-peak gate normally falls through to the cached
# WS depth. If the WS cache is wildly inflated (Apr 25 2026: cache=86,
# fresh=1 on KXETH15M and dozens of others), this lets bot submit
# Kelly-size IOCs into ~1ct top-of-book and accumulate micro-fills.
# This ratio fires the clamp even on cold-start when fresh-REST shows
# >=10x divergence from cache — strict enough to avoid single-sample
# flicker false-positives, lax enough to catch the dominant phantom
# pattern. See kb/failures/ws-cache-drift-microfills-2026-04-25.md (TBD).
IOC_DRIFT_CHECK_COLD_START_RATIO = 0.1

# Rolling-window smoothing (Apr 25 2026): a single REST sample is
# itself volatile — WS_DRIFT_PROBE_REST_STABILITY observed two REST
# calls 1s apart on the same ticker disagreeing by 900 contracts.
# Use the PEAK depth across recent REST observations as the clamp
# authority. Phantom WS still triggers (REST stays consistently low →
# peak stays low). Transient REST blips do NOT trigger (peak preserves
# an earlier higher reading). 5s = ~5 IOC-paths worth of observations
# — wide enough to ride out single-call noise, narrow enough that a
# real depth collapse propagates within a few seconds.
IOC_DRIFT_CHECK_REST_WINDOW_S = 5.0

# If REST-drift-corrected depth would clamp count below this floor, abort the
# IOC rather than filling a near-zero-EV micro-position. TM at 99c with 1ct
# fill: revenue=100 - cost=99 - fee=1 = 0¢ win vs -$1.00 loss = −$0.01 EV.
# Retry happens naturally via IOC_TICKER_COOLDOWN (15s); book often refills.
# Scoped to no_clamp + _drift_corrected cases only so genuine thin-book
# top_of_book clamps (explicit policy choice) are unaffected.
# See kb/decisions/ioc-thin-clamp-abort.md.
IOC_MIN_COUNT_AFTER_CLAMP = 5

# ─── Raw Kalshi API payload journal (diagnostic) ──────────────────────────
# Appends full API responses to raw_api_journal.jsonl for root-cause work on
# (1) sub-dollar revenue N→1 destruction (4 confirmed cases, $32/90d) and
# (2) IOC double-count (2/48 in 30d, root cause still unpinpointed after
# 6 audit waves). Both are blocked on raw payload evidence — see
# kb/failures/execution-pipeline-audit-2026-04-24.md.
#
# Volume: ~70 settlements/day × ~1 KB ≈ 2 MB/month; IOC fills comparable.
# Default OFF. Flip env var on VPS + restart to begin collection.
LOG_RAW_SETTLEMENTS = os.environ.get("LOG_RAW_SETTLEMENTS", "0") == "1"

LOG_RAW_IOC_FILLS = os.environ.get("LOG_RAW_IOC_FILLS", "0") == "1"

RAW_API_JOURNAL_PATH = "raw_api_journal.jsonl"

# ─── NBBO Fallback Gates ──────────────────────────────────────────────────
# When orderbook is empty, fall back to market NBBO yes_ask IF within these gates.
# Data: 456/468 missed candidates had empty orderbooks; simulated PnL +$196/wk.
# Per-asset: (min_price_cents, max_price_cents, max_stc_seconds_or_None)
NBBO_FALLBACK_GATES = {
    "BTC": (80, 99, 300.0),     # Lowered from 86 for LPNE (80-87c near-expiry); 97.9% WR at 86c+
    "ETH": (90, 99, 300.0),     # 90c matches ETH_MIN_ENTRY_PRICE; raised from 85c (data: 85-89c 86.2% WR, negative EV)
    "SOL": (90, 99, 300.0),     # Raised from 86→90: NBBO sub-90c = 85.7% WR -$319; orderbook trades unaffected (+$470)
    "XRP": (92, 99, 300.0),     # 180-300s validated; will evaluate 300-600s after 1 week NBBO data
    "BNB": (90, 99, 300.0),     # P2.4 2026-05-19: analog default mirror of ETH (same 85-89c sub-fee shape; n=272 at 90c+). No direct NBBO observations — post-T4 follow-up to refine.
}

# ─── Adaptive Escalation ─────────────────────────────────────────────────
ESCALATION_WAIT_LONG = 15.0       # maker wait when >=180s to close

BTC_ESCALATION_WAIT_OVERRIDE = 7.0  # BTC: 7s instead of 15s at STC>=180s

                                    # Data: ask_confirmed avg 2.7s, escalation_wait avg 19.5s, slip 3.4c
ESCALATION_WAIT_MEDIUM = 7.0      # maker wait when 120-180s to close (86% fills within 7s)

ESCALATION_WAIT_SHORT = 5.0       # maker wait when 60-120s to close

EARLY_ESCALATION_MIN_MOVE = 5      # ask must move ≥5¢ above maker price to trigger (was 2; raised to reduce adverse selection — 4L at 2-4c slip cost $50.62/2wk)

# ─── Post-only rejection → taker escalation ────────────────────────────
POST_ONLY_MAX_SAME_PRICE = 2          # Tier 1: max attempts at same maker price before degrading

POST_ONLY_DEGRADED_EXTRA_OFFSET = 1   # Tier 2: extra ¢ offset for degraded maker attempt

POST_ONLY_REJECTION_EXPIRY = 30.0     # Seconds before rejection count resets (stale data guard)

# ─── Confirmation Addon ─────────────────────────────────────────────────
ADDON_ENABLED = True

ADDON_MIN_PRICE_IMPROVEMENT = 3       # cents improvement from entry to trigger

ADDON_MIN_SECONDS_SINCE_FILL = 10.0   # seconds after fill before addon eligible

ADDON_MIN_STC_REMAINING = 45.0        # need ≥45s remaining at addon time

ADDON_SIZE_FRACTION = 0.50            # addon = 50% of original count

ADDON_MAX_PER_POSITION = 1            # max 1 addon per position

ADDON_MAX_ENTRY_PRICE = 98            # 98¢ cap — still profitable after fees

# ─── Dip Addon ────────────────────────────────────────────────────────
DIP_ADDON_ENABLED = False                 # Killed: 55.2% WR, no edge (29 settled, 16W/13L)

DIP_ADDON_SHADOW_MODE = False             # Was PHASE 1 shadow — data conclusive, no edge

# ─── Price Shadow — edge data for 70-85c markets ──────────────────────
PRICE_SHADOW_ENABLED = True        # Shadow-evaluate POR for edge data collection

PRICE_SHADOW_FLOOR = 70            # Lowest price to shadow-evaluate

NO_SIDE_MIN_ENTRY_PRICE = 5        # Lowest NO price for shadow data collection (all product types)

# ─── Low-Price Shadow — observation-only data collection band ────────
# Phase C of shadow coverage expansion (2026-05-02): floor 70 → 20,
# STC cap 600 → 900. Operator principle: "We should not be limited by
# data collection." MIN_ENTRY_PRICE (live floor) is unchanged — this band
# is shadow-only. See kb/decisions/shadow-coverage-expansion-may01.md.
LOW_PRICE_SHADOW_ENABLED = True    # Shadow-evaluate 20-79c 15M signals

LOW_PRICE_SHADOW_MIN_PRICE = 20    # Floor (was 70 pre-Phase-C; 20 leaves room for far-from-BE training data)

LOW_PRICE_SHADOW_MAX_PRICE = 79    # Ceiling (80c+ already live for some assets)

LOW_PRICE_SHADOW_MAX_STC = 900     # Full scan-window (was 600; captures entire decision life)

LP_MAX_RISK_PER_TRADE = 0.10       # Capped sizing: 10% bankroll cap

LP_KELLY_FRACTION = 0.25           # Capped sizing: quarter-Kelly

LP_WINDOW_CAP = 2                  # Max signals per 15M window (correlation cap)

LP_HOUR_CAP = 4                    # Max signals per hour (correlation cap)

DIP_ADDON_MIN_DROP_CENTS = 3              # ask must drop ≥3¢ below entry

DIP_ADDON_MIN_SECONDS_SINCE_FILL = 5.0   # wait after fill before eligible

DIP_ADDON_MIN_STC_REMAINING = 90.0       # need ≥90s (aligns with maker-only threshold)

DIP_ADDON_SIZE_FRACTION = 0.50            # addon = 50% of original count

DIP_ADDON_MAX_PER_POSITION = 1            # max 1 dip addon per position

DIP_ADDON_MAX_TOTAL_RISK = 0.35           # original + addon ≤ 35% of bankroll

DIP_ADDON_MIN_ENTRY_PRICE = 80            # lowered to match hourly floor (80¢)

DIP_ADDON_SHADOW_FLOOR = 50              # shadow logs ALL dips down to 50¢ for data collection

# Strategy constants — return values from evaluate_execution_strategy()
STRATEGY_WAIT = "WAIT"

STRATEGY_MAKER_PATIENT = "MAKER_PATIENT"

STRATEGY_MAKER_AGGRESSIVE = "MAKER_AGGRESSIVE"

STRATEGY_TAKER_NOW = "TAKER_NOW"

STRATEGY_PANIC_CAPTURE = "PANIC_CAPTURE"

# Decided-contract strategy names recognized by scan() (literal usages at
# bot/_impl.py:~14614 (_dc_strat mapping), ~16477 + ~20494 (tuple membership
# checks)). `decided_t2_z2` is INTENTIONALLY OMITTED from
# STRATEGY_LIMIT_BUMP_RESERVE_CENTS (T2_Z2 was shadowed Apr 1 2026 with no
# aggressive reserve grant); without this set the registry has a hole that
# false-positives the HPSB invariant for `decided_t2_z2`.
KNOWN_DC_STRATEGIES = frozenset({
    "decided_t1", "decided_t1b",
    "decided_t2", "decided_t2_z2", "decided_t2_z25",
    "hourly_dc",
})

KALSHI_WS_URL = ("wss://api.elections.kalshi.com/trade-api/ws/v2"
                 if os.environ.get("KALSHI_ENV") == "production"
                 else "wss://demo-api.kalshi.co/trade-api/ws/v2")

# WS silence watchdog: if no protocol message (fill/snapshot/delta/ack/etc.)
# arrives from Kalshi for this many seconds while we have active
# subscriptions, force-reconnect. The `websockets` library handles
# ping/pong internally, so a silent-but-alive connection is invisible
# from the iterator's perspective — hence the explicit watchdog.
# See kb/failures/ws-15m-silence-2026-04-24.md.
WS_SILENCE_TIMEOUT_SECONDS = 180    # 3 min — err on side of faster reconnect

WS_SILENCE_GRACE_SECONDS = 60       # don't trip watchdog in first minute after connect

WS_WATCHDOG_CHECK_INTERVAL = 30     # check every 30s


# ─────────────────────────────────────────────────────────────────────────────
# B1: Composite adverse-selection gate (ClickUp 86ba1zdwm, umbrella 86ba1zcd3)
#
# Two independent gates protecting against catastrophic 15M losses
# diagnosed in kb/decisions/b1-orderbook-prior-gate-plan.md (R0, 2026-05-21).
#
# Gate A — orderbook-prior adverse-selection (asset-agnostic, entry >= 90c).
#   Block YES entries when bot's calibrated_prob disagrees with market
#   orderbook by >5 pts AND the NO bid book has >$5 of conviction at
#   non-trivial prices (NO bids at price >= 2c, dropping pure liquidity
#   makers). Catches the "informed counterparty" loss class. The 90c
#   entry floor matches the R0 sim window — sub-90c entries are out of
#   measured scope.
#
# Gate B — HYPE high-price buf gate (HYPE-only, entry >= 98c).
#   Block YES entries at 98-99c on HYPE when bot's spot-distance-from-strike
#   < 0.75%. HYPE has the widest per-asset feed divergence (single-venue
#   Coinbase blind spot; p99 = 76.6 bps vs BTC's 33.1 bps per the R0
#   per-asset divergence table). The asymmetric risk/reward at 98-99c
#   requires near-certainty including feed-noise; thin buf is structurally
#   negative-EV regardless of cal_p. Retires when B2 ships multi-venue
#   synthetic RTI feed (ticket 86ba1zf5j).
#
# Rollback: flip *_GATE_ENABLED to False; shadow rows continue to log
# (would-block computed regardless of enable flag, scanner-side gates
# the trade-block — mirrors TM96 cal_mlp gate's R-p7-deploy-r10 pattern).
# ─────────────────────────────────────────────────────────────────────────────

# Gate A — orderbook-prior block
ORDERBOOK_PRIOR_GATE_ENABLED = True
ORDERBOOK_PRIOR_GATE_MIN_ENTRY_CENTS = 90     # entry-price floor — R0 sim only measured entry>=90c
ORDERBOOK_PRIOR_GATE_MIN_DISAGREE = 0.05      # bot cal_p must beat market floor by >5pts (strict)
ORDERBOOK_PRIOR_GATE_MIN_CONVICTION_CENTS = 500  # >$5 NO-side $ at prices >= MIN_NO_BID_PRICE (strict)
ORDERBOOK_PRIOR_GATE_MIN_NO_BID_PRICE = 2     # filter out 0-1c liquidity-only NO bids
ORDERBOOK_PRIOR_GATE_FILTER_STAGE = "orderbook_prior_block"

# Gate B — HYPE high-price buf block
HYPE_HIGH_PRICE_BUF_GATE_ENABLED = True
HYPE_HIGH_PRICE_BUF_GATE_MIN_ENTRY_CENTS = 98   # only entries at 98-99c
HYPE_HIGH_PRICE_BUF_GATE_MIN_BUF_PCT = 0.75     # require bot_buf >= 0.75% for HYPE 98-99c
HYPE_HIGH_PRICE_BUF_GATE_FILTER_STAGE = "hype_high_price_buf_block"
