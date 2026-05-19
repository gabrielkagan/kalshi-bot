"""P4.1: Band-calibrated sizing — band-stratified Kelly probability lookup.

Origin (ClickUp 86b9zjrp7, Phase 4 of money-printer roadmap, 2026-05-17):

30d shadow calibration audit (n=3,374 settled signals in 70-99c) showed
that the live decision blend (`final_prob` = `live_prob` = `a1_raw_prob`
in `fifteenm_shadow_signals`) is miscalibrated by band:

  - Underconfident at 94-99c (says ~0.85, realized ~0.95-1.00)
  - Overconfident at 70-89c (says ~0.80, realized ~0.73-0.85)

`PositionSizer.compute()` consumes that probability directly in the Kelly
formula (`bot/models.py:1058-1061`), so the miscalibration multiplies into
positions that are 5-10x too large at 80-89c — empirically realized as
-$187 in 23 trades on 2026-05-14.

This contract locks the calibration table (per-asset x per-band realized
rates with hierarchical shrinkage toward the band-aggregate prior) AND
forbids passing the raw `final_prob` name directly into any 15M
`PositionSizer.compute()` call site in `bot/scanner/__init__.py`. The
helper `bot.helpers.band_calibration.calibrated_prob_for_sizing` must
wrap it.

Out of scope (NOT enforced):
  - `_v2_prob` (V2 sizing path, separate calibration domain)
  - `no_prob`  (NO-side sizing, separate calibration domain)
  - hourly / SPX / weather product types (helper returns raw_prob
    unchanged when `product_type` is not 15M/None)

Sister anchors:
  - bot/helpers/band_calibration.py (helper home)
  - kb/decisions/money-printer-roadmap-may17.md (calibration audit + scope)
  - kb/decisions/p4-1-pickup-prompt-may17.md (Bit kickoff)
  - agent_docs/p4_1_calibration_baseline.md (soak baseline + refresh recipe)

Lessons applied:
  - DD-2 / W0 (assertion-as-fossil): no test pins the literal name
    `final_prob` at `_sizer.compute(...)` (verified via grep at write time).
  - L97 (band-stratified soak, not aggregate): the soak baseline in the
    sidecar pins per-(asset x band) rates, not per-asset Brier aggregate.
"""

from __future__ import annotations

import ast
import logging
import math
from pathlib import Path
from typing import Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"


@pytest.fixture(autouse=True)
def _disable_kill_switch_for_calibration_assertions(monkeypatch):
    """Most tests in this file exercise the calibration math under the
    assumption that the helper does NOT short-circuit. The wholesale
    kill switch `BAND_CALIBRATION_KILL_SWITCH` defaults to True post
    2026-05-19 (ticket 86b9znd21), so we flip it OFF here per-test.
    The dedicated `test_kill_switch_*` tests below either pin against
    the source literal (default-is-True) or explicitly re-set the
    runtime constant to True inside the test body."""
    from bot.helpers import band_calibration as bc
    monkeypatch.setattr(bc, "BAND_CALIBRATION_KILL_SWITCH", False)


# ─────────────────────────────────────────────────────────────────────────────
# Anchor data — frozen 2026-05-17 09:35 UTC against state.db sync from VPS
# HEAD=e3aecd4. Hybrid lookback (operator decision 2026-05-17):
#   - 30d for bands 70-79, 80-85, 86-89, 90-93 (regime-sensitive)
#   - 60d for bands 94-96, 97-98, 99           (thin-cell stability)
# Shrinkage k=30 toward band-aggregate prior pooled across all 6 assets
# within each band's own window. Re-derivation recipe in the baseline doc.
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_BAND_BOUNDS = [
    ("70-79", 70, 79),
    ("80-85", 80, 85),
    ("86-89", 86, 89),
    ("90-93", 90, 93),
    ("94-96", 94, 96),
    ("97-98", 97, 98),
    ("99",    99, 100),
]

EXPECTED_WINDOW_DAYS = {
    "70-79": 30, "80-85": 30, "86-89": 30, "90-93": 30,
    "94-96": 60, "97-98": 60, "99": 60,
}

EXPECTED_BAND_PRIORS = {
    "70-79": 0.728845,
    "80-85": 0.803787,
    "86-89": 0.850467,
    "90-93": 0.889952,
    "94-96": 0.961538,
    "97-98": 0.971831,
    "99":    1.000000,
}

# (asset, band) -> (n_cell, raw_p_cell @ 6dp). 6dp precision is required so
# the (raw -> shrunk) derivation rounds consistently to 4dp at the shrunk
# pinning (BTC 97-98 was the limiting case: 31/32 = 0.96875, and 4dp
# rounding to 0.9688 perturbs the 4th decimal of shrunk).
EXPECTED_RAW_CELLS = {
    ("BTC",  "70-79"): (293, 0.750853),
    ("BTC",  "80-85"): ( 81, 0.802469),
    ("BTC",  "86-89"): ( 28, 0.857143),
    ("BTC",  "90-93"): ( 36, 0.805556),
    ("BTC",  "94-96"): ( 48, 0.937500),
    ("BTC",  "97-98"): ( 32, 0.968750),
    ("BTC",  "99"   ): ( 64, 1.000000),
    ("ETH",  "70-79"): (513, 0.742690),
    ("ETH",  "80-85"): (239, 0.811715),
    ("ETH",  "86-89"): ( 91, 0.868132),
    ("ETH",  "90-93"): ( 98, 0.948980),
    ("ETH",  "94-96"): ( 51, 0.960784),
    ("ETH",  "97-98"): ( 43, 1.000000),
    ("ETH",  "99"   ): ( 83, 1.000000),
    ("SOL",  "70-79"): (414, 0.734300),
    ("SOL",  "80-85"): ( 69, 0.768116),
    ("SOL",  "86-89"): ( 28, 0.857143),
    ("SOL",  "90-93"): ( 20, 0.800000),
    ("SOL",  "94-96"): ( 12, 1.000000),
    ("SOL",  "97-98"): ( 21, 1.000000),
    ("SOL",  "99"   ): ( 29, 1.000000),
    ("XRP",  "70-79"): (599, 0.734558),
    ("XRP",  "80-85"): (126, 0.841270),
    ("XRP",  "86-89"): ( 33, 0.878788),
    ("XRP",  "90-93"): ( 37, 0.864865),
    ("XRP",  "94-96"): ( 32, 0.968750),
    ("XRP",  "97-98"): ( 35, 0.914286),
    ("XRP",  "99"   ): ( 29, 1.000000),
    ("HYPE", "70-79"): (178, 0.646067),
    ("HYPE", "80-85"): ( 31, 0.645161),
    ("HYPE", "86-89"): ( 10, 0.800000),
    ("HYPE", "90-93"): (  9, 0.888889),
    ("HYPE", "94-96"): (  4, 1.000000),
    ("HYPE", "97-98"): (  2, 1.000000),
    ("HYPE", "99"   ): (  6, 1.000000),
    ("DOGE", "70-79"): (142, 0.697183),
    ("DOGE", "80-85"): ( 35, 0.828571),
    ("DOGE", "86-89"): ( 24, 0.750000),
    ("DOGE", "90-93"): (  9, 0.888889),
    ("DOGE", "94-96"): (  9, 1.000000),
    ("DOGE", "97-98"): (  9, 1.000000),
    ("DOGE", "99"   ): (  7, 1.000000),
}

EXPECTED_SHRINKAGE_K = 30

# Pre-computed shrunk values: (n * raw_p + k * band_prior) / (n + k), then
# rounded to 4dp. Pinning these locks the entire derivation chain.
EXPECTED_SHRUNK = {
    ("BTC",  "70-79"): 0.7488,
    ("BTC",  "80-85"): 0.8028,
    ("BTC",  "86-89"): 0.8537,
    ("BTC",  "90-93"): 0.8439,
    ("BTC",  "94-96"): 0.9467,
    ("BTC",  "97-98"): 0.9702,
    ("BTC",  "99"   ): 1.0000,
    ("ETH",  "70-79"): 0.7419,
    ("ETH",  "80-85"): 0.8108,
    ("ETH",  "86-89"): 0.8638,
    ("ETH",  "90-93"): 0.9351,
    ("ETH",  "94-96"): 0.9611,
    ("ETH",  "97-98"): 0.9884,
    ("ETH",  "99"   ): 1.0000,
    ("SOL",  "70-79"): 0.7339,
    ("SOL",  "80-85"): 0.7789,
    ("SOL",  "86-89"): 0.8537,
    ("SOL",  "90-93"): 0.8540,
    ("SOL",  "94-96"): 0.9725,
    ("SOL",  "97-98"): 0.9834,
    ("SOL",  "99"   ): 1.0000,
    ("XRP",  "70-79"): 0.7343,
    ("XRP",  "80-85"): 0.8341,
    ("XRP",  "86-89"): 0.8653,
    ("XRP",  "90-93"): 0.8761,
    ("XRP",  "94-96"): 0.9653,
    ("XRP",  "97-98"): 0.9408,
    ("XRP",  "99"   ): 1.0000,
    ("HYPE", "70-79"): 0.6580,
    ("HYPE", "80-85"): 0.7232,
    ("HYPE", "86-89"): 0.8379,
    ("HYPE", "90-93"): 0.8897,
    ("HYPE", "94-96"): 0.9661,
    ("HYPE", "97-98"): 0.9736,
    ("HYPE", "99"   ): 1.0000,
    ("DOGE", "70-79"): 0.7027,
    ("DOGE", "80-85"): 0.8171,
    ("DOGE", "86-89"): 0.8058,
    ("DOGE", "90-93"): 0.8897,
    ("DOGE", "94-96"): 0.9704,
    ("DOGE", "97-98"): 0.9783,
    ("DOGE", "99"   ): 1.0000,
}

EXPECTED_ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE")
EXPECTED_SNAPSHOT_DATE = "2026-05-17"


# ─────────────────────────────────────────────────────────────────────────────
# Module import + API surface
# ─────────────────────────────────────────────────────────────────────────────

def test_helper_module_importable():
    """The helper module must exist with the documented public symbols."""
    from bot.helpers import band_calibration  # noqa: F401
    assert hasattr(band_calibration, "calibrated_prob_for_sizing")
    assert hasattr(band_calibration, "CALIBRATED_PROB_BY_BAND")
    assert hasattr(band_calibration, "BAND_PRIORS")
    assert hasattr(band_calibration, "BAND_BOUNDS")
    assert hasattr(band_calibration, "SHRINKAGE_K")
    assert hasattr(band_calibration, "SNAPSHOT_DATE")


def test_snapshot_metadata_pinned():
    """Snapshot date + shrinkage constant must match the audit baseline."""
    from bot.helpers import band_calibration as bc
    assert bc.SNAPSHOT_DATE == EXPECTED_SNAPSHOT_DATE
    assert bc.SHRINKAGE_K == EXPECTED_SHRINKAGE_K


def test_band_bounds_match_audit():
    """Band edges in the helper must match the audit's 7-band partition."""
    from bot.helpers import band_calibration as bc
    assert list(bc.BAND_BOUNDS) == EXPECTED_BAND_BOUNDS


def test_band_priors_pinned():
    """All 7 band-aggregate priors must match the baseline within 1e-4."""
    from bot.helpers import band_calibration as bc
    for band, expected in EXPECTED_BAND_PRIORS.items():
        actual = bc.BAND_PRIORS[band]
        assert math.isclose(actual, expected, abs_tol=1e-4), (
            f"BAND_PRIORS[{band!r}]: expected {expected}, got {actual}"
        )


def test_all_42_cells_present_and_pinned():
    """All 42 (asset, band) cells must be present in CALIBRATED_PROB_BY_BAND
    with the shrunk values pinned to 4dp."""
    from bot.helpers import band_calibration as bc
    assert len(bc.CALIBRATED_PROB_BY_BAND) == 42, (
        f"expected 42 cells (6 assets x 7 bands), got "
        f"{len(bc.CALIBRATED_PROB_BY_BAND)}"
    )
    for cell, expected in EXPECTED_SHRUNK.items():
        actual = bc.CALIBRATED_PROB_BY_BAND[cell]
        assert math.isclose(actual, expected, abs_tol=1e-4), (
            f"CALIBRATED_PROB_BY_BAND[{cell!r}]: expected {expected}, "
            f"got {actual}"
        )


def test_shrunk_values_recompute_from_raw():
    """Shrunk values must be reproducible from raw cells + priors + k.
    Locks the formula: shrunk = (n*p + k*prior) / (n+k)."""
    from bot.helpers import band_calibration as bc
    for cell, (n, raw_p) in EXPECTED_RAW_CELLS.items():
        band = cell[1]
        prior = EXPECTED_BAND_PRIORS[band]
        expected = (n * raw_p + EXPECTED_SHRINKAGE_K * prior) / (
            n + EXPECTED_SHRINKAGE_K
        )
        actual = bc.CALIBRATED_PROB_BY_BAND[cell]
        assert math.isclose(actual, expected, abs_tol=1e-4), (
            f"derivation mismatch at {cell!r}: pinned shrunk={actual}, "
            f"recomputed from (n={n}, raw_p={raw_p}, prior={prior}, "
            f"k={EXPECTED_SHRINKAGE_K})={expected:.6f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helper behavior — calibrated_prob_for_sizing
# ─────────────────────────────────────────────────────────────────────────────

def test_helper_returns_calibrated_for_known_cell_15m():
    """For an in-table (asset, band) on 15M, must return the shrunk value
    (NOT the caller's raw_prob)."""
    from bot.helpers.band_calibration import calibrated_prob_for_sizing
    # DOGE @ 86c is the audit's failing band. raw_prob ~0.81, shrunk 0.8058.
    out = calibrated_prob_for_sizing("DOGE", 86, raw_prob=0.81, product_type=None)
    assert math.isclose(out, 0.8058, abs_tol=1e-4)
    out2 = calibrated_prob_for_sizing("DOGE", 86, raw_prob=0.81, product_type="15m")
    assert math.isclose(out2, 0.8058, abs_tol=1e-4)


def test_helper_passes_through_for_non_15m_product():
    """Non-15M product types (hourly, SPX, weather) must get raw_prob back
    unchanged — P4.1 is 15M-only.

    The `_KNOWN_15M_PRODUCT_TYPES` set is `{None, "15m"}` — `"crypto_15m"`
    is intentionally absent (R1 M2): that string is a CapitalAllocator
    *strategy-key*, never a `window.get("product_type")` *value*.
    `discover_active_windows()` in `bot/settlement.py` produces only `"15m"`
    or `"hourly"` for crypto windows. Including `"crypto_15m"` would be
    dead-code documentation.
    """
    from bot.helpers.band_calibration import calibrated_prob_for_sizing
    for pt in ("hourly", "spx_hourly", "weather", "sports", "weather_no",
               "crypto_15m"):  # crypto_15m is NOT a real product_type value
        out = calibrated_prob_for_sizing("BTC", 90, raw_prob=0.85, product_type=pt)
        assert out == 0.85, f"product_type={pt!r}: expected pass-through, got {out}"


def test_disabled_cells_escape_hatch():
    """`BAND_CALIBRATION_DISABLED_CELLS` is an operator runbook escape hatch
    for the 14d soak. Adding a cell to that set short-circuits the helper
    to `raw_prob` for that cell ONLY, leaving the other 41 cells calibrated.
    Default state is empty — flag adding a cell as an operational
    intervention, not a code change."""
    from bot.helpers import band_calibration as bc
    # Default: empty.
    assert bc.BAND_CALIBRATION_DISABLED_CELLS == set(), (
        "BAND_CALIBRATION_DISABLED_CELLS must default to empty set; "
        "any populated state means a soak rollback is live — confirm "
        "with operator runbook before shipping"
    )
    # Behavior when populated (test-local toggle, restored after):
    try:
        bc.BAND_CALIBRATION_DISABLED_CELLS.add(("DOGE", "86-89"))
        # Disabled cell: returns raw_prob (NOT the 0.8058 calibrated value).
        out = bc.calibrated_prob_for_sizing("DOGE", 86, raw_prob=0.81, product_type="15m")
        assert out == 0.81, (
            f"disabled cell ('DOGE','86-89') must return raw_prob; got {out}"
        )
        # Adjacent cell (DOGE 90-93) must still be calibrated.
        out2 = bc.calibrated_prob_for_sizing("DOGE", 91, raw_prob=0.81, product_type="15m")
        assert math.isclose(out2, 0.8897, abs_tol=1e-4), (
            f"non-disabled cell ('DOGE','90-93') must remain calibrated; got {out2}"
        )
        # Different asset, same band: still calibrated.
        out3 = bc.calibrated_prob_for_sizing("ETH", 86, raw_prob=0.81, product_type="15m")
        assert math.isclose(out3, 0.8638, abs_tol=1e-4), (
            f"non-disabled cell ('ETH','86-89') must remain calibrated; got {out3}"
        )
    finally:
        bc.BAND_CALIBRATION_DISABLED_CELLS.discard(("DOGE", "86-89"))


# Kill switch (ticket 86b9znd21, 2026-05-19).


def test_kill_switch_default_is_true_wholesale_disable():
    """`BAND_CALIBRATION_KILL_SWITCH` source-level default must be True
    (ticket 86b9znd21, 2026-05-19): the helper short-circuits to raw_prob
    for ALL cells unconditionally, reverting 15M Kelly sizing to pre-P4.1
    behavior without unwiring the 10 call sites. Source-level pin (NOT
    runtime) because the autouse fixture below monkeypatches the runtime
    constant to False for calibration-math assertions in this file."""
    import bot.helpers.band_calibration as bc
    from pathlib import Path
    src = Path(bc.__file__).read_text()
    expected = "BAND_CALIBRATION_KILL_SWITCH: bool = True"
    assert expected in src, (
        f"expected '{expected}' literal in "
        f"bot/helpers/band_calibration.py source — the wholesale kill "
        f"switch must default True until soak data re-enables P4.1"
    )


def test_kill_switch_short_circuits_helper_to_raw_prob():
    """When `BAND_CALIBRATION_KILL_SWITCH` is True, `calibrated_prob_for_sizing`
    returns `raw_prob` for ALL inputs unconditionally — regardless of
    asset/band/product_type/DISABLED_CELLS state. This is the wholesale
    revert to pre-P4.1 behavior."""
    from bot.helpers import band_calibration as bc
    # Explicitly set True (overrides the file-level autouse fixture for
    # this test only; the fixture's monkeypatch teardown restores it).
    bc.BAND_CALIBRATION_KILL_SWITCH = True
    # In-table DOGE 86c → kill switch wins over calibrated 0.8058.
    out = bc.calibrated_prob_for_sizing("DOGE", 86, raw_prob=0.81, product_type="15m")
    assert out == 0.81, (
        f"kill switch must short-circuit in-table cell ('DOGE','86-89') to "
        f"raw_prob; got {out}"
    )
    # In-table BTC 99c → kill switch wins over calibrated 1.0.
    out2 = bc.calibrated_prob_for_sizing("BTC", 99, raw_prob=0.90, product_type="15m")
    assert out2 == 0.90, (
        f"kill switch must short-circuit in-table cell ('BTC','99') to "
        f"raw_prob; got {out2}"
    )
    # Non-15M product type already passes through; kill switch is a no-op there.
    out3 = bc.calibrated_prob_for_sizing("BTC", 90, raw_prob=0.85, product_type="hourly")
    assert out3 == 0.85


def test_helper_passes_through_for_unknown_asset(caplog):
    """Unknown assets (e.g., a new asset before calibration is built) must
    fall back to raw_prob with a one-time warning."""
    from bot.helpers import band_calibration as bc
    bc._reset_warning_cache_for_tests()  # ensure clean state for caplog
    with caplog.at_level(logging.WARNING, logger=bc.__name__):
        out = bc.calibrated_prob_for_sizing("LTC", 85, raw_prob=0.80, product_type="15m")
    assert out == 0.80
    # Second call: same cell, MUST NOT re-warn (one-time)
    with caplog.at_level(logging.WARNING, logger=bc.__name__):
        bc.calibrated_prob_for_sizing("LTC", 85, raw_prob=0.80, product_type="15m")
    warn_lines = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "LTC" in r.getMessage()
    ]
    assert len(warn_lines) == 1, (
        f"expected exactly 1 one-time warning for missing cell, got "
        f"{len(warn_lines)}: {[r.getMessage() for r in warn_lines]}"
    )


def test_helper_passes_through_for_price_outside_70_100(caplog):
    """Prices outside 70-100c (e.g., sub-50c long-tail) must fall back to
    raw_prob silently (no warning — design choice: out-of-range is expected
    for sub-70c entries, not a missing-cell anomaly)."""
    from bot.helpers.band_calibration import calibrated_prob_for_sizing
    for px in (10, 50, 69, 101):
        out = calibrated_prob_for_sizing("BTC", px, raw_prob=0.30, product_type="15m")
        assert out == 0.30, f"price={px}: expected pass-through, got {out}"


def test_helper_classify_band_correctness():
    """_classify_band must partition the 70-100c range correctly into the
    7 documented bands."""
    from bot.helpers.band_calibration import _classify_band
    cases = [
        (70, "70-79"), (75, "70-79"), (79, "70-79"),
        (80, "80-85"), (85, "80-85"),
        (86, "86-89"), (89, "86-89"),
        (90, "90-93"), (93, "90-93"),
        (94, "94-96"), (96, "94-96"),
        (97, "97-98"), (98, "97-98"),
        (99, "99"),    (100, "99"),
    ]
    for px, expected in cases:
        assert _classify_band(px) == expected, (
            f"_classify_band({px}) expected {expected!r}, got "
            f"{_classify_band(px)!r}"
        )
    # Out of range:
    for px in (10, 50, 69, 101, 150):
        assert _classify_band(px) is None, (
            f"_classify_band({px}) expected None, got {_classify_band(px)!r}"
        )


def test_helper_signature_matches_pickup():
    """Public signature must be:
        calibrated_prob_for_sizing(asset, market_price_cents, raw_prob,
                                   product_type=None) -> float
    """
    import inspect
    from bot.helpers.band_calibration import calibrated_prob_for_sizing
    sig = inspect.signature(calibrated_prob_for_sizing)
    params = list(sig.parameters.keys())
    assert params == ["asset", "market_price_cents", "raw_prob", "product_type"], (
        f"unexpected signature: {sig}"
    )
    pt_param = sig.parameters["product_type"]
    assert pt_param.default is None


# ─────────────────────────────────────────────────────────────────────────────
# AST guard — bot/scanner/__init__.py wire-in
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def scanner_ast() -> ast.Module:
    assert SCANNER_PY.exists(), f"canonical source missing: {SCANNER_PY}"
    src = SCANNER_PY.read_text()
    return ast.parse(src, filename=str(SCANNER_PY))


def _find_sizer_compute_calls(tree: ast.Module) -> list[Tuple[int, ast.Call]]:
    """Return [(lineno, Call), ...] for every `*.sizer.compute(...)` or
    `self._sizer.compute(...)` call in the scanner."""
    sites = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compute"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr in ("_sizer", "sizer")
        ):
            sites.append((node.lineno, node))
    return sites


def test_scanner_sizer_compute_floor(scanner_ast: ast.Module):
    """Floor check — catches accidental deletion of sizing call sites."""
    sites = _find_sizer_compute_calls(scanner_ast)
    # At ship time there are 12 `self._sizer.compute(...)` sites in scanner
    # (per `grep -n 'sizer\\.compute' bot/scanner/__init__.py`). Floor at 10
    # to allow modest churn but flag a wholesale teardown.
    assert len(sites) >= 10, (
        f"expected >= 10 _sizer.compute call sites in scanner, got "
        f"{len(sites)} — sizing path may have been gutted"
    )


def test_no_bare_final_prob_at_sizer_compute(scanner_ast: ast.Module):
    """Every `_sizer.compute(X, ...)` call where X is the bare name
    `final_prob` MUST wrap it via `calibrated_prob_for_sizing(...)`.

    Allowed first-arg forms:
      - Call(func=Name(id='calibrated_prob_for_sizing'), ...)  # wrapped
      - Name(id='_v2_prob')                                    # V2 (out of scope)
      - Name(id='no_prob')                                     # NO (out of scope)
      - any other Name / Subscript / Constant                  # not 'final_prob'

    Forbidden:
      - Name(id='final_prob')  # raw — would resize off broken probability
    """
    sites = _find_sizer_compute_calls(scanner_ast)
    bad: list[int] = []
    for lineno, call in sites:
        if not call.args:
            continue
        first = call.args[0]
        if isinstance(first, ast.Name) and first.id == "final_prob":
            bad.append(lineno)
    if bad:
        raise AssertionError(
            "bare `final_prob` passed to _sizer.compute() in "
            "bot/scanner/__init__.py at lines:\n"
            + "\n".join(f"  line {ln}" for ln in bad) + "\n\n"
            "Wrap via calibrated_prob_for_sizing(asset, best_ask, "
            "final_prob, product_type=window.get('product_type')). "
            "See kb/decisions/money-printer-roadmap-may17.md §Phase 4."
        )


def test_v2_and_no_paths_still_unwrapped(scanner_ast: ast.Module):
    """V2 (`_v2_prob`) and NO (`no_prob`) sizing paths must REMAIN unwrapped
    — P4.1 explicitly scopes to 15M YES-side. Wrapping them would
    silently extend Phase 4 to out-of-scope domains.

    This is a negative test: we want to see these names still appear as
    bare first args at some sizer.compute site."""
    sites = _find_sizer_compute_calls(scanner_ast)
    first_arg_names = set()
    for _, call in sites:
        if call.args and isinstance(call.args[0], ast.Name):
            first_arg_names.add(call.args[0].id)
    assert "_v2_prob" in first_arg_names, (
        "expected _v2_prob to appear as bare first arg at some sizer.compute "
        "site (V2 sizing path) — if you intentionally wrapped V2, file a "
        "separate Bit and update this contract"
    )
    assert "no_prob" in first_arg_names, (
        "expected no_prob to appear as bare first arg at some sizer.compute "
        "site (NO-side sizing) — if you intentionally wrapped NO, file a "
        "separate Bit and update this contract"
    )


def test_calibrated_helper_wraps_use_in_scope_product_type(scanner_ast: ast.Module):
    """R1 C1 anti-regression — every `calibrated_prob_for_sizing(...)` call
    in scanner must pass a `product_type` kwarg whose value Name (or
    Attribute) resolves to a binding inside the enclosing FunctionDef.

    Locks the bug class where the wrap was copy-pasted with
    `product_type=window.get("product_type")` into helper methods that
    iterate `_pt = item["product_type"]` from a queue (no `window` in
    scope → NameError on first non-empty tick, silent data corruption
    inside try/except wrappers).

    Resolution heuristic per method: collect the set of bound Names
    inside the FunctionDef body (parameters + ast.Assign targets +
    ast.For targets + ast.With as-targets); the value name OR its
    root attribute base MUST be in that set.
    """
    # Step 1: find every calibrated_prob_for_sizing(...) call in scanner.
    cps_calls: list[tuple[int, ast.Call, ast.FunctionDef]] = []

    def _collect(node, fdef=None):
        if isinstance(node, ast.FunctionDef):
            fdef = node
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "calibrated_prob_for_sizing"
        ):
            assert fdef is not None, "wrap not inside a FunctionDef"
            cps_calls.append((node.lineno, node, fdef))
        for child in ast.iter_child_nodes(node):
            _collect(child, fdef)

    _collect(scanner_ast)
    assert len(cps_calls) >= 10, (
        f"expected >= 10 calibrated_prob_for_sizing() call sites in "
        f"scanner, got {len(cps_calls)}"
    )

    # Step 2: for each call, collect names bound in its enclosing FunctionDef.
    def _bound_names(fdef: ast.FunctionDef) -> set[str]:
        names: set[str] = set()
        for arg in fdef.args.args + fdef.args.kwonlyargs:
            names.add(arg.arg)
        if fdef.args.vararg:
            names.add(fdef.args.vararg.arg)
        if fdef.args.kwarg:
            names.add(fdef.args.kwarg.arg)
        for sub in ast.walk(fdef):
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    for n in ast.walk(tgt):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
            elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
                tgt = sub.target
                for n in ast.walk(tgt):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
            elif isinstance(sub, (ast.For, ast.AsyncFor)):
                for n in ast.walk(sub.target):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
            elif isinstance(sub, (ast.With, ast.AsyncWith)):
                for item in sub.items:
                    if item.optional_vars is not None:
                        for n in ast.walk(item.optional_vars):
                            if isinstance(n, ast.Name):
                                names.add(n.id)
        return names

    # Step 3: assert each call's product_type kwarg resolves to a bound name.
    bad: list[str] = []
    for lineno, call, fdef in cps_calls:
        pt_kwarg = next(
            (kw for kw in call.keywords if kw.arg == "product_type"), None
        )
        if pt_kwarg is None:
            bad.append(f"  line {lineno} (in {fdef.name}): missing product_type kwarg")
            continue
        v = pt_kwarg.value
        if isinstance(v, ast.Constant):
            continue  # literal None / "15m" — always valid
        # Find the root Name in any Attribute/Call/Subscript chain.
        root = v
        while isinstance(root, (ast.Attribute, ast.Subscript)):
            root = root.value
        if isinstance(root, ast.Call):
            root = root.func
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
        if isinstance(root, ast.Name):
            if root.id not in _bound_names(fdef) and root.id != "self":
                bad.append(
                    f"  line {lineno} (in {fdef.name}): product_type uses "
                    f"name {root.id!r} not bound in this method"
                )
    if bad:
        raise AssertionError(
            "calibrated_prob_for_sizing(...) wrap sites pass product_type "
            "values that reference names NOT in scope of the enclosing "
            "method — would NameError on the first non-empty tick:\n"
            + "\n".join(bad) + "\n\n"
            "Use the local product_type variable (typically `_pt`) bound "
            "inside the helper method's for-loop iteration."
        )


def test_calibrated_helper_imported_in_scanner(scanner_ast: ast.Module):
    """Scanner must import `calibrated_prob_for_sizing` so the wire-in is
    namespace-resolvable. Catches refactor that drops the import."""
    found = False
    for node in ast.walk(scanner_ast):
        if isinstance(node, ast.ImportFrom):
            if (
                node.module == "bot.helpers.band_calibration"
                and any(
                    alias.name == "calibrated_prob_for_sizing"
                    for alias in node.names
                )
            ):
                found = True
                break
    assert found, (
        "bot/scanner/__init__.py must import "
        "`from bot.helpers.band_calibration import calibrated_prob_for_sizing`"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Trace tests — anchor regression for the audit's motivating cases
# ─────────────────────────────────────────────────────────────────────────────

def test_trace_doge_86c_motivating_case():
    """DOGE @ 86c is the May-14 loss band. Pre-P4.1 raw_prob ~0.81 → bot
    computed positive edge vs market_only ~0.87 (and was wrong; n=24
    realized 0.75). After shrinkage k=30 the calibrated value is 0.8058
    (pulled UP toward band prior 0.85 from cell raw 0.75 — the prior
    smooths the n=24 noise but is still BELOW market_only 0.874, so Kelly
    should be NEGATIVE → no trade)."""
    from bot.helpers.band_calibration import calibrated_prob_for_sizing
    out = calibrated_prob_for_sizing("DOGE", 86, raw_prob=0.81, product_type="15m")
    assert math.isclose(out, 0.8058, abs_tol=1e-4)
    # And the implied edge should be NEGATIVE: 0.8058 - 0.86 = -0.054
    assert out - 0.86 < 0


def test_trace_btc_99c_high_confidence_band():
    """BTC @ 99c shadow realized 1.0 (n=64 in 60d window). Calibrated
    must equal 1.0 (band prior is also 1.0; shrinkage is a no-op)."""
    from bot.helpers.band_calibration import calibrated_prob_for_sizing
    out = calibrated_prob_for_sizing("BTC", 99, raw_prob=0.90, product_type="15m")
    assert math.isclose(out, 1.0000, abs_tol=1e-4)


def test_trace_hype_82c_thin_cell_shrinks_toward_prior():
    """HYPE @ 82c: cell raw=0.6452 (n=31) but band prior=0.8038. Shrinkage
    pulls the cell UP to 0.7232 — protects against the small-sample
    pessimism in HYPE 80-85 while still reflecting it's a lower-WR cell
    than band average."""
    from bot.helpers.band_calibration import calibrated_prob_for_sizing
    out = calibrated_prob_for_sizing("HYPE", 82, raw_prob=0.79, product_type="15m")
    assert math.isclose(out, 0.7232, abs_tol=1e-4)
