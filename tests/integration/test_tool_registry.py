import pytest
import json
import numpy as np
from unittest.mock import MagicMock, AsyncMock, patch, mock_open
from services.tool_registry import ToolRegistry

SAMPLE_SWAGGER = {
    "paths": {
        "/vehicle/{id}": {
            "get": {
                "operationId": "get_vehicle",
                "summary": "Dohvati vozilo",
                "description": "Vraća detalje.",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ]
            }
        }
    }
}

@pytest.mark.asyncio
async def test_load_swagger_success_and_caching(redis_client):
    registry = ToolRegistry(redis_client)
    
    mock_embedding = [0.1, 0.2, 0.3]
    mock_create = AsyncMock()
    mock_create.return_value.data = [MagicMock(embedding=mock_embedding)]
    registry.client.embeddings.create = mock_create

    with patch("builtins.open", mock_open(read_data=json.dumps(SAMPLE_SWAGGER))):
        await registry.load_swagger("swagger.json")

    assert registry.is_ready is True
    
    # [POPRAVAK] Ključ je 'tool_embed', ne 'tool_embedding'
    keys = await redis_client.keys("tool_embed:*")
    assert len(keys) > 0

    # Drugi prolaz (iz cachea)
    registry.client.embeddings.create = AsyncMock()
    registry_2 = ToolRegistry(redis_client)
    registry_2.client = registry.client 
    
    with patch("builtins.open", mock_open(read_data=json.dumps(SAMPLE_SWAGGER))):
        await registry_2.load_swagger("swagger.json")
    
    registry_2.client.embeddings.create.assert_not_called()
    assert registry_2.is_ready is True