"""Contract tests for scripts/ops/discover_15m_series.py (ticket 86bbvdc8y).

Pins the pure decision helpers of the daily Mac-side 15M-series discovery
job — the human-side closure of the 2026-09-05 gap where NEAR/ZEC/commodity
15M series ran for 2+ months without anyone noticing. Network + rclone I/O
is not exercised here (the script's ``--dry-run`` is the manual smoke).
"""
from __future__ import annotations

import json

import pytest

from scripts.ops import discover_15m_series as d


# ─── /series payload → 15M set ──────────────────────────────────────────────


def _series_row(ticker, frequency="fifteen_min", category="Crypto", title=None):
    return {"ticker": ticker, "frequency": frequency, "category": category,
            "title": title or ticker}


def test_fifteen_min_series_uses_frequency_or_15m_suffix():
    payload = {"series": [
        _series_row("KXBTC15M"),
        _series_row("KXGOLD15M", category="Commodities"),
        _series_row("KXGBPUSD15MTEST", frequency="custom"),   # suffix-only match
        _series_row("KXODD", frequency="fifteen_min"),          # frequency-only match
        _series_row("KXBTCD", frequency="daily"),               # neither → excluded
        {"ticker": None}, "garbage",                            # malformed → skipped
    ]}
    out = d.fifteen_min_series_from_series_payload(payload)
    assert set(out) == {"KXBTC15M", "KXGOLD15M", "KXGBPUSD15MTEST", "KXODD"}
    assert out["KXGOLD15M"]["category"] == "Commodities"


def test_fifteen_min_series_empty_payload():
    assert d.fifteen_min_series_from_series_payload({}) == {}
    assert d.fifteen_min_series_from_series_payload({"series": None}) == {}


# ─── kalshi_rest bronze chunk → prefixes ────────────────────────────────────


def _rest_record(tickers):
    inner = {"http_status": 200, "page_idx": 1, "cursor_in": None, "cursor_out": "",
             "response": {"cursor": "", "markets": [{"ticker": t} for t in tickers]}}
    env = {"_wire_recv_ts": "2026-09-05T17:57:00Z", "_source": "kalshi_rest",
           "_conn": None, "_channel": "markets", "_collector_seq": 1,
           "_raw": json.dumps(inner)}
    return json.dumps(env)


def test_series_prefixes_from_rest_chunk_lines_extracts_15m_prefixes_only():
    lines = [
        _rest_record(["KXNEAR15M-26SEP051600-00", "KXGOLD15M-26SEP041500-15",
                      "KXBTCD-26SEP0517-T100", "KXMVECROSSCATEGORY-X",
                      "KXGBPUSD15MTEST-26SEP05-1"]),
        "",                      # blank
        "{not json",             # malformed
        json.dumps({"_raw": json.dumps({"error": "http_500"})}),  # failure record
    ]
    assert d.series_prefixes_from_rest_chunk_lines(lines) == {
        "KXNEAR15M", "KXGOLD15M", "KXGBPUSD15MTEST",
    }


def test_series_15m_regex_requires_prefix_boundary():
    # ``KXBTC15M`` must not be extracted from a lookalike ``KXBTC15MX-…`` as
    # ``KXBTC15M`` — the whole pre-dash segment is the series.
    assert d.series_prefixes_from_rest_chunk_lines(
        [_rest_record(["KXBTC15MX-1"])]) == {"KXBTC15MX"}


# ─── diff / classify / alert decision ───────────────────────────────────────


def test_diff_new_series_is_set_difference_sorted():
    assert d.diff_new_series({"B", "A", "C"}, {"A"}) == ["B", "C"]
    assert d.diff_new_series({"A"}, {"A", "Z"}) == []


def test_bot_unknown_series_lists_series_the_bot_does_not_trade():
    from bot.constants import SERIES_TICKERS
    # Examples must be series the bot will NOT onboard (NEAR/ZEC are being
    # onboarded on the stacked 86bbvdc8y bot branch — using them here would
    # make this pin flip RED the moment SERIES_TICKERS grows).
    current = set(SERIES_TICKERS.values()) | {"KXGOLD15M", "KXWTI15M"}
    assert d.bot_unknown_series(current, SERIES_TICKERS.values()) == ["KXGOLD15M", "KXWTI15M"]


@pytest.mark.parametrize("new,first_run,expected", [
    (["KXGOLD15M"], False, True),
    (["KXGOLD15M"], True, False),   # bootstrap run seeds silently
    ([], False, False),
    ([], True, False),
])
def test_should_alert_only_on_new_after_bootstrap(new, first_run, expected):
    assert d.should_alert(new, first_run) is expected


def test_format_report_bootstrap_vs_new_vs_quiet():
    cur = {"KXGOLD15M": {"title": "Gold 15-minute", "category": "Commodities"}}
    boot = d.format_report(new_series=[], current=cur, bot_unknown=["KXGOLD15M"],
                           first_seen={}, first_run=True)
    assert "BOOTSTRAP" in boot and "no alert" in boot
    new = d.format_report(
        new_series=["KXGOLD15M"], current=cur, bot_unknown=["KXGOLD15M"],
        first_seen={"KXGOLD15M": {"earliest_close": "2026-07-31T18:15:00Z",
                                  "settled_n": 2487, "distinct_days": 35}},
        first_run=False, bronze_only=["KXODD15M"])
    assert "NEW 15M SERIES" in new and "KXGOLD15M" in new
    assert "2026-07-31T18:15:00Z" in new and "days=35" in new
    assert "NOT in /series: KXODD15M" in new
    assert "bot does not trade 1: KXGOLD15M" in new
    quiet = d.format_report(new_series=[], current=cur, bot_unknown=[],
                            first_seen={}, first_run=False)
    assert "no new series" in quiet


# ─── state round-trip ───────────────────────────────────────────────────────


def test_state_save_load_round_trip(tmp_path):
    p = str(tmp_path / "known_series.json")
    assert d.load_state(p) is None
    d.save_state(p, {"series": {"KXBTC15M": {"title": "x"}}, "updated": "t"})
    assert d.load_state(p)["series"] == {"KXBTC15M": {"title": "x"}}
    assert not (tmp_path / "known_series.json.tmp").exists(), "atomic replace"


# ─── launchd wiring ─────────────────────────────────────────────────────────


def test_launchd_plist_and_wrapper_point_at_each_other():
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    plist = open(os.path.join(root, "scripts/ops/launchd/io.kalshi.15m-discovery.plist")).read()
    wrapper = open(os.path.join(root, "scripts/ops/discover_15m_series.sh")).read()
    assert "scripts/ops/discover_15m_series.sh" in plist
    assert "scripts.ops.discover_15m_series" in wrapper
    # monitor-the-monitor: logs land under $HOME, never /tmp
    for line in plist.splitlines():
        if "launchd.out" in line or "launchd.err" in line:
            assert "/tmp" not in line and "kalshi-15m-discovery/" in line
    assert "kalshi-15m-discovery/launchd.out" in plist
    assert 'kalshi-15m-discovery/run.log' in wrapper
    # R1-MN6: Telegram creds only reach launchd if the wrapper sources .env
    assert "TELEGRAM_BOT_TOKEN=" in wrapper and "TELEGRAM_CHAT_ID=" in wrapper
    # R1-MN5: install comment must create the log dir launchd will not create
    assert "mkdir -p ~/kalshi-15m-discovery" in plist


# ─── main() end-to-end with I/O monkeypatched (R1-M3) ────────────────────────


def _patch_io(monkeypatch, series, bronze=(set(), None), first_seen=None):
    payload = {"series": [{"ticker": t, "frequency": "fifteen_min", "category": "Crypto",
                           "title": t} for t in series]}
    monkeypatch.setattr(d, "fetch_series_catalog", lambda: payload)
    monkeypatch.setattr(d, "latest_rest_chunk_prefixes", lambda remote: bronze)
    monkeypatch.setattr(d, "fetch_first_seen",
                        lambda s, max_pages=20: first_seen or {"earliest_close": "2026-06-30T17:30:00Z",
                                                               "settled_n": 1, "distinct_days": 1,
                                                               "truncated": False})
    alerts = []
    monkeypatch.setattr(d, "raise_alert", lambda text, sd, **kw: alerts.append((text, kw)))
    return alerts


def test_main_bootstrap_is_silent_then_new_series_alerts(monkeypatch, tmp_path):
    alerts = _patch_io(monkeypatch, ["KXBTC15M", "KXETH15M"])
    calls = []
    monkeypatch.setattr(d, "fetch_first_seen", lambda s, max_pages=20: calls.append(s) or {})
    assert d.main(["--state-dir", str(tmp_path), "--no-bronze"]) == 0
    assert alerts == [], "bootstrap run must not alert"
    assert calls == [], "bootstrap must not fan out first-seen paging (R2-MN4)"
    st = d.load_state(str(tmp_path / d.STATE_FILENAME))
    assert set(st["series"]) == {"KXBTC15M", "KXETH15M"}
    # second run: a new series appears → exactly one alert, state grows
    alerts = _patch_io(monkeypatch, ["KXBTC15M", "KXETH15M", "KXGOLD15M"])
    assert d.main(["--state-dir", str(tmp_path), "--no-bronze"]) == 0
    assert len(alerts) == 1 and "KXGOLD15M" in alerts[0][0]
    st = d.load_state(str(tmp_path / d.STATE_FILENAME))
    assert st["series"]["KXGOLD15M"]["earliest_close"] == "2026-06-30T17:30:00Z"
    # third run: nothing new → no alert
    alerts = _patch_io(monkeypatch, ["KXBTC15M", "KXETH15M", "KXGOLD15M"])
    assert d.main(["--state-dir", str(tmp_path), "--no-bronze"]) == 0
    assert alerts == []


def test_main_dry_run_writes_nothing(monkeypatch, tmp_path):
    alerts = _patch_io(monkeypatch, ["KXBTC15M"])
    assert d.main(["--state-dir", str(tmp_path), "--no-bronze", "--dry-run"]) == 0
    assert not (tmp_path / d.STATE_FILENAME).exists()
    assert not (tmp_path / d.REPORT_FILENAME).exists()
    assert alerts == []


def test_main_empty_catalog_is_a_loud_failure(monkeypatch, tmp_path):
    alerts = _patch_io(monkeypatch, [])
    assert d.main(["--state-dir", str(tmp_path), "--no-bronze"]) == 1
    assert (tmp_path / "LAST_FAILURE.txt").exists()
    assert len(alerts) == 1 and "FAILED" in alerts[0][0]
    assert alerts[0][1].get("sentinel_prefix") == "DISCOVERY_FAILED"
    assert not (tmp_path / d.STATE_FILENAME).exists(), "must not record an empty known-set"


def test_main_corrupt_state_is_a_failure_not_a_rebootstrap(monkeypatch, tmp_path):
    (tmp_path / d.STATE_FILENAME).write_text("{not json")
    alerts = _patch_io(monkeypatch, ["KXBTC15M"])
    assert d.main(["--state-dir", str(tmp_path), "--no-bronze"]) == 1
    assert len(alerts) == 1 and "FAILED" in alerts[0][0]
    moved = [p.name for p in tmp_path.iterdir() if p.name.startswith(d.STATE_FILENAME + ".corrupt-")]
    assert moved, "corrupt state file must be moved aside for a human"
    assert not (tmp_path / d.STATE_FILENAME).exists()


def test_main_bronze_only_prefix_is_reported_and_tracked(monkeypatch, tmp_path):
    _patch_io(monkeypatch, ["KXBTC15M"])
    d.main(["--state-dir", str(tmp_path), "--no-bronze"])  # bootstrap
    alerts = _patch_io(monkeypatch, ["KXBTC15M"], bronze=({"KXODD15M"}, "some/chunk.zst"))
    assert d.main(["--state-dir", str(tmp_path)]) == 0
    assert len(alerts) == 1 and "NOT in /series: KXODD15M" in alerts[0][0]
    st = d.load_state(str(tmp_path / d.STATE_FILENAME))
    assert st["series"]["KXODD15M"]["source"] == "bronze_only"
    assert st["bronze_chunk_checked"] == "some/chunk.zst"
