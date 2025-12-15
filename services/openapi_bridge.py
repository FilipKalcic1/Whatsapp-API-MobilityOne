import httpx
import structlog
import asyncio
import json
import re 
from datetime import datetime, timedelta 
from typing import Dict, Any, Optional, Union

# Resilience Libraries
from tenacity import (
    retry, stop_after_attempt, wait_exponential, 
    retry_if_exception_type
)

# AI
from openai import AsyncAzureOpenAI 
from config import get_settings
import redis.asyncio as redis

# --- KONFIGURACIJA ---
logger = structlog.get_logger("openapi_bridge")
settings = get_settings()

class OpenAPIGateway:
    def __init__(self, base_url: str):
        """
        HYBRID GATEWAY - Najbolje od oba svijeta:
        - Jednostavna i robustna autentifikacija (iz prvog koda)
        - Pametan routing i AI Inspector (iz drugog koda)
        """
        self.base_url = base_url.rstrip('/')
        
        # Limiti konekcija
        self.limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        
        # Inicijalni headeri (API key ako postoji)
        headers = {}
        if hasattr(settings, "MOBILITY_API_KEY") and settings.MOBILITY_API_KEY:
            headers["Authorization"] = f"Bearer {settings.MOBILITY_API_KEY}"

        self.client = httpx.AsyncClient(timeout=30.0, limits=self.limits, headers=headers)
        
        # Redis za caching tokena
        redis_url = getattr(settings, "REDIS_URL", "redis://redis:6379/0")
        self.redis = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)

        # AI Client za "Inspekciju" velikih JSON-a
        self.ai_client = AsyncAzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION
        )

    async def execute_tool(self, tool_def: dict, params: dict, user_context: dict = None) -> Any:
        """
        Izvršava alat s pametnim routingom i AI Inspectorom.
        """
        raw_path = tool_def["path"]
        method = tool_def["method"].upper()
        
        try:
            # 1. PAMETNA RASPODJELA PARAMETARA (iz drugog koda)
            path_params, query_params, body_params, ai_headers = self._distribute_parameters(raw_path, method, params)

            # Validacija path parametara
            missing_path_vars = [var for var in re.findall(r"\{([a-zA-Z0-9_\-]+)\}", raw_path) if var not in path_params]
            
            if missing_path_vars:
                return {"error": "MissingParameter", "message": f"Fali path parametar: {missing_path_vars}"}

            # 2. PRIPREMA HEADERA
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

            # 🏢 LOGIKA TENANTA (poboljšana iz drugog koda)
            tenant_id = None
            
            # A) Prvo iz konteksta
            if user_context and user_context.get("tenant_id"):
                tenant_id = user_context["tenant_id"]
            
            # B) Ako je AI poslao u headerima
            elif ai_headers.get("x-tenant"):
                tenant_id = ai_headers["x-tenant"]
            
            # C) Fallback na PersonID (često isto u dev-u)
            elif user_context and user_context.get("person_id"):
                tenant_id = user_context["person_id"]
                logger.info(f"🔑 Using person_id as tenant: {tenant_id}")
            
            # D) Zadnja opcija: default iz settings
            if not tenant_id:
                tenant_id = getattr(settings, "DEFAULT_TENANT_ID", None)

            if tenant_id:
                headers["x-tenant"] = str(tenant_id)
            else:
                logger.warning("⚠️ No x-tenant header provided")


            
            # 3. Sastavljanje URL-a
            final_url = raw_path.format(**path_params)
            if not final_url.startswith("http"):
                base = self.base_url.rstrip("/")
                path = final_url.lstrip("/")
                final_url = f"{base}/{path}"

            # 4. DEBUG LOGGING (maskiramo auth token)
            masked_headers = {k: '***' if 'authorization' in k.lower() else v for k, v in headers.items()}
            logger.info(f"🚀 API REQUEST: {method} {final_url}")
            logger.info(f"   headers: {masked_headers}")
            if method in ["POST", "PUT", "PATCH"] and body_params:
                logger.info(f"   body: {str(body_params)[:200]}")

            # 5. IZVRŠAVANJE ZAHTJEVA
            response = await self._do_request(
                method=method,
                url=final_url,
                params=query_params if query_params else None,
                json=body_params if body_params and method in ["POST", "PUT", "PATCH"] else None,
                headers=headers
            )

            # 6. AI INSPECTOR (za velike odgovore)
            if user_context and user_context.get("last_user_message") and isinstance(response, dict) and "error" not in response:
                user_query = user_context["last_user_message"]
                if user_query and len(json.dumps(response, default=str)) > 500:
                    logger.info("🕵️ Running AI Inspector on response...")
                    response = await self._ai_extract_info(user_query, response)

            return response

        except Exception as e:
            logger.error(f"❌ Gateway execution failed: {str(e)}", exc_info=True)
            return {"error": "GatewayError", "details": str(e)}

    # --- AI INSPECTOR (iz drugog koda) ---
    async def _ai_extract_info(self, user_query: str, json_data: Any) -> Any:
        """
        Šalje podatke LLM-u da pronađe odgovor, umjesto da vraća 5MB JSON-a botu.
        """
        try:
            data_str = json.dumps(json_data, default=str)
            
            # Ako je JSON mali (< 500 znakova), ne troši AI resurse
            if len(data_str) < 500: 
                return json_data

            # Truncate na sigurnu granicu
            if len(data_str) > 25000: 
                data_str = data_str[:25000] + "...(truncated)"

            prompt = f"""
            SYSTEM: You are a Data Extraction Assistant.
            USER QUERY: "{user_query}"
            RAW DATA: {data_str}

            INSTRUCTIONS:
            1. Search the RAW DATA for the exact answer to the user's question.
            2. Ignore technical fields like GUIDs, TenantIds, RowVersions unless explicitly asked.
            3. Focus on business data: Statuses, Dates, Names, Mileages, Plates.
            4. Return the answer as a CLEAN, COMPACT JSON object.
            5. If the answer is NOT in the data, return {{"status": "not_found"}}.
            """

            completion = await self.ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=500
            )
            
            result_text = completion.choices[0].message.content.strip()
            
            # Očisti markdown formatiranje
            if result_text.startswith("```json"):
                result_text = result_text.replace("```json", "").replace("```", "")
            
            try:
                extracted = json.loads(result_text)
                logger.info("✨ AI Extracted Data:", data=extracted)
                return extracted
            except:
                return {"info": result_text}

        except Exception as e:
            logger.warning("AI Inspector failed, returning raw data", error=str(e))
            return json_data

    # --- HTTP EXECUTOR s JEDNOSTAVNOM AUTH LOGIKOM (iz prvog koda) ---
    @retry(
        stop=stop_after_attempt(3), 
        wait=wait_exponential(multiplier=1, min=1, max=10), 
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def _do_request(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        
        # 1. PRIPREMA HEADERA
        if "headers" not in kwargs: 
            kwargs["headers"] = {}
        
        # Dodaj token
        if "Authorization" in self.client.headers:
            kwargs["headers"]["Authorization"] = self.client.headers["Authorization"]

        # 2. FIX ZA UPLOAD (GREŠKA 415)
        # Ako šaljemo 'files', requests/httpx biblioteka mora SAMA postaviti boundary.
        # Ako ručno ostavimo 'Content-Type': 'application/json', upload će puknuti (415).
        if "files" in kwargs and kwargs["files"]:
            kwargs["headers"].pop("Content-Type", None)
            logger.info("ℹ️ Removed Content-Type header for file upload (415 Fix).")

        # 3. POKUŠAJ 1
        response = await self.client.request(method, url, **kwargs)

        # 4. AUTH REFRESH LOGIKA (401 & 403)
        if response.status_code in (401, 403):
            logger.warning(f"⚠️ {response.status_code} Auth Error - Attempting token refresh...")
            
            if await self._refresh_token():
                # Ažuriraj token u headerima
                kwargs["headers"]["Authorization"] = self.client.headers["Authorization"]
                # POKUŠAJ 2
                response = await self.client.request(method, url, **kwargs)
            else:
                logger.error("❌ Token refresh failed.")
                return {"error": "AuthFailed", "details": "Could not refresh token"}

        # 5. RUKOVANJE GREŠKAMA (SPRJEČAVANJE AI HALUCINACIJA)
        # Ako je status i dalje loš (npr. 415, 400, 500), ne vraćaj samo JSON.
        if response.status_code >= 400:
            error_msg = f"HTTP {response.status_code} Error: {response.text[:500]}"
            logger.error(f"🛑 {error_msg}")
            
            # Vraćamo strukturu koju AI razumije kao NEUSPJEH
            return {
                "status": "error",
                "http_code": response.status_code,
                "message": "Action failed on server side.",
                "server_response": response.text[:1000] # Dajemo AI-u tekst greške da shvati zašto
            }
        # 6. USPJEH
        try:
            return response.json()
        except json.JSONDecodeError:
            # Ponekad uspjeh nema JSON body (npr. 204 No Content)
            return {"status": "success", "message": "Request successful (no content)"}

    # --- JEDNOSTAVNA TOKEN LOGIKA (iz prvog koda) ---
    async def _refresh_token(self) -> bool:
        """
        JEDNOSTAVNA LOGIKA: 
        1. Probaj dohvatit iz Redisa (ako je netko drugi već osvježio).
        2. Ako ne valja, dohvati novi s Identity servera.
        3. Spremi u Redis i u self.client.
        """
        if not settings.MOBILITY_AUTH_URL: 
            return False

        TOKEN_KEY = "mobility_access_token"

        # 1. Provjeri Redis (Cache Hit)
        try:
            cached_token = await self.redis.get(TOKEN_KEY)
            current_local_token = self.client.headers.get("Authorization", "").replace("Bearer ", "")
            
            # Ako je u Redisu noviji token nego što mi imamo lokalno, uzmi ga
            if cached_token and cached_token != current_local_token:
                self.client.headers["Authorization"] = f"Bearer {cached_token}"
                logger.info("✅ Loaded fresh token from Redis.")
                return True
        except Exception as e:
            logger.warning(f"Redis error (ignoring): {e}")

        # 2. Dohvati novi token (API Call)
        logger.info("🔄 Fetching new token from Identity Server...")
        payload = {
            "client_id": settings.MOBILITY_CLIENT_ID,
            "client_secret": settings.MOBILITY_CLIENT_SECRET,
            "grant_type": "client_credentials"
        }
        
        # Dodaci ako postoje
        if getattr(settings, "MOBILITY_AUDIENCE", None):
            payload["audience"] = settings.MOBILITY_AUDIENCE
        if getattr(settings, "MOBILITY_SCOPE", None):
            payload["scope"] = settings.MOBILITY_SCOPE

        try:
            # Koristimo novi klijent da ne petljamo po glavnom
            async with httpx.AsyncClient() as auth_client:
                resp = await auth_client.post(settings.MOBILITY_AUTH_URL, data=payload, timeout=10.0)
            
            if resp.status_code == 200:
                data = resp.json()
                new_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                
                # Spremi
                self.client.headers["Authorization"] = f"Bearer {new_token}"
                await self.redis.set(TOKEN_KEY, new_token, ex=int(expires_in) - 60)
                
                logger.info("✅ Token refreshed and saved.")
                return True
            else:
                logger.error(f"❌ Auth endpoint returned {resp.status_code}: {resp.text}")
                return False

        except Exception as e:
            logger.error(f"❌ Critical Auth Error: {e}")
            return False

    # --- PAMETNA RASPODJELA PARAMETARA (iz drugog koda) ---
    def _distribute_parameters(self, path: str, method: str, params: dict):
        """
        Razdvaja parametre na Path, Query, Body i Headere.
        Bitno: Prepoznaje ako je AI pokušao poslati 'x-tenant' kao parametar.
        """
        req_vars = re.findall(r"\{([a-zA-Z0-9_\-]+)\}", path)
        p_path, p_query, p_body = {}, {}, {}
        special_headers = {}

        for k, v in params.items():
            key_lower = k.lower()
            
            # 1. HEADER DETEKCIJA (Ako AI pokuša poslati tenant ili auth)
            if key_lower in ["x-tenant", "tenantid", "tenant", "authorization"]:
                special_headers[key_lower] = str(v)
                continue

            # 2. PATH Varijable (npr. /Users/{id})
            if k in req_vars:
                p_path[k] = v
            
            # 3. BODY (samo za POST/PUT/PATCH)
            elif method.upper() in ["POST", "PUT", "PATCH", "DELETE"]:
                p_body[k] = v
            
            # 4. QUERY String (sve ostalo ide u ?key=value)
            else:
                p_query[k] = v
                
        return p_path, p_query, p_body, special_headers

    async def close(self):
        await self.client.aclose()
        await self.redis.close()