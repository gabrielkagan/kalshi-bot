# Scripts

Audit, research, and one-off analysis scripts. Read-only against `state.db` unless explicitly noted.

## Conventions
- **Regime-filter every analysis.** Identify when the relevant config changed (`git log market_config.py bot.py`) and filter `settled_trades` to that regime only. Pre-regime data is misleading.
- **Kelly-sized PnL only.** Sim PnL and counterfactuals must use the bot's actual Kelly + risk parameters. Never flat 1-contract.
- **Wilson CI on win rates** when n<200. Use `scripts/wilson_ci.py` if it exists, else compute inline.
- **Verify schema before querying.** `PRAGMA table_info(<table>)` and `SELECT DISTINCT <col>` before assuming column values.

## Audit scripts
- `audit_runner.sh` — aggregate runner; called by hourly cron
- `15m_live_audit.py`, `hourly_alpha_research.py`, etc. — per-system
- `doc_drift_check.py` — runs before commits that change config values

## DB connections
Any new `sqlite3.connect()` here must include both `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=10000`. The bot, `supabase_sync`, sports, analyst all share `state.db`.
