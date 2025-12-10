from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from typing import Literal, Optional, List, Dict 

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
    #OPENAI_API_KEY: str
    #OPENAI_MODEL: str = "gpt-3.5-turbo"
    AZURE_OPENAI_ENDPOINT: str
    AZURE_OPENAI_API_KEY: str
    AZURE_OPENAI_API_VERSION: str = "2024-08-01-preview"
    AZURE_OPENAI_DEPLOYMENT_NAME: str = "gpt-4o-mini"
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str = "text-embedding-3-small"
    
    # --- 3RD PARTY ---
    INFOBIP_BASE_URL: str
    INFOBIP_API_KEY: str
    INFOBIP_SENDER_NUMBER: str
    INFOBIP_SECRET_KEY: str

    # --- MOBILITY ONE API ---
    MOBILITY_API_URL: str 
    MOBILITY_API_KEY: Optional[str] = None
    
    # Auth
    MOBILITY_AUTH_URL: Optional[str] = None
    MOBILITY_CLIENT_ID: Optional[str] = None
    MOBILITY_CLIENT_SECRET: Optional[str] = None
    MOBILITY_SCOPE: Optional[str] = None 
    MOBILITY_AUDIENCE: str = "none"

    MOBILITY_TENANT_ID: Optional[str] = None
    MOBILITY_USER_CHECK_ENDPOINT: str = "/PersonData/{personIdOrEmail}"
    
    # Opcionalni override
    SWAGGER_URL: Optional[str] = None

    ACTIVE_SERVICES: Dict[str, str] = {
        "automation": "v1.0.0",   
        "tenantmgt": "v2.0.0-alpha",     
        "vehiclemgt" : "v2.0.0-alpha"
    }

    @property
    def swagger_sources(self) -> List[str]:
        """
        Pametno generira listu svih Swagger URL-ova poštujući verzije.
        """
        sources = []

        # 1. Ručni override (ako postoji)
        if self.SWAGGER_URL:
            sources.append(self.SWAGGER_URL)

        # 2. Dinamičko generiranje linkova
        if self.MOBILITY_API_URL and self.MOBILITY_API_URL.startswith("http"):
            base_url = self.MOBILITY_API_URL.rstrip('/')
            
            # [POPRAVAK] Iteriramo kroz (servis, verzija)
            for service, version in self.ACTIVE_SERVICES.items():
                # Format: .../{service}/swagger/{version}/swagger.json
                url = f"{base_url}/{service}/swagger/{version}/swagger.json"
                sources.append(url)
        else:
            # Fallback na lokalne datoteke
            for service in self.ACTIVE_SERVICES.keys():
                sources.append(f"temporary/{service}.json")

        return list(set(sources))
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

@lru_cache()
def get_settings() -> Settings:
    return Settings()