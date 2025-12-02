import asyncio
import uuid
import structlog
import orjson
import redis.asyncio as redis
from typing import Optional, Dict, Any

logger = structlog.get_logger("queue")

# --- KONSTANTE ---
STREAM_INBOUND = "whatsapp_stream_inbound"
QUEUE_OUTBOUND = "whatsapp_outbound"
QUEUE_SCHEDULE = "schedule_retry"

QUEUE_DLQ_INBOUND = "dlq:inbound"    
QUEUE_DLQ_OUTBOUND = "dlq:outbound"
QUEUE_DLQ_PERMANENT = "dlq:permanent"

class QueueService:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def enqueue_inbound(self, sender: str, text: str, message_id: str) -> str:
        """Sprema ulaznu poruku u Redis Stream."""
        payload = {
            "sender": sender,
            "text": text,
            "message_id": message_id,
            "timestamp": str(asyncio.get_event_loop().time()),
            "retry_count": 0
        }
        
        stream_id = await self.redis.xadd(STREAM_INBOUND, payload)
        logger.debug("Inbound message queued", stream_id=stream_id, sender=sender)
        return stream_id

    async def store_inbound_dlq(self, payload: dict, error: str):
        """Sprema 'otrovnu' poruku u DLQ."""
        if "retry_count" not in payload:
            payload["retry_count"] = 0
            
        dlq_entry = {
            "original_payload": payload,
            "error": str(error),
            "failed_at": str(asyncio.get_event_loop().time())
        }
        
        data = orjson.dumps(dlq_entry).decode('utf-8')
        await self.redis.rpush(QUEUE_DLQ_INBOUND, data)
        
        logger.critical("Message moved to Inbound DLQ", msg_id=payload.get('message_id'), reason=error)

    async def auto_heal_dlq(self):
        """
        [ENTERPRISE FEATURE] Oživljava poruke iz DLQ-a ili ih trajno briše.
        Ova metoda je nedostajala i uzrokovala grešku!
        """
        for _ in range(10):
            raw_data = await self.redis.lpop(QUEUE_DLQ_INBOUND)
            if not raw_data:
                break

            try:
                entry = orjson.loads(raw_data)
                payload = entry.get("original_payload", {})
                
                try:
                    current_retries = int(payload.get("retry_count", 0))
                except (ValueError, TypeError):
                    current_retries = 0
                
                if current_retries >= 3:
                    logger.error("Message is TOXIC (3x fail). Moving to Permanent DLQ.", sender=payload.get("sender"))
                    await self.redis.rpush(QUEUE_DLQ_PERMANENT, raw_data)
                else:
                    payload["retry_count"] = current_retries + 1
                    await self.redis.xadd(STREAM_INBOUND, payload)
                    logger.info("Auto-Healing: Restored message from DLQ", attempt=payload["retry_count"])

            except Exception as e:
                logger.error("Failed to heal DLQ message", error=str(e))
                await self.redis.rpush(QUEUE_DLQ_PERMANENT, raw_data)

    async def enqueue(self, to: str, text: str, correlation_id: str = None, attempts: int = 0):
        if not correlation_id:
            correlation_id = str(uuid.uuid4())

        payload = {
            "to": to,
            "text": text,
            "cid": correlation_id,
            "attempts": attempts
        }
        
        data = orjson.dumps(payload).decode('utf-8')
        await self.redis.rpush(QUEUE_OUTBOUND, data)
        logger.debug("Outbound message enqueued", to=to, cid=correlation_id)

    async def schedule_retry(self, payload: Dict[str, Any]):
        attempts = payload.get('attempts', 0) + 1
        cid = payload.get("cid")

        if attempts >= 5:
            logger.error("Max outbound retries reached.", cid=cid)
            return 

        delay = 2 ** attempts 
        payload['attempts'] = attempts
        
        execute_at = asyncio.get_event_loop().time() + delay
        data = orjson.dumps(payload).decode('utf-8')
        
        await self.redis.zadd(QUEUE_SCHEDULE, {data: execute_at})
        logger.info("Message rescheduled", cid=cid, attempt=attempts, delay=delay)