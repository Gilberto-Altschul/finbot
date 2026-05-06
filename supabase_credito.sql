-- supabase_credito.sql
-- Execute no Supabase SQL Editor para adicionar suporte a cartão de crédito.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. Novas colunas em finbot_expenses ───────────────────────────────────────

ALTER TABLE finbot_expenses
  ADD COLUMN IF NOT EXISTS payment_method   TEXT DEFAULT 'debito'
    CHECK (payment_method IN ('debito', 'credito', 'dinheiro')),
  ADD COLUMN IF NOT EXISTS installment_of   INTEGER,
  ADD COLUMN IF NOT EXISTS installment_total INTEGER;

-- ── 2. Tabela de configurações por usuário ────────────────────────────────────

CREATE TABLE IF NOT EXISTS finbot_user_settings (
    user_phone            TEXT PRIMARY KEY,
    cartao_dia_vencimento INT  NOT NULL DEFAULT 1,
    cartao_dia_corte      INT  NOT NULL DEFAULT 24,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS trg_user_settings_updated_at ON finbot_user_settings;
CREATE TRIGGER trg_user_settings_updated_at
    BEFORE UPDATE ON finbot_user_settings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── 3. RPC: gastos por fatura ─────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION expenses_by_fatura(
    p_phone      TEXT,
    p_due_date   DATE,
    p_corte_day  INT
)
RETURNS TABLE(
    id BIGINT, amount NUMERIC, category TEXT,
    description TEXT, created_at TIMESTAMPTZ,
    payment_method TEXT, installment_of INT, installment_total INT
)
LANGUAGE SQL STABLE AS $$
    SELECT
        id, amount, category, description, created_at,
        payment_method, installment_of, installment_total
    FROM finbot_expenses
    WHERE user_phone = p_phone
      AND payment_method = 'credito'
      AND (
        CASE
          WHEN EXTRACT(DAY FROM created_at AT TIME ZONE 'America/Sao_Paulo') <= p_corte_day
            THEN (DATE_TRUNC('month', created_at AT TIME ZONE 'America/Sao_Paulo') + INTERVAL '1 month')::DATE
          ELSE (DATE_TRUNC('month', created_at AT TIME ZONE 'America/Sao_Paulo') + INTERVAL '2 months')::DATE
        END + (EXTRACT(DAY FROM p_due_date)::INT - 1)
      ) = p_due_date
    ORDER BY created_at DESC;
$$;
