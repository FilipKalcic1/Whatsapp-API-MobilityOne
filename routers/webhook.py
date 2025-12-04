import uuid
import structlog
import json
from fastapi import APIRouter, Depends, Request, HTTPException
from services.queue import QueueService
from security import validate_infobip_signature
from fastapi_limiter.depends import RateLimiter

router = APIRouter()
logger = structlog.get_logger("webhook")

# Dependency Injection
def get_queue(request: Request) -> QueueService: 
    return request.app.state.queue

# routers/webhook.py

@router.post(
    "/webhook/whatsapp", 
    dependencies=[
        Depends(validate_infobip_signature), 
        Depends(RateLimiter(times=100, minutes=1)) 
    ]
)
async def whatsapp_webhook(request: Request, queue: QueueService = Depends(get_queue)):
    try:
        payload = await request.json()
        logger.info("🔥 RAW INFOBIP PAYLOAD 🔥", payload=payload) 
    except Exception as e:
        logger.error("Failed to parse JSON", error=str(e))
        return {"status": "error", "reason": "invalid_json"}

    results = payload.get("results", [])
    if not results:
        return {"status": "ignored", "reason": "empty_structure"}

    msg = results[0]
    
    # 1. POPRAVAK: Izvlačenje teksta iz 'content' liste
    text = ""
    if "content" in msg and isinstance(msg["content"], list):
        # Infobip šalje listu sadržaja, uzimamo prvi element ako je tekst
        first_content = msg["content"][0]
        if first_content.get("type") == "TEXT":
            text = first_content.get("text", "")
    else:
        # Fallback za stari format (ako ga ikad pošalju)
        text = msg.get("text", "")

    # 2. POPRAVAK: Sender je 'sender' ili 'from'
    sender = msg.get("sender") or msg.get("from")
    message_id = msg.get("messageId") or str(uuid.uuid4())

    if not sender or not text:
        logger.warning("Ignoriram poruku (fali pošiljatelj ili tekst)", sender=sender, has_text=bool(text))
        return {"status": "ignored", "reason": "incomplete_data"}

    logger.info("✅ Poruka uspješno pročitana", sender=sender, text=text)
    
    stream_id = await queue.enqueue_inbound(
        sender=sender, 
        text=text, 
        message_id=message_id
    )
    
    return {"status": "queued", "stream_id": stream_id}