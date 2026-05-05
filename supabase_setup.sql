-- supabase_setup.sql
-- Run this in the Supabase SQL Editor (once)
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Tables ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS finbot_expenses (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone  TEXT        NOT NULL,
    amount      NUMERIC(10,2) NOT NULL CHECK (amount > 0),
    category    TEXT        NOT NULL,
    description TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS finbot_conversation (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone  TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exp_phone ON finbot_expenses(user_phone);
CREATE INDEX IF NOT EXISTS idx_exp_date  ON finbot_expenses(created_at);
CREATE INDEX IF NOT EXISTS idx_conv_phone ON finbot_conversation(user_phone, created_at);

-- ── RPC functions (required by database.py) ───────────────────────────────────

-- Total per category for the current month
CREATE OR REPLACE FUNCTION expenses_by_category(p_phone TEXT)
RETURNS TABLE(category TEXT, total NUMERIC, count BIGINT)
LANGUAGE SQL STABLE AS $$
    SELECT
        category,
        ROUND(SUM(amount)::NUMERIC, 2) AS total,
        COUNT(*) AS count
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
    GROUP BY category
    ORDER BY total DESC;
$$;

-- Grand total for the current month
CREATE OR REPLACE FUNCTION expenses_monthly_total(p_phone TEXT)
RETURNS NUMERIC
LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(ROUND(SUM(amount)::NUMERIC, 2), 0)
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW());
$$;

-- Total for a specific category in the current month
CREATE OR REPLACE FUNCTION expenses_category_total(p_phone TEXT, p_category TEXT)
RETURNS NUMERIC
LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(ROUND(SUM(amount)::NUMERIC, 2), 0)
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND LOWER(category) = LOWER(p_category)
      AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW());
$$;

-- Daily spending for the last N days
CREATE OR REPLACE FUNCTION expenses_daily_trend(p_phone TEXT, p_days INT DEFAULT 7)
RETURNS TABLE(day TEXT, total NUMERIC)
LANGUAGE SQL STABLE AS $$
    SELECT
        TO_CHAR(DATE_TRUNC('day', created_at AT TIME ZONE 'America/Sao_Paulo'), 'DD/MM') AS day,
        ROUND(SUM(amount)::NUMERIC, 2) AS total
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND created_at >= NOW() - (p_days || ' days')::INTERVAL
    GROUP BY DATE_TRUNC('day', created_at AT TIME ZONE 'America/Sao_Paulo')
    ORDER BY DATE_TRUNC('day', created_at AT TIME ZONE 'America/Sao_Paulo');
$$;
