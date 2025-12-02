import pytest
from unittest.mock import MagicMock, AsyncMock
import httpx
from services.openapi_bridge import OpenAPIGateway

@pytest.mark.asyncio
async def test_execute_tool_http_error():
    gateway = OpenAPIGateway("http://api.test")
    gateway.client.request = AsyncMock()
    
    mock_response = MagicMock()
    mock_response.status_code = 404
    # Simuliramo HTTP grešku
    error = httpx.HTTPStatusError("404 Not Found", request=MagicMock(), response=mock_response)
    gateway.client.request.side_effect = error

    tool_def = {"path": "/test", "method": "GET", "description": "", "openai_schema": {}, "operationId": "test"}
    
    result = await gateway.execute_tool(tool_def, {})
    

    assert result["error"] is True
    assert "Greška vanjskog sustava: 404" in result["message"]

@pytest.mark.asyncio
async def test_execute_tool_network_error():
    gateway = OpenAPIGateway("http://api.test")
    # Simuliramo mrežnu grešku
    gateway.client.request = AsyncMock(side_effect=httpx.RequestError("DNS failure"))

    tool_def = {"path": "/test", "method": "GET", "description": "", "openai_schema": {}, "operationId": "test"}
    result = await gateway.execute_tool(tool_def, {})
    

    assert result["error"] is True
    assert result["message"] == "Nisam uspio kontaktirati sustav (Network Error)."