import pytest
from unittest.mock import patch
from main import app

@pytest.mark.asyncio
async def test_webhook_receives_message(async_client, redis_client):
    """
    E2E TEST:
    1. Šaljemo POST na webhook.
    2. Provjeravamo 200 OK.
    3. Provjeravamo da je poruka završila u Redis Streamu (za workera).
    """
    payload = {
        "results": [{
            "from": "38591234567",
            "text": "Gdje je moje vozilo?",
            "messageId": "msg-test-1"
        }]
    }

    # Mockamo potpis jer u testu ne želimo računati HMAC
    with patch("routers.webhook.validate_infobip_signature", return_value=True):
        
        response = await async_client.post("/webhook/whatsapp", json=payload)

        # 1. Provjera odgovora
        assert response.status_code == 200
        assert response.json()["status"] == "queued"

        # 2. Provjera baze (Redisa)
        # Ključ streama definiran u queue.py je 'whatsapp_stream_inbound'
        stream_key = "whatsapp_stream_inbound"
        assert stream_key in redis_client.streams
        assert len(redis_client.streams[stream_key]) == 1
        
        # Provjeri sadržaj poruke
        msg_id, fields = redis_client.streams[stream_key][0]
        assert fields["sender"] == "38591234567"
        assert fields["text"] == "Gdje je moje vozilo?"