# Scripts

Audit, research, and one-off analysis scripts. Read-only against `state.db` unless explicitly noted.

## Conventions
- **Regime-filter every analysis.** Identify when the relevant config changed (`git log market_config.py bot.py`) and filter `settled_trades` to that regime only. Pre-regime data is misleading.
- **Kelly-sized PnL only.** Sim PnL and counterfactuals must use the bot's actual Kelly + risk parameters. Never flat 1-contract.
- **Wilson CI on win rates** when n<200. Use `scripts/wilson_ci.py` if it exists, else compute inline.
- **Verify schema before querying.** `PRAGMA table_info(<table>)` and `SELECT DISTINCT <col>` before assuming column values.
- **`settled_trades.pnl_cents` is GROSS, not net.** It excludes Kalshi fees. Any aggregate labeled "Net PnL" / "total_pnl" / "pnl" must use `SUM(pnl_cents - COALESCE(fee_cents, 0))`. The COALESCE protects legacy NULL-fee rows from being silently dropped. The exception is `sports_shadow_log` which has no `fee_cents` column (simulated PnL pre-fees by design); tag those sites with `# noqa: sports_shadow_log has no fee_cents`. Regression test: `tests/test_audit_scripts_net_pnl.py`. Postmortem: `kb/failures/audit-pnl-fee-omission-apr29.md`.

## Audit scripts
- `audit_runner.sh` — aggregate runner; called by hourly cron
- `15m_live_audit.py`, `hourly_alpha_research.py`, etc. — per-system
- `doc_drift_check.py` — runs before commits that change config values

## DB connections
Any new `sqlite3.connect()` here must include both `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=10000`. The bot, `supabase_sync`, sports, analyst all share `state.db`.
