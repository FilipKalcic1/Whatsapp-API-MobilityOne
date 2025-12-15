import httpx
import structlog
import asyncio
import json
import re 
from datetime import datetime, timedelta 
from typing import Dict, Any, Optional

# Resilience Libraries
from tenacity import retry, stop_after_attempt, wait_exponential
from aiobreaker import CircuitBreaker

# Config & Infrastructure
from config import get_settings
import redis.asyncio as redis

# --- KONFIGURACIJA ---
logger = structlog.get_logger("openapi_bridge")
settings = get_settings()

# Circuit Breaker konfiguracija
api_breaker = CircuitBreaker(fail_max=5, timeout_duration=timedelta(seconds=30))

class OpenAPIGateway:
    def __init__(self, base_url: str):
        """
        EXECUTOR MODULE (vFINAL - FULL FIX)
        - Auth: Client Credentials (Robot) + Header Impersonation (Korisnik).
        - Fixes: Rješava 403 (sub claim), 400 (grant type) i AttributeError.
        """
        self.base_url = base_url.rstrip('/')
        self.limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        
        headers = {}
        if hasattr(settings, "MOBILITY_API_KEY") and settings.MOBILITY_API_KEY:
            headers["Authorization"] = f"Bearer {settings.MOBILITY_API_KEY}"

        self.client = httpx.AsyncClient(timeout=30.0, limits=self.limits, headers=headers)
        
        redis_url = getattr(settings, "REDIS_URL", "redis://redis:6379/0")
        self.redis = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        self.token_expires_at = datetime.utcnow()

    async def execute_tool(self, tool_def: dict, params: dict, user_context: dict = None) -> Any:
        """
        Glavna metoda za izvršavanje API poziva.
        """
        # 1. Osiguraj token (Robotski) prije poziva
        await self._ensure_token_validity()
        
        raw_path = tool_def["path"]
        method = tool_def["method"].upper()
        
        try:
            # 2. Raspodjela parametara
            path_params, query_params, body_params, ai_headers = self._distribute_parameters(raw_path, method, params)

            # --- AUTO-INJECTION (DriverId) ---
            # Ubacujemo tvoj ID direktno u parametre funkcije
            person_id = None
            if user_context and user_context.get("person_id"):
                person_id = user_context["person_id"]
                
                # Za GET (Pretraga)
                if method == "GET" and "driverid" not in [k.lower() for k in query_params]:
                    query_params["driverId"] = person_id
                # Za POST (Booking)
                if method == "POST" and "assignedtoid" not in [k.lower() for k in body_params]:
                    body_params["AssignedToId"] = person_id
            # ---------------------------------

            # Validacija path parametara
            missing = [var for var in re.findall(r"\{([a-zA-Z0-9_\-]+)\}", raw_path) if var not in path_params]
            if missing: return {"error": "MissingParameter", "message": f"Fali: {missing}"}

            # 3. URL
            final_url = raw_path.format(**path_params)
            if not final_url.startswith("http"):
                base = self.base_url.rstrip("/")
                path = final_url.lstrip("/")
                final_url = f"{base}/{path}"

            # 4. HEADER PREPARATION
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

            # Tenant Strategija
            tenant_id = None
            if user_context: tenant_id = user_context.get("tenant_id")
            if not tenant_id: tenant_id = ai_headers.get("x-tenant")
            if not tenant_id: tenant_id = getattr(settings, "DEFAULT_TENANT_ID", None)
            
            # Fallback Tenant
            fallback_tenant = person_id

            # Postavi Tenant Header
            current_tenant = tenant_id or fallback_tenant
            if current_tenant: 
                headers["x-tenant"] = str(current_tenant)

            # 🚨 IMPERSONATION FIX 🚨
            # Backend traži 'sub' (User ID) u tokenu. Budući da koristimo robotski token koji ga nema,
            # šaljemo ID korisnika u headerima kako bi backend middleware prepoznao tko smo.
            if person_id:
                headers["x-user-id"] = person_id
                headers["sub"] = person_id 
                headers["x-driver-id"] = person_id
                logger.info(f"🎭 Impersonating User: {person_id}")

            # LOGGING
            if method in ["POST", "PUT"]:
                 logger.info(f"🚀 API REQUEST: {method} {final_url}", tenant=current_tenant, BODY=str(body_params)[:200])
            else:
                 logger.info(f"🚀 API REQUEST: {method} {final_url}", tenant=current_tenant, PARAMS=query_params)

            # 5. EXECUTE
            response = await self._do_request(
                method, final_url, params=query_params, 
                json=body_params if method in ["POST", "PUT", "PATCH"] else None, 
                headers=headers
            )

            # 6. SMART 403 RETRY
            # Ako prvi tenant ne valja, probaj fallback na person_id
            is_403 = isinstance(response, dict) and (response.get("status") == 403 or "Forbidden" in str(response))
            
            if is_403 and fallback_tenant and str(fallback_tenant) != str(current_tenant):
                logger.warning(f"⚠️ 403 Forbidden. Retrying with Fallback Tenant: {fallback_tenant}")
                headers["x-tenant"] = str(fallback_tenant)
                response = await self._do_request(
                    method, final_url, params=query_params, 
                    json=body_params if method in ["POST", "PUT", "PATCH"] else None, 
                    headers=headers
                )

            return response

        except Exception as e:
            logger.error(f"❌ Gateway failed: {str(e)}", exc_info=True)
            return {"error": "GatewayError", "details": str(e)}

    @api_breaker
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _do_request(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        """
        Izvršava HTTP zahtjev s automatskim refreshom tokena na 401.
        """
        if "headers" not in kwargs: kwargs["headers"] = {}
        
        # Uvijek koristi najnoviji token iz clienta
        if "Authorization" in self.client.headers:
            kwargs["headers"]["Authorization"] = self.client.headers["Authorization"]

        try:
            response = await self.client.request(method, url, **kwargs)
            
            # Auth Trap (401)
            if response.status_code == 401:
                logger.warning("⚠️ 401 Auth Error - Refreshing token...")
                if await self._secure_refresh_token():
                    kwargs["headers"]["Authorization"] = self.client.headers["Authorization"]
                    response = await self.client.request(method, url, **kwargs)
                else:
                    return {"error": "AuthFailed", "message": "Refresh failed"}

            # HTML Trap (neki serveri vraćaju 200 OK ali HTML login page)
            if response.status_code == 200 and "text/html" in response.headers.get("content-type", ""):
                 return {"error": "FatalAuth", "message": "API returned HTML Login page."}

            if response.status_code == 403: 
                try: return response.json() 
                except: return {"status": 403, "error": "Forbidden"}

            if response.status_code >= 400:
                return {"error": f"Http{response.status_code}", "message": response.text[:500]}
            
            try: return response.json()
            except: return {"info": response.text[:500]}

        except httpx.HTTPError as e: raise e
        except Exception as e: return {"error": "UnexpectedError", "message": str(e)}

    # --- AUTHENTICATION METHODS ---
    
    async def _ensure_token_validity(self):
        """Provjerava valjanost tokena prije poziva."""
        if not settings.MOBILITY_AUTH_URL: return
        
        # Ako token ističe za manje od 5 min ili ga nemamo
        is_expired = datetime.utcnow() >= self.token_expires_at - timedelta(seconds=300)
        missing_header = "Authorization" not in self.client.headers
        
        if is_expired or missing_header:
            await self._secure_refresh_token()

    async def _secure_refresh_token(self) -> bool:
        """
        Dohvaća token koristeći CLIENT CREDENTIALS (Robot).
        """
        if not settings.MOBILITY_AUTH_URL: return False
        TOKEN_KEY = "mobility_access_token"
        
        # Redis check
        cached = await self.redis.get(TOKEN_KEY)
        if cached:
            if cached != self.client.headers.get("Authorization", "").replace("Bearer ", ""):
                self.client.headers["Authorization"] = f"Bearer {cached}"
                return True

        logger.info("🔄 Fetching SYSTEM token (client_credentials)...")
        payload = {
            "client_id": settings.MOBILITY_CLIENT_ID,
            "client_secret": settings.MOBILITY_CLIENT_SECRET,
            "grant_type": "client_credentials"
        }
        if settings.MOBILITY_SCOPE: payload["scope"] = settings.MOBILITY_SCOPE

        try:
            async with httpx.AsyncClient() as c:
                resp = await c.post(settings.MOBILITY_AUTH_URL, data=payload, timeout=10.0)
                
                # Retry logic for scope
                if resp.status_code == 400 and "scope" in payload:
                    logger.warning("⚠️ Auth 400 (Invalid Scope). Retrying without scope...")
                    del payload["scope"]
                    resp = await c.post(settings.MOBILITY_AUTH_URL, data=payload, timeout=10.0)

                if resp.status_code == 200:
                    d = resp.json()
                    token = d.get("access_token")
                    exp = int(d.get("expires_in", 3600))
                    
                    self.token_expires_at = datetime.utcnow() + timedelta(seconds=exp)
                    await self.redis.set(TOKEN_KEY, token, ex=exp - 60)
                    self.client.headers["Authorization"] = f"Bearer {token}"
                    logger.info("✅ Token refreshed.")
                    return True
                else:
                    logger.error(f"❌ Auth failed: {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"Auth Exception: {e}")
            return False

    def _distribute_parameters(self, path: str, method: str, params: dict):
        """
        Razdvaja parametre na Path, Query, Body i Headere.
        """
        req_vars = re.findall(r"\{([a-zA-Z0-9_\-]+)\}", path)
        p_path, p_query, p_body, headers = {}, {}, {}, {}
        for k, v in params.items():
            if k.lower() in ["x-tenant", "tenantid", "authorization"]: headers[k.lower()] = str(v)
            elif k in req_vars: p_path[k] = v
            elif method.upper() in ["POST", "PUT", "PATCH"]: p_body[k] = v
            else: p_query[k] = v     
        return p_path, p_query, p_body, headers

    async def close(self):
        await self.client.aclose()
        await self.redis.close()