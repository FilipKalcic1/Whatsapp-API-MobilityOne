import pytest
import json
from unittest.mock import MagicMock, AsyncMock, patch, mock_open
from services.tool_registry import ToolRegistry

SAMPLE_SWAGGER = {
    "paths": {
        "/vehicle/{id}": {
            "get": {
                "operationId": "get_vehicle",
                "summary": "Dohvati vozilo",
                "parameters": [
                    {"name": "id", "in": "path", "schema": {"type": "string"}}
                ]
            }
        }
    }
}

@pytest.mark.asyncio
async def test_load_swagger_success_and_caching(redis_client):
    registry = ToolRegistry(redis_client)
    
    # Mockamo OpenAI za prvi prolaz
    mock_embedding = [0.1, 0.2, 0.3]
    registry.client.embeddings.create = AsyncMock()
    registry.client.embeddings.create.return_value.data = [MagicMock(embedding=mock_embedding)]

    # Mockamo čitanje swaggera s diska
    with patch("builtins.open", mock_open(read_data=json.dumps(SAMPLE_SWAGGER))):
        await registry.load_swagger("swagger.json")

    assert registry.is_ready is True
    # Provjera da je embedding učitan u memoriju
    assert len(registry.tool_embeddings) == 1
    assert registry.tool_embeddings[0]["id"] == "get_vehicle"

    # --- DRUGI PROLAZ (Cache Hit) ---
    # Kreiramo novi registry ali s popunjenim cacheom (simulacija restarta)
    registry_2 = ToolRegistry(redis_client)
    registry_2.cache_data = registry.cache_data # Prenosimo cache
    registry_2.client.embeddings.create = AsyncMock() # Resetiramo mock
    
    with patch("builtins.open", mock_open(read_data=json.dumps(SAMPLE_SWAGGER))):
        await registry_2.load_swagger("swagger.json")
    
    # Ključno: OpenAI se NE SMIJE zvati drugi put
    registry_2.client.embeddings.create.assert_not_called()
    assert len(registry_2.tool_embeddings) == 1