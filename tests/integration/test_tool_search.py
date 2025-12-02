import pytest
import numpy as np
from unittest.mock import MagicMock, AsyncMock
from services.tool_registry import ToolRegistry

@pytest.mark.asyncio
async def test_find_relevant_tools_logic(redis_client):
    """
    Integracijski test za cosine similarity s novom strukturom.
    """
    registry = ToolRegistry(redis_client)
    
    # 1. Ručno punimo 'tool_embeddings' listu (kako to radi novi kod)
    # Ubacujemo dva alata s ortogonalnim vektorima
    registry.tool_embeddings = [
        {
            "id": "get_vehicle",
            "embedding": [1.0, 0.0],
            "def": {"name": "get_vehicle"} 
        },
        {
            "id": "get_weather",
            "embedding": [0.0, 1.0],
            "def": {"name": "get_weather"}
        }
    ]
    registry.is_ready = True

    # 2. Mockamo OpenAI query embedding
    # Upit sličan vozilu -> [0.9, 0.1]
    mock_create = AsyncMock()
    mock_create.return_value.data = [MagicMock(embedding=[0.9, 0.1])]
    registry.client.embeddings.create = mock_create

    # 3. Tražimo
    results = await registry.find_relevant_tools("Gdje je auto?")

    # 4. Provjera
    assert len(results) >= 1
    # Mora vratiti definiciju vozila jer je [0.9, 0.1] puno bliže [1.0, 0.0] nego [0.0, 1.0]
    assert results[0]["name"] == "get_vehicle"