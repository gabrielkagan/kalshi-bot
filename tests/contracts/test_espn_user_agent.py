"""Ticket 86bbvqhyr (2026-09-06) — ESPN User-Agent, defined once, accepted by ESPN.

ROOT CAUSE: ``site.api.espn.com`` (Akamai edge) began returning 403 on
~2026-08-05 to any request whose User-Agent's LEADING product token is
not a recognised HTTP-client family. Both ESPN call sites sent
``KalshiBot/1.0`` → 100% 403 for all 24 leagues for ~5 weeks; the
collector wrote well-formed 403 rows so every volume-based check
passed. Postmortem: kb/failures/espn-403-user-agent-silent-outage-sep06.md.

RCA table (2026-09-06 16:37Z, same URL, same IP, UA swapped):
  PASS  python-requests/<any>, curl/<any>, Python-urllib/3.x,
        Go-http-client/1.1, okhttp/4.9, python-httpx/0.27, aiohttp/3.9,
        Apache-HttpClient/4.5, "python-requests/2.32.3 KalshiBot/1.0"
  403   KalshiBot/1.0, kalshi-bot/1.0, KalshiBot/2.0, KalshiCollector/1.0,
        Bot/1.0, foo/1.0, Mozilla/5.0 (bare + full Chrome string),
        Wget/1.21, node-fetch/1.0, Java/17, Dalvik/2.1, requests/2.32.3,
        "KalshiBot/1.0 python-requests/2.32.3", EMPTY UA.

Pins:
  1. ``ESPN_USER_AGENT`` exists at BOTH sites and is the SAME string.
     The collector cannot import ``bot.*`` (collector-no-bot contract),
     so both evaluate ``requests.utils.default_user_agent()`` — the
     requests library is the single source of truth.
  2. The string's leading token is in the family ESPN accepted on
     2026-09-06 (``python-requests/``).
  3. The string's leading token is NOT one ESPN rejected.
  4. Neither production file carries the ``KalshiBot/1.0`` literal any
     more, and neither hardcodes a UA literal at the header-assignment
     site (AST: the header value must be the ``ESPN_USER_AGENT`` Name).
  5. A live ``requests.Session`` built by each class carries the constant.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_FILE = REPO_ROOT / "bot" / "engines" / "sports_engine.py"
COLLECTOR_FILE = REPO_ROOT / "collector" / "espn_archiver.py"

# Leading-token families observed to PASS on 2026-09-06 (see module doc).
ACCEPTED_LEADING_TOKEN_RE = re.compile(r"^python-requests/\S+$")

# Leading tokens observed to be REJECTED on 2026-09-06. Case-insensitive
# on purpose: ``kalshi-bot`` and ``KalshiBot`` both 403'd.
REJECTED_LEADING_TOKENS = (
    "kalshibot", "kalshi-bot", "kalshicollector", "bot", "foo",
    "mozilla", "wget", "node-fetch", "java", "dalvik", "requests",
)


def _leading_token(ua: str) -> str:
    return ua.split()[0] if ua.split() else ""


def _load_constants():
    import bot.engines.sports_engine as se
    import collector.espn_archiver as ea
    return se, ea


def test_espn_user_agent_defined_at_both_sites_and_equal():
    se, ea = _load_constants()
    assert hasattr(se, "ESPN_USER_AGENT"), (
        "bot/engines/sports_engine.py must define ESPN_USER_AGENT "
        "(ticket 86bbvqhyr)."
    )
    assert hasattr(ea, "ESPN_USER_AGENT"), (
        "collector/espn_archiver.py must define ESPN_USER_AGENT "
        "(ticket 86bbvqhyr)."
    )
    assert isinstance(se.ESPN_USER_AGENT, str) and se.ESPN_USER_AGENT.strip()
    assert se.ESPN_USER_AGENT == ea.ESPN_USER_AGENT, (
        f"UA drift: bot={se.ESPN_USER_AGENT!r} collector="
        f"{ea.ESPN_USER_AGENT!r}. Both sites must bind to the same "
        f"single source of truth (requests.utils.default_user_agent())."
    )


def test_espn_user_agent_is_requests_default():
    """The single source of truth is the requests library's own default."""
    import requests
    se, _ = _load_constants()
    assert se.ESPN_USER_AGENT == requests.utils.default_user_agent()


def test_espn_user_agent_leading_token_in_accepted_family():
    se, _ = _load_constants()
    assert ACCEPTED_LEADING_TOKEN_RE.match(_leading_token(se.ESPN_USER_AGENT)), (
        f"ESPN_USER_AGENT={se.ESPN_USER_AGENT!r}: leading token must be "
        f"in the family ESPN accepted on 2026-09-06 (python-requests/*). "
        f"Re-verify with scripts/ops/espn_live_probe.py before changing."
    )


def test_espn_user_agent_leading_token_not_rejected():
    se, _ = _load_constants()
    lead = _leading_token(se.ESPN_USER_AGENT).lower()
    assert lead, "empty User-Agent is rejected by ESPN (403 on 2026-09-06)"
    product = lead.split("/")[0]
    assert product not in REJECTED_LEADING_TOKENS, (
        f"leading product token {product!r} is in the ESPN reject list "
        f"observed 2026-09-06."
    )


@pytest.mark.parametrize("path", [BOT_FILE, COLLECTOR_FILE])
def test_no_rejected_ua_string_constant_remains(path: Path):
    """AST-level: no string CONSTANT in the module has a rejected leading
    product token (comments may still cite the old literal for history)."""
    tree = ast.parse(path.read_text())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lead = _leading_token(node.value).lower()
            if "/" in lead and lead.split("/")[0] in REJECTED_LEADING_TOKENS:
                offenders.append(node.value)
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} still carries a rejected-UA string "
        f"constant: {offenders!r}"
    )


def _user_agent_assignment_values(path: Path):
    """Yield the RHS node of every ``<x>.headers["User-Agent"] = <rhs>``."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.slice, ast.Constant)
                and tgt.slice.value == "User-Agent"
            ):
                yield node.value


@pytest.mark.parametrize("path", [BOT_FILE, COLLECTOR_FILE])
def test_header_assignment_binds_the_constant_not_a_literal(path: Path):
    values = list(_user_agent_assignment_values(path))
    assert values, (
        f"{path.relative_to(REPO_ROOT)}: no `headers['User-Agent'] = …` "
        f"assignment found — the session must set the UA explicitly so "
        f"a future requests default change is visible here."
    )
    for v in values:
        assert isinstance(v, ast.Name) and v.id == "ESPN_USER_AGENT", (
            f"{path.relative_to(REPO_ROOT)}: User-Agent header must be "
            f"assigned from the ESPN_USER_AGENT name, got "
            f"{ast.dump(v)[:80]}"
        )


def test_live_sessions_carry_the_constant():
    se, ea = _load_constants()
    feed = se.ESPNLiveFeed()
    assert feed._session.headers["User-Agent"] == se.ESPN_USER_AGENT

    class _W:
        def write(self, _env):
            pass

    archiver = ea.ESPNArchiver(
        writers_by_channel={"nba": _W()}, leagues=["nba"],
        inter_league_sleep_seconds=0.0,
    )
    assert archiver._session.headers["User-Agent"] == ea.ESPN_USER_AGENT
