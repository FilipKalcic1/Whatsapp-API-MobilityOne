import pytest
import orjson
from unittest.mock import MagicMock, AsyncMock, patch
from worker import WhatsappWorker, QUEUE_OUTBOUND
from services.queue import QueueService
from services.context import ContextService

@pytest.mark.asyncio
async def test_worker_full_successful_flow(redis_client):
    worker = WhatsappWorker()
    worker.redis = redis_client
    worker.queue = QueueService(redis_client)
    worker.context = ContextService(redis_client)
    
    worker.gateway = MagicMock()
    worker.gateway.execute_tool = AsyncMock(return_value={"status": "OK", "loc": "Zagreb"})
    
    worker.registry = MagicMock()
    worker.registry.find_relevant_tools = AsyncMock(return_value=[])
    worker.registry.tools_map = {"get_loc": {"path": "/loc", "method": "GET"}}
    
    tool_decision = {
        "tool": "get_loc", 
        "parameters": {}, 
        "tool_call_id": "call_1", 
        "raw_tool_calls": [],
        "response_text": None
    }
    final_decision = {
        "tool": None, 
        "parameters": {},
        "response_text": "Vozilo je u Zagrebu."
    }
    
    stream_id = await worker.queue.enqueue_inbound("38599", "Gdje je auto?", "msg_1")

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    
    # [POPRAVAK] Dodan patch za _check_rate_limit da izbjegnemo probleme s FakeRedis pipelineom
    with patch("worker.AsyncSessionLocal", return_value=mock_session), \
         patch("worker.UserService") as MockUserService, \
         patch("worker.analyze_intent", side_effect=[tool_decision, final_decision]), \
         patch.object(worker, '_check_rate_limit', return_value=True): 
        
        mock_user_service = MockUserService.return_value
        mock_user = MagicMock()
        mock_user.display_name = "Test User"
        mock_user.api_identity = "test@email.com"
        mock_user_service.get_active_identity = AsyncMock(return_value=mock_user)
        
        payload = {"sender": "38599", "text": "Gdje je auto?"}
        await worker._process_single_message_transaction(stream_id, payload)

    # Sada bi ovo trebalo prolaziti jer worker ne puca na rate limitu
    worker.gateway.execute_tool.assert_called_once()
    
    outbound_msg = await redis_client.lpop(QUEUE_OUTBOUND)
    assert outbound_msg is not None
    data = orjson.loads(outbound_msg)
    assert data["text"] == "Vozilo je u Zagrebu."