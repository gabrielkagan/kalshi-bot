"""Regression tests for the BENIGN_PATTERN regex in
.github/workflows/post_deploy_verify.yml step [3/6].

ClickUp 86b9w1r95: the post-deploy verify step greps recent journal
output for `error|traceback|exception` and false-failed on transient
Kalshi 5xx + network-transient ERROR lines. The fix widens
BENIGN_PATTERN to also whitelist:

- `API error:.*-> 5[0-9][0-9]` (Kalshi 500/502/503/504)
- `API error:.*->.*(Timeout|ConnectionError|ReadTimeout|`
  `ConnectTimeout|RemoteDisconnected|MaxRetryError)` (network transients)
- `Retrying.*after connection broken` (urllib3 retry warnings)

This test extracts BENIGN_PATTERN out of the workflow YAML and replays
the exact `grep -iE | grep -ivE` pipeline against staged sample lines
in `tmp_path` to confirm:

1. Transient Kalshi 5xx / timeout / retry lines are SUPPRESSED.
2. Genuine Tracebacks / CRITICAL / internal AssertionErrors / non-
   Kalshi ERROR lines STILL trigger the gate.
3. The existing whitelisted lines (events.*400, Unknown subscription
   ID) continue to be suppressed.
"""
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
POST_DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "post_deploy_verify.yml"


def _extract_patterns_from_yml() -> tuple[str, str]:
    """Pull ERROR_PATTERN= and BENIGN_PATTERN= literal values out of
    the workflow YAML. String-grep only (no PyYAML dependency; CI box
    + Mac fixture parity per Bit 2.3 R2 lesson).
    """
    text = POST_DEPLOY_YML.read_text()
    err_match = re.search(
        r'ERROR_PATTERN\s*=\s*"([^"]+)"', text, re.MULTILINE
    )
    ben_match = re.search(
        r'BENIGN_PATTERN\s*=\s*"([^"]+)"', text, re.MULTILINE
    )
    assert err_match, (
        "post_deploy_verify.yml has no `ERROR_PATTERN=\"...\"` "
        "literal — the test cannot validate the regex pipeline. "
        "Either restore the literal or update this test."
    )
    assert ben_match, (
        "post_deploy_verify.yml has no `BENIGN_PATTERN=\"...\"` "
        "literal — the test cannot validate the regex pipeline."
    )
    return err_match.group(1), ben_match.group(1)


def _replay_grep_pipeline(lines: list[str], tmp_path: Path) -> list[str]:
    """Replay the exact two-stage `grep -iE | grep -ivE` pipeline used
    in workflow step [3/6] against `lines`. Returns the surviving
    lines (= what the gate sees as "non-benign errors").
    """
    err_pat, ben_pat = _extract_patterns_from_yml()
    fixture = tmp_path / "journal.txt"
    fixture.write_text("\n".join(lines) + "\n")

    # Stage 1: grep -iE ERROR_PATTERN
    stage1 = subprocess.run(
        ["grep", "-iE", err_pat, str(fixture)],
        capture_output=True, text=True,
    )
    # grep exit 0=match, 1=no match, 2+=error. 1 is fine.
    assert stage1.returncode in (0, 1), (
        f"stage 1 grep failed unexpectedly: rc={stage1.returncode} "
        f"stderr={stage1.stderr}"
    )
    if stage1.returncode == 1:
        return []

    # Stage 2: grep -ivE BENIGN_PATTERN
    stage2 = subprocess.run(
        ["grep", "-ivE", ben_pat],
        input=stage1.stdout, capture_output=True, text=True,
    )
    assert stage2.returncode in (0, 1)
    if stage2.returncode == 1:
        return []
    return [ln for ln in stage2.stdout.splitlines() if ln]


# ───────────────────────────────────────────────────────────────────
# Suppressed (transient / known-benign) — verify gate IGNORES these
# ───────────────────────────────────────────────────────────────────

# Each tuple is (label, journal-line). The label is for the pytest
# parametrize id; the line is what journalctl would emit for the
# kalshi-bot unit including the systemd prefix.
TRANSIENT_BENIGN_LINES = [
    (
        "kalshi_502",
        "May 11 23:45:12 vps kalshi-bot[12345]: ERROR API error: "
        "GET /trade-api/v2/markets -> 502 body=Bad Gateway",
    ),
    (
        "kalshi_503",
        "May 11 23:45:13 vps kalshi-bot[12345]: ERROR API error: "
        "POST /trade-api/v2/portfolio/orders -> 503 body=Service Unavailable",
    ),
    (
        "kalshi_504_gateway_timeout",
        "May 11 23:45:14 vps kalshi-bot[12345]: ERROR API error: "
        "GET /trade-api/v2/events -> 504 body=Gateway Timeout",
    ),
    (
        "kalshi_500",
        "May 11 23:45:15 vps kalshi-bot[12345]: ERROR API error: "
        "GET /trade-api/v2/markets/KXBTC-26MAY11-T100000 -> 500 body=oops",
    ),
    (
        "kalshi_read_timeout",
        "May 11 23:45:16 vps kalshi-bot[12345]: ERROR API error: "
        "GET /trade-api/v2/markets -> "
        "HTTPSConnectionPool(host='api.elections.kalshi.com', port=443): "
        "Read timed out. (read timeout=5)",
    ),
    (
        "kalshi_connect_timeout",
        "May 11 23:45:17 vps kalshi-bot[12345]: ERROR API error: "
        "POST /trade-api/v2/portfolio/orders -> "
        "ConnectTimeoutError(...)",
    ),
    (
        "kalshi_connection_error",
        "May 11 23:45:18 vps kalshi-bot[12345]: ERROR API error: "
        "GET /trade-api/v2/events -> "
        "ConnectionError: Connection aborted",
    ),
    (
        "kalshi_remote_disconnected",
        "May 11 23:45:19 vps kalshi-bot[12345]: ERROR API error: "
        "GET /trade-api/v2/markets -> "
        "RemoteDisconnected('Remote end closed connection without "
        "response')",
    ),
    (
        "urllib3_retry",
        "May 11 23:45:20 vps kalshi-bot[12345]: WARNING "
        "urllib3.connectionpool: Retrying (Retry(total=2, "
        "connect=None, read=None, redirect=None, status=None)) "
        "after connection broken by 'NewConnectionError'",
    ),
    # Pre-existing benign lines stay benign.
    (
        "events_400_sports",
        "May 11 23:45:21 vps kalshi-bot[12345]: ERROR API error: "
        "GET /trade-api/v2/events?series_ticker=KXFIFAGAME -> 400 "
        "body={'error': 'no active games'}",
    ),
    (
        "ws_unknown_subscription_id",
        "May 11 23:45:22 vps kalshi-bot[12345]: WARNING "
        "WS_ERROR_FRAME: Unknown subscription ID code=7",
    ),
]


@pytest.mark.parametrize(
    "label,line",
    TRANSIENT_BENIGN_LINES,
    ids=[t[0] for t in TRANSIENT_BENIGN_LINES],
)
def test_transient_kalshi_errors_are_suppressed(label, line, tmp_path):
    """Each line is a known-transient external-API or WS noise line
    that the bot recovers from. The verify pipeline must IGNORE it.
    """
    survivors = _replay_grep_pipeline([line], tmp_path)
    assert survivors == [], (
        f"BENIGN_PATTERN failed to suppress transient line `{label}`. "
        f"Survivor lines (would trip verify gate):\n  "
        + "\n  ".join(survivors)
    )


# ───────────────────────────────────────────────────────────────────
# Genuine errors — verify gate STILL TRIPS on these
# ───────────────────────────────────────────────────────────────────

GENUINE_ERROR_LINES = [
    (
        "python_traceback",
        "May 11 23:46:00 vps kalshi-bot[12345]: "
        "Traceback (most recent call last):",
    ),
    (
        "python_traceback_terminal_exception",
        # Terminal exception line of a traceback — contains `Error`
        # token so ERROR_PATTERN matches and BENIGN_PATTERN must not
        # filter it.
        "May 11 23:46:01 vps kalshi-bot[12345]: "
        "ValueError: filter_stage 'foo' is not a known value",
    ),
    (
        "assertion_error_validate_config",
        # Realistic Severity-1 crash-loop signature: market_config
        # mismatch raises AssertionError, journald shows the matching
        # `Error` token. (Bare `CRITICAL` log-level keyword without
        # any `error|traceback|exception` token is a pre-existing
        # ERROR_PATTERN gap and out of scope for this bit; documented
        # in the workflow comment.)
        "May 11 23:46:02 vps kalshi-bot[12345]: "
        "AssertionError: validate_market_configs: MIN_ENTRY_PRICE "
        "mismatch — bot says 75 but market_config says 78",
    ),
    (
        "assertion_error",
        "May 11 23:46:03 vps kalshi-bot[12345]: ERROR "
        "AssertionError: filter_stage 'foo' not in ALLOWED_STAGES",
    ),
    (
        "kalshi_400_non_events",
        # 400 on a markets endpoint (not events.*) is unusual — should
        # still trip the gate; only events.*400 is whitelisted.
        "May 11 23:46:04 vps kalshi-bot[12345]: ERROR API error: "
        "POST /trade-api/v2/portfolio/orders -> 400 body=invalid "
        "ticker",
    ),
    (
        "internal_db_locked_fatal",
        "May 11 23:46:05 vps kalshi-bot[12345]: ERROR "
        "sqlite3.OperationalError: database is locked (after 3 retries)",
    ),
    (
        "scanner_error_non_kalshi",
        "May 11 23:46:06 vps kalshi-bot[12345]: ERROR "
        "OpportunityScanner: unexpected exception in scan_body: "
        "KeyError 'event_ticker'",
    ),
    # R1 adv-reviewer M1 regression coverage (86b9w1r95): genuine
    # non-5xx responses whose JSON body happens to contain keyword
    # tokens must STILL trip the gate. Pre-fix greedy `.*` would have
    # silently suppressed these as "transient network errors."
    (
        "kalshi_401_body_contains_connection_error_keyword",
        # Auth failure — body says "ConnectionError" as part of an
        # auth-retry hint. Real 401, must trip gate.
        "May 11 23:46:07 vps kalshi-bot[12345]: ERROR API error: "
        "POST /trade-api/v2/login -> 401 "
        "body={'error':'auth ConnectionError, please retry'}",
    ),
    (
        "kalshi_403_body_contains_timeout_keyword",
        # Rate-limit / suspension — body mentions "Timeout" in the
        # suspension reason. Real 403, must trip gate.
        "May 11 23:46:08 vps kalshi-bot[12345]: ERROR API error: "
        "GET /trade-api/v2/portfolio/balance -> 403 "
        "body={'detail':'rate-limit Timeout suspension'}",
    ),
    (
        "kalshi_422_body_contains_connection_error_keyword",
        # Validation failure — body mentions exception class names
        # in the validator message. Real 422, must trip gate.
        "May 11 23:46:09 vps kalshi-bot[12345]: ERROR API error: "
        "POST /trade-api/v2/portfolio/orders -> 422 "
        "body={'msg':'ConnectionError schema'}",
    ),
]


@pytest.mark.parametrize(
    "label,line",
    GENUINE_ERROR_LINES,
    ids=[t[0] for t in GENUINE_ERROR_LINES],
)
def test_genuine_errors_still_trip_gate(label, line, tmp_path):
    """Each line represents a real failure mode that should still
    fail the deploy gate. Widening BENIGN_PATTERN must not swallow
    these.
    """
    survivors = _replay_grep_pipeline([line], tmp_path)
    assert survivors, (
        f"BENIGN_PATTERN incorrectly suppressed genuine error "
        f"`{label}`. Widening went too far — verify gate would now "
        f"silently pass on real failures.\nLine: {line}"
    )


# ───────────────────────────────────────────────────────────────────
# Mixed: gate threshold survives a realistic 2-min log window.
# ───────────────────────────────────────────────────────────────────


def test_gate_threshold_holds_with_realistic_mixed_window(tmp_path):
    """Realistic scenario: 2-min log window with many transient Kalshi
    5xx lines + one genuine traceback. The gate's threshold is `> 2`
    non-benign survivors; the single traceback must survive (= 1 line)
    and the 5xx lines must NOT survive (= 0 lines).

    With ERROR_COUNT=1 the existing `> 2` workflow threshold still
    passes, but a future spec tightening to `> 0` would correctly fail
    on the genuine traceback while ignoring the transient noise.
    """
    lines = (
        # 8 transient Kalshi 5xx + retry lines — should ALL be filtered.
        [line for _, line in TRANSIENT_BENIGN_LINES]
        # 1 genuine traceback — should survive.
        + [GENUINE_ERROR_LINES[0][1]]
    )
    survivors = _replay_grep_pipeline(lines, tmp_path)
    # Exactly the genuine traceback should remain.
    assert len(survivors) == 1, (
        f"Expected exactly 1 survivor (the genuine traceback), got "
        f"{len(survivors)}:\n  " + "\n  ".join(survivors)
    )
    assert "Traceback (most recent call last)" in survivors[0]
