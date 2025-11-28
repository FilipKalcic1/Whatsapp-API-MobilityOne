import pytest
import asyncio
import orjson
from unittest.mock import MagicMock, AsyncMock, patch
from worker import WhatsappWorker, STREAM_INBOUND, QUEUE_OUTBOUND

@pytest.mark.asyncio
async def test_worker_initialization_and_shutdown():
    """
    Testira 'start' metodu (linije 89-156) i 'shutdown' (365-372).
    Pokriva: Spajanje na Redis, Učitavanje Swaggera (HTTP), Sentry init.
    """
    worker = WhatsappWorker()
    
    # 1. Priprema Mock objekata (SVE što se awaita mora biti AsyncMock)
    
    # Redis Mock
    mock_redis_instance = AsyncMock()
    mock_redis_instance.xgroup_create.return_value = True 
    mock_redis_instance.aclose.return_value = None

    # Registry Mock
    mock_registry_instance = MagicMock()
    mock_registry_instance.load_swagger = AsyncMock(return_value=None)
    mock_registry_instance.start_auto_update = AsyncMock(return_value=None)

    # [POPRAVAK] Gateway Mock - close() metoda mora biti awaitable!
    mock_gateway_instance = MagicMock()
    mock_gateway_instance.close = AsyncMock(return_value=None)

    # Mockamo sve vanjske servise
    with patch("worker.start_http_server"), \
         patch("worker.redis.from_url", return_value=mock_redis_instance), \
         patch("worker.httpx.AsyncClient", return_value=AsyncMock()), \
         patch("worker.ToolRegistry", return_value=mock_registry_instance), \
         patch("worker.OpenAPIGateway", return_value=mock_gateway_instance), \
         patch("worker.QueueService"), \
         patch("worker.ContextService"), \
         patch("worker.settings") as mock_settings:
        
        # Postavke da aktiviramo sve grane koda (if SENTRY, if HTTP swagger)
        mock_settings.SENTRY_DSN = "https://fake@sentry.io/1"
        mock_settings.MOBILITY_API_URL = "http://fake-api"
        mock_settings.SWAGGER_URL = "http://fake-swagger/swagger.json" 

        # HACK: Prekidamo 'while self.running' petlju nakon prvog kruga
        async def stop_worker_loop(*args, **kwargs):
            worker.running = False 
            return None
        
        # Patchamo sleep da simuliramo rad i gašenje
        with patch("asyncio.sleep", side_effect=stop_worker_loop):
            await worker.start()
        
        # PROVJERE
        mock_registry_instance.load_swagger.assert_called() 
        mock_registry_instance.start_auto_update.assert_called() 
        mock_redis_instance.xgroup_create.assert_called()
        
        # Provjeri shutdown
        assert worker.running is False
        mock_redis_instance.aclose.assert_called()
        mock_gateway_instance.close.assert_called() # Provjera da je gateway zatvoren

@pytest.mark.asyncio
async def test_process_inbound_batch_logic():
    """Testira čitanje iz Redis Streama (linije 159-181)."""
    worker = WhatsappWorker()
    worker.running = True
    worker.redis = MagicMock()
    worker.worker_id = "test_id"
    
    # Simuliramo poruku u streamu
    sample_stream = [[STREAM_INBOUND, [("msg_1", {"sender": "123", "text": "Hi"})]]]
    # xreadgroup mora biti AsyncMock
    worker.redis.xreadgroup = AsyncMock(return_value=sample_stream)
    
    # Mockiramo obradu jedne poruke
    worker._process_single_message_transaction = AsyncMock()
    
    await worker._process_inbound_batch()
    
    worker.redis.xreadgroup.assert_called()
    worker._process_single_message_transaction.assert_called_with("msg_1", {"sender": "123", "text": "Hi"})

@pytest.mark.asyncio
async def test_process_outbound_success():
    """Testira čitanje iz reda i slanje (linije 307-320, 345-362)."""
    worker = WhatsappWorker()
    worker.running = True
    worker.redis = MagicMock()
    worker.http = MagicMock()
    
    # Simuliramo poruku u redu
    payload = {"to": "38599", "text": "Hello"}
    # blpop mora biti AsyncMock
    worker.redis.blpop = AsyncMock(return_value=[QUEUE_OUTBOUND, orjson.dumps(payload).decode()])
    
    # Simuliramo uspješan HTTP request
    worker.http.post = AsyncMock()
    worker.http.post.return_value.raise_for_status = MagicMock()
    
    await worker._process_outbound()
    
    worker.http.post.assert_called() # Pokriva _send_infobip

@pytest.mark.asyncio
async def test_check_rate_limit_logic():
    """Testira logiku limita (linije 282+)."""
    worker = WhatsappWorker()
    worker.redis = MagicMock()
    
    # Slučaj 1: Prva poruka (postavlja expire)
    worker.redis.incr = AsyncMock(return_value=1)
    worker.redis.expire = AsyncMock()
    
    assert await worker._check_rate_limit("user1") is True
    worker.redis.expire.assert_called()

    # Slučaj 2: Previše poruka (blokada)
    worker.redis.incr = AsyncMock(return_value=21)
    assert await worker._check_rate_limit("user2") is False

@pytest.mark.asyncio
async def test_handle_onboarding_flow():
    """Testira onboarding logiku (linije 133-156)."""
    worker = WhatsappWorker()
    worker.redis = MagicMock()
    worker.queue = MagicMock()
    worker.queue.enqueue = AsyncMock()
    mock_user_service = MagicMock()
    mock_user_service.onboard_user = AsyncMock()

    # Slučaj 1: Novi korisnik (šaljemo welcome)
    worker.redis.get = AsyncMock(return_value=None)
    worker.redis.setex = AsyncMock()
    
    await worker._handle_onboarding("123", "Hi", mock_user_service)
    assert "Dobrodošli" in str(worker.queue.enqueue.call_args)
    worker.redis.setex.assert_called()

    # Slučaj 2: Čekamo email, ali format je loš
    worker.redis.get = AsyncMock(return_value="WAITING_EMAIL")
    await worker._handle_onboarding("123", "bad", mock_user_service)
    assert "Neispravan format" in str(worker.queue.enqueue.call_args)

@pytest.mark.asyncio
async def test_process_retries_logic():
    """Testira retry mehanizam."""
    worker = WhatsappWorker()
    worker.running = True
    worker.redis = MagicMock()
    worker.queue = MagicMock()
    worker.queue.enqueue = AsyncMock()
    
    # ZRANGEBYSCORE vraća zadatak
    retry_payload = {"to": "123", "text": "retry", "cid": "1", "attempts": 1}
    worker.redis.zrangebyscore = AsyncMock(return_value=[orjson.dumps(retry_payload).decode()])
    worker.redis.zrem = AsyncMock(return_value=1)
    
    await worker._process_retries()
    
    worker.queue.enqueue.assert_called()