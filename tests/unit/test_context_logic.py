import pytest
import orjson
from unittest.mock import MagicMock, AsyncMock, patch
from services.context import ContextService

@pytest.mark.asyncio
async def test_context_summarization_trigger(redis_client):
    """
    Provjerava da se pokreće sažimanje (OpenAI) kada poruke pređu limit tokena.
    """
    service = ContextService(redis_client)
    
    # 1. Napunimo Redis s puno "teških" poruka da pređemo limit od 2500 tokena
    # Pretvaramo se da imamo 10 poruka, svaka ima puno teksta
    fake_history = []
    for i in range(10):
        fake_history.append(orjson.dumps({
            "role": "user", 
            "content": "word " * 500 # Puno riječi
        }).decode())
        
    # Mockamo Redis operacije koje čitaju povijest
    service.redis = MagicMock()
    service.redis.llen = AsyncMock(return_value=10)
    service.redis.lrange = AsyncMock(return_value=fake_history)
    service.redis.pipeline = MagicMock()
    
    # Pipeline context manager mock
    pipeline_mock = AsyncMock()
    service.redis.pipeline.return_value.__aenter__.return_value = pipeline_mock
    
    # 2. Mockamo OpenAI (da ne trošimo pare)
    mock_ai_response = MagicMock()
    mock_ai_response.choices[0].message.content = "Sažetak razgovora..."
    service.client.chat.completions.create = AsyncMock(return_value=mock_ai_response)
    
    # 3. Pokrećemo logiku limita
    # (Metoda je privatna, pa je zovemo direktno za test)
    await service._enforce_token_limit("ctx:user1")
    
    # 4. Provjera
    # Mora se pozvati OpenAI za sažimanje
    service.client.chat.completions.create.assert_called_once()
    
    # Mora se pozvati LTRIM (brisanje starih) i LPUSH (ubacivanje sažetka)
    pipeline_mock.ltrim.assert_called()
    pipeline_mock.lpush.assert_called()