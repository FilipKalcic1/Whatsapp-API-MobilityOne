import pytest
import orjson
from unittest.mock import MagicMock, AsyncMock, patch
from services.ai import analyze_intent

@pytest.mark.asyncio
async def test_ai_retries_on_invalid_json():
    """
    Provjerava da li sustav ponovno pokušava ako AI vrati neispravan JSON za alat.
    """
    # 1. Mockamo OpenAI response
    # Prvi odgovor: Loš JSON (fali zagrada)
    bad_msg = MagicMock()
    bad_msg.tool_calls = [MagicMock()]
    bad_msg.tool_calls[0].function.name = "get_car"
    bad_msg.tool_calls[0].function.arguments = '{"plate": "ZG-123"' # FALI ZAGRADA }
    
    # Drugi odgovor: Dobar JSON (nakon retryja)
    good_msg = MagicMock()
    good_msg.tool_calls = [MagicMock()]
    good_msg.tool_calls[0].function.name = "get_car"
    good_msg.tool_calls[0].function.arguments = '{"plate": "ZG-123"}'
    
    # Mockamo create metodu da vraća redom: Bad, Good
    mock_create = AsyncMock(side_effect=[
        MagicMock(choices=[MagicMock(message=bad_msg)]),
        MagicMock(choices=[MagicMock(message=good_msg)])
    ])
    
    with patch("services.ai.client.chat.completions.create", mock_create):
        # 2. Akcija
        result = await analyze_intent([], "Daj auto")
        
        # 3. Assert
        # Mora uspjeti iz drugog pokušaja
        assert result["tool"] == "get_car"
        assert result["parameters"]["plate"] == "ZG-123"
        
        # Ključno: Provjeri da je OpenAI pozvan DVA puta
        assert mock_create.call_count == 2