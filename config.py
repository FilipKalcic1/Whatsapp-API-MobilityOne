from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from typing import Literal, Optional

class Settings(BaseSettings):
    APP_ENV: Literal["development", "production", "testing"] = "development"
    
    # --- INFRASTRUCTURE ---
    REDIS_URL: str
    DATABASE_URL: str
    SENTRY_DSN: Optional[str] = None

    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 3600
    
    # --- AI CONFIGURATION ---
    OPENAI_API_KEY: str
    OPENAI_MODEL: str = "gpt-3.5-turbo"
    
    # --- 3RD PARTY ---
    INFOBIP_BASE_URL: str
    INFOBIP_API_KEY: str
    INFOBIP_SENDER_NUMBER: str
    INFOBIP_SECRET_KEY: str

    # --- MOBILITY ONE API ---
    MOBILITY_API_URL: str 
    MOBILITY_API_TOKEN: Optional[str] = None
    
    # Auth
    MOBILITY_AUTH_URL: Optional[str] = None
    MOBILITY_CLIENT_ID: Optional[str] = None
    MOBILITY_CLIENT_SECRET: Optional[str] = None
    MOBILITY_SCOPE: str = "add-case"
    
    # [NOVO] Ovo je falilo
    MOBILITY_USER_CHECK_ENDPOINT: str = "/PersonData/{personIdOrEmail}"
    
    SWAGGER_URL: Optional[str] = None
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

@lru_cache()
def get_settings() -> Settings:
    return Settings()