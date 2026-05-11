# migrations/

One-shot migration scripts. **NOT on the default Python import path** —
this directory deliberately lacks an `__init__.py` so no production code
can accidentally `import migrations.X` at runtime.

Operator-invoked only. Each script is idempotent (safe to re-run).

Created in Sprint 10.6 (2026-05-11). Per master plan
`kb/decisions/repo-modularization-plan-may05.md` Phase GG.

## Scripts

### `migrate_to_supabase.py`

One-time bulk load: SQLite `state.db` → Supabase Postgres.

**When to run:** initial Supabase setup, after applying schema via the
Supabase SQL Editor and exporting `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`.

```bash
source ~/.env
python3 migrations/migrate_to_supabase.py [--dry-run]
```

Resolves `state.db` from repo root via explicit 2-level parent navigation
(Sprint 10.6 fix for `__file__`-derived path orphaning — see comment at
the body of `main()`).

### `migrate_composite_pk.py`

One-time schema migration: switch `positions` and `settled_trades` from
single-column PK (`ticker`) to composite PK (`ticker, strategy_group`).

**When to run:** with the bot STOPPED only. Idempotent — checks for the
`strategy_group` column before migrating.

```bash
# Stop the bot first:
ssh kalshi-vps "sudo systemctl stop kalshi-bot"

# From repo root:
python3 migrations/migrate_composite_pk.py

# Restart:
ssh kalshi-vps "sudo systemctl start kalshi-bot"
```

Resolves `state.db` via `STATE_DB_PATH` env var (default: `./state.db`).
**Must be run from repo root** so the CWD-relative default works.

## Adding a new migration

1. New file at `migrations/<descriptive_name>.py`.
2. Anchor any filesystem path explicitly via `os.path.dirname(os.path.dirname(__file__))`
   (gets the repo root) — NOT bare `__file__`-derived paths, which break
   if the script moves.
3. Idempotent: check for the migration's effect before applying (e.g.,
   `IF NOT EXISTS` SQL guards, `if column already exists: return`).
4. Document it in this README under "Scripts" above.
5. Run-date log: when you actually run the migration, append the date +
   commit hash + any notes to the bottom of this file under "Run history".

## Run history

(append entries as migrations are applied)

- migrate_to_supabase.py — see git log for original 2026-Q1 run; not re-runnable
- migrate_composite_pk.py — see git log for original run date
