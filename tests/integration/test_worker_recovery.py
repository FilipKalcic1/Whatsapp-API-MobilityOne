import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from worker import WhatsappWorker, STREAM_INBOUND

@pytest.mark.asyncio
async def test_worker_recovers_stalled_messages(redis_client):
    """
    Simulira situaciju gdje je poruka 'zapela' (processing time > limit)
    i provjerava da li je worker preuzima (claim).
    """
    worker = WhatsappWorker()
    worker.redis = redis_client
    worker.worker_id = "test_worker_2"
    worker.running = True
    
    # Mockamo business logiku da ne radi ništa (samo želimo testirati flow)
    worker._process_single_message_transaction = AsyncMock()

    # 1. Ubacimo poruku u stream i stvorimo 'zombie' entry
    # (Simuliramo da ju je neki drugi worker 'dead_worker' uzeo ali nije ACK-ao)
    msg_id = await redis_client.xadd(STREAM_INBOUND, {"sender": "123", "text": "stalled"})
    
    # Kreiramo grupu
    try:
        await redis_client.xgroup_create(STREAM_INBOUND, "workers_group", mkstream=True)
    except:
        pass

    # 'dead_worker' čita poruku
    await redis_client.xreadgroup("workers_group", "dead_worker", {STREAM_INBOUND: ">"}, count=1)
    
    # 2. Sada naš 'test_worker_2' pokušava oporaviti poruke
    # Koristimo min_idle_time=0 da odmah uhvati poruku (u produkciji je 60s)
    
    # Moramo patchati xautoclaim jer FakeRedis možda ne podržava potpunu logiku vremena
    # Ali ako koristiš pravi Redis u testovima (preko dockera), ovo radi.
    # Ako koristiš FakeRedis, moramo mockati xautoclaim da vrati našu poruku.
    
    with patch.object(worker.redis, 'xautoclaim', new_callable=AsyncMock) as mock_claim:
        mock_claim.return_value = (
            "0-0", # next_id
            [(msg_id, {"sender": "123", "text": "stalled"})], # messages
            [] # deleted
        )
        
        await worker._recover_stalled_messages()
        
        # 3. Assert
        # Provjeri da je worker pokušao obraditi tu poruku
        worker._process_single_message_transaction.assert_called_once()
        args = worker._process_single_message_transaction.call_args[0]
        assert args[0] == msg_id # ID poruke
        assert args[1]["sender"] == "123"