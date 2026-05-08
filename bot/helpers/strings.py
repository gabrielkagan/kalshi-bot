"""Bit 3.2: String/numeric coercion helpers, extracted from bot/_impl.py."""
# ── FP / Dollar String Helpers ──────────────────────────────────────────────
def dollars_str_to_cents(s) -> int:
    """Convert dollar string like '0.8800' to integer cents (88)."""
    if s is None:
        return 0
    return round(float(s) * 100)


def cents_to_dollars_str(cents: int) -> str:
    """Convert integer cents (88) to dollar string '0.8800'."""
    return f"{cents / 100:.4f}"


def fp_str_to_int(s) -> int:
    """Convert FP string like '5.00' to integer (5)."""
    if s is None:
        return 0
    return int(round(float(s)))


def int_to_fp_str(n: int) -> str:
    """Convert integer (5) to FP string '5.00'."""
    return f"{n:.2f}"
