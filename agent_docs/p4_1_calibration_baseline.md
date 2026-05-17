# P4.1 Band-Calibrated Sizing — Baseline (frozen 2026-05-17)

**Status:** LIVE since 2026-05-17 (ClickUp `86b9zjrp7`).
**Helper home:** `bot/helpers/band_calibration.py`
**Contract:** `tests/contracts/test_p4_1_band_calibrated_sizing.py`
**Source-of-truth roadmap:** `kb/decisions/money-printer-roadmap-may17.md` § Phase 4

## What this Bit changed

Before P4.1, all 15M `_sizer.compute()` calls received the live blend
output `final_prob` (= `live_prob` = `a1_raw_prob` in
`fifteenm_shadow_signals`) directly. Per the 2026-05-17 30d shadow
audit, that probability is miscalibrated across price bands —
underconfident at 94-99c, overconfident at 70-89c — so Kelly was
multiplying the calibration error into 5-10× too-large positions at
the wrong bands.

After P4.1, the 10 wrapped `_sizer.compute(...)` 15M call sites
(out of 12 total `_sizer.compute` sites in scanner; the 11th is V2
`_v2_prob`, the 12th is NO-side `no_prob` — both intentionally
unwrapped) in `bot/scanner/__init__.py` wrap `final_prob` with
`calibrated_prob_for_sizing(asset, best_ask, final_prob, product_type=<PT>)`,
where `<PT>` is the local `_pt` variable in shadow helper methods
(`_process_price_shadow`, `_process_overnight_lp_shadow`,
`_process_low_price_shadow`) or `window.get("product_type")` in
scan() loops that iterate `for window in eligible_windows`. The
helper looks up a band-stratified empirical realized rate
(hierarchical-pooled toward the band-aggregate prior with shrinkage
k=30) and returns it for use in Kelly. **Trade-selection gates
upstream continue to use `final_prob` unchanged** — only Kelly
*magnitude* changes.

Out of scope (intentionally unwrapped):
- `_v2_prob` (V2 sizing path, separate calibration domain).
- `no_prob` (NO-side sizing, separate calibration domain).
- Hourly / SPX / weather / sports product types (helper short-circuits
  to `raw_prob` unchanged when `product_type` is not 15M/None).

## Baseline data (frozen 2026-05-17 09:35 UTC, VPS HEAD `e3aecd4`)

### Band-aggregate priors (shrinkage target)

Pooled across all 6 assets within each band's own lookback window.

| Band  | Window | n     | prior |
|-------|--------|-------|-------|
| 70-79 |   30d  | 2139  | 0.7288 |
| 80-85 |   30d  |  581  | 0.8038 |
| 86-89 |   30d  |  214  | 0.8505 |
| 90-93 |   30d  |  209  | 0.8900 |
| 94-96 |   60d  |  156  | 0.9615 |
| 97-98 |   60d  |  142  | 0.9718 |
| 99    |   60d  |  218  | 1.0000 |

### Per-(asset × band) realized rates + shrunk values

`shrunk = (n_cell · raw_p_cell + 30 · band_prior) / (n_cell + 30)` then
rounded to 4dp. Cells marked **[THIN]** have `n_cell < 30` and are
heavily pulled toward the band prior.

| asset | band  | window | n   | raw_p   | shrunk  |
|-------|-------|--------|-----|---------|---------|
| BTC   | 70-79 |  30d   | 293 | 0.7509  | 0.7488  |
| BTC   | 80-85 |  30d   |  81 | 0.8025  | 0.8028  |
| BTC   | 86-89 |  30d   |  28 | 0.8571  | 0.8537 [THIN] |
| BTC   | 90-93 |  30d   |  36 | 0.8056  | 0.8439  |
| BTC   | 94-96 |  60d   |  48 | 0.9375  | 0.9467  |
| BTC   | 97-98 |  60d   |  32 | 0.9688  | 0.9702  |
| BTC   | 99    |  60d   |  64 | 1.0000  | 1.0000  |
| ETH   | 70-79 |  30d   | 513 | 0.7427  | 0.7419  |
| ETH   | 80-85 |  30d   | 239 | 0.8117  | 0.8108  |
| ETH   | 86-89 |  30d   |  91 | 0.8681  | 0.8638  |
| ETH   | 90-93 |  30d   |  98 | 0.9490  | 0.9351  |
| ETH   | 94-96 |  60d   |  51 | 0.9608  | 0.9611  |
| ETH   | 97-98 |  60d   |  43 | 1.0000  | 0.9884  |
| ETH   | 99    |  60d   |  83 | 1.0000  | 1.0000  |
| SOL   | 70-79 |  30d   | 414 | 0.7343  | 0.7339  |
| SOL   | 80-85 |  30d   |  69 | 0.7681  | 0.7789  |
| SOL   | 86-89 |  30d   |  28 | 0.8571  | 0.8537 [THIN] |
| SOL   | 90-93 |  30d   |  20 | 0.8000  | 0.8540 [THIN] |
| SOL   | 94-96 |  60d   |  12 | 1.0000  | 0.9725 [THIN] |
| SOL   | 97-98 |  60d   |  21 | 1.0000  | 0.9834 [THIN] |
| SOL   | 99    |  60d   |  29 | 1.0000  | 1.0000 [THIN] |
| XRP   | 70-79 |  30d   | 599 | 0.7346  | 0.7343  |
| XRP   | 80-85 |  30d   | 126 | 0.8413  | 0.8341  |
| XRP   | 86-89 |  30d   |  33 | 0.8788  | 0.8653  |
| XRP   | 90-93 |  30d   |  37 | 0.8649  | 0.8761  |
| XRP   | 94-96 |  60d   |  32 | 0.9688  | 0.9653  |
| XRP   | 97-98 |  60d   |  35 | 0.9143  | 0.9408  |
| XRP   | 99    |  60d   |  29 | 1.0000  | 1.0000 [THIN] |
| HYPE  | 70-79 |  30d   | 178 | 0.6461  | 0.6580  |
| HYPE  | 80-85 |  30d   |  31 | 0.6452  | 0.7232  |
| HYPE  | 86-89 |  30d   |  10 | 0.8000  | 0.8379 [THIN] |
| HYPE  | 90-93 |  30d   |   9 | 0.8889  | 0.8897 [THIN] |
| HYPE  | 94-96 |  60d   |   4 | 1.0000  | 0.9661 [THIN] |
| HYPE  | 97-98 |  60d   |   2 | 1.0000  | 0.9736 [THIN] |
| HYPE  | 99    |  60d   |   6 | 1.0000  | 1.0000 [THIN] |
| DOGE  | 70-79 |  30d   | 142 | 0.6972  | 0.7027  |
| DOGE  | 80-85 |  30d   |  35 | 0.8286  | 0.8171  |
| DOGE  | 86-89 |  30d   |  24 | 0.7500  | 0.8058 [THIN] |
| DOGE  | 90-93 |  30d   |   9 | 0.8889  | 0.8897 [THIN] |
| DOGE  | 94-96 |  60d   |   9 | 1.0000  | 0.9704 [THIN] |
| DOGE  | 97-98 |  60d   |   9 | 1.0000  | 0.9783 [THIN] |
| DOGE  | 99    |  60d   |   7 | 1.0000  | 1.0000 [THIN] |

These values are pinned in `bot/helpers/band_calibration.py` and
verified by `tests/contracts/test_p4_1_band_calibrated_sizing.py`. Any
drift breaks the contract test before reaching deploy.

## Why hybrid (30d for 70-93c / 60d for 94-100c)?

Temporal stability check on 2026-05-17 revealed a real regime shift
in the 80-89c bands over 60d: 80-85 trended 0.85 → 0.81, 86-89
trended 0.90 → 0.84. Using 60d for those bands would average the
older higher realized rate with the current lower one, sizing
*larger* at exactly the bands that are now failing — the very bug
P4.1 is trying to fix.

The 94-100c bands have stable realized rates (0.95-1.00) but very
thin samples in 30d (BTC 94-96: n=14, ETH 99: n=22, SOL 99: n=8).
60d roughly doubles n there without regime contamination because the
realized rate is at its ceiling.

The WS deployment is older than 60d (Phase 2 WS rollout > Mar; only
refactors landed during the 60d window — `d1974b1` 2026-05-09
KalshiFeed extraction, `f560d30` 2026-05-16 kalshi_wire extraction
— both behavior-preserving), so the 60d window is not contaminated
by a pre-WS bot regime.

## Refresh recipe (re-derivation, NOT for live use)

This baseline is **frozen**. Re-derive only when explicitly running
P4.2 (isotonic calibration layer with weekly retrain) or P4.1.x
follow-ups that re-anchor the snapshot. For any re-derivation:

```python
# After db-sync to /tmp/state.db (see .claude/skills/references/db-sync.md):
import sqlite3
c = sqlite3.connect('/tmp/state.db')

BANDS = [
    ('70-79',  "market_price BETWEEN 70 AND 79", 30),
    ('80-85',  "market_price BETWEEN 80 AND 85", 30),
    ('86-89',  "market_price BETWEEN 86 AND 89", 30),
    ('90-93',  "market_price BETWEEN 90 AND 93", 30),
    ('94-96',  "market_price BETWEEN 94 AND 96", 60),
    ('97-98',  "market_price IN (97,98)",        60),
    ('99',     "market_price IN (99,100)",       60),
]
ASSETS = ['BTC','ETH','SOL','XRP','HYPE','DOGE']
K = 30
TODAY = '2026-05-17'  # anchor for "days ago"

# Band priors (across all assets, per band's own window)
band_priors = {
    band: c.execute(f"""
      SELECT AVG(CASE WHEN market_result='yes' THEN 1.0 ELSE 0.0 END)
      FROM fifteenm_shadow_signals
      WHERE market_result IN ('yes','no') AND {pred}
        AND evaluation_time >= datetime('{TODAY}','-{w} days')
    """).fetchone()[0]
    for band, pred, w in BANDS
}

# Per-cell raw + shrunk
for asset in ASSETS:
    for band, pred, w in BANDS:
        n, raw_p = c.execute(f"""
          SELECT COUNT(*),
                 AVG(CASE WHEN market_result='yes' THEN 1.0 ELSE 0.0 END)
          FROM fifteenm_shadow_signals
          WHERE asset=? AND market_result IN ('yes','no') AND {pred}
            AND evaluation_time >= datetime('{TODAY}','-{w} days')
        """, (asset,)).fetchone()
        prior = band_priors[band]
        shrunk = prior if n == 0 else (n*raw_p + K*prior) / (n+K)
        print(f"({asset!r}, {band!r}): n={n}, raw_p={raw_p:.6f}, shrunk={round(shrunk,4)}")
```

## Soak monitoring (14d through 2026-05-31)

**Rollback rule:** per-(asset × band) realized rate must stay within
±5pp of the values pinned above. If any single cell drifts beyond
that band, disable that cell via the operator escape hatch
`BAND_CALIBRATION_DISABLED_CELLS` (see below) — the bot will fall
back to bare `final_prob` for THAT cell only, leaving the other 41
cells calibrated.

### Per-cell rollback (operator escape hatch)

Add the cell tuple to `BAND_CALIBRATION_DISABLED_CELLS` in
`bot/helpers/band_calibration.py` and ship a single-line commit. The
helper short-circuits to `raw_prob` for any cell in that set,
sidestepping the per-cell-revert-all-wraps coarseness:

```python
# bot/helpers/band_calibration.py — escape hatch (default: empty set)
BAND_CALIBRATION_DISABLED_CELLS: set = {
    ("DOGE", "86-89"),   # ROLLED BACK 2026-MM-DD: realized 0.74 vs baseline 0.8058
}
```

The contract test `test_disabled_cells_escape_hatch` exercises this
path. The default state is empty; any populated state means a soak
rollback is live and should be tracked against the soak-monitoring
ticket.

**Why band-stratified (not aggregate per-asset Brier)?** L97 lesson
from P2.3: aggregate masks band-localized failure. The May-14 -$187
86-89c loss was completely invisible to per-asset aggregate Brier
monitoring on HYPE/DOGE because their 90c+ trades were +EV. The
sister-fix here pins per-cell realized rates, not Brier averages.

**Daily check (operator):** After each day's settled rows finalize,
re-run the per-cell query above with `TODAY` = today and compare to
the pinned values. Flag any cell where `|realized_now - pinned| > 0.05`.
A `/data-health` extension to surface this automatically is a
follow-up (filed against P4.2).

## Out-of-scope items intentionally NOT changed

- **DOGE_MIN_ENTRY_PRICE** (85): not tightened to 90. The 86-89c
  edge-positive subset (edge ≥5pp in 30d shadow) has true ~1.6pp
  realized edge; closing the band kills the edge subset along with
  the negative-edge bulk.
- **MARKET_BLEND_W_BY_ASSET**: not changed. The problem is
  band-shaped, not asset-shaped — a scalar per asset can't
  simultaneously fix 99c-underconfidence and 86-89c-overconfidence on
  the same asset.
- **cal_mlp v1.1**: not retrained. Already well-calibrated at 80-93c
  on shadow data. Retrain (P4.5) is deferred and re-evaluated only
  after P4.2 soak.

## ClickUp pointers

- P4.1 (this Bit): `86b9zjrp7` — Band-calibrated sizing (HIGH)
- P4.2: `86b9zjrv6` — Isotonic calibration layer + weekly retrain (HIGH)
- P4.3: `86b9zjt01` — Open 80-89c with micro-sizing (NORMAL, depends P4.1)
- P4.4: `86b9zjt46` — Up-size 94-99c paired with P3.1 tail-gate (NORMAL)
- P4.5: `86b9zjt7v` — cal_mlp v1.2 retrain (deferred, gated on P4.2 evidence)
