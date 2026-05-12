"""Per-asset CALMLP_BUNDLE_DIR override (Phase 1a of v2 asymmetric rollout).

Plan: kb/decisions/v2-asymmetric-rollout-and-xrp-rca-may05.md
Resume: kb/decisions/session-resume-may06-from-phase0-closure.md

Precedence (resolved in `integration._resolve_bundle_dir`):
  1. CALMLP_BUNDLE_DIR_<ASSET_UPPER>  (per-asset, takes precedence if non-empty after strip)
  2. CALMLP_BUNDLE_DIR                (global fallback; <ASSET> substitution still done by caller)
  3. ""                               (no override; _load reads CURRENT file)

The resolver returns a RAW string. <ASSET> substitution and relative-path
resolution against project_root continue to live in `_load` (unchanged).

Companion to tests/integration/test_cal_mlp_invariants.py (global-only kill-switch tests
from May 4 ship). Existing global-fallback semantics MUST remain intact.
"""
import ast
import os
import sys
from pathlib import Path

import pytest

_CAL_MLP_DIR = Path(__file__).resolve().parents[2] / 'scripts' / 'cal_mlp'
if str(_CAL_MLP_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_MLP_DIR))


_KNOWN_ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
_ALLOWED_BUNDLE_DIR_ENV_VARS = (
    {'CALMLP_BUNDLE_DIR'} | {f'CALMLP_BUNDLE_DIR_{a}' for a in _KNOWN_ASSETS}
)


@pytest.fixture(autouse=True)
def _isolate_calmlp_bundle_env(monkeypatch):
    """Operator-env hygiene: a developer running tests with
    CALMLP_BUNDLE_DIR_<ASSET> exported in their shell (the exact pattern
    Phase 1a ships) would otherwise see kill-switch tests fail because
    per-asset overrides global. Clear all variants before every test."""
    for var in _ALLOWED_BUNDLE_DIR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Resolver unit tests (pure — no _load, no fs IO).
# ---------------------------------------------------------------------------

def test_resolver_per_asset_wins_over_global(monkeypatch):
    """Per-asset env var takes precedence when both are set.
    This is the load-bearing precedence rule for asymmetric rollout —
    operator sets CALMLP_BUNDLE_DIR_ETH=v2 while leaving global at v1
    so only ETH gets the new bundle."""
    import integration
    monkeypatch.setenv('CALMLP_BUNDLE_DIR_ETH', '/per-asset/path')
    monkeypatch.setenv('CALMLP_BUNDLE_DIR', '/global/path')
    assert integration._resolve_bundle_dir('ETH') == '/per-asset/path'


def test_resolver_falls_back_to_global_when_per_asset_unset(monkeypatch):
    """Per-asset unset → global value returned. Preserves the May 4
    kill-switch contract: setting only CALMLP_BUNDLE_DIR still rolls
    back all assets via the existing <ASSET> substitution downstream."""
    import integration
    monkeypatch.delenv('CALMLP_BUNDLE_DIR_BTC', raising=False)
    monkeypatch.setenv('CALMLP_BUNDLE_DIR', '/global/path')
    assert integration._resolve_bundle_dir('BTC') == '/global/path'


def test_resolver_returns_empty_when_neither_set(monkeypatch):
    """Both unset → empty string. Caller treats empty as 'no override'
    and reads the CURRENT file (existing behavior)."""
    import integration
    monkeypatch.delenv('CALMLP_BUNDLE_DIR_SOL', raising=False)
    monkeypatch.delenv('CALMLP_BUNDLE_DIR', raising=False)
    assert integration._resolve_bundle_dir('SOL') == ''


def test_resolver_uppercases_asset_for_env_lookup(monkeypatch):
    """Env-var name is always uppercase. Caller may pass mixed-case asset
    (bot/_impl.py uses uppercase but defensive normalization avoids a silent
    fall-through to global if a future caller passes 'eth')."""
    import integration
    monkeypatch.setenv('CALMLP_BUNDLE_DIR_ETH', '/per-asset/eth')
    monkeypatch.delenv('CALMLP_BUNDLE_DIR', raising=False)
    assert integration._resolve_bundle_dir('eth') == '/per-asset/eth'
    assert integration._resolve_bundle_dir('Eth') == '/per-asset/eth'


def test_resolver_empty_or_whitespace_per_asset_falls_back(monkeypatch):
    """Empty / whitespace-only per-asset value treated as unset
    (mirrors the .strip() pattern from the global env var)."""
    import integration
    monkeypatch.setenv('CALMLP_BUNDLE_DIR_XRP', '   ')
    monkeypatch.setenv('CALMLP_BUNDLE_DIR', '/global/path')
    assert integration._resolve_bundle_dir('XRP') == '/global/path'

    monkeypatch.setenv('CALMLP_BUNDLE_DIR_XRP', '')
    assert integration._resolve_bundle_dir('XRP') == '/global/path'


def test_resolver_strips_whitespace_around_value(monkeypatch):
    """Both per-asset and global values get .strip()'d — operators
    occasionally include trailing whitespace (esp. via shell scripts).
    Mirrors the existing CALMLP_BUNDLE_DIR + CALMLP_ENABLED pattern."""
    import integration
    monkeypatch.setenv('CALMLP_BUNDLE_DIR_BTC', '  /per-asset/btc  ')
    monkeypatch.delenv('CALMLP_BUNDLE_DIR', raising=False)
    assert integration._resolve_bundle_dir('BTC') == '/per-asset/btc'

    monkeypatch.delenv('CALMLP_BUNDLE_DIR_BTC', raising=False)
    monkeypatch.setenv('CALMLP_BUNDLE_DIR', '\t/global\n')
    assert integration._resolve_bundle_dir('BTC') == '/global'


def test_resolver_does_not_substitute_asset_placeholder(monkeypatch):
    """Resolver returns raw string. <ASSET> substitution stays in _load
    (post-resolution, applied uniformly to whatever the resolver returned).
    Pinning this contract prevents an accidental refactor where the
    resolver substitutes and the caller substitutes again — empty
    substitution would silently leave the placeholder in errors."""
    import integration
    monkeypatch.setenv('CALMLP_BUNDLE_DIR', 'models/cal_mlp_<ASSET>/abc')
    monkeypatch.delenv('CALMLP_BUNDLE_DIR_ETH', raising=False)
    out = integration._resolve_bundle_dir('ETH')
    assert out == 'models/cal_mlp_<ASSET>/abc'
    assert '<ASSET>' in out


def test_resolver_empty_asset_uses_global_only(monkeypatch):
    """Defensive: empty / whitespace asset string. Per-asset key would
    be 'CALMLP_BUNDLE_DIR_' (malformed) so we skip per-asset lookup
    and return global. Upstream _load fails on missing models_dir
    via existing path."""
    import integration
    monkeypatch.setenv('CALMLP_BUNDLE_DIR', '/global/path')
    monkeypatch.delenv('CALMLP_BUNDLE_DIR_', raising=False)
    assert integration._resolve_bundle_dir('') == '/global/path'
    assert integration._resolve_bundle_dir('   ') == '/global/path'


# ---------------------------------------------------------------------------
# Integration tests — _load uses resolver, full env precedence honored.
# ---------------------------------------------------------------------------

def test_load_per_asset_does_not_leak_across_assets(tmp_path, monkeypatch):
    """CALMLP_BUNDLE_DIR_ETH set, but loading BTC must NOT see the ETH
    override. BTC falls through to its CURRENT file. This is the load-
    bearing isolation guarantee for asymmetric rollout — the whole point
    of per-asset overrides."""
    import integration
    eth_dir = tmp_path / 'models' / 'cal_mlp_ETH'
    eth_dir.mkdir(parents=True)
    btc_dir = tmp_path / 'models' / 'cal_mlp_BTC'
    btc_dir.mkdir(parents=True)
    # No CURRENT file for BTC → expect no_current via CURRENT path,
    # NOT via the ETH override path.
    eth_override = eth_dir / 'eth_train_id'
    eth_override.mkdir()
    monkeypatch.setenv('CALMLP_BUNDLE_DIR_ETH', str(eth_override))
    monkeypatch.delenv('CALMLP_BUNDLE_DIR_BTC', raising=False)
    monkeypatch.delenv('CALMLP_BUNDLE_DIR', raising=False)
    monkeypatch.setenv('CALMLP_ENABLED', '1')

    pred = integration.CalMLPPredictor('BTC', project_root=tmp_path)
    with pytest.raises(integration.CalMLPError) as exc:
        pred._load()
    assert exc.value.code == 'no_current'
    msg = str(exc.value)
    assert 'CURRENT' in msg, (
        f"BTC must hit CURRENT path (not ETH override); got: {msg}"
    )
    assert 'eth_train_id' not in msg, (
        f"BTC leaked the ETH override path: {msg}"
    )


def test_load_per_asset_overrides_global_in_load(tmp_path, monkeypatch):
    """Both per-asset and global set → per-asset wins through full _load
    path. Verified by error message referencing the per-asset path
    (not the global path)."""
    import integration
    asset_dir = tmp_path / 'models' / 'cal_mlp_ETH'
    asset_dir.mkdir(parents=True)
    (asset_dir / 'CURRENT').write_text('whatever\n')
    per_asset = tmp_path / 'models' / 'cal_mlp_ETH' / 'per_asset_id_DNE'
    monkeypatch.setenv('CALMLP_BUNDLE_DIR_ETH', str(per_asset))
    monkeypatch.setenv(
        'CALMLP_BUNDLE_DIR',
        str(tmp_path / 'models' / 'cal_mlp_<ASSET>' / 'global_id_DNE'),
    )
    monkeypatch.setenv('CALMLP_ENABLED', '1')

    pred = integration.CalMLPPredictor('ETH', project_root=tmp_path)
    with pytest.raises(integration.CalMLPError) as exc:
        pred._load()
    msg = str(exc.value)
    assert 'per_asset_id_DNE' in msg, (
        f"per-asset must win in _load; got: {msg}"
    )
    assert 'global_id_DNE' not in msg, (
        f"global path leaked despite per-asset being set: {msg}"
    )


def test_load_per_asset_missing_directory_raises(tmp_path, monkeypatch):
    """Per-asset override pointing to a nonexistent directory must
    fail with no_current (mirroring global override behavior)."""
    import integration
    asset_dir = tmp_path / 'models' / 'cal_mlp_SOL'
    asset_dir.mkdir(parents=True)
    (asset_dir / 'CURRENT').write_text('ignored\n')
    bad = asset_dir / 'sol_DNE_id'
    monkeypatch.setenv('CALMLP_BUNDLE_DIR_SOL', str(bad))
    monkeypatch.delenv('CALMLP_BUNDLE_DIR', raising=False)
    monkeypatch.setenv('CALMLP_ENABLED', '1')

    pred = integration.CalMLPPredictor('SOL', project_root=tmp_path)
    with pytest.raises(integration.CalMLPError) as exc:
        pred._load()
    assert exc.value.code == 'no_current'
    assert 'sol_DNE_id' in str(exc.value)


def test_load_per_asset_substitutes_asset_placeholder(tmp_path, monkeypatch):
    """Pin contract: <ASSET> substitution applies UNIFORMLY post-resolution.
    A per-asset override containing <ASSET> still gets substituted in _load
    (operators occasionally copy-paste the global template into the per-asset
    slot). Substitution lives in ONE place; resolver returns raw string.
    A future refactor that moves substitution into a global-only branch
    (rationale: "per-asset already names the asset") would silently break
    this. Test fails loudly on that drift."""
    import integration
    asset_dir = tmp_path / 'models' / 'cal_mlp_ETH'
    asset_dir.mkdir(parents=True)
    (asset_dir / 'CURRENT').write_text('whatever\n')
    monkeypatch.setenv(
        'CALMLP_BUNDLE_DIR_ETH',
        str(tmp_path / 'models' / 'cal_mlp_<ASSET>' / 'pinned_v2_id_DNE'),
    )
    monkeypatch.setenv('CALMLP_ENABLED', '1')

    pred = integration.CalMLPPredictor('ETH', project_root=tmp_path)
    with pytest.raises(integration.CalMLPError) as exc:
        pred._load()
    msg = str(exc.value)
    assert 'cal_mlp_ETH' in msg, (
        f"per-asset override <ASSET> must be substituted; got: {msg}"
    )
    assert '<ASSET>' not in msg, f"placeholder leaked unsubstituted: {msg}"
    assert 'pinned_v2_id_DNE' in msg


def test_load_lowercase_asset_substitution_uses_raw_self_asset(
    tmp_path, monkeypatch,
):
    """Pin contract: `<ASSET>` substitution at integration.py uses
    `self.asset` RAW (no case normalization). The resolver uppercases
    only for env-var lookup; all other `_load` paths use `self.asset` as
    given. This test reaches the substitution site and verifies the
    substituted token in the error message.

    Why this matters: a refactor that uppercases the substitution alone
    (e.g. `replace('<ASSET>', self.asset.upper())`) without also
    uppercasing `models_dir` produces silent train/serve skew — model
    loaded from `models/cal_mlp_eth/<id>` but bundle filename expects
    `cal_mlp_ETH_<id>_phase5_bundle.json`. Loud failure modes (typo'd
    path, missing dir) mask this. We pre-create `cal_mlp_eth` so the
    `models_dir.exists()` pre-check passes (note: case-insensitive on
    APFS, case-sensitive on Linux — works on both because the OS
    lookup matches our exact creation), then assert the substituted
    error message contains `cal_mlp_eth` (lowercase, raw), not
    `cal_mlp_ETH` (would surface a regression).
    """
    import integration
    (tmp_path / 'models' / 'cal_mlp_eth').mkdir(parents=True)
    # <ASSET> placeholder + sentinel id — substitution at line ~720
    # produces `cal_mlp_eth/sentinel_DNE` (raw self.asset).
    monkeypatch.setenv(
        'CALMLP_BUNDLE_DIR_ETH',
        str(tmp_path / 'models' / 'cal_mlp_<ASSET>' / 'sentinel_id_DNE'),
    )
    monkeypatch.setenv('CALMLP_ENABLED', '1')

    pred = integration.CalMLPPredictor('eth', project_root=tmp_path)
    with pytest.raises(integration.CalMLPError) as exc:
        pred._load()
    assert exc.value.code == 'no_current'
    msg = str(exc.value)
    # Substitution must use raw self.asset → error path contains
    # 'cal_mlp_eth' (lowercase). A regression to .upper() in the
    # substitution alone would put 'cal_mlp_ETH' here.
    assert 'cal_mlp_eth' in msg, (
        f"<ASSET> substitution must use raw self.asset; got: {msg}"
    )
    assert 'cal_mlp_ETH' not in msg, (
        f"substitution silently uppercased — train/serve skew vector. "
        f"Got: {msg}"
    )
    assert 'sentinel_id_DNE' in msg


def test_load_global_used_for_other_assets_when_only_eth_per_asset_set(
    tmp_path, monkeypatch,
):
    """End-to-end operator scenario for Phase 1a asymmetric rollout.

    Operator pins ETH to v2 via `CALMLP_BUNDLE_DIR_ETH` while leaving
    `CALMLP_BUNDLE_DIR` (global) at v1 — BTC/SOL/XRP must KEEP using
    the global v1 pin via `_load`, not silently slip into the per-asset
    branch. A regression where ANY per-asset env causes the resolver to
    short-circuit globally would not be caught by the resolver-only
    tests OR by the leak-isolation test (which UNSETS the global).
    This test pins the actual mixed-state operator workflow.
    """
    import integration
    (tmp_path / 'models' / 'cal_mlp_BTC').mkdir(parents=True)
    (tmp_path / 'models' / 'cal_mlp_ETH').mkdir(parents=True)

    monkeypatch.setenv(
        'CALMLP_BUNDLE_DIR_ETH',
        str(tmp_path / 'models' / 'cal_mlp_ETH' / 'eth_v2_id_DNE'),
    )
    monkeypatch.setenv(
        'CALMLP_BUNDLE_DIR',
        str(tmp_path / 'models' / 'cal_mlp_<ASSET>' / 'global_v1_id_DNE'),
    )
    monkeypatch.setenv('CALMLP_ENABLED', '1')

    # BTC must resolve via GLOBAL (with <ASSET>=BTC substitution).
    pred = integration.CalMLPPredictor('BTC', project_root=tmp_path)
    with pytest.raises(integration.CalMLPError) as exc:
        pred._load()
    msg = str(exc.value)
    assert 'global_v1_id_DNE' in msg, (
        f"BTC must use global path; got: {msg}"
    )
    assert 'cal_mlp_BTC' in msg, (
        f"<ASSET> must be substituted with BTC for BTC's load; got: {msg}"
    )
    assert 'eth_v2_id_DNE' not in msg, (
        f"BTC leaked ETH per-asset override (resolver short-circuited "
        f"to per-asset for non-ETH asset): {msg}"
    )


def test_load_per_asset_rejects_file_path(tmp_path, monkeypatch):
    """Per-asset override pointing to a regular file (operator typo,
    e.g. someone pointed at the bundle JSON itself) must fail with
    'not a directory' — same contract as global override."""
    import integration
    asset_dir = tmp_path / 'models' / 'cal_mlp_XRP'
    asset_dir.mkdir(parents=True)
    typo = asset_dir / 'bundle.json'
    typo.write_text('{}')
    monkeypatch.setenv('CALMLP_BUNDLE_DIR_XRP', str(typo))
    monkeypatch.delenv('CALMLP_BUNDLE_DIR', raising=False)
    monkeypatch.setenv('CALMLP_ENABLED', '1')

    pred = integration.CalMLPPredictor('XRP', project_root=tmp_path)
    with pytest.raises(integration.CalMLPError) as exc:
        pred._load()
    msg = str(exc.value).lower()
    assert 'not a directory' in msg, (
        f"file-path typo on per-asset must fail with 'not a directory'; "
        f"got: {msg}"
    )


# ---------------------------------------------------------------------------
# AST guard — every read of CALMLP_BUNDLE_DIR* must go through resolver.
# ---------------------------------------------------------------------------

def _walk_bundle_dir_offenders(tree: ast.AST) -> list:
    """Single source of truth for AST guard logic. Returns a list of
    `(form, function_name, env_var_name, lineno)` tuples for every
    CALMLP_BUNDLE_DIR* env-var read OUTSIDE `_resolve_bundle_dir`.

    Used by both the production guard (against integration.py) and the
    self-test (against a synthesized fake module). Single source of truth
    avoids the drift hazard of two parallel walkers.

    Coverage of bypass forms:
      - os.environ.get('NAME', ...) / os.getenv('NAME', ...)  [Call+Constant]
      - os.environ['NAME']                                     [Subscript+Constant]
      - os.environ.get(f'CALMLP_BUNDLE_DIR_{x}')                [Call+JoinedStr]
      - os.environ.get('CALMLP_BUNDLE_DIR' + suffix)            [Call+BinOp]

    Allowlist is explicit (`_ALLOWED_BUNDLE_DIR_ENV_VARS`): adding a 5th
    asset requires updating `_KNOWN_ASSETS` — a deliberate decision, not
    a silent expansion of the kill-switch surface.
    """
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_func(n: ast.AST) -> str:
        cur = parents.get(n)
        while cur is not None:
            if isinstance(cur, ast.FunctionDef):
                return cur.name
            cur = parents.get(cur)
        return '<module>'

    def is_environ_attr(v: ast.AST) -> bool:
        if isinstance(v, ast.Attribute) and v.attr == 'environ':
            return True
        if isinstance(v, ast.Name) and v.id == 'environ':
            return True
        return False

    def first_literal(arg: ast.AST):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.JoinedStr) and arg.values:
            head = arg.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                return head.value
        if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
            return first_literal(arg.left)
        return None

    def matches(arg: ast.AST) -> str:
        """Symmetric prefix match: ANY literal starting with
        'CALMLP_BUNDLE_DIR' is flagged regardless of node type. The
        allowlist's purpose is to ensure the *suffix* matches a known
        asset (BTC/ETH/SOL/XRP) — but an unknown suffix read OUTSIDE
        the resolver is itself a drift signal (typo, 5th asset not yet
        in `_KNOWN_ASSETS`, or a hypothetical non-path env var that
        shouldn't share the prefix). Inside-resolver reads are exempted
        by the `enclosing_func != '_resolve_bundle_dir'` filter."""
        lit = first_literal(arg)
        if lit is None or not lit.startswith('CALMLP_BUNDLE_DIR'):
            return ''
        if isinstance(arg, ast.Constant):
            return lit if lit in _ALLOWED_BUNDLE_DIR_ENV_VARS else f'<unknown:{lit}>'
        # f-string / concat leading literal.
        return f'<dynamic:{lit}*>'

    offenders = []
    for node in ast.walk(tree):
        # Form 1+3+4: os.environ.get(NAME, ...) / os.getenv(NAME, ...)
        if isinstance(node, ast.Call) and node.args:
            is_env_read = False
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == 'getenv':
                    is_env_read = True
                elif node.func.attr == 'get' and is_environ_attr(node.func.value):
                    is_env_read = True
            if is_env_read:
                m = matches(node.args[0])
                if m:
                    func = enclosing_func(node)
                    if func != '_resolve_bundle_dir':
                        offenders.append(('Call', func, m, node.lineno))
            continue
        # Form 2: os.environ[NAME]
        if isinstance(node, ast.Subscript) and is_environ_attr(node.value):
            slice_node = node.slice
            # Py3.8 wraps in ast.Index; handle both shapes.
            if hasattr(ast, 'Index') and isinstance(slice_node, ast.Index):
                slice_node = slice_node.value  # type: ignore[attr-defined]
            m = matches(slice_node)
            if m:
                func = enclosing_func(node)
                if func != '_resolve_bundle_dir':
                    offenders.append(('Subscript', func, m, node.lineno))
    return offenders


def test_calmlp_bundle_dir_only_read_inside_resolver():
    """AST guard: every read of CALMLP_BUNDLE_DIR or
    CALMLP_BUNDLE_DIR_{BTC,ETH,SOL,XRP} in scripts/cal_mlp/integration.py
    must occur inside the function `_resolve_bundle_dir`.

    A refactor that adds a second direct env read elsewhere silently
    breaks per-asset precedence (per-asset would apply in one site but
    not the other → mixed behavior). This test fails loudly on that
    drift via `_walk_bundle_dir_offenders` (companion self-test
    `test_calmlp_bundle_dir_ast_guard_self_test` validates that walker).
    """
    src_path = _CAL_MLP_DIR / 'integration.py'
    tree = ast.parse(src_path.read_text())
    offenders = _walk_bundle_dir_offenders(tree)
    assert offenders == [], (
        f"CALMLP_BUNDLE_DIR* must only be read inside _resolve_bundle_dir; "
        f"found direct reads at: {offenders}. "
        f"If adding a new asset, update _KNOWN_ASSETS in this test."
    )


def test_calmlp_bundle_dir_ast_guard_self_test():
    """Self-test: validate `_walk_bundle_dir_offenders` against a
    synthesized fake module containing both halves of the contract:

      - 5 BAD functions covering all 4 syntactic bypass forms
        (Call+Constant, Subscript+Constant, Call+JoinedStr, Call+BinOp,
        Call+Constant via os.getenv) — each MUST appear in offenders
        (matcher half).
      - 1 `_resolve_bundle_dir` function containing a bundle-dir read
        — MUST NOT appear in offenders (filter half).

    Both halves matter:
      - Matcher regression (e.g., dropping JoinedStr support) →
        bad_fstring missing from offenders → assertion fails loudly
        with set-difference report.
      - Filter regression (e.g., inverting `!= '_resolve_bundle_dir'`
        to `==`, or dropping the filter) → resolver appears in
        offenders → second assertion fails loudly.

    Without the resolver fake half, a regression that exempts EVERY
    function (or NO function) from the offender list would silently
    pass. This test pins both ends.
    """
    fake_src = (
        "import os\n"
        "from os import environ\n"
        "def _resolve_bundle_dir(asset):\n"
        "    # Production resolver — env read here MUST be exempted by\n"
        "    # `enclosing_func != '_resolve_bundle_dir'` filter.\n"
        "    return os.environ.get('CALMLP_BUNDLE_DIR_ETH', '')\n"
        "def bad_call():\n"
        "    return os.environ.get('CALMLP_BUNDLE_DIR', '')\n"
        "def bad_subscript():\n"
        "    return os.environ['CALMLP_BUNDLE_DIR_BTC']\n"
        "def bad_fstring(asset):\n"
        "    return os.environ.get(f'CALMLP_BUNDLE_DIR_{asset}', '')\n"
        "def bad_concat(asset):\n"
        "    return os.environ.get('CALMLP_BUNDLE_DIR_' + asset, '')\n"
        "def bad_getenv():\n"
        "    return os.getenv('CALMLP_BUNDLE_DIR_SOL')\n"
        "def bad_name_environ_call():\n"
        "    # `from os import environ` form — Name('environ') branch\n"
        "    # of is_environ_attr. A regression that drops that branch\n"
        "    # would silently miss this read.\n"
        "    return environ.get('CALMLP_BUNDLE_DIR_XRP', '')\n"
        "def bad_name_environ_subscript():\n"
        "    return environ['CALMLP_BUNDLE_DIR_BTC']\n"
        "def bad_unknown_asset():\n"
        "    # Constant literal NOT in _ALLOWED_BUNDLE_DIR_ENV_VARS\n"
        "    # (e.g. typo, or a 5th asset added without updating\n"
        "    # _KNOWN_ASSETS). Symmetric prefix-match must flag this\n"
        "    # — previously slipped past because Constant branch did\n"
        "    # exact-match against the allowlist while dynamic forms\n"
        "    # used prefix-match.\n"
        "    return os.environ.get('CALMLP_BUNDLE_DIR_DOGE', '')\n"
    )
    tree = ast.parse(fake_src)
    offenders = _walk_bundle_dir_offenders(tree)
    offender_funcs = {o[1] for o in offenders}
    expected_offenders = {
        'bad_call', 'bad_subscript', 'bad_fstring', 'bad_concat',
        'bad_getenv', 'bad_name_environ_call', 'bad_name_environ_subscript',
        'bad_unknown_asset',
    }
    assert offender_funcs == expected_offenders, (
        f"AST guard self-test mismatch.\n"
        f"  Expected offenders: {expected_offenders}\n"
        f"  Got offenders:      {offender_funcs}\n"
        f"  Matcher gap (missed bypass forms): "
        f"{expected_offenders - offender_funcs}\n"
        f"  Filter gap (resolver / unexpected funcs flagged): "
        f"{offender_funcs - expected_offenders}"
    )
    # Belt-and-suspenders: explicit assertion on the filter half so a
    # future reader sees the contract spelled out.
    assert '_resolve_bundle_dir' not in offender_funcs, (
        "Filter regression: env reads inside `_resolve_bundle_dir` must "
        "be EXEMPTED from offenders. Current offenders include the resolver."
    )
