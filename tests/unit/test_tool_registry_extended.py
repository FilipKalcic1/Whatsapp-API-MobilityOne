import pytest
import json
import httpx
from unittest.mock import MagicMock, AsyncMock, patch, mock_open
from services.tool_registry import ToolRegistry

@pytest.mark.asyncio
async def test_tool_registry_caching_logic(redis_client):
    """
    Testira da se embedding ne računa ponovno ako postoji u Redisu.
    Pokriva: _get_cached_embedding
    """
    registry = ToolRegistry(redis_client)
    
    # 1. Simuliramo Cache MISS (nema u Redisu) -> Mora zvati OpenAI
    redis_client.get = AsyncMock(return_value=None)
    redis_client.set = AsyncMock()
    
    # Mockamo OpenAI
    mock_create = AsyncMock()
    mock_create.return_value.data = [MagicMock(embedding=[0.1, 0.2])]
    registry.client.embeddings.create = mock_create
    
    # Poziv
    vec1 = await registry._get_cached_embedding("key1", "text")
    
    assert vec1 == [0.1, 0.2]
    mock_create.assert_called_once() # OpenAI pozvan
    redis_client.set.assert_called_once() # Spremljeno u Redis

    # 2. Simuliramo Cache HIT (ima u Redisu) -> Ne smije zvati OpenAI
    redis_client.get = AsyncMock(return_value=json.dumps([0.9, 0.9]))
    mock_create.reset_mock()
    
    vec2 = await registry._get_cached_embedding("key1", "text")
    
    assert vec2 == [0.9, 0.9]
    mock_create.assert_not_called() # OpenAI NIJE pozvan (ušteda!)

@pytest.mark.asyncio
async def test_process_spec_edge_cases(redis_client):
    """
    Testira parsiranje Swaggera kada fale neki podaci.
    Pokriva: _process_spec (generiranje operationId) i _to_openai_schema
    """
    registry = ToolRegistry(redis_client)
    registry._get_cached_embedding = AsyncMock(return_value=[0.1])
    
    # Swagger bez operationId i s requestBody parametrima
    weird_swagger = {
        "paths": {
            "/cars/{id}": {
                "post": {
                    # NEMA "operationId" -> kod ga mora generirati
                    "summary": "Update car",
                    "parameters": [{"name": "id", "in": "path", "required": True}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "properties": {"color": {"type": "string"}},
                                    "required": ["color"]
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    
    await registry._process_spec(weird_swagger)
    
    # Provjere
    assert len(registry.tools_map) == 1
    
    # Provjeri je li generirao ID (post_cars_id)
    generated_id = "post_cars_id"
    assert generated_id in registry.tools_map
    
    # Provjeri je li parsirao parametre iz Body-ja
    schema = registry.tools_map[generated_id]["openai_schema"]
    props = schema["function"]["parameters"]["properties"]
    
    assert "id" in props # Iz patha
    assert "color" in props # Iz bodyja

@pytest.mark.asyncio
async def test_auto_update_logic(redis_client):
    """
    Testira 'while True' petlju za auto-update.
    Trik: Bacamo exception u sleep() da prekinemo beskonačnu petlju testa.
    Pokriva: start_auto_update
    """
    registry = ToolRegistry(redis_client)
    registry.load_swagger = AsyncMock()
    
    # Mockamo HTTP klijenta koji vraća novi ETag
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"ETag": "new_version_v2"}
    
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.head.return_value = mock_response
    
    # Postavljamo stari hash
    registry.current_hash = "old_version_v1"
    
    # PATCH: httpx i sleep
    # Prvi sleep prolazi, drugi baca grešku da zaustavi test
    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("asyncio.sleep", side_effect=[None, Exception("Stop Loop")]):
        
        try:
            await registry.start_auto_update("http://api.com/swagger.json", interval=1)
        except Exception:
            pass # Ignoriramo naš "Stop Loop" exception
            
    # Provjera: Ako je ETag drugačiji, morao je pozvati load_swagger
    registry.load_swagger.assert_called_with("http://api.com/swagger.json")
    assert registry.current_hash == "new_version_v2"
    