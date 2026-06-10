# config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    # LLM
    gemini_api_key: str
    groq_api_key: str = ""          # https://console.groq.com/keys
    encryption_key: str = ""        # Chave Fernet de 32 bytes (base64)
    openrouter_api_key: str = ""    # https://openrouter.ai/keys

    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_number: str = "whatsapp:+14155238886"

    # Supabase
    supabase_url: str
    supabase_key: str

    # Pluggy
    pluggy_client_id: str = ""
    pluggy_client_secret: str = ""
    default_item_id: str = ""

    # App
    environment: str = "development"
    sync_interval_hours: int = 6
    finbot_persona: str = "default"

    # Cartão de crédito (fallback global — substituído por settings do usuário)
    cartao_dia_vencimento: int = 1
    cartao_dia_corte: int = 24

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
