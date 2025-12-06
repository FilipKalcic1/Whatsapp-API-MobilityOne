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
        """
        Izvršava API poziv, automatski ubacuje sve potrebne headere (Auth i x-tenant)
        i delegira poziv na _do_request (koji hendla token refresh).
        """
        method = tool_def["method"].upper()
        path = tool_def["path"]
        
        # Inicijalizacija spremnika
        path_params = {}
        query_params = {}
        body_params = {}
        
        # Razvrstavanje parametara (Path vs Query vs Body)
        for key, value in params.items():
            if f"{{{key}}}" in path:
                path_params[key] = value
            elif method in ["POST", "PUT", "PATCH"]:
                body_params[key] = value
            else:
                query_params[key] = value

        # Priprema HTTP zaglavlja
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # 1. Tenant Injection (CRITICAL)
        if user_context and user_context.get("tenant_id"):
            headers["x-tenant"] = user_context["tenant_id"]

        # 2. Finalizacija URL-a
        try:
            final_url = f"{self.base_url}{path.format(**path_params)}"
        except KeyError as e:
            raise Exception(f"AI je zaboravio obavezni path parametar: {str(e)}")
        
        try:
            # DELEGACIJA POZIVA na _do_request za automatski token refresh
            response_data = await self._do_request(
                method, 
                final_url, 
                params=query_params if query_params else None,
                json=body_params if body_params else None,
                headers=headers 
            )
            
            # Ako _do_request vrati grešku (nakon 401 i propalog refresh-a)
            if isinstance(response_data, dict) and response_data.get("error"):
                 raise Exception(response_data.get("message"))

            # _do_request već vraća parsirani JSON ili status 204
            return response_data

        except httpx.HTTPStatusError as e:
            error_msg = f"API Error {e.response.status_code}: {e.response.text}"
            logger.error("API Call Failed", url=final_url, error=error_msg)
            raise Exception(error_msg)
        except Exception as e:
            logger.error("System Error during API call", error=str(e))
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

        # --- Token Refresh Logika (Distribuirana) ---
        if response.status_code == 401:
            logger.info("Token expired (401), attempting distributed refresh...")
            
            refreshed = await self._secure_refresh_token()
            
            if refreshed:
                # Ažuriraj header za ponovni pokušaj
                if "headers" in kwargs:
                    kwargs["headers"]["Authorization"] = self.client.headers["Authorization"]
                else:
                    kwargs["headers"] = {"Authorization": self.client.headers["Authorization"]}
                
                # Manual retry call
                response = await self.client.request(method, url, **kwargs)
            else:
                logger.error("Token refresh failed.")
                return {"error": True, "message": "Autorizacija neuspjela."}

        if response.status_code >= 500:
            response.raise_for_status()

        response.raise_for_status()
        
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