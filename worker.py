import asyncio
import uuid
import signal
import socket
import redis.asyncio as redis
import httpx
import structlog
import orjson
import sentry_sdk
from prometheus_client import start_http_server, Counter, Histogram
from typing import Optional, Dict, List, Any
from sentry_sdk import capture_exception


from models import UserMapping  
from config import get_settings
from logger_config import configure_logger
from database import AsyncSessionLocal
from services.queue import QueueService, STREAM_INBOUND, QUEUE_OUTBOUND, QUEUE_SCHEDULE
from services.context import ContextService
from services.tool_registry import ToolRegistry
from services.openapi_bridge import OpenAPIGateway
from services.user_service import UserService
from services.engine import MessageEngine  # 👈 NOVI IMPORT
from services.maintenance import MaintenanceService
import sys 

settings = get_settings()
configure_logger()
logger = structlog.get_logger("worker")

# --- DEFINICIJA METRIKA ---
MSG_PROCESSED = Counter('whatsapp_msg_total', 'Ukupan broj obrađenih poruka', ['status'])
AI_LATENCY = Histogram('ai_processing_seconds', 'Vrijeme obrade AI zahtjeva', buckets=[1, 2, 5, 10, 20])

# --- SIGURNOST LOGIRANJA ---
SENSITIVE_KEYS = {
    'email', 'phone', 'password', 'token', 'authorization', 'secret', 
    'apikey', 'to', 'oib', 'jmbg', 'iban', 'card', 'credit_card', 'pin'
}

def sanitize_log_data(data: Any) -> Any:
    """Rekurzivno maskira osjetljive podatke."""
    if isinstance(data, dict):
        return {k: ("***MASKED***" if any(s in k.lower() for s in SENSITIVE_KEYS) else sanitize_log_data(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [sanitize_log_data(v) for v in data]
    return data

def summarize_data(data: Any) -> Any:
    """Pametno sažima podatke umjesto da ih serijalizira pa reže."""
    if isinstance(data, list):
        if len(data) > 20: 
            return f"<List with {len(data)} items>"
        return [summarize_data(item) for item in data]

    if isinstance(data, dict):
        if len(data) > 50:
            return {
                "info": "Large dictionary summarized",
                "keys_count": len(data),
                "keys_sample": list(data.keys())[:5]
            }
        
        clean_dict = {}
        for k, v in data.items():
            if any(s in k.lower() for s in SENSITIVE_KEYS):
                clean_dict[k] = "***MASKED***"
            elif isinstance(v, (dict, list, str)) and len(str(v)) > 500:
                clean_dict[k] = f"<Truncated type {type(v).__name__}, len={len(str(v))}>"
            else:
                clean_dict[k] = summarize_data(v)
        return clean_dict

    if isinstance(data, str) and len(data) > 1000:
        return data[:200] + f"... <truncated {len(data)-200} chars>"

    return data

class WhatsappWorker:
    def __init__(self):
        self.worker_id = str(uuid.uuid4())[:8]
        self.hostname = socket.gethostname()
        self.running = True
        
        self.redis = None
        self.gateway = None
        self.http = None
        self.queue = None
        self.context = None
        self.registry = None
        self.maintenance = None
        self.engine = None  # 👈 NOVI ENGINE
        self.consecutive_errors = 0 
        self.panic_threshold = 5  
        self.panic_sleep = 30    
        self.default_tenant_id = getattr(settings, "MOBILITY_TENANT_ID", None) 

    async def start(self):
        """Inicijalizacija infrastrukture i pokretanje glavne petlje."""
        logger.info("Worker starting", id=self.worker_id, host=self.hostname)

        # 1. Sentry Monitoring
        if settings.SENTRY_DSN:
            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                environment=settings.APP_ENV,
                traces_sample_rate=0.1, 
            )
        
        # 2. Prometheus Metrike
        try:
            start_http_server(8001)
            logger.info("Prometheus metrics server running on port 8001")
        except Exception as e:
            logger.warning("Failed to start metrics server", error=str(e))
        
        # 3. Infrastruktura (Redis, HTTP, Queue, Context)
        self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        self.http = httpx.AsyncClient(timeout=15.0)
        self.queue = QueueService(self.redis)
        self.context = ContextService(self.redis)
        
        # 4. Inicijalizacija Message Engine-a
        self.engine = MessageEngine(
            redis=self.redis,
            queue=self.queue,
            context=self.context,
            default_tenant_id=self.default_tenant_id
        )
        
        # 5. Inicijalizacija API Gateway-a
        if settings.MOBILITY_API_URL:
            key_status = "SET" if settings.MOBILITY_API_KEY else "MISSING"
            logger.info("Gateway Init", url=settings.MOBILITY_API_URL, key_status=key_status)
            
            self.gateway = OpenAPIGateway(base_url=settings.MOBILITY_API_URL)
            self.engine.gateway = self.gateway  # 👈 DODAJEMO GATEWAY U ENGINE
        else:
            logger.warning("MOBILITY_API_URL not set. AI tools will fail.")

        # 6. Učitavanje Swaggera
        self.registry = ToolRegistry(self.redis)
        self.engine.registry = self.registry  # 👈 DODAJEMO REGISTRY U ENGINE
        
        for src in settings.swagger_sources:
            try:
                logger.info(f"Loading swagger source", source=src)
                await self.registry.load_swagger(src)
                
                if src.startswith("http"):
                    asyncio.create_task(self.registry.start_auto_update(src))
            except Exception as e:
                logger.error(f"Failed to load swagger source", source=src, error=str(e))

        # 7. Maintenance Servis
        self.maintenance = MaintenanceService()

        # 8. Redis Stream Grupa
        try:
            await self.redis.xgroup_create(STREAM_INBOUND, "workers_group", id="$", mkstream=True)
        except redis.ResponseError:
            pass 

        logger.info("Worker ready. Processing loop started.")
        
        tick = 0
        
        # 9. Glavna Petlja
        while self.running:
            await self.redis.setex("worker:heartbeat", 30, "alive")
            await self.redis.setex(f"worker:heartbeat:{self.hostname}:{self.worker_id}", 30, "alive")

            try:
                tasks = [
                    self._process_inbound_batch(),
                    self._process_outbound(),
                    self._process_retries(),
                    self.maintenance.run_daily_cleanup()
                ]
                
                if tick % 100 == 0:
                    tasks.append(self._recover_stalled_messages())

                if tick % 300 == 0:
                    tasks.append(self.queue.auto_heal_dlq())

                await asyncio.gather(*tasks, return_exceptions=True)
                
                if self.consecutive_errors > 0:
                    logger.info("System recovered. Error counter reset.", prev_errors=self.consecutive_errors)
                    self.consecutive_errors = 0

                await asyncio.sleep(0.01) 
                tick += 1
                
            except Exception as e:
                self.consecutive_errors += 1
                logger.error("Critical Main Loop Error", error=str(e), attempt=self.consecutive_errors)
                capture_exception(e) 

                if self.consecutive_errors >= self.panic_threshold:
                    logger.critical("Fatal error loop. Exiting to allow Docker restart.")
                    sys.exit(1) 
                    await asyncio.sleep(self.panic_sleep)
                else:
                    await asyncio.sleep(1)

        await self.shutdown()

    async def _process_inbound_batch(self):
        if not self.running: return

        try:
            streams = await self.redis.xreadgroup(
                groupname="workers_group",
                consumername=self.worker_id,
                streams={STREAM_INBOUND: ">"},
                count=10,
                block=2000
            )
            
            if not streams: return

            tasks = []
            for _, messages in streams:
                for msg_id, data in messages:
                    tasks.append(self._process_single_message_transaction(msg_id, data))
            
            if tasks:
                await asyncio.gather(*tasks)

        except Exception as e:
            logger.error("Stream read error", error=str(e))

    async def _recover_stalled_messages(self):
        if not self.running: return

        try:
            claimed = await self.redis.xautoclaim(
                name=STREAM_INBOUND,
                groupname="workers_group",
                consumername=self.worker_id,
                min_idle_time=300000, 
                start_id="0-0",
                count=10
            )
            
            messages = claimed[1]
            
            if messages:
                logger.warning("Recovering stalled messages", count=len(messages))
                tasks = []
                for msg_id, payload in messages:
                    tasks.append(self._process_single_message_transaction(msg_id, payload))
                
                if tasks:
                    await asyncio.gather(*tasks)
                    
        except Exception as e:
            logger.error("Recovery loop failed", error=str(e))

    async def _process_single_message_transaction(self, msg_id: str, payload: dict):
        try:
            sender = payload.get('sender')
            text = payload.get('text', '').strip()
            
            if sender and text:
                # 👈 KORISTIMO ENGINE ZA RATE LIMIT I BUSINESS LOGIC
                if await self.engine.check_rate_limit(sender):
                    with AI_LATENCY.time():
                        await self.engine.handle_business_logic(sender, text)
                    MSG_PROCESSED.labels(status="success").inc()
                else:
                    logger.warning("Rate limit exceeded", sender=sender)
                    MSG_PROCESSED.labels(status="rate_limit").inc()
            
            await self.redis.xack(STREAM_INBOUND, "workers_group", msg_id)
            await self.redis.xdel(STREAM_INBOUND, msg_id)

        except Exception as e:
            safe_payload = sanitize_log_data(payload)
            logger.error("Message processing failed", id=msg_id, payload=safe_payload, error=str(e))
            
            MSG_PROCESSED.labels(status="error").inc()
            capture_exception(e)
            
            await self.queue.store_inbound_dlq(payload, str(e))
            
            await self.redis.xack(STREAM_INBOUND, "workers_group", msg_id)
            await self.redis.xdel(STREAM_INBOUND, msg_id)

    async def _process_outbound(self):
        if not self.running: return
        
        try:
            task = await self.redis.blpop(QUEUE_OUTBOUND, timeout=1)
            if not task: return
            
            payload = orjson.loads(task[1])
            await self._send_infobip(payload)
            
        except Exception as e:
            logger.error("Outbound processing error", error=str(e))
            capture_exception(e) 
            if 'payload' in locals():
                await self.queue.schedule_retry(payload)

    async def _process_retries(self):
        if not self.running: return
        
        try:
            now = asyncio.get_event_loop().time()
            tasks = await self.redis.zrangebyscore(QUEUE_SCHEDULE, 0, now, start=0, num=1)
            
            if tasks:
                if await self.redis.zrem(QUEUE_SCHEDULE, tasks[0]):
                    data = orjson.loads(tasks[0])
                    logger.info("Retrying message", cid=data.get('cid'), attempt=data.get('attempts'))
                    
                    await self.queue.enqueue(
                        to=data['to'], 
                        text=data['text'], 
                        correlation_id=data.get('cid'), 
                        attempts=data.get('attempts')
                    )
        except Exception as e:
            logger.error("Retry processing error", error=str(e))
            capture_exception(e)

    async def _send_infobip(self, payload):
        url = f"https://{settings.INFOBIP_BASE_URL}/whatsapp/1/message/text"
        headers = {
            "Authorization": f"App {settings.INFOBIP_API_KEY}", 
            "Content-Type": "application/json"
        }
        body = {
            "from": settings.INFOBIP_SENDER_NUMBER, 
            "to": payload['to'], 
            "content": {"text": payload['text']}
        }
        
        try:
            logger.info("Šaljem poruku", to="***MASKED***")
            resp = await self.http.post(url, json=body, headers=headers)
            resp.raise_for_status()
        except Exception as e:
            logger.error("Failed to send WhatsApp message", error=str(e))
            raise e

    async def shutdown(self):
        logger.info("Worker shutting down...")
        self.running = False
        await asyncio.sleep(15)
        
        if self.http: await self.http.aclose()
        if self.gateway: await self.gateway.close()
        if self.redis: await self.redis.aclose()
        logger.info("Shutdown complete.")

async def main():
    worker = WhatsappWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: setattr(worker, 'running', False))
    await worker.start()

if __name__ == "__main__":
    try:    
        asyncio.run(main())
    except KeyboardInterrupt:
        pass