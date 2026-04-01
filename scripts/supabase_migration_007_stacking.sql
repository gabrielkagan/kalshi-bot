-- Migration 007: Stacking infrastructure — composite PK (ticker, strategy_group)
-- Run in Supabase SQL Editor. Idempotent — safe to run multiple times.

-- ═══════════════════════════════════════════════════════════════════════════
--  positions
-- ═══════════════════════════════════════════════════════════════════════════

-- Backfill NULL strategies
UPDATE positions SET strategy = 'TAKER_NOW' WHERE strategy IS NULL;

-- Add strategy_group column if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'positions' AND column_name = 'strategy_group'
    ) THEN
        ALTER TABLE positions ADD COLUMN strategy_group TEXT NOT NULL DEFAULT 'main';
    END IF;
END $$;

-- Add is_stacked column if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'positions' AND column_name = 'is_stacked'
    ) THEN
        ALTER TABLE positions ADD COLUMN is_stacked INTEGER DEFAULT 0;
    END IF;
END $$;

-- Set strategy to NOT NULL with default
ALTER TABLE positions ALTER COLUMN strategy SET NOT NULL;
ALTER TABLE positions ALTER COLUMN strategy SET DEFAULT 'main';

-- Backfill strategy_group from strategy
UPDATE positions SET strategy_group =
    CASE
        WHEN strategy IN ('MAKER_PATIENT','TAKER_NOW','MAKER_AGGRESSIVE',
                          'PANIC_CAPTURE','CONFIRMATION_ADDON','DIP_ADDON')
             THEN 'main'
        WHEN strategy LIKE 'decided_%' THEN 'decided'
        WHEN strategy = '' THEN 'main'
        ELSE strategy
    END
WHERE strategy_group = 'main' OR strategy_group IS NULL;

-- Drop old primary key and add composite PK
-- Supabase/Postgres: must drop and recreate constraint
DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    -- Find the existing PK constraint name
    SELECT tc.constraint_name INTO constraint_name
    FROM information_schema.table_constraints tc
    WHERE tc.table_name = 'positions'
      AND tc.constraint_type = 'PRIMARY KEY';

    IF constraint_name IS NOT NULL THEN
        -- Check if it's already the composite PK
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.key_column_usage
            WHERE table_name = 'positions'
              AND constraint_name = constraint_name
              AND column_name = 'strategy_group'
        ) THEN
            EXECUTE format('ALTER TABLE positions DROP CONSTRAINT %I', constraint_name);
            ALTER TABLE positions ADD PRIMARY KEY (ticker, strategy_group);
        END IF;
    ELSE
        -- No PK exists, just add it
        ALTER TABLE positions ADD PRIMARY KEY (ticker, strategy_group);
    END IF;
END $$;


-- ═══════════════════════════════════════════════════════════════════════════
--  settled_trades
-- ═══════════════════════════════════════════════════════════════════════════

-- Backfill NULL strategies
UPDATE settled_trades SET strategy = 'TAKER_NOW' WHERE strategy IS NULL;

-- Add strategy_group column if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'settled_trades' AND column_name = 'strategy_group'
    ) THEN
        ALTER TABLE settled_trades ADD COLUMN strategy_group TEXT NOT NULL DEFAULT 'main';
    END IF;
END $$;

-- Add is_stacked column if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'settled_trades' AND column_name = 'is_stacked'
    ) THEN
        ALTER TABLE settled_trades ADD COLUMN is_stacked INTEGER DEFAULT 0;
    END IF;
END $$;

-- Set strategy to NOT NULL with default
ALTER TABLE settled_trades ALTER COLUMN strategy SET NOT NULL;
ALTER TABLE settled_trades ALTER COLUMN strategy SET DEFAULT 'main';

-- Backfill strategy_group from strategy
UPDATE settled_trades SET strategy_group =
    CASE
        WHEN strategy IN ('MAKER_PATIENT','TAKER_NOW','MAKER_AGGRESSIVE',
                          'PANIC_CAPTURE','CONFIRMATION_ADDON','DIP_ADDON')
             THEN 'main'
        WHEN strategy LIKE 'decided_%' THEN 'decided'
        WHEN strategy = '' THEN 'main'
        ELSE strategy
    END
WHERE strategy_group = 'main' OR strategy_group IS NULL;

-- Drop old primary key and add composite PK
DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    SELECT tc.constraint_name INTO constraint_name
    FROM information_schema.table_constraints tc
    WHERE tc.table_name = 'settled_trades'
      AND tc.constraint_type = 'PRIMARY KEY';

    IF constraint_name IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.key_column_usage
            WHERE table_name = 'settled_trades'
              AND constraint_name = constraint_name
              AND column_name = 'strategy_group'
        ) THEN
            EXECUTE format('ALTER TABLE settled_trades DROP CONSTRAINT %I', constraint_name);
            ALTER TABLE settled_trades ADD PRIMARY KEY (ticker, strategy_group);
        END IF;
    ELSE
        ALTER TABLE settled_trades ADD PRIMARY KEY (ticker, strategy_group);
    END IF;
END $$;
