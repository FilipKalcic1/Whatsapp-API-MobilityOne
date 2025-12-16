"""
OpenAPI Gateway - Production Ready (v8.0)

FEATURES:
1. Uses tool metadata for URL building
2. Auto-injects personId, AssignedToId, TenantId
3. Handles LIST responses (MasterData)
4. Proper error messages
5. Query string building for GET requests
"""

import httpx
import structlog
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, Union, List
from urllib.parse import urlencode, quote

import redis.asyncio as redis

from config import get_settings

logger = structlog.get_logger("openapi_gateway")
settings = get_settings()

TOKEN_CACHE_KEY = "mobility:access_token"


class OpenAPIGateway:
    """Production API Gateway v8."""
    
    def __init__(self, base_url: str = None):
        self.base_url = (base_url or settings.MOBILITY_API_URL).rstrip("/")
        self.auth_url = settings.MOBILITY_AUTH_URL
        self.client_id = settings.MOBILITY_CLIENT_ID
        self.client_secret = settings.MOBILITY_CLIENT_SECRET
        self.default_tenant = settings.tenant_id
        
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
        )
        
        self._redis: Optional[redis.Redis] = None
        self._token: Optional[str] = None
        self._token_expires_at: datetime = datetime.utcnow()
        
        logger.info("Gateway v8 initialized", base_url=self.base_url)
    
    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis
    
    # =========================================================================
    # AUTHENTICATION
    # =========================================================================
    
    async def _get_valid_token(self) -> str:
        """Get valid OAuth2 token."""
        if self._token and datetime.utcnow() < self._token_expires_at - timedelta(seconds=60):
            return self._token
        
        try:
            r = await self._get_redis()
            cached = await r.get(TOKEN_CACHE_KEY)
            if cached:
                self._token = cached
                self._token_expires_at = datetime.utcnow() + timedelta(minutes=30)
                return cached
        except:
            pass
        
        return await self._fetch_fresh_token()
    
    async def _fetch_fresh_token(self) -> str:
        """Fetch new OAuth2 token."""
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "audience": "none"
        }
        
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as auth_client:
                response = await auth_client.post(self.auth_url, data=payload, headers=headers)
                
                if response.status_code != 200:
                    raise Exception(f"Auth failed: {response.status_code}")
                
                data = response.json()
                token = data.get("access_token")
                expires_in = int(data.get("expires_in", 3600))
                
                self._token = token
                self._token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
                
                try:
                    r = await self._get_redis()
                    await r.setex(TOKEN_CACHE_KEY, expires_in - 120, token)
                except:
                    pass
                
                logger.info("Token acquired", expires_in=expires_in)
                return token
                
        except Exception as e:
            logger.error("Token error", error=str(e))
            raise
    
    async def _invalidate_token(self):
        """Clear token."""
        self._token = None
        self._token_expires_at = datetime.utcnow()
        try:
            r = await self._get_redis()
            await r.delete(TOKEN_CACHE_KEY)
        except:
            pass
    
    # =========================================================================
    # CONVENIENCE METHODS
    # =========================================================================
    
    async def get_master_data(self, person_id: str) -> Optional[Dict]:
        """Get master data for person - handles LIST response."""
        if not person_id:
            return None
        
        result = await self.execute_tool(
            tool_def={"path": "/automation/MasterData", "method": "GET"},
            params={"personId": person_id},
            user_context={"tenant_id": self.default_tenant}
        )
        
        if isinstance(result, dict) and result.get("error"):
            return None
        
        if isinstance(result, list):
            return result[0] if len(result) > 0 else None
        
        if isinstance(result, dict):
            if "Data" in result and isinstance(result["Data"], list):
                return result["Data"][0] if len(result["Data"]) > 0 else None
            return result
        
        return None
    
    async def get_person_by_phone(self, phone: str) -> Optional[Dict]:
        """Lookup person by phone number."""
        if not phone:
            return None
        
        clean_phone = "".join(c for c in phone if c.isdigit())
        if clean_phone.startswith("00"):
            clean_phone = clean_phone[2:]
        
        logger.info("Looking up person", phone_suffix=clean_phone[-4:])
        
        result = await self.execute_tool(
            tool_def={"path": "/tenantmgt/Persons", "method": "GET"},
            params={"Filter": f"Phone(=){clean_phone}"},
            user_context={"tenant_id": self.default_tenant}
        )
        
        if isinstance(result, dict) and result.get("error"):
            # Try alternative filter
            result = await self.execute_tool(
                tool_def={"path": "/tenantmgt/Persons", "method": "GET"},
                params={"Filter": f"Mobile(=){clean_phone}"},
                user_context={"tenant_id": self.default_tenant}
            )
        
        if isinstance(result, dict) and result.get("error"):
            return None
        
        if isinstance(result, list) and len(result) > 0:
            return result[0]
        
        if isinstance(result, dict):
            if "Data" in result and isinstance(result["Data"], list) and len(result["Data"]) > 0:
                return result["Data"][0]
            if "Id" in result:
                return result
        
        return None
    
    # =========================================================================
    # GENERIC TOOL EXECUTION
    # =========================================================================
    
    async def execute_tool(
        self, 
        tool_def: Dict[str, Any], 
        params: Dict[str, Any],
        user_context: Dict[str, Any] = None
    ) -> Union[Dict, List, str]:
        """
        Execute API call using tool definition.
        
        tool_def should contain:
        - path: API path
        - method: HTTP method
        - service: (optional) swagger service name
        - parameters: (optional) parameter metadata
        - auto_inject: (optional) parameters to inject automatically
        """
        path = tool_def.get("path", "")
        method = tool_def.get("method", "GET").upper()
        operation_id = tool_def.get("operationId", "unknown")
        auto_inject = tool_def.get("auto_inject", [])
        
        user_context = user_context or {}
        
        logger.info(f"Executing: {operation_id}", method=method, path=path[:50])
        
        try:
            token = await self._get_valid_token()
            
            # Substitute path parameters {id}
            final_path, remaining = self._substitute_path_params(path, params)
            
            # =================================================================
            # AUTO-INJECT PARAMETERS
            # =================================================================
            
            person_id = user_context.get("person_id")
            tenant_id = user_context.get("tenant_id") or self.default_tenant
            
            # MasterData: inject personId
            if "masterdata" in path.lower() and method == "GET":
                if "personId" not in remaining and person_id:
                    remaining["personId"] = person_id
                    logger.debug("Injected personId")
            
            # General auto-injection from tool definition
            for param in auto_inject:
                param_lower = param.lower()
                if param_lower == "personid" and "personId" not in remaining and person_id:
                    remaining["personId"] = person_id
                elif param_lower == "assignedtoid" and "AssignedToId" not in remaining and person_id:
                    remaining["AssignedToId"] = person_id
            
            # Separate query params and body
            if method in ("GET", "DELETE"):
                query_params = remaining
                body = None
            else:
                query_params = {}
                body = remaining.copy()
                
                # For POST requests, inject booking defaults
                if "calendar" in path.lower() and method == "POST":
                    if "AssignedToId" not in body and person_id:
                        body["AssignedToId"] = person_id
                        logger.debug("Injected AssignedToId")
                    
                    body.setdefault("AssigneeType", 1)
                    body.setdefault("EntryType", 0)
            
            # Build URL
            url = self._build_url(final_path, query_params)
            
            # Headers
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            
            if tenant_id:
                headers["x-tenant"] = tenant_id
            
            logger.info(f"API Request", method=method, url=url[:100], tenant=tenant_id[:8] if tenant_id else "N/A")
            
            return await self._execute_request(method, url, headers, body)
            
        except Exception as e:
            logger.error(f"Tool failed: {operation_id}", error=str(e))
            return {"error": True, "message": str(e)}
    
    def _build_url(self, path: str, query_params: Dict[str, Any] = None) -> str:
        """Build URL with query parameters."""
        if not path.startswith("/"):
            path = "/" + path
        
        url = f"{self.base_url}{path}"
        
        if query_params:
            # Filter out None values
            clean = {k: v for k, v in query_params.items() if v is not None}
            if clean:
                parts = []
                for k, v in clean.items():
                    if k == "Filter":
                        # Don't encode Filter value
                        parts.append(f"{k}={v}")
                    else:
                        parts.append(f"{k}={quote(str(v), safe='')}")
                url = f"{url}?{'&'.join(parts)}"
        
        return url
    
    def _substitute_path_params(self, path: str, params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Replace {placeholder} in path."""
        remaining = params.copy()
        placeholders = re.findall(r"\{([a-zA-Z0-9_]+)\}", path)
        
        for ph in placeholders:
            for key in list(remaining.keys()):
                if key.lower() == ph.lower():
                    path = path.replace(f"{{{ph}}}", str(remaining[key]))
                    del remaining[key]
                    break
        
        return path, remaining
    
    async def _execute_request(
        self, 
        method: str, 
        url: str, 
        headers: Dict, 
        body: Optional[Dict] = None
    ) -> Union[Dict, List, str]:
        """Execute HTTP request with retry."""
        max_retries = 2
        
        for attempt in range(max_retries + 1):
            try:
                if method == "GET":
                    response = await self.client.get(url, headers=headers)
                elif method == "POST":
                    logger.debug(f"POST body", body=body)
                    response = await self.client.post(url, headers=headers, json=body)
                elif method == "PUT":
                    response = await self.client.put(url, headers=headers, json=body)
                elif method == "DELETE":
                    response = await self.client.delete(url, headers=headers)
                elif method == "PATCH":
                    response = await self.client.patch(url, headers=headers, json=body)
                else:
                    return {"error": True, "message": f"Unsupported method: {method}"}
                
                # 401: Refresh token
                if response.status_code == 401 and attempt < max_retries:
                    logger.warning("401 - refreshing token")
                    await self._invalidate_token()
                    token = await self._get_valid_token()
                    headers["Authorization"] = f"Bearer {token}"
                    continue
                
                # Error responses
                if response.status_code == 400:
                    error_text = response.text[:300]
                    logger.error(f"HTTP 400", body=error_text)
                    return {
                        "error": True, 
                        "status": 400, 
                        "message": f"Neispravni parametri: {error_text}"
                    }
                
                if response.status_code == 403:
                    logger.error(f"HTTP 403 Forbidden")
                    return {
                        "error": True, 
                        "status": 403, 
                        "message": "Nemate dozvolu za ovu operaciju."
                    }
                
                if response.status_code == 404:
                    return {
                        "error": True, 
                        "status": 404, 
                        "message": "Resurs nije pronađen."
                    }
                
                if response.status_code >= 400:
                    logger.error(f"HTTP {response.status_code}", body=response.text[:200])
                    return {
                        "error": True, 
                        "status": response.status_code, 
                        "message": response.text[:200]
                    }
                
                # Success - parse response
                content_type = response.headers.get("content-type", "")
                
                try:
                    return response.json()
                except:
                    return {"data": response.text}
                    
            except httpx.TimeoutException:
                if attempt < max_retries:
                    continue
                return {"error": True, "message": "Timeout - pokušajte ponovno"}
                
            except Exception as e:
                if attempt < max_retries:
                    continue
                return {"error": True, "message": str(e)}
        
        return {"error": True, "message": "Max retries exceeded"}
    
    async def close(self):
        """Cleanup."""
        if self.client:
            await self.client.aclose()
        if self._redis:
            await self._redis.close()