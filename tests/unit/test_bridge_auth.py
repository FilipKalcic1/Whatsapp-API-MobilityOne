import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import httpx
from services.openapi_bridge import OpenAPIGateway

@pytest.mark.asyncio
async def test_token_refresh_flow():
    """
    Simulira 401 (Expired) na prvom pozivu, uspješan refresh, 
    i onda uspješan ponovljeni poziv.
    """
    gateway = OpenAPIGateway("http://api.test")
    
    with patch("services.openapi_bridge.settings") as mock_settings:
        mock_settings.MOBILITY_AUTH_URL = "http://auth.test"
        mock_settings.MOBILITY_CLIENT_ID = "cid"
        mock_settings.MOBILITY_CLIENT_SECRET = "sec"
        mock_settings.MOBILITY_API_TOKEN = "old_token"

        # [POPRAVAK] Kreiramo dummy request jer ga raise_for_status() treba interno
        dummy_req = httpx.Request("GET", "http://api.test")

        # 1. Stvarni Response objekti s povezanim requestom
        real_resp_401 = httpx.Response(401, request=dummy_req)
        real_resp_token = httpx.Response(200, json={"access_token": "NEW_FRESH_TOKEN"}, request=dummy_req)
        real_resp_200 = httpx.Response(200, json={"data": "success"}, request=dummy_req)

        # 2. Koristimo MagicMock za klijenta (omogućuje preciznu kontrolu)
        mock_client_instance = MagicMock()
        
        # <--- [KLJUČNI POPRAVAK] --->
        # Postavljamo headers da bude pravi rječnik (dict), a ne Mock objekt.
        # Tako će kod moći stvarno zapisati i pročitati vrijednost.
        mock_client_instance.headers = {} 
        
        # Konfiguriramo Context Manager za 'async with'
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)

        # Konfiguriramo metode request i post
        # side_effect: Prvo vrati 401 (expired), pa onda 200 (success nakon refresha)
        mock_client_instance.request = AsyncMock(side_effect=[real_resp_401, real_resp_200])
        # return_value za auth poziv
        mock_client_instance.post = AsyncMock(return_value=real_resp_token)
        
        # Postavljamo mock na gateway
        gateway.client = mock_client_instance
        
        # Patchamo httpx.AsyncClient da vrati naš mock (za interni poziv u _secure_refresh_token)
        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            
            tool_def = {"path": "/data", "method": "GET", "description": "t", "openai_schema": {}, "operationId": "op"}
            result = await gateway.execute_tool(tool_def, {})

            # 4. Provjera
            assert result == {"data": "success"}
            
            # Provjera da je header ažuriran na glavnom klijentu
            assert gateway.client.headers["Authorization"] == "Bearer NEW_FRESH_TOKEN"