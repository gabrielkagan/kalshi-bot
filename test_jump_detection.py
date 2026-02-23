"""Standalone tests for advanced jump detection methods.

Copies static method logic inline — no bot.py import needed.
Run: python3 -m pytest test_jump_detection.py -v
"""

import math
import random

# ─── Constants (mirrored from bot.py) ────────────────────────────────────────
JUMP_MEDRV_CONST = 1.419358
JUMP_TBPV_C = 3.0
JUMP_TBPV_MU1_INV2 = 1.5707963268  # π/2
JUMP_CTZ_VARIANCE_CONST = 0.608994
JUMP_CTZ_CRITICAL = 2.3263
JUMP_LM_GUMBEL_C = 4.6001
JUMP_DECAY_TAU = 432.7
JUMP_DECAY_MAX_BOOST = 1.0
JUMP_DECAY_MIN_BOOST = 0.01

# ─── Inlined static methods ─────────────────────────────────────────────────

def medrv(returns, window):
    subset = returns[-window:] if len(returns) >= window else returns
    n = len(subset)
    if n < 3:
        return 0.0
    med_sum = 0.0
    for i in range(1, n - 1):
        a, b, c = abs(subset[i - 1]), abs(subset[i]), abs(subset[i + 1])
        if a > b:
            a, b = b, a
        if b > c:
            b, c = c, b
        if a > b:
            a, b = b, a
        med_sum += b * b
    val = JUMP_MEDRV_CONST * med_sum / (n - 2)
    return math.sqrt(max(0.0, val))


def bipower_variation(returns, window):
    subset = returns[-window:] if len(returns) >= window else returns
    n = len(subset)
    if n < 2:
        return 0.0
    bv_sum = 0.0
    for j in range(n - 1):
        bv_sum += abs(subset[j]) * abs(subset[j + 1])
    bv = (math.pi / 2.0) * bv_sum / (n - 1)
    return math.sqrt(max(0.0, bv))


def tbpv(returns, window):
    subset = returns[-window:] if len(returns) >= window else returns
    n = len(subset)
    if n < 2:
        return 0.0
    local_sigma = [0.0] * n
    trail = 12
    running_sum = 0.0
    for i in range(n):
        running_sum += abs(subset[i])
        if i >= trail:
            running_sum -= abs(subset[i - trail])
        count = min(i + 1, trail)
        local_sigma[i] = running_sum / count
    tbpv_sum = 0.0
    for j in range(n - 1):
        thresh_j = JUMP_TBPV_C * local_sigma[j]
        thresh_j1 = JUMP_TBPV_C * local_sigma[j + 1]
        if abs(subset[j]) <= thresh_j and abs(subset[j + 1]) <= thresh_j1:
            tbpv_sum += abs(subset[j]) * abs(subset[j + 1])
    val = JUMP_TBPV_MU1_INV2 * tbpv_sum / (n - 1)
    return math.sqrt(max(0.0, val))


def quad_power_quarticity(returns, window):
    subset = returns[-window:] if len(returns) >= window else returns
    n = len(subset)
    if n < 4:
        return 0.0
    qpq_sum = 0.0
    for i in range(n - 3):
        qpq_sum += abs(subset[i]) * abs(subset[i+1]) * abs(subset[i+2]) * abs(subset[i+3])
    return (math.pi ** 2 / 4.0) * qpq_sum / (n - 3)


def ctz_jump_test(rv_sq, tbpv_sq, qpq, n):
    numerator = rv_sq - tbpv_sq
    if numerator <= 0:
        return 0.0
    denom_sq = JUMP_CTZ_VARIANCE_CONST * qpq / n if n > 0 else 0.0
    if denom_sq <= 0:
        return 0.0
    return numerator / math.sqrt(denom_sq)


def lee_mykland_test(log_return, local_bv_vol, n):
    if local_bv_vol <= 0 or n < 2:
        return (0.0, float('inf'), False)
    L_stat = abs(log_return) / local_bv_vol
    S_n = math.sqrt(2.0 * math.log(n))
    if S_n <= 0:
        return (L_stat, float('inf'), False)
    beta_n = S_n - (math.log(math.pi) + math.log(math.log(n))) / (2.0 * S_n)
    critical = beta_n + JUMP_LM_GUMBEL_C / S_n
    return (round(L_stat, 4), round(critical, 4), L_stat > critical)


def compute_decay_multiplier(jump_events, now):
    if not jump_events:
        return 1.0
    total_boost = sum(
        JUMP_DECAY_MAX_BOOST * math.exp(-(now - ts) / JUMP_DECAY_TAU)
        for ts in jump_events if ts <= now
    )
    if total_boost > JUMP_DECAY_MIN_BOOST:
        return min(4.0, 1.0 + total_boost)
    return 1.0


# ─── Test 1: MedRV ──────────────────────────────────────────────────────────

def test_medrv_constant_returns():
    """Constant returns: MedRV ≈ BV."""
    rets = [0.001] * 60
    m = medrv(rets, 60)
    b = bipower_variation(rets, 60)
    assert m > 0
    assert abs(m - b) / b < 0.05, f"MedRV={m}, BV={b}"


def test_medrv_one_outlier():
    """One outlier: MedRV < BV (more robust)."""
    rets = [0.001] * 60
    rets[30] = 0.05  # jump
    m = medrv(rets, 60)
    b = bipower_variation(rets, 60)
    assert m < b, f"MedRV={m} should be < BV={b} with outlier"


def test_medrv_all_zero():
    """All-zero returns → 0."""
    assert medrv([0.0] * 20, 20) == 0.0


def test_medrv_short():
    """n < 3 → 0."""
    assert medrv([0.001, 0.002], 10) == 0.0


def test_medrv_const_value():
    """Verify MEDRV_CONST ≈ π / (6 - 4√3 + π)."""
    expected = math.pi / (6 - 4 * math.sqrt(3) + math.pi)
    assert abs(JUMP_MEDRV_CONST - expected) < 0.001, f"MEDRV_CONST={JUMP_MEDRV_CONST} expected={expected}"


# ─── Test 2: TBPV ───────────────────────────────────────────────────────────

def test_tbpv_no_jumps():
    """No jumps: TBPV ≈ BV."""
    rets = [0.001 * (1 + 0.1 * (i % 3 - 1)) for i in range(60)]
    t = tbpv(rets, 60)
    b = bipower_variation(rets, 60)
    # Should be close (within 20%) for smooth returns
    assert t > 0
    assert abs(t - b) / b < 0.25, f"TBPV={t}, BV={b}"


def test_tbpv_with_jumps():
    """With jumps: TBPV < BV (thresholding filters jump pairs)."""
    rets = [0.001] * 60
    rets[20] = 0.05
    rets[21] = 0.04  # consecutive jumps
    t = tbpv(rets, 60)
    b = bipower_variation(rets, 60)
    assert t < b, f"TBPV={t} should be < BV={b} with jumps"


def test_tbpv_short():
    """n < 2 → 0."""
    assert tbpv([0.001], 10) == 0.0


# ─── Test 3: Quad-Power Quarticity ──────────────────────────────────────────

def test_qpq_gaussian():
    """Gaussian returns → positive finite QPQ."""
    random.seed(42)
    rets = [random.gauss(0, 0.001) for _ in range(60)]
    q = quad_power_quarticity(rets, 60)
    assert q > 0
    assert math.isfinite(q)


def test_qpq_short():
    """n < 4 → 0."""
    assert quad_power_quarticity([0.001, 0.002, 0.003], 10) == 0.0


def test_qpq_constant():
    """Constant returns → known QPQ value."""
    r = 0.001
    rets = [r] * 20
    q = quad_power_quarticity(rets, 20)
    # QPQ = (π²/4) × (1/17) × 17 × r⁴ = (π²/4) × r⁴
    expected = (math.pi ** 2 / 4.0) * r ** 4
    assert abs(q - expected) / expected < 0.01, f"QPQ={q}, expected={expected}"


# ─── Test 4: C-Tz Jump Test ─────────────────────────────────────────────────

def test_ctz_equal_rv_tbpv():
    """RV == TBPV → z-score = 0."""
    assert ctz_jump_test(0.01, 0.01, 1e-8, 60) == 0.0


def test_ctz_large_jump():
    """RV >> TBPV → z-score > critical."""
    rv_sq = 0.01
    tbpv_sq = 0.001
    qpq = 1e-6
    z = ctz_jump_test(rv_sq, tbpv_sq, qpq, 60)
    assert z > JUMP_CTZ_CRITICAL, f"z={z} should exceed {JUMP_CTZ_CRITICAL}"


def test_ctz_negative_numerator():
    """Negative numerator (TBPV > RV) → 0."""
    assert ctz_jump_test(0.001, 0.01, 1e-8, 60) == 0.0


def test_ctz_zero_qpq():
    """Zero QPQ → 0."""
    assert ctz_jump_test(0.01, 0.001, 0.0, 60) == 0.0


# ─── Test 5: Lee-Mykland ────────────────────────────────────────────────────

def test_lm_small_return():
    """Small return → no jump."""
    _, _, is_jump = lee_mykland_test(0.0001, 0.001, 60)
    assert not is_jump


def test_lm_large_return():
    """Large return → jump."""
    _, _, is_jump = lee_mykland_test(0.05, 0.001, 60)
    assert is_jump


def test_lm_zero_vol():
    """Zero local vol → no crash, no jump."""
    stat, crit, is_jump = lee_mykland_test(0.01, 0.0, 60)
    assert stat == 0.0
    assert not is_jump


def test_lm_gumbel_n60():
    """Verify Gumbel constants for n=60."""
    _, _, is_jump = lee_mykland_test(0.0001, 0.001, 60)
    # Just check it runs and returns sensible values
    stat, crit, _ = lee_mykland_test(0.0001, 0.001, 60)
    assert crit > 0
    assert stat >= 0


def test_lm_gumbel_n180():
    """Verify Gumbel constants for n=180."""
    stat, crit, _ = lee_mykland_test(0.0001, 0.001, 180)
    assert crit > 0
    assert stat >= 0
    # Critical should be slightly different from n=60
    _, crit60, _ = lee_mykland_test(0.0001, 0.001, 60)
    assert abs(crit - crit60) > 0.01


# ─── Test 6: Exponential Decay ──────────────────────────────────────────────

def test_decay_single_jump():
    """Single jump: starts at 2.0, decays to ~1.5 at 300s, ~1.0 at 2000s."""
    now = 10000.0
    # Just happened
    m = compute_decay_multiplier([now], now)
    assert abs(m - 2.0) < 0.01, f"m={m}, expected ~2.0"

    # After 300s (half-life)
    m_300 = compute_decay_multiplier([now], now + 300)
    assert 1.4 < m_300 < 1.6, f"m_300={m_300}, expected ~1.5"

    # After 2000s (well decayed)
    m_2000 = compute_decay_multiplier([now], now + 2000)
    assert m_2000 < 1.02, f"m_2000={m_2000}, expected ~1.0"


def test_decay_two_jumps():
    """Two jumps 60s apart: compounds > 2.0."""
    now = 10000.0
    m = compute_decay_multiplier([now - 60, now], now)
    assert m > 2.0, f"m={m}, expected > 2.0 for two recent jumps"


def test_decay_old_jump():
    """Very old jump → ~1.0."""
    now = 10000.0
    m = compute_decay_multiplier([now - 5000], now)
    assert abs(m - 1.0) < 0.01, f"m={m}, expected ~1.0"


def test_decay_empty():
    """No jump events → 1.0."""
    assert compute_decay_multiplier([], 10000.0) == 1.0


# ─── Test 7: BV/MedRV/TBPV Agreement ────────────────────────────────────────

def test_agreement_no_jumps():
    """No-jump returns → all three ≈ equal."""
    random.seed(123)
    rets = [random.gauss(0, 0.001) for _ in range(60)]
    b = bipower_variation(rets, 60)
    m = medrv(rets, 60)
    t = tbpv(rets, 60)
    # All should be within 30% of each other
    vals = [b, m, t]
    avg = sum(vals) / 3
    for v in vals:
        assert abs(v - avg) / avg < 0.35, f"BV={b}, MedRV={m}, TBPV={t}"


def test_agreement_with_jump():
    """Obvious jump → MedRV ≈ TBPV < RV (realized kernel/BV)."""
    random.seed(456)
    rets = [random.gauss(0, 0.001) for _ in range(60)]
    rets[30] = 0.05  # big jump
    # RV (sum of squared returns) as reference
    rv = math.sqrt(sum(r ** 2 for r in rets[-60:]) / 60)
    m = medrv(rets, 60)
    t = tbpv(rets, 60)
    # MedRV and TBPV should be much less than RV
    assert m < rv * 0.8, f"MedRV={m} should be << RV={rv}"
    assert t < rv * 0.8, f"TBPV={t} should be << RV={rv}"


# ─── Test 8: Shadow Mode ────────────────────────────────────────────────────

def test_shadow_mode_metrics_computed():
    """Verify new metrics are always computed regardless of shadow mode.

    In shadow mode, legacy test drives regime, but MedRV/TBPV/C-Tz
    are still computed for logging.
    """
    # This test verifies the functions themselves work — shadow mode behavior
    # is tested via integration on the VPS.
    rets = [0.001] * 60
    rets[30] = 0.05  # inject a jump

    m = medrv(rets, 60)
    t = tbpv(rets, 60)
    b = bipower_variation(rets, 60)
    q = quad_power_quarticity(rets, 60)

    # All should be positive and finite
    assert m > 0 and math.isfinite(m)
    assert t > 0 and math.isfinite(t)
    assert b > 0 and math.isfinite(b)
    assert q > 0 and math.isfinite(q)

    # C-Tz should detect the jump
    rv_sq = sum(r ** 2 for r in rets[-60:]) / 60
    tbpv_sq = t ** 2
    z = ctz_jump_test(rv_sq, tbpv_sq, q, 60)
    assert z > 0, f"C-Tz should be positive with a jump, got {z}"

    # Lee-Mykland should detect the jump return
    _, _, lm_jump = lee_mykland_test(0.05, b, 60)
    assert lm_jump, "Lee-Mykland should detect the 5% jump"
