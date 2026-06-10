-- ── 1. Tabela de Categorias Customizadas ─────────────────────────────────────
-- Permite remover o hardcode do agent.py e tools.py
CREATE TABLE IF NOT EXISTS finbot_categories (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone  TEXT NOT NULL REFERENCES finbot_user_settings(user_phone),
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('expense', 'income')),
    created_at  DATE DEFAULT CURRENT_DATE,
    UNIQUE(user_phone, name)
);

-- ── 2. Tabela de Itens Recorrentes ──────────────────────────────────────────
-- Para assinaturas, contas fixas e salários
CREATE TABLE IF NOT EXISTS finbot_recurring (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_phone      TEXT NOT NULL REFERENCES finbot_user_settings(user_phone),
    amount          NUMERIC(10,2) NOT NULL,
    category        TEXT NOT NULL,
    description     TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN ('expense', 'income')),
    day_of_month    INT NOT NULL CHECK (day_of_month BETWEEN 1 AND 31),
    payment_method  TEXT DEFAULT 'debito',
    active          BOOLEAN DEFAULT TRUE,
    created_at      DATE DEFAULT CURRENT_DATE
);

-- Adicionar índices para performance
CREATE INDEX idx_categories_user ON finbot_categories(user_phone);
CREATE INDEX idx_recurring_user ON finbot_recurring(user_phone);