"""Regression tests for audit-script PnL accounting.

Background: `settled_trades.pnl_cents` is `revenue - total_cost` and EXCLUDES
fees. True net PnL (after Kalshi fees) = `pnl_cents - fee_cents`. The
canonical query in `dashboard_snapshot.py` correctly subtracts fees, but
`scripts/15m_live_audit.py` and `scripts/alpha_audit.py` had 19 SQL sites
using bare `SUM(pnl_cents)` and labeling the result "Net PnL" or "pnl".

This bug overstates PnL by the total fee burden (~$77/30d on current
trade volume), biasing every shadow-vs-live promotion comparison toward
"ship it." Postmortem: kb/failures/audit-pnl-fee-omission-apr29.md.

Tests:
1. **AST/regex regression** — assert no bare `SUM(pnl_cents)` exists in the
   two audit scripts. The fix is `SUM(pnl_cents - fee_cents)`.
2. **Numerical correctness** — build a tmp sqlite with known gross/fee
   values, run the relevant SQL, assert it returns NET (gross - fees).
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_15M = REPO_ROOT / 'scripts' / '15m_live_audit.py'
SCRIPTS_ALPHA = REPO_ROOT / 'scripts' / 'alpha_audit.py'
# Round-1 review #1: bug class extends to these scripts too. The most
# critical is 15m_alpha_research.py which drives shadow→live promotion
# decisions (10 sites). audit_cron.py writes JSON consumed downstream.
# generate_whitepaper_stats.py publishes external numbers in PDFs/README.
SCRIPTS_AUDIT_CRON = REPO_ROOT / 'scripts' / 'audit_cron.py'
SCRIPTS_15M_RESEARCH = REPO_ROOT / 'scripts' / '15m_alpha_research.py'
SCRIPTS_WHITEPAPER = REPO_ROOT / 'scripts' / 'generate_whitepaper_stats.py'
SCRIPTS_MAKER_OPP = REPO_ROOT / 'scripts' / 'maker_opportunity_cost.py'
ANALYSIS_REGIME = REPO_ROOT / 'analysis' / 'regime_analysis.py'

ALL_AFFECTED_SCRIPTS = [
    SCRIPTS_15M, SCRIPTS_ALPHA,
    SCRIPTS_AUDIT_CRON, SCRIPTS_15M_RESEARCH,
    SCRIPTS_WHITEPAPER, SCRIPTS_MAKER_OPP,
    ANALYSIS_REGIME,  # Round-2 #2 caught
]


def _bare_sum_pnl_sites(src: str) -> list[tuple[int, str]]:
    """Return (lineno, line) tuples where `SUM(pnl_cents)` appears WITHOUT
    the canonical `pnl_cents - COALESCE(fee_cents, 0)` correction.

    Round-2 #7: pattern_corrected updated to canonical form (was non-
    canonical bare `pnl - fee` which itself silently drops NULL-fee rows).
    Round-2 #1 + LOW #8: a `# noqa: sports_shadow_log has no fee_cents`
    marker on the SAME line marks tables that legitimately have no
    fee_cents column — the only such table currently is
    `sports_shadow_log` (simulated/observation PnL pre-fees by design).
    """
    out = []
    pattern_bare = re.compile(r'SUM\s*\(\s*pnl_cents\s*\)')
    pattern_corrected = re.compile(
        r'SUM\s*\(\s*pnl_cents\s*-\s*COALESCE\s*\(\s*fee_cents\s*,\s*0\s*\)\s*\)'
    )
    NOQA_MARKER = 'noqa:'
    # KNOWN WEAK SPOT (Round-4 #1): the noqa filter accepts ANY text after
    # `noqa:` as justification, so a frivolous `# noqa: yolo` would pass.
    # Mitigated by: (a) reviewers see the rationale text on the same line,
    # (b) `scripts/CLAUDE.md` documents the only legitimate causes
    # (sports_shadow_log no-fee-column, Supabase RPC-parity). To tighten:
    # enumerate valid markers (`noqa: no-fee-cents` / `noqa: rpc-parity`).
    for i, line in enumerate(src.splitlines(), start=1):
        # Skip pure-comment lines (line begins with optional ws + `#`).
        if line.lstrip().startswith('#'):
            continue
        if pattern_bare.search(line) and not pattern_corrected.search(line):
            if NOQA_MARKER in line:
                continue
            out.append((i, line.rstrip()))
    return out


def test_15m_live_audit_no_bare_sum_pnl_cents():
    """`SUM(pnl_cents)` in scripts/15m_live_audit.py overstates net PnL by
    the fee burden. Replace with `SUM(pnl_cents - fee_cents)`."""
    src = SCRIPTS_15M.read_text()
    sites = _bare_sum_pnl_sites(src)
    assert not sites, (
        f"Found {len(sites)} bare `SUM(pnl_cents)` site(s) in scripts/15m_live_audit.py. "
        f"Each overstates net PnL by total fees. Replace with "
        f"`SUM(pnl_cents - fee_cents)`. Sites:\n" +
        "\n".join(f"  line {ln}: {line}" for ln, line in sites)
    )


def test_alpha_audit_no_bare_sum_pnl_cents():
    """Same contract for scripts/alpha_audit.py."""
    src = SCRIPTS_ALPHA.read_text()
    sites = _bare_sum_pnl_sites(src)
    assert not sites, (
        f"Found {len(sites)} bare `SUM(pnl_cents)` site(s) in scripts/alpha_audit.py. "
        f"Replace with `SUM(pnl_cents - fee_cents)`. Sites:\n" +
        "\n".join(f"  line {ln}: {line}" for ln, line in sites)
    )


@pytest.mark.parametrize("script", [
    SCRIPTS_AUDIT_CRON, SCRIPTS_15M_RESEARCH,
    SCRIPTS_WHITEPAPER, SCRIPTS_MAKER_OPP,
    ANALYSIS_REGIME,  # Round-2 #2
])
def test_sister_scripts_no_bare_sum_pnl_cents(script):
    """Round-1 review #1: same bug class in 4 sister scripts:
    - 15m_alpha_research.py: 10 sites; THE shadow→live promotion-decision
      script. Eliminating promotion bias requires fixing this.
    - audit_cron.py: 4 sites; writes JSON downstream consumers see.
    - generate_whitepaper_stats.py: 1 site; affects published PDFs/README.
    - maker_opportunity_cost.py: 1 site.
    """
    if not script.exists():
        pytest.skip(f"{script.name} not in tree")
    src = script.read_text()
    sites = _bare_sum_pnl_sites(src)
    assert not sites, (
        f"Found {len(sites)} bare `SUM(pnl_cents)` site(s) in {script.name}. "
        f"Replace with `SUM(pnl_cents - COALESCE(fee_cents, 0))`. Sites:\n" +
        "\n".join(f"  line {ln}: {line}" for ln, line in sites)
    )


def test_no_undocumented_bare_sum_pnl_in_repo():
    """Postmortem prevention #1 (Round-2 #4): glob the entire repo for
    `SUM(pnl_cents)` not in the explicit allowlist. Catches a 7th-script-
    tomorrow regression without anyone remembering to add it.

    Allowlisted: ALL_AFFECTED_SCRIPTS (already covered by per-script tests),
    `dashboard_snapshot.py` (out of scope for this fix per postmortem),
    test files, and any line tagged `# noqa: ... fee_cents`.
    """
    allowlist_paths = {p.resolve() for p in ALL_AFFECTED_SCRIPTS}
    allowlist_paths.add((REPO_ROOT / 'bot' / 'snapshots' / 'dashboard_snapshot.py').resolve())  # Sprint 10 Bit 10.4 (2026-05-12)
    allowlist_paths.add((REPO_ROOT / 'tests' / 'test_audit_scripts_net_pnl.py').resolve())
    pattern_bare = re.compile(r'SUM\s*\(\s*pnl_cents\s*\)')
    pattern_corrected = re.compile(
        r'SUM\s*\(\s*pnl_cents\s*-\s*COALESCE\s*\(\s*fee_cents\s*,\s*0\s*\)\s*\)'
    )
    NOQA_MARKER = 'noqa:'
    found = []
    for py in REPO_ROOT.rglob('*.py'):
        if py.resolve() in allowlist_paths:
            continue
        # Skip vendored / virtual env / build / test dirs.
        parts = py.parts
        if any(p in {'.venv', 'venv', '.tox', 'build', '__pycache__', 'tests'}
               for p in parts):
            continue
        try:
            src = py.read_text()
        except Exception:
            continue
        for i, line in enumerate(src.splitlines(), start=1):
            if line.lstrip().startswith('#'):
                continue
            if pattern_bare.search(line) and not pattern_corrected.search(line):
                if NOQA_MARKER in line:
                    continue  # explicit justification — see helper docstring
                rel = py.relative_to(REPO_ROOT)
                found.append(f"{rel}:{i}: {line.strip()}")
    assert not found, (
        f"Found {len(found)} undocumented bare `SUM(pnl_cents)` site(s) "
        f"in the repo. Either add the file to ALL_AFFECTED_SCRIPTS in this "
        f"test (and apply the canonical fix), tag the line with "
        f"`# noqa: <reason involving fee_cents>`, or replace with "
        f"`SUM(pnl_cents - COALESCE(fee_cents, 0))`. Sites:\n" +
        "\n".join("  " + s for s in found)
    )


def test_corrected_sum_pnl_returns_net_not_gross(tmp_path):
    """End-to-end numerical check: `SUM(pnl_cents - fee_cents)` returns the
    NET value when fees are positive expenses. Bare `SUM(pnl_cents)` returns
    gross — proves the bug magnitude on a known fixture."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE settled_trades (
            id INTEGER PRIMARY KEY,
            pnl_cents INTEGER,   -- revenue - total_cost; EXCLUDES fees
            fee_cents INTEGER    -- positive = expense
        );
        INSERT INTO settled_trades (pnl_cents, fee_cents) VALUES
            (100,  20),  -- gross +100, fee 20  → net  +80
            (-50,  15),  -- gross -50,  fee 15  → net  -65
            (200,  30);  -- gross +200, fee 30  → net +170
    """)
    # Bare (the bug)
    bare = conn.execute("SELECT SUM(pnl_cents) FROM settled_trades").fetchone()[0]
    assert bare == 250, f"sanity: bare sum = {bare}"
    # Fixed (the contract)
    net = conn.execute("SELECT SUM(pnl_cents - fee_cents) FROM settled_trades").fetchone()[0]
    assert net == 185, f"net sum should be 185 (250 - 65 fees), got {net}"
    # Independent verification: net = bare - fees
    fees = conn.execute("SELECT SUM(fee_cents) FROM settled_trades").fetchone()[0]
    assert net == bare - fees, "net = gross - fees (algebraic check)"
    conn.close()


def test_corrected_sum_with_group_by_and_edges(tmp_path):
    """Round-1 review #6: validate the canonical SQL works correctly under
    GROUP BY (multi-asset/strategy aggregation), with NULL-pnl rows
    (settlement-pending), and zero-pnl rows (no fee or fee = pnl edge).
    """
    db = tmp_path / "edges.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE settled_trades (
            id INTEGER PRIMARY KEY,
            asset TEXT,
            pnl_cents INTEGER,
            fee_cents INTEGER
        );
        INSERT INTO settled_trades (asset, pnl_cents, fee_cents) VALUES
            ('BTC',   100,    20),    -- net  +80
            ('BTC',   -50,    15),    -- net  -65
            ('ETH',   200,    30),    -- net +170
            ('ETH',  NULL,    10),    -- settlement-pending; SUM skips
            ('SOL',     0,    20),    -- zero gross, fee = -20 net
            ('XRP',    50,  NULL);    -- legacy null fee → treated as 0 → net +50
    """)
    rows = conn.execute("""
        SELECT asset, SUM(pnl_cents - COALESCE(fee_cents, 0)) AS net
        FROM settled_trades
        GROUP BY asset
        ORDER BY asset
    """).fetchall()
    expected = {'BTC': 15, 'ETH': 170, 'SOL': -20, 'XRP': 50}
    actual = {asset: net for asset, net in rows}
    assert actual == expected, f"GROUP BY net sums: {actual} != {expected}"
    # Total across all assets:
    total = conn.execute(
        "SELECT SUM(pnl_cents - COALESCE(fee_cents, 0)) FROM settled_trades"
    ).fetchone()[0]
    assert total == 215, f"total net = 15 + 170 + (-20) + 50 = 215; got {total}"
    conn.close()


def test_corrected_sql_handles_null_fee_cents(tmp_path):
    """Older settled_trades rows may have NULL fee_cents (added later).
    `pnl_cents - fee_cents` returns NULL for those rows, and SQL's SUM
    skips NULLs — meaning OLD rows with no fee data would be DROPPED from
    the sum entirely.

    To avoid silently losing rows, the fix should use COALESCE:
    `SUM(pnl_cents - COALESCE(fee_cents, 0))`. This test pins that
    behavior so we don't regress when fixing.
    """
    db = tmp_path / "test_null.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE settled_trades (id INTEGER PRIMARY KEY, pnl_cents INTEGER, fee_cents INTEGER);
        INSERT INTO settled_trades (pnl_cents, fee_cents) VALUES
            (100, 20),
            (50, NULL),  -- old row with no fee data
            (200, 30);
    """)
    naive = conn.execute("SELECT SUM(pnl_cents - fee_cents) FROM settled_trades").fetchone()[0]
    assert naive == 250, f"naive (skips NULL row) returns {naive}"
    coalesced = conn.execute(
        "SELECT SUM(pnl_cents - COALESCE(fee_cents, 0)) FROM settled_trades"
    ).fetchone()[0]
    assert coalesced == 300, f"coalesced (treats NULL fee as 0) = 300; got {coalesced}"
    conn.close()


def test_audit_scripts_use_canonical_coalesce_form():
    """Every `SUM(pnl_cents - ...)` site must include the canonical
    `COALESCE(fee_cents, 0)` somewhere in the SUM expression. Round-2 #6:
    relaxed from `fullmatch` to `search` so legitimate variants like
    `SUM((pnl_cents - COALESCE(fee_cents, 0)) * count)` (PnL-weighted-by-
    contracts) still pass. The bare-SUM check (`_bare_sum_pnl_sites`)
    catches the original bug (no subtraction at all)."""
    canonical_substring = re.compile(
        r'COALESCE\s*\(\s*fee_cents\s*,\s*0\s*\)'
    )
    any_subtraction = re.compile(r'SUM\s*\(\s*pnl_cents\s*-')
    for path in ALL_AFFECTED_SCRIPTS:
        if not path.exists():
            continue
        src = path.read_text()
        for match in any_subtraction.finditer(src):
            # Locate the full SUM(...) call (depth-balanced parens).
            depth = 0
            j = match.start()
            while j < len(src):
                if src[j] == '(':
                    depth += 1
                elif src[j] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            site_text = src[match.start():j+1]
            assert canonical_substring.search(site_text), (
                f"{path.name}: `SUM(pnl_cents - ...)` at offset "
                f"{match.start()} must use `COALESCE(fee_cents, 0)` to "
                f"prevent silently dropping NULL-fee rows. Site: "
                f"{site_text!r}."
            )
