"""
MobilityOne Configuration - Production Ready (v5.0)

FIXED:
- Proper swagger URLs with correct versions
- All 3 services: automation, tenantmgt, vehiclemgt
"""
from functools import lru_cache
from typing import List, Optional, Dict
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator


# =============================================================================
# SWAGGER SERVICE DEFINITIONS
# =============================================================================

SWAGGER_SERVICES: Dict[str, str] = {
    "automation": "v1.0.0",
    "tenantmgt": "v2.0.0-alpha",
    "vehiclemgt": "v2.0.0-alpha"
}


class Settings(BaseSettings):
    """Centralized configuration."""
    
    # --- APPLICATION ---
    APP_ENV: str = Field(default="development")
    
    # --- DATABASE ---
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://appuser:password@localhost:5432/mobility_tenants"
    )
    DB_PASSWORD: str = Field(default="")
    DB_POOL_SIZE: int = Field(default=10)
    DB_MAX_OVERFLOW: int = Field(default=20)
    DB_POOL_RECYCLE: int = Field(default=3600)
    
    # --- REDIS ---
    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    
    # --- INFOBIP ---
    INFOBIP_API_KEY: str = Field(default="")
    INFOBIP_BASE_URL: str = Field(default="api.infobip.com")
    INFOBIP_SECRET_KEY: str = Field(default="")
    INFOBIP_SENDER_NUMBER: str = Field(default="")
    
    # --- MOBILITYONE API ---
    MOBILITY_API_URL: str = Field(default="https://dev-k1.mobilityone.io")
    MOBILITY_AUTH_URL: str = Field(default="https://dev-k1.mobilityone.io/sso/connect/token")
    MOBILITY_CLIENT_ID: str = Field(default="m1AI")
    MOBILITY_CLIENT_SECRET: str = Field(default="")
    MOBILITY_SCOPE: Optional[str] = Field(default=None)
    MOBILITY_AUDIENCE: Optional[str] = Field(default=None)
    MOBILITY_API_TOKEN: Optional[str] = Field(default=None)
    
    # Tenant ID
    MOBILITY_TENANT_ID: Optional[str] = Field(default=None)
    DEFAULT_TENANT_ID: Optional[str] = Field(default=None)
    
    # --- AZURE OPENAI ---
    AZURE_OPENAI_ENDPOINT: str = Field(default="")
    AZURE_OPENAI_API_KEY: str = Field(default="")
    AZURE_OPENAI_API_VERSION: str = Field(default="2024-08-01-preview")
    AZURE_OPENAI_DEPLOYMENT_NAME: str = Field(default="gpt-4o-mini")
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str = Field(default="text-embedding-ada-002")
    AI_CONFIDENCE_THRESHOLD: float = Field(default=0.85)
    
    # --- SWAGGER (optional override) ---
    SWAGGER_URL: Optional[str] = Field(default=None)
    SWAGGER_SOURCES: Optional[str] = Field(default=None)
    
    # --- MONITORING ---
    SENTRY_DSN: Optional[str] = Field(default=None)
    GRAFANA_PASSWORD: str = Field(default="admin")
    
    @property
    def tenant_id(self) -> str:
        """Get tenant ID."""
        return self.MOBILITY_TENANT_ID or self.DEFAULT_TENANT_ID or ""
    
    @property
    def swagger_sources(self) -> List[str]:
        """
        Build ALL swagger source URLs.
        
        Format: {base_url}/{service}/swagger/{version}/swagger.json
        
        Services and versions:
        - automation: v1.0.0
        - tenantmgt: v2.0.0-alpha  
        - vehiclemgt: v2.0.0-alpha
        """
        base = self.MOBILITY_API_URL.rstrip("/")
        sources = []
        
        # Build URLs for all services
        for service, version in SWAGGER_SERVICES.items():
            url = f"{base}/{service}/swagger/{version}/swagger.json"
            sources.append(url)
        
        return sources
    
    @field_validator("MOBILITY_API_URL", mode="before")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/") if v else v
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()