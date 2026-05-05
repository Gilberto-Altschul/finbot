# 🤖 FinBot — WhatsApp Finance Agent

Agente de finanças pessoais via WhatsApp. O LLM é o cérebro: decide o que fazer, quando chamar ferramentas e como responder.

**Stack:** Python · FastAPI · Google Gemini Flash · Groq Llama 3.3 (fallback) · Supabase · Twilio

---

## Como funciona

```
Usuário: "almoço 35"
    ↓
  LLM interpreta → chama registrar_gasto(35, Alimentação, almoço)
    ↓
  Tool executa → salva no Supabase, retorna totais
    ↓
  LLM formata → "✅ R$ 35,00 registrado! Total em Alimentação: R$ 320,50"
    ↓
  WhatsApp
```

---

## Setup

### 1. Clone e instale

```bash
git clone https://github.com/seu-usuario/finbot
cd finbot

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure o banco (Supabase)

Acesse o **SQL Editor** do Supabase e execute o arquivo `supabase_setup.sql`.

### 3. Configure as variáveis de ambiente

```bash
cp .env.example .env
# Edite .env com suas chaves
```

| Variável | Onde obter | Custo |
|---|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com/app/apikey) | Gratuito |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com/keys) | Gratuito |
| `TWILIO_*` | [console.twilio.com](https://console.twilio.com) | Pago por msg |
| `SUPABASE_URL/KEY` | Dashboard do seu projeto Supabase | Gratuito |

### 4. Rode localmente

```bash
uvicorn main:app --reload
```

### 5. Exponha o webhook

```bash
npx ngrok http 8000
```

Configure no Twilio Sandbox:
`When a message comes in → https://xxxx.ngrok.io/webhook`

---

## Ferramentas do agente

| Tool | Quando o LLM usa |
|---|---|
| `registrar_gasto` | Qualquer menção a gasto/compra |
| `resumo_mensal` | "resumo", "quanto gastei" |
| `total_categoria` | "quanto gastei com X" |
| `ultimos_gastos` | "histórico", "últimos gastos" |
| `tendencia_semanal` | "como estou essa semana" |

---

## Comandos

```bash
pytest              # testes
uvicorn main:app --reload   # dev
uvicorn main:app            # produção
```

---

## GitHub Actions

CI roda automaticamente em todo push. Secrets necessários no GitHub:
`GEMINI_API_KEY` · `GROQ_API_KEY` · `TWILIO_ACCOUNT_SID` · `TWILIO_AUTH_TOKEN` · `SUPABASE_URL` · `SUPABASE_KEY`

---

## Adicionando novas ferramentas

Edite `tools.py`:
1. Adicione o schema em `SCHEMAS`
2. Adicione o `case` em `execute()`

O LLM começa a usar automaticamente — sem mais nenhuma mudança.
