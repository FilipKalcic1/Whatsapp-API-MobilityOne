import pytest
import json
from unittest.mock import MagicMock, AsyncMock, patch, mock_open
from services.tool_registry import ToolRegistry

@pytest.mark.asyncio
async def test_tool_registry_json_caching(redis_client):
    """
    Testira da registry koristi lokalni cache ako hash odgovara.
    """
    registry = ToolRegistry(redis_client)
    
    # 1. Priprema Cache-a (Simuliramo da smo učitali iz JSON-a)
    registry.cache_data = {
        "get_vehicle": {
            "hash": "MATCHING_HASH",
            "embedding": [0.99, 0.99] # Ovo želimo dobiti
        }
    }
    
    # Mockamo _save_cache da ne piše po disku
    registry._save_cache = MagicMock()
    
    # 2. Mockamo hashiranje da vrati isti hash kao u cacheu
    registry._calculate_tool_hash = MagicMock(return_value="MATCHING_HASH")
    
    # 3. Mockamo OpenAI (da budemo sigurni da se NE zove)
    registry._get_embedding = AsyncMock(return_value=[0.1, 0.1]) 
    
    # 4. Input Swagger
    spec = {
        "paths": {
            "/vehicle": {
                "get": {
                    "operationId": "get_vehicle",
                    "description": "Test description"
                }
            }
        }
    }
    
    # 5. Izvrši
    await registry._process_spec(spec)
    
    # 6. Provjere
    # Mora koristiti vektor iz cache-a [0.99, 0.99], a ne od OpenAI [0.1, 0.1]
    assert registry.tool_embeddings[0]["embedding"] == [0.99, 0.99]
    
    # OpenAI metoda se NE SMIJE zvati (ušteda novca!)
    registry._get_embedding.assert_not_called()

@pytest.mark.asyncio
async def test_process_spec_edge_cases(redis_client):
    """Testira generiranje ID-a kad fali operationId."""
    registry = ToolRegistry(redis_client)
    
    # [POPRAVAK] Forsiramo prazan cache za ovaj test!
    # Ovo sprječava da "stari" podaci s diska ometaju test.
    registry.cache_data = {} 
    
    # Mockamo save da ne zagađujemo disk
    registry._save_cache = MagicMock()
    
    registry._get_embedding = AsyncMock(return_value=[0.1])
    
    weird_swagger = {
        "paths": {
            "/cars/{id}": {
                "post": {
                    "summary": "Update car",
                    "parameters": [{"name": "id", "in": "path"}]
                }
            }
        }
    }
    
    await registry._process_spec(weird_swagger)
    
    assert "post_cars_id" in registry.tools_map
    
    # Sada će ovo proći jer smo ispraznili cache, pa kod MORA zvati embedding
    registry._get_embedding.assert_called()