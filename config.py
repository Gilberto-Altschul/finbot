# config.py
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # LLM
    gemini_api_key: str
    groq_api_key: str = ""          # https://console.groq.com/keys
    openrouter_api_key: str = ""    # https://openrouter.ai/keys

    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_number: str = "whatsapp:+14155238886"

    # Supabase
    supabase_url: str
    supabase_key: str

    # App
    environment: str = "development"

    # Cartão de crédito (fallback global — substituído por settings do usuário)
    cartao_dia_vencimento: int = 1
    cartao_dia_corte: int = 24

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
