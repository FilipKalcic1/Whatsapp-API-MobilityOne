import pytest
import json
import numpy as np
from unittest.mock import MagicMock, AsyncMock, patch
from services.tool_registry import ToolRegistry

@pytest.mark.asyncio
async def test_find_relevant_tools_logic(redis_client):
    """
    Integracijski test: Provjerava radi li matematika pretraživanja (dot product).
    Ne zovemo stvarni OpenAI, ali koristimo pravu logiku registra.
    """
    registry = ToolRegistry(redis_client)
    
    # 1. Priprema podataka
    # Definiramo dva alata: jedan za vozila, jedan za vrijeme
    registry.tools_names = ["get_vehicle", "get_weather"]
    registry.tools_map = {
        "get_vehicle": {"openai_schema": {"name": "get_vehicle"}},
        "get_weather": {"openai_schema": {"name": "get_weather"}}
    }
    
    # Vektori: [1, 0] i [0, 1] su ortogonalni (potpuno različiti)
    registry.tools_vectors = np.array([
        [1.0, 0.0], # get_vehicle
        [0.0, 1.0]  # get_weather
    ])
    registry.is_ready = True

    # 2. Mockamo OpenAI embedding da vrati vektor koji je sličan vozilu
    # Ako je upit "auto", embedding je [0.9, 0.1] -> vrlo sličan [1, 0]
    mock_create = AsyncMock()
    mock_create.return_value.data = [MagicMock(embedding=[0.9, 0.1])]
    registry.client.embeddings.create = mock_create

    # 3. Akcija
    results = await registry.find_relevant_tools("Gdje je auto?")

    # 4. Assert
    assert len(results) >= 1
    assert results[0]["name"] == "get_vehicle" # Mora naći vozilo, ne vrijeme