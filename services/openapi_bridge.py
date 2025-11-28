import httpx
import structlog
import asyncio
from typing import Dict, Any, Optional
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)
from aiobreaker import CircuitBreaker, CircuitBreakerError

from config import get_settings

logger = structlog.get_logger("openapi_bridge")
settings = get_settings()

# --- KONFIGURACIJA CIRCUIT BREAKERA ---

api_breaker = CircuitBreaker(fail_max=5, reset_timeout_duration=30)

class OpenAPIGateway:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        
        # Connection Pooling za High Concurrency
        self.limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        
        headers = {}
        if settings.MOBILITY_API_TOKEN:
            headers["Authorization"] = f"Bearer {settings.MOBILITY_API_TOKEN}"

        self.client = httpx.AsyncClient(timeout=15.0, limits=self.limits, headers=headers)
        self._auth_lock = asyncio.Lock() 

    async def execute_tool(self, tool_def: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Glavna javna metoda. Priprema parametre i poziva zaštićenu metodu _do_request.
        Hvata exceptione biblioteka i pretvara ih u standardizirani rječnik grešaka
        kako bi ostatak sustava (worker) mogao nastaviti raditi bez rušenja.
        """
        

        path = tool_def['path']
        method = tool_def['method'].upper()
        req_data = params.copy()
        
        # Path params injection 
        for key, value in params.items():
            placeholder = "{" + key + "}"
            if placeholder in path:
                path = path.replace(placeholder, str(value))
                if key in req_data: del req_data[key]

        # Header extraction 
        headers = {}
        keys_to_remove = []
        for key, value in req_data.items():
            if key.lower().startswith('x-') or key.lower() == 'tenantid':
                headers[key] = str(value)
                keys_to_remove.append(key)
        
        for k in keys_to_remove: del req_data[k]

        full_url = f"{self.base_url}{path}"
        
        request_kwargs = {"headers": headers}
        if method in ["GET", "DELETE"]:
            request_kwargs["params"] = req_data
        else:
            request_kwargs["json"] = req_data


        try:
            return await self._do_request(method, full_url, **request_kwargs)


            logger.error("Circuit Breaker OPEN: Skipping external API call.")
            return {"error": True, "message": "Vanjski servis je privremeno nedostupan (Circuit Open)."}
            
        except httpx.HTTPStatusError as e:

            logger.error("API Error Response", status=e.response.status_code, error=str(e))
            return {"error": True, "message": f"Greška vanjskog sustava: {e.response.status_code}"}
            
        except httpx.RequestError as e:

            logger.error("API Network Failure", error=str(e))
            return {"error": True, "message": "Nisam uspio kontaktirati sustav (Network Error)."}
            
        except Exception as e:
            # Sve ostalo
            logger.error("Unexpected Gateway Error", error=str(e))
            return {"error": True, "message": "Interna greška sustava."}

    @api_breaker
    @retry(     
        stop=stop_after_attempt(3), # Maksimalno 3 pokušaja
        wait=wait_exponential(multiplier=1, min=1, max=10), # Backoff: 1s, 2s, 4s...
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)), # Retry samo na mrežne greške
        before_sleep=before_sleep_log(logger, "warning") # Logiraj prije čekanja
    )
    async def _do_request(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        """
        Interna metoda koja izvršava stvarni HTTP poziv.
        Ova metoda diže Exceptione kako bi ih Tenacity i CircuitBreaker mogli pratiti.
        """
        response = await self.client.request(method, url, **kwargs)

        # --- Token Refresh Logika ---
        if response.status_code == 401:
            logger.info("Token expired (401), attempting refresh...")
            

            refreshed = await self._secure_refresh_token()
            
            if refreshed:

                raise httpx.RequestError("Token refreshed, forcing retry...")
            else:

                logger.error("Token refresh failed.")
                return {"error": True, "message": "Autorizacija neuspjela."}


        if response.status_code >= 500:

            response.raise_for_status()

        response.raise_for_status()
        
        if response.status_code == 204:
            return {"status": "success", "data": None}
            
        return response.json()

    async def _secure_refresh_token(self) -> bool:
        """Thread-safe token refresh."""
        if not settings.MOBILITY_AUTH_URL: return False

        # Brza provjera ako je netko drugi već zaključao (optimizacija)
        if self._auth_lock.locked():
            async with self._auth_lock: return True
            
        async with self._auth_lock:
            try:
                logger.info("Refreshing token via OAuth2...")
                payload = {
                    "client_id": settings.MOBILITY_CLIENT_ID,
                    "client_secret": settings.MOBILITY_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                    "scope": settings.MOBILITY_SCOPE
                }
                # Koristimo novi klijent za auth da izbjegnemo rekurziju ili limite
                async with httpx.AsyncClient() as client:
                    resp = await client.post(settings.MOBILITY_AUTH_URL, data=payload, timeout=10.0)
                    resp.raise_for_status()
                    
                    token = resp.json().get("access_token")
                    if token:
                        # Ažuriramo header na glavnom klijentu
                        self.client.headers["Authorization"] = f"Bearer {token}"
                        logger.info("Token refreshed successfully.")
                        return True
            except Exception as e:
                logger.error("Token refresh failed", error=str(e))
                return False
        return False

    async def close(self):
        await self.client.aclose()