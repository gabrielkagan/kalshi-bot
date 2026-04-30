"""R-p7-deploy-r11: regression — run_pipeline.sh must default to
`--include-sub-floor` for v2/v3-and-beyond training runs.

Background (kb/concepts/calibrator-data-hygiene-apr29.md):

The bot collects sub-floor shadow data via three filter_stages:
  - low_price_shadow      (70-79¢, all assets)
  - floor_raise_shadow    (75¢ → MIN_ENTRY-1¢, BTC/SOL/XRP)
  - eth_low_floor_shadow  (75¢ → MIN_ENTRY-1¢, ETH-specific label)

Each is settled with `market_result` populated and ~92-100% feature-clean
(same NULL pattern as decided rows). HOWEVER, scripts/cal_mlp/extract_data.py
applies a per-asset floor from features.py:ASSET_FLOORS (BTC 88 / ETH 90 /
SOL 86 / XRP 92) by default. Rows below that get bucketed into
'below_asset_floor' and DISCARDED.

Without `--include-sub-floor`, every shadow row we collect is wasted at
training time. v2 K=1 (May 19) and v3 K=2 (June 22) both NEED the
sub-floor data to train an effective MIN_ENTRY-lowering safety net.

This test locks the pipeline default at INCLUDE_SUB_FLOOR=1 and asserts
the flag is wired through to the extract command.
"""
from pathlib import Path
import re

import pytest


PIPELINE = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp' / 'run_pipeline.sh'


def _read():
    if not PIPELINE.exists():
        pytest.skip(f"{PIPELINE} not present")
    return PIPELINE.read_text()


def test_pipeline_default_includes_sub_floor():
    """The shell var INCLUDE_SUB_FLOOR must default to 1.
    Operators wanting v1 cfg_fp must explicitly opt out.
    """
    src = _read()
    assert re.search(
        r'INCLUDE_SUB_FLOOR="\$\{INCLUDE_SUB_FLOOR:-1\}"',
        src,
    ), (
        "run_pipeline.sh must default INCLUDE_SUB_FLOOR=1. "
        "Default-off would silently discard floor_raise_shadow and "
        "eth_low_floor_shadow data at v2/v3 training time."
    )


def test_pipeline_passes_flag_when_enabled():
    """When INCLUDE_SUB_FLOOR=1, the script must build SUB_FLOOR_FLAG
    that contains '--include-sub-floor' and pass it to extract_data.py."""
    src = _read()
    # Build branch
    assert re.search(
        r'if\s+\[\s+"\$INCLUDE_SUB_FLOOR"\s*=\s*"1"\s+\];\s*then\s*\n\s*SUB_FLOOR_FLAG="--include-sub-floor"',
        src,
    ), (
        "run_pipeline.sh must set SUB_FLOOR_FLAG=\"--include-sub-floor\" "
        "when INCLUDE_SUB_FLOOR=1."
    )
    # Empty branch
    assert re.search(
        r'else\s*\n\s*SUB_FLOOR_FLAG=""',
        src,
    ), (
        "run_pipeline.sh must set SUB_FLOOR_FLAG=\"\" when INCLUDE_SUB_FLOOR=0 "
        "to support legacy v1 cfg_fp reproduction."
    )


def test_pipeline_threads_flag_into_extract():
    """The extract invocation must use $SUB_FLOOR_FLAG. If the variable
    is set but never threaded into the command, the flag is dead code
    and the data is still discarded.

    Bash `\\` line-continuations mean the command can span lines, so we
    reconstruct the logical command by joining continued lines and then
    look for $SUB_FLOOR_FLAG inside the extract_data.py invocation.
    """
    src = _read()
    # Join lines ending with `\` into a single logical line.
    logical_lines: list[str] = []
    buf = ''
    for line in src.splitlines():
        stripped_right = line.rstrip()
        if stripped_right.endswith('\\'):
            buf += stripped_right[:-1] + ' '
        else:
            logical_lines.append(buf + line)
            buf = ''
    if buf:
        logical_lines.append(buf)

    extract_logical = [l for l in logical_lines if 'extract_data.py' in l and 'python' in l]
    assert extract_logical, "extract_data.py invocation not found in pipeline"
    assert any('$SUB_FLOOR_FLAG' in l for l in extract_logical), (
        "extract_data.py invocation must include $SUB_FLOOR_FLAG. "
        "Variable defined but not threaded through = dead code; "
        "training would still discard sub-floor data."
    )


def test_extract_data_accepts_include_sub_floor_flag():
    """The downstream extract_data.py must actually have an
    --include-sub-floor argument that lowers the floor. If this flag
    silently dropped from the CLI, the pipeline arg would error or be
    ignored and we'd be back to silent data loss.
    """
    extract = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp' / 'extract_data.py'
    if not extract.exists():
        pytest.skip("extract_data.py not present")
    src = extract.read_text()
    assert "add_argument('--include-sub-floor'" in src, (
        "extract_data.py must accept --include-sub-floor CLI flag for the "
        "pipeline's INCLUDE_SUB_FLOOR=1 default to be meaningful."
    )


def test_features_module_supports_sub_floor():
    """features.py must expose include_sub_floor in its asset_min_price
    interface so the extract floor actually shifts when the flag is on.
    AST-style guard against silent removal of the GLOBAL_MIN_ENTRY_PRICE
    fallback."""
    import sys
    cal_mlp = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp'
    if str(cal_mlp) not in sys.path:
        sys.path.insert(0, str(cal_mlp))
    import features  # noqa: E402
    assert hasattr(features, 'GLOBAL_MIN_ENTRY_PRICE')
    assert features.GLOBAL_MIN_ENTRY_PRICE == 75, (
        "GLOBAL_MIN_ENTRY_PRICE was 75 when low_price_shadow was scoped to 70¢+ "
        "(bot.py:LOW_PRICE_SHADOW_MIN_PRICE). Changing it loses sub-floor coverage."
    )
    # asset_min_price contract: include_sub_floor=True returns GLOBAL_MIN_ENTRY_PRICE
    # for every asset; False returns ASSET_FLOORS lookup.
    for asset in ('BTC', 'ETH', 'SOL', 'XRP'):
        assert features.asset_min_price(asset, include_sub_floor=True) == 75
        assert features.asset_min_price(asset, include_sub_floor=False) == features.ASSET_FLOORS[asset]
