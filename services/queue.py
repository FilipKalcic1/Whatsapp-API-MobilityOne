"""
Queue Service - Production Ready (v2.0)

Manages message queues in Redis:
1. Inbound stream (WhatsApp → Worker)
2. Outbound queue (Worker → Infobip)
3. Retry scheduling
4. Dead-letter queue handling
"""

import asyncio
import uuid
import structlog
import orjson
import redis.asyncio as redis
from typing import Optional, Dict, Any

logger = structlog.get_logger("queue")

# Queue names
STREAM_INBOUND = "whatsapp_stream_inbound"
QUEUE_OUTBOUND = "whatsapp_outbound"
QUEUE_SCHEDULE = "schedule_retry"
QUEUE_DLQ_INBOUND = "dlq:inbound"
QUEUE_DLQ_PERMANENT = "dlq:permanent"


class QueueService:
    """
    Redis-based message queue management.
    
    Uses:
    - Streams for inbound (reliable, consumer groups)
    - Lists for outbound (simple FIFO)
    - Sorted sets for scheduling (time-based)
    """
    
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
    
    # =========================================================================
    # INBOUND
    # =========================================================================
    
    async def enqueue_inbound(
        self,
        sender: str,
        text: str,
        message_id: str
    ) -> str:
        """
        Add inbound WhatsApp message to processing stream.
        
        Returns stream entry ID.
        """
        payload = {
            "sender": sender,
            "text": text,
            "message_id": message_id,
            "timestamp": str(asyncio.get_event_loop().time()),
            "retry_count": "0"
        }
        
        stream_id = await self.redis.xadd(STREAM_INBOUND, payload)
        logger.debug("Inbound queued", stream_id=stream_id, sender=sender[-4:])
        
        return stream_id
    
    async def store_inbound_dlq(self, payload: dict, error: str):
        """
        Move failed message to dead-letter queue.
        """
        dlq_entry = {
            "original_payload": payload,
            "error": str(error),
            "failed_at": str(asyncio.get_event_loop().time())
        }
        
        data = orjson.dumps(dlq_entry).decode("utf-8")
        await self.redis.rpush(QUEUE_DLQ_INBOUND, data)
        
        logger.warning("Message moved to DLQ",
                      msg_id=payload.get("message_id"),
                      error=error[:100])
    
    async def auto_heal_dlq(self):
        """
        Attempt to recover messages from DLQ.
        
        - Messages with < 3 retries: re-queue
        - Messages with >= 3 retries: move to permanent DLQ
        """
        processed = 0
        
        for _ in range(10):  # Process up to 10 per cycle
            raw_data = await self.redis.lpop(QUEUE_DLQ_INBOUND)
            
            if not raw_data:
                break
            
            try:
                entry = orjson.loads(raw_data)
                payload = entry.get("original_payload", {})
                
                retry_count = int(payload.get("retry_count", 0))
                
                if retry_count >= 3:
                    # Too many failures - permanent storage
                    await self.redis.rpush(QUEUE_DLQ_PERMANENT, raw_data)
                    await self.redis.expire(QUEUE_DLQ_PERMANENT, 86400 * 14)  # 14 days
                    logger.warning("Message moved to permanent DLQ", 
                                  retries=retry_count)
                else:
                    # Retry
                    payload["retry_count"] = str(retry_count + 1)
                    await self.redis.xadd(STREAM_INBOUND, payload)
                    logger.info("DLQ message re-queued", 
                               attempt=retry_count + 1)
                
                processed += 1
                
            except Exception as e:
                logger.error("DLQ heal failed", error=str(e))
                # Move corrupted entry to permanent
                await self.redis.rpush(QUEUE_DLQ_PERMANENT, raw_data)
        
        if processed:
            logger.info(f"DLQ heal complete: {processed} processed")
    
    # =========================================================================
    # OUTBOUND
    # =========================================================================
    
    async def enqueue(
        self,
        to: str,
        text: str,
        correlation_id: str = None,
        attempts: int = 0
    ):
        """
        Add outbound message to send queue.
        """
        if not correlation_id:
            correlation_id = str(uuid.uuid4())
        
        payload = {
            "to": to,
            "text": text,
            "cid": correlation_id,
            "attempts": attempts
        }
        
        data = orjson.dumps(payload).decode("utf-8")
        await self.redis.rpush(QUEUE_OUTBOUND, data)
        
        logger.debug("Outbound queued", to=to[-4:], cid=correlation_id[:8])
    
    async def schedule_retry(self, payload: Dict[str, Any]):
        """
        Schedule a message for retry with exponential backoff.
        """
        attempts = payload.get("attempts", 0) + 1
        
        if attempts >= 5:
            logger.error("Max retries reached", cid=payload.get("cid"))
            return
        
        # Exponential backoff: 2, 4, 8, 16 seconds
        delay = 2 ** attempts
        execute_at = asyncio.get_event_loop().time() + delay
        
        payload["attempts"] = attempts
        data = orjson.dumps(payload).decode("utf-8")
        
        await self.redis.zadd(QUEUE_SCHEDULE, {data: execute_at})
        
        logger.info("Retry scheduled",
                   cid=payload.get("cid"),
                   attempt=attempts,
                   delay=delay)