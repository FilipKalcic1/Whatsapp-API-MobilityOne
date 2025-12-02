import pytest
import asyncio
import orjson
from unittest.mock import MagicMock, AsyncMock, patch
from worker import WhatsappWorker, STREAM_INBOUND, QUEUE_OUTBOUND

@pytest.mark.asyncio
async def test_worker_initialization_and_shutdown():
    worker = WhatsappWorker()
    
    mock_redis_instance = AsyncMock()
    mock_redis_instance.xgroup_create.return_value = True 
    mock_redis_instance.aclose.return_value = None

    mock_registry_instance = MagicMock()
    mock_registry_instance.load_swagger = AsyncMock(return_value=None)
    mock_registry_instance.start_auto_update = AsyncMock(return_value=None)

    mock_gateway_instance = MagicMock()
    mock_gateway_instance.close = AsyncMock(return_value=None)

    with patch("worker.start_http_server"), \
         patch("worker.redis.from_url", return_value=mock_redis_instance), \
         patch("worker.httpx.AsyncClient", return_value=AsyncMock()), \
         patch("worker.ToolRegistry", return_value=mock_registry_instance), \
         patch("worker.OpenAPIGateway", return_value=mock_gateway_instance), \
         patch("worker.QueueService"), \
         patch("worker.ContextService"), \
         patch("worker.settings") as mock_settings:
        
        mock_settings.SENTRY_DSN = "https://fake@sentry.io/1"
        mock_settings.MOBILITY_API_URL = "http://fake-api"
        mock_settings.SWAGGER_URL = "http://fake-swagger/swagger.json" 

        async def stop_worker_loop(*args, **kwargs):
            worker.running = False 
            return None
        
        with patch("asyncio.sleep", side_effect=stop_worker_loop):
            await worker.start()
        
        mock_registry_instance.load_swagger.assert_called() 
        mock_redis_instance.xgroup_create.assert_called()
        assert worker.running is False

@pytest.mark.asyncio
async def test_process_inbound_batch_logic():
    worker = WhatsappWorker()
    worker.running = True
    worker.redis = MagicMock()
    worker.worker_id = "test_id"
    
    sample_stream = [[STREAM_INBOUND, [("msg_1", {"sender": "123", "text": "Hi"})]]]
    worker.redis.xreadgroup = AsyncMock(return_value=sample_stream)
    worker._process_single_message_transaction = AsyncMock()
    
    await worker._process_inbound_batch()
    
    worker.redis.xreadgroup.assert_called()
    worker._process_single_message_transaction.assert_called_with("msg_1", {"sender": "123", "text": "Hi"})

@pytest.mark.asyncio
async def test_process_outbound_success():
    worker = WhatsappWorker()
    worker.running = True
    worker.redis = MagicMock()
    worker.http = MagicMock()
    
    payload = {"to": "38599", "text": "Hello"}
    worker.redis.blpop = AsyncMock(return_value=[QUEUE_OUTBOUND, orjson.dumps(payload).decode()])
    
    worker.http.post = AsyncMock()
    worker.http.post.return_value.raise_for_status = MagicMock()
    
    await worker._process_outbound()
    
    worker.http.post.assert_called()

@pytest.mark.asyncio
async def test_check_rate_limit_logic():
    """Testira logiku limita s ispravnim mockanjem Pipelinea."""
    worker = WhatsappWorker()
    worker.redis = MagicMock()
    
    mock_pipeline = MagicMock()
    mock_pipeline.__aenter__.return_value = mock_pipeline
    mock_pipeline.__aexit__.return_value = None
    
    # execute() je async
    mock_pipeline.execute = AsyncMock(return_value=[1, True]) 
    
    worker.redis.pipeline.return_value = mock_pipeline

    assert await worker._check_rate_limit("user1") is True
    
    # incr i expire se zovu synchronous
    mock_pipeline.incr.assert_called()
    mock_pipeline.expire.assert_called()

    # Test prekoračenja
    mock_pipeline.execute = AsyncMock(return_value=[21, True])
    assert await worker._check_rate_limit("user2") is False

@pytest.mark.asyncio
async def test_handle_onboarding_flow():
    worker = WhatsappWorker()
    worker.redis = MagicMock()
    worker.queue = MagicMock()
    worker.queue.enqueue = AsyncMock()
    mock_user_service = MagicMock()
    mock_user_service.onboard_user = AsyncMock()

    worker.redis.get = AsyncMock(return_value=None)
    worker.redis.setex = AsyncMock()
    
    await worker._handle_onboarding("123", "Hi", mock_user_service)
    assert "Dobrodošli" in str(worker.queue.enqueue.call_args)

@pytest.mark.asyncio
async def test_process_retries_logic():
    worker = WhatsappWorker()
    worker.running = True
    worker.redis = MagicMock()
    worker.queue = MagicMock()
    worker.queue.enqueue = AsyncMock()
    
    retry_payload = {"to": "123", "text": "retry", "cid": "1", "attempts": 1}
    worker.redis.zrangebyscore = AsyncMock(return_value=[orjson.dumps(retry_payload).decode()])
    worker.redis.zrem = AsyncMock(return_value=1)
    
    await worker._process_retries()
    
    worker.queue.enqueue.assert_called()