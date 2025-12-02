import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import httpx
from services.openapi_bridge import OpenAPIGateway
from redis.exceptions import LockError

@pytest.mark.asyncio
async def test_distributed_token_refresh_flow():
    """
    Testira novu logiku s Redis Lockom:
    1. Prvi poziv vraća 401 (Unauthorized).
    2. Sustav dohvaća Redis Lock.
    3. Osvježava token preko HTTP-a.
    4. Sprema novi token u Redis.
    5. Ponavlja originalni zahtjev s novim tokenom -> vraća 200.
    """
    
    # 1. MOCKIRANJE REDISA I SETTINGS-a
    with patch("services.openapi_bridge.redis.from_url") as mock_redis_cls, \
         patch("services.openapi_bridge.settings") as mock_settings:
        
        # Konfiguracija postavki
        mock_settings.MOBILITY_AUTH_URL = "http://auth.test"
        mock_settings.MOBILITY_CLIENT_ID = "cid"
        mock_settings.MOBILITY_CLIENT_SECRET = "sec"
        mock_settings.MOBILITY_API_TOKEN = "old_expired_token"
        mock_settings.REDIS_URL = "redis://mock:6379/0"

        # Konfiguracija Mock Redis klijenta
        mock_redis_instance = AsyncMock()
        mock_redis_cls.return_value = mock_redis_instance
        
        # Simulacija Redis .get() -> vraća None (nema cachea)
        mock_redis_instance.get.return_value = None

        # Lock mora biti MagicMock (sinkroni) koji vraća Async Context Manager
        mock_redis_instance.lock = MagicMock()
        mock_lock = AsyncMock()
        mock_lock.__aenter__.return_value = True 
        mock_lock.__aexit__.return_value = None   
        mock_redis_instance.lock.return_value = mock_lock

        # 2. INICIJALIZACIJA GATEWAY-a
        gateway = OpenAPIGateway("http://api.test")
        
        # 3. MOCKIRANJE HTTP KLIJENTA
        dummy_req = httpx.Request("GET", "http://api.test")
        
        # Definiramo Response objekte
        real_resp_401 = httpx.Response(401, request=dummy_req)
        # Token response mora imati ispravan JSON
        real_resp_token = httpx.Response(200, json={"access_token": "NEW_REDIS_TOKEN", "expires_in": 3600}, request=dummy_req)
        real_resp_200 = httpx.Response(200, json={"data": "success_after_refresh"}, request=dummy_req)

        mock_client = MagicMock()
        mock_client.headers = {"Authorization": "Bearer old_expired_token"}
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        # Scenarij: Prvo 401, onda (nakon refresha) 200
        mock_client.request = AsyncMock(side_effect=[real_resp_401, real_resp_200])
        
        # Patchamo httpx.AsyncClient da vrati naš mock
        with patch("httpx.AsyncClient", return_value=mock_client):
            
            # [POPRAVAK] Ovo mora biti AsyncMock jer kod koristi 'await auth_client.post(...)'
            mock_client.post = AsyncMock(return_value=real_resp_token)
            
            # Postavljamo klijent u gateway
            gateway.client = mock_client
            
            # 4. IZVRŠAVANJE TESTA
            tool_def = {"path": "/data", "method": "GET", "description": "t", "openai_schema": {}, "operationId": "op"}
            result = await gateway.execute_tool(tool_def, {})

            # 5. VERIFIKACIJA
            # Ako ovo padne, znači da _secure_refresh_token vraća False
            assert result == {"data": "success_after_refresh"}
            
            # Provjeri Redis operacije
            mock_redis_instance.lock.assert_called_with("mobility_token_refresh_lock", timeout=10, blocking_timeout=2.0)
            
            args, _ = mock_redis_instance.set.call_args
            assert args[0] == "mobility_access_token"
            assert args[1] == "NEW_REDIS_TOKEN"

@pytest.mark.asyncio
async def test_fast_path_redis_cache():
    """
    Testira scenarij gdje je token već u Redisu.
    """
    with patch("services.openapi_bridge.redis.from_url") as mock_redis_cls, \
         patch("services.openapi_bridge.settings") as mock_settings:
         
        mock_settings.MOBILITY_API_TOKEN = "old_token"
        
        mock_redis = AsyncMock()
        mock_redis_cls.return_value = mock_redis
        
        # Token već postoji u Redisu
        mock_redis.get.return_value = "FRESH_TOKEN_FROM_CACHE"
        
        gateway = OpenAPIGateway("http://api.test")
        gateway.client.headers = {"Authorization": "Bearer old_token"}
        
        success = await gateway._secure_refresh_token()
        
        assert success is True
        assert gateway.client.headers["Authorization"] == "Bearer FRESH_TOKEN_FROM_CACHE"
        
        mock_redis.lock.assert_not_called()