-- supabase_setup.sql
-- Run this in the Supabase SQL Editor (once)
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Tables ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS finbot_expenses (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone  TEXT        NOT NULL,
    amount      NUMERIC(10,2) NOT NULL CHECK (amount >= 0),
    category    TEXT        NOT NULL,
    subcategory TEXT,
    transaction_type TEXT DEFAULT 'expense' CHECK (transaction_type IN ('expense', 'income')),
    beneficiario TEXT,
    description TEXT        NOT NULL,
    pluggy_transaction_id TEXT UNIQUE,
    payment_method   TEXT DEFAULT 'debito' CHECK (payment_method IN ('debito', 'credito', 'dinheiro')),
    installment_of   INTEGER,
    installment_total INTEGER,
    purchase_date    DATE,
    billing_date     DATE,
    created_at  DATE NOT NULL DEFAULT CURRENT_DATE
);

CREATE TABLE IF NOT EXISTS finbot_user_connections (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone  TEXT NOT NULL UNIQUE,
    pluggy_item_id TEXT NULL, -- Explicitly nullable, como pretendido pelo uso no código Python
    pending_pdf_url TEXT,
    status      TEXT DEFAULT 'ativo',
    cartao_dia_vencimento INT  NOT NULL DEFAULT 1,
    cartao_dia_corte      INT  NOT NULL DEFAULT 24,
    created_at  DATE DEFAULT CURRENT_DATE
);

-- Garante que pluggy_item_id seja anulável, caso a tabela já exista e a coluna não seja
ALTER TABLE finbot_user_connections
ALTER COLUMN pluggy_item_id DROP NOT NULL;


CREATE TABLE IF NOT EXISTS finbot_conversation (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone  TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  DATE NOT NULL DEFAULT CURRENT_DATE
);

CREATE INDEX IF NOT EXISTS idx_exp_phone ON finbot_expenses(user_phone);
CREATE INDEX IF NOT EXISTS idx_exp_date  ON finbot_expenses(created_at);
CREATE INDEX IF NOT EXISTS idx_conv_phone ON finbot_conversation(user_phone, created_at);

CREATE TABLE IF NOT EXISTS finbot_budgets (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone  TEXT NOT NULL,
    category    TEXT NOT NULL,
    amount      NUMERIC(10,2) NOT NULL CHECK (amount >= 0),
    mes_referencia TEXT NOT NULL, -- Formato YYYY-MM
    created_at  DATE DEFAULT CURRENT_DATE,
    UNIQUE(user_phone, category, mes_referencia)
);

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
      AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)
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
      AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE);
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
      AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE);
$$;

-- Grand total income for the current month
CREATE OR REPLACE FUNCTION income_monthly_total(p_phone TEXT)
RETURNS NUMERIC
LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(ROUND(SUM(amount)::NUMERIC, 2), 0)
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND transaction_type = 'income'
      AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE);
$$;

-- Daily spending for the last N days
CREATE OR REPLACE FUNCTION expenses_daily_trend(p_phone TEXT, p_days INT DEFAULT 7)
RETURNS TABLE(day TEXT, total NUMERIC)
LANGUAGE SQL STABLE AS $$
    SELECT
        TO_CHAR(created_at, 'DD/MM') AS day,
        ROUND(SUM(amount)::NUMERIC, 2) AS total
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND transaction_type = 'expense'
      AND created_at >= CURRENT_DATE - p_days
    GROUP BY created_at
    ORDER BY created_at;
$$;

-- Gastos de uma fatura específica (Crédito)
-- Busca todos os gastos onde payment_method é 'credito', sejam parcelados ou não.
DROP FUNCTION IF EXISTS expenses_by_fatura(TEXT, DATE, INT);
CREATE OR REPLACE FUNCTION expenses_by_fatura(p_phone TEXT, p_due_date DATE, p_corte_day INT)
RETURNS TABLE(id BIGINT, amount NUMERIC, category TEXT, description TEXT, created_at DATE, purchase_date DATE, installment_of INT, installment_total INT)
LANGUAGE SQL STABLE AS $$
    SELECT
        id,
        amount,
        category,
        description,
        created_at,
        purchase_date,
        installment_of,
        installment_total
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND payment_method = 'credito'
      AND transaction_type = 'expense'
      AND DATE_TRUNC('month', billing_date) = DATE_TRUNC('month', p_due_date)
    ORDER BY purchase_date DESC;
$$;

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
RETURNS TABLE(mes_referencia TEXT, amount NUMERIC, created_at DATE)
LANGUAGE SQL STABLE AS $$
    SELECT mes_referencia, amount, created_at
    FROM finbot_budgets
    WHERE user_phone = p_phone AND LOWER(category) = LOWER(p_category)
    ORDER BY mes_referencia DESC;
$$;

-- ── Merchant Learning & Categories ──────────────────────────────────────────

-- Tabela de aprendizado do FinBot ( Merchant -> Categoria )
CREATE TABLE IF NOT EXISTS finbot_merchant_mappings (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone      TEXT NOT NULL,
    merchant_name   TEXT NOT NULL,
    category        TEXT NOT NULL,
    subcategory     TEXT NOT NULL,
    created_at      DATE DEFAULT CURRENT_DATE,
    UNIQUE(user_phone, merchant_name)
);

-- Tabelas auxiliares para categorização global e emojis
CREATE TABLE IF NOT EXISTS finbot_categories (
    id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    emoji TEXT
);

CREATE TABLE IF NOT EXISTS finbot_subcategories (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_name TEXT NOT NULL,
    name          TEXT NOT NULL,
    keywords      TEXT[] DEFAULT '{}',
    created_at    DATE DEFAULT CURRENT_DATE
);

CREATE INDEX IF NOT EXISTS idx_merchant_phone ON finbot_merchant_mappings(user_phone);

-- ── Dados de Inicialização (Categorias e Subcategorias) ──────────────────────

INSERT INTO finbot_categories (name, emoji) VALUES
('Moradia', '🏠'),
('Alimentação', '🍔'),
('Transporte', '🚗'),
('Saúde', '💊'),
('Lazer', '🎬'),
('Vestuário e Beleza', '👕'),
('Educação', '📚'),
('Financeiro', '💳'),
('Pets', '🐾'),
('Receitas', '💰'),
('Família e Dependentes', '👨‍👩‍👧'),
('Empresa', '💼')
ON CONFLICT (name) DO NOTHING;
-- Adiciona restrição de unicidade para evitar duplicatas nas subcategorias
ALTER TABLE finbot_subcategories ADD CONSTRAINT IF NOT EXISTS unique_cat_sub UNIQUE (category_name, name);

INSERT INTO finbot_subcategories (category_name, name, keywords)
VALUES 
('Família e Dependentes', 'Mesada', ARRAY['mesada', 'filhos', 'transferencia mensal']),
('Família e Dependentes', 'Pensão', ARRAY['pensao', 'alimenticia']),
('Família e Dependentes', 'Apoio Familiar', ARRAY['ajuda', 'pais', 'irmaos', 'parentes']),
('Família e Dependentes', 'Presente Familiar', ARRAY['aniversario', 'formatura', 'casamento', 'presente']),
('Família e Dependentes', 'Emergência Familiar', ARRAY['saude urgente', 'conserto', 'necessidade']),
('Família e Dependentes', 'Empréstimo Pessoal', ARRAY['emprestimo', 'vai receber'])
ON CONFLICT (category_name, name) DO NOTHING;
