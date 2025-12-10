import httpx
import structlog
import asyncio
import logging
import redis.asyncio as redis
from redis.exceptions import LockError
from datetime import timedelta 
from typing import Dict, Any, Optional
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryError 
)
from aiobreaker import CircuitBreaker, CircuitBreakerError
import json

from config import get_settings

logger = structlog.get_logger("openapi_bridge")
settings = get_settings()

# --- KONFIGURACIJA CIRCUIT BREAKERA ---
api_breaker = CircuitBreaker(fail_max=5, timeout_duration=timedelta(seconds=30))

class OpenAPIGateway:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        
        # Connection Pooling
        self.limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        
        headers = {}
        # [POPRAVAK] Sada koristimo KEY umjesto TOKEN, jer se tako zove u config.py
        if hasattr(settings, "MOBILITY_API_KEY") and settings.MOBILITY_API_KEY:
            headers["Authorization"] = f"Bearer {settings.MOBILITY_API_KEY}"

        self.client = httpx.AsyncClient(timeout=15.0, limits=self.limits, headers=headers)
        
        # Inicijalizacija Redisa za distribuirano zaključavanje
        redis_url = getattr(settings, "REDIS_URL", "redis://redis:6379/0")
        self.redis = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)

    async def execute_tool(self, tool_def: dict, params: dict, user_context: dict = None) -> Any:
        method = tool_def["method"].upper()
        path = tool_def["path"]
        
        path_params = {}
        query_params = {}
        body_params = {}
        
        # Razvrstavanje parametara
        for key, value in params.items():
            if f"{{{key}}}" in path:
                path_params[key] = value
            elif method in ["POST", "PUT", "PATCH"]:
                body_params[key] = value
            else:
                query_params[key] = value

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Tenant Injection
        if user_context and user_context.get("tenant_id"):
            headers["x-tenant"] = user_context["tenant_id"]

        # Finalizacija URL-a
        try:
            final_url = f"{self.base_url}{path.format(**path_params)}"
        except KeyError as e:
            raise Exception(f"Fali obavezni parametar u URL-u: {str(e)}")
        
        # ================================================================
        # 🕵️ SPY LOG 1: ŠTO ŠALJEMO?
        # ================================================================
        logger.warning(f"🚀 [API REQUEST] {method} {final_url}")
        logger.warning(f"📦 [API PAYLOAD] Query: {query_params} | Body: {body_params}")
        # ================================================================

        try:
            response_data = await self._do_request(
                method, 
                final_url, 
                params=query_params if query_params else None,
                json=body_params if body_params else None,
                headers=headers 
            )
            
            # ================================================================
            # 🕵️ SPY LOG 2: ŠTO SMO DOBILI?
            # ================================================================
            logger.warning(f"✅ [API RESULT] Data: {str(response_data)[:500]}...") # Prvih 500 znakova
            # ================================================================

            if isinstance(response_data, dict) and response_data.get("error"):
                 raise Exception(response_data.get("message"))

            return response_data

        except httpx.HTTPStatusError as e:
            # Ovo će uhvatiti 404, 400, 500
            error_msg = f"❌ API HTTP Error {e.response.status_code}: {e.response.text}"
            logger.error(error_msg)
            return {"error": f"API Error: {e.response.status_code}"} # Vrati AI-u da zna da je greška
            
        except Exception as e:
            logger.error("❌ System Error during API call", error=str(e))
            raise e

    @api_breaker
    @retry(     
        stop=stop_after_attempt(3), 
        wait=wait_exponential(multiplier=1, min=1, max=10), 
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)), 
        before_sleep=before_sleep_log(logger, logging.WARNING) 
    )
    async def _do_request(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        response = await self.client.request(method, url, **kwargs)

        # Token Refresh Logika... (tvoja postojeća logika)
        if response.status_code == 401:
            logger.info("Token expired (401), attempting distributed refresh...")
            refreshed = await self._secure_refresh_token()
            if refreshed:
                if "headers" in kwargs:
                    kwargs["headers"]["Authorization"] = self.client.headers["Authorization"]
                else:
                    kwargs["headers"] = {"Authorization": self.client.headers["Authorization"]}
                response = await self.client.request(method, url, **kwargs)
            else:
                logger.error("Token refresh failed.")
                return {"error": True, "message": "Auth failed"}

        # [BITNO] Ako API vrati 404 Not Found, httpx ne diže error sam od sebe ako ne zoveš raise_for_status()
        # Ali mi želimo vidjeti logove prije nego pukne.
        
        if response.status_code >= 400:
            logger.error(f"🛑 API FAIL {response.status_code} Body: {response.text}")
            response.raise_for_status() # Ovo baca exception koji execute_tool hvata
        
        if response.status_code == 204:
            return {"status": "success", "data": None}
            
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"response_text": response.text}
            
    async def _secure_refresh_token(self) -> bool:
        """
        Koristi Redis Lock da osigura da samo jedan worker osvježava token.
        """
        if not settings.MOBILITY_AUTH_URL: return False

        TOKEN_KEY = "mobility_access_token"
        LOCK_KEY = "mobility_token_refresh_lock"

        # 1. Prvo provjeri u Redisu (možda ga je netko već osvježio)
        cached_token = await self.redis.get(TOKEN_KEY)
        if cached_token:
            current_token = self.client.headers.get("Authorization", "").replace("Bearer ", "")
            if cached_token != current_token:
                self.client.headers["Authorization"] = f"Bearer {cached_token}"
                logger.info("Loaded fresh token from Redis cache (Fast Path).")
                return True

        # 2. Pokušaj zaključati proces (Distributed Lock)
        try:
            async with self.redis.lock(LOCK_KEY, timeout=10, blocking_timeout=2.0):
                
                # Double-check inside lock
                cached_token = await self.redis.get(TOKEN_KEY)
                if cached_token:
                    current_token = self.client.headers.get("Authorization", "").replace("Bearer ", "")
                    if cached_token != current_token:
                        self.client.headers["Authorization"] = f"Bearer {cached_token}"
                        return True

                logger.info("Acquired lock. Refreshing token via OAuth2...")
                
                payload = {
                    "client_id": settings.MOBILITY_CLIENT_ID,
                    "client_secret": settings.MOBILITY_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                    "audience": settings.MOBILITY_AUDIENCE
                }
                
                if settings.MOBILITY_SCOPE and settings.MOBILITY_SCOPE.strip():
                    payload["scope"] = settings.MOBILITY_SCOPE
                
                async with httpx.AsyncClient() as auth_client:
                    resp = await auth_client.post(settings.MOBILITY_AUTH_URL, data=payload, timeout=10.0)
                    resp.raise_for_status()
                    
                    data = resp.json()
                    new_token = data.get("access_token")
                    expires_in = data.get("expires_in", 3600)
                    
                    if new_token:
                        await self.redis.set(TOKEN_KEY, new_token, ex=int(expires_in) - 60)
                        self.client.headers["Authorization"] = f"Bearer {new_token}"
                        logger.info("Token refreshed and saved to Redis.")
                        return True
                        
        except LockError:
            logger.info("Lock is busy. Waiting for other worker...")
            await asyncio.sleep(1.0)
            cached_token = await self.redis.get(TOKEN_KEY)
            if cached_token:
                self.client.headers["Authorization"] = f"Bearer {cached_token}"
                return True
            return False

        except Exception as e:
            logger.error("Token refresh critical failure", error=str(e))
            return False
            
        return False

    async def close(self):
        await self.client.aclose()
        await self.redis.close()