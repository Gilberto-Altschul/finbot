-- supabase_budget.sql
-- Execute no Supabase SQL Editor para adicionar suporte a orçamento por categoria.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Tabela de orçamento ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS finbot_budgets (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone    TEXT        NOT NULL,
    category      TEXT        NOT NULL,
    amount        NUMERIC(10,2) NOT NULL CHECK (amount > 0),
    mes_referencia TEXT        NOT NULL,  -- formato 'YYYY-MM'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_budgets_phone  ON finbot_budgets(user_phone);
CREATE INDEX IF NOT EXISTS idx_budgets_mes    ON finbot_budgets(user_phone, mes_referencia);

-- ── RPC: orçamento atual de uma categoria ────────────────────────────────────
-- Retorna o limite mais recente para o mês informado ou anterior mais próximo.

CREATE OR REPLACE FUNCTION budget_get(p_phone TEXT, p_category TEXT, p_mes TEXT)
RETURNS NUMERIC
LANGUAGE SQL STABLE AS $$
    SELECT amount
    FROM finbot_budgets
    WHERE user_phone = p_phone
      AND LOWER(category) = LOWER(p_category)
      AND mes_referencia <= p_mes
    ORDER BY mes_referencia DESC
    LIMIT 1;
$$;

-- ── RPC: todos os limites do mês atual ───────────────────────────────────────

CREATE OR REPLACE FUNCTION budget_all(p_phone TEXT, p_mes TEXT)
RETURNS TABLE(category TEXT, amount NUMERIC)
LANGUAGE SQL STABLE AS $$
    SELECT DISTINCT ON (LOWER(category))
        category,
        amount
    FROM finbot_budgets
    WHERE user_phone = p_phone
      AND mes_referencia <= p_mes
    ORDER BY LOWER(category), mes_referencia DESC;
$$;

-- ── RPC: histórico de um limite por categoria ─────────────────────────────────

CREATE OR REPLACE FUNCTION budget_history(p_phone TEXT, p_category TEXT)
RETURNS TABLE(mes_referencia TEXT, amount NUMERIC, created_at TIMESTAMPTZ)
LANGUAGE SQL STABLE AS $$
    SELECT mes_referencia, amount, created_at
    FROM finbot_budgets
    WHERE user_phone = p_phone
      AND LOWER(category) = LOWER(p_category)
    ORDER BY mes_referencia DESC
    LIMIT 12;
$$;
