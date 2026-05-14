-- supabase_setup.sql
-- Run this in the Supabase SQL Editor (once)
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Tables ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS finbot_expenses (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone  TEXT        NOT NULL,
    amount      NUMERIC(10,2) NOT NULL CHECK (amount > 0),
    category    TEXT        NOT NULL,
    subcategory TEXT,
    transaction_type TEXT DEFAULT 'expense' CHECK (transaction_type IN ('expense', 'income')),
    beneficiario TEXT,
    description TEXT        NOT NULL,
    pluggy_transaction_id TEXT UNIQUE,
    payment_method   TEXT DEFAULT 'debito' CHECK (payment_method IN ('debito', 'credito', 'dinheiro')),
    installment_of   INTEGER,
    installment_total INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS finbot_user_connections (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone  TEXT NOT NULL UNIQUE,
    pluggy_item_id TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
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
-- NOTA: Todas as funções usam created_at::DATE para evitar problemas de fuso
-- horário. Datas manuais são salvas como T12:00:00Z e comparadas apenas pela
-- data, sem conversão de timezone.

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
      AND transaction_type = 'expense'
      AND DATE_TRUNC('month', created_at::DATE) = DATE_TRUNC('month', CURRENT_DATE)
    GROUP BY category
    ORDER BY total DESC;
$$;

-- Grand total expenses for the current month
CREATE OR REPLACE FUNCTION expenses_monthly_total(p_phone TEXT)
RETURNS NUMERIC
LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(ROUND(SUM(amount)::NUMERIC, 2), 0)
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND transaction_type = 'expense'
      AND DATE_TRUNC('month', created_at::DATE) = DATE_TRUNC('month', CURRENT_DATE);
$$;

-- Total for a specific category in the current month
CREATE OR REPLACE FUNCTION expenses_category_total(p_phone TEXT, p_category TEXT)
RETURNS NUMERIC
LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(ROUND(SUM(amount)::NUMERIC, 2), 0)
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND transaction_type = 'expense'
      AND LOWER(category) = LOWER(p_category)
      AND DATE_TRUNC('month', created_at::DATE) = DATE_TRUNC('month', CURRENT_DATE);
$$;

-- Grand total income for the current month
CREATE OR REPLACE FUNCTION income_monthly_total(p_phone TEXT)
RETURNS NUMERIC
LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(ROUND(SUM(amount)::NUMERIC, 2), 0)
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND transaction_type = 'income'
      AND DATE_TRUNC('month', created_at::DATE) = DATE_TRUNC('month', CURRENT_DATE);
$$;

-- Daily spending for the last N days
CREATE OR REPLACE FUNCTION expenses_daily_trend(p_phone TEXT, p_days INT DEFAULT 7)
RETURNS TABLE(day TEXT, total NUMERIC)
LANGUAGE SQL STABLE AS $$
    SELECT
        TO_CHAR(created_at::DATE, 'DD/MM') AS day,
        ROUND(SUM(amount)::NUMERIC, 2) AS total
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND transaction_type = 'expense'
      AND created_at::DATE >= CURRENT_DATE - p_days
    GROUP BY created_at::DATE
    ORDER BY created_at::DATE;
$$;
