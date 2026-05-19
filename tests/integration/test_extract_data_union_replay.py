"""Bit B (86ba0jn0w) of HYPE/DOGE cal_mlp v1.1 retrain umbrella 86ba0jmyq.

**SCOPE RETRACTED post-R1:** The original Bit B "UNION across
evaluated_opportunities + historical_replay_calmlp" approach REGRESSED on
the two-extractor architecture shipped at P2.1.a-3 (ticket `86b9wuhhr`,
commit `3a9d690a`). The canonical HYPE/DOGE replay extractor is
`scripts/cal_mlp/extract_data_replay.py` with its own
`compute_cfg_fp_replay` namespace; production `extract_data.py` keeps
the strict 32-column NULL contract for `evaluated_opportunities` rows.

Bit B (post-R1) keeps the safe scope only:

- Widens `--asset` choices on `extract_data.py` from
  `['BTC','ETH','SOL','XRP']` to
  `['BTC','ETH','SOL','XRP','HYPE','DOGE']` so HYPE/DOGE *live* rows in
  `evaluated_opportunities` can flow through the production recipe.
- HYPE/DOGE *replay* rows continue to flow through
  `extract_data_replay.py` unchanged.

Plan doc: `kb/decisions/v1-1-B-extract-union-plan.md` (post-R1 retraction
+ L99 STALE_PATTERNS section).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "cal_mlp"))


# ── CLI surface: --asset accepts HYPE + DOGE (alongside BTC/ETH/SOL/XRP) ──


@pytest.mark.parametrize("asset", ["HYPE", "DOGE", "BTC", "ETH", "SOL", "XRP"])
def test_parse_args_accepts_asset(asset, monkeypatch):
    """--asset must accept HYPE + DOGE alongside BTC/ETH/SOL/XRP."""
    import extract_data
    monkeypatch.setattr(sys, "argv", ["extract_data.py", "--asset", asset])
    ns = extract_data.parse_args()
    assert ns.asset == asset


def test_parse_args_rejects_unknown_asset(monkeypatch):
    """Sanity: --asset still rejects garbage (LINK, ADA, etc.)."""
    import extract_data
    monkeypatch.setattr(sys, "argv", ["extract_data.py", "--asset", "LINK"])
    with pytest.raises(SystemExit):
        extract_data.parse_args()


# ── Anti-regression: no UNION leak into the production path ─────────


def test_extract_data_has_no_replay_table_sql_reads():
    """Anti-regression: production `extract_data.py` must NOT issue SQL
    reads against `historical_replay_calmlp`. The HYPE/DOGE replay
    corpus is owned by `extract_data_replay.py` (P2.1.a-3, ticket
    `86b9wuhhr`) with its own `compute_cfg_fp_replay` namespace. A UNION
    here would regress on that architecture and trip
    `build_feature_frame`'s NULL contract once `market_price` is
    backfilled (Bit E + sister).

    Allows docstring references (we intentionally point readers at
    `extract_data_replay.py` from the docstring), forbids SQL reads."""
    src_path = PROJECT_ROOT / "scripts" / "cal_mlp" / "extract_data.py"
    src = src_path.read_text()
    forbidden_patterns = [
        "FROM historical_replay_calmlp",
        'name=\'historical_replay_calmlp\'',
        'name="historical_replay_calmlp"',
        "_replay_table_exists",
        "_replay_select_for_eval_shape",
    ]
    found = [p for p in forbidden_patterns if p in src]
    assert not found, (
        f"{src_path}: production extractor must not SQL-read the replay "
        f"table — that's `extract_data_replay.py`'s responsibility. "
        f"Forbidden patterns found: {found}. See "
        "kb/decisions/v1-1-B-extract-union-plan.md L99 STALE patterns."
    )


def test_extract_data_replay_still_exists():
    """Sanity: the canonical HYPE/DOGE replay extractor must still exist —
    Bit B's widening of `extract_data.py --asset` is COMPLEMENTARY to
    `extract_data_replay.py`, not a replacement."""
    src_path = PROJECT_ROOT / "scripts" / "cal_mlp" / "extract_data_replay.py"
    assert src_path.is_file(), (
        f"Canonical HYPE/DOGE replay extractor missing at {src_path}. "
        "P2.1.a-3 (ticket `86b9wuhhr`, commit `3a9d690a`) is load-bearing "
        "for the umbrella `86ba0jmyq` v1.1 retrain."
    )
