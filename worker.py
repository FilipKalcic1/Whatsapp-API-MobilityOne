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
from services.ai import analyze_intent
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
        self.consecutive_errors = 0 
        self.panic_threshold = 5  
        self.panic_sleep = 30     


    async def start(self):
        """Inicijalizacija infrastrukture i pokretanje glavne petlje."""
        logger.info("Worker starting", id=self.worker_id, host=self.hostname)

        if settings.SENTRY_DSN:
            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                environment=settings.APP_ENV,
                traces_sample_rate=0.1, 
            )

        try:
            start_http_server(8001)
            logger.info("Prometheus metrics server running on port 8001")
        except Exception as e:
            logger.warning("Failed to start metrics server", error=str(e))
        
        self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        self.http = httpx.AsyncClient(timeout=15.0)
        self.queue = QueueService(self.redis)
        self.context = ContextService(self.redis)
        
        if settings.MOBILITY_API_URL:
            self.gateway = OpenAPIGateway(base_url=settings.MOBILITY_API_URL)
        else:
            logger.warning("MOBILITY_API_URL not set. AI tools will fail.")

        self.registry = ToolRegistry(self.redis)
        swagger_src = settings.SWAGGER_URL or "swagger.json"
        
        try:
            logger.info(f"Loading swagger from: {swagger_src}")
            await self.registry.load_swagger(swagger_src)
            if swagger_src.startswith("http"):
                asyncio.create_task(self.registry.start_auto_update(swagger_src))
        except Exception as e:
            logger.error("Failed to load Swagger definition", error=str(e))

        # Inicijalizacija Maintenance servisa
        self.maintenance = MaintenanceService()

        try:
            await self.redis.xgroup_create(STREAM_INBOUND, "workers_group", id="$", mkstream=True)
        except redis.ResponseError:
            pass 

        logger.info("Worker ready. Processing loop started.")
        
        tick = 0
        
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
                
                # [FAZA 1] Recovery (svakih ~100 taktova / ~1-2 sec)
                if tick % 100 == 0:
                    tasks.append(self._recover_stalled_messages())

                # [NOVO] AUTO-HEAL DLQ (svakih ~300 taktova / ~3-6 sec)
                # Provjerava ima li mrtvih poruka i vraća ih u život
                if tick % 300 == 0:
                    tasks.append(self.queue.auto_heal_dlq())

                await asyncio.gather(*tasks, return_exceptions=True)
                
                if self.consecutive_errors > 0:
                    logger.info("System recovered. Error counter reset.", prev_errors=self.consecutive_errors)
                    self.consecutive_errors = 0

                await asyncio.sleep(0.01) # Brzi sleep za responzivnost
                tick += 1
                
            except Exception as e:
                self.consecutive_errors += 1
                logger.error("Critical Main Loop Error", error=str(e), attempt=self.consecutive_errors)
                capture_exception(e) 

                if self.consecutive_errors >= self.panic_threshold:
                    logger.critical("Fatal error loop. Exiting to allow Docker restart.")
                    sys.exit(1) 
                    
                    # Docker će vidjeti exit code 1 i napraviti restart
                    # Spavamo duže da ne gušimo CPU i disk logovima
                    await asyncio.sleep(self.panic_sleep)
                    
                    # Opcionalno: Resetiramo brojač nakon spavanja da probamo opet "od nule"
                    # ili ga ostavimo visokim da idući fail opet triggera paniku.
                    # Ovdje ga je sigurnije ne resetirati odmah nego pustiti da 'try' blok prođe.
                else:
                    # Obični kratki sleep za manje greške
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
        """
        [ENTERPRISE] Recovery poruka koje su zapele (timeout 5 minuta).
        """
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
                if await self._check_rate_limit(sender):
                    with AI_LATENCY.time():
                        await self._handle_business_logic(sender, text)
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
            
            # [ENTERPRISE] Spremanje u DLQ (pametni queue s retry logikom)
            await self.queue.store_inbound_dlq(payload, str(e))
            
            await self.redis.xack(STREAM_INBOUND, "workers_group", msg_id)
            await self.redis.xdel(STREAM_INBOUND, msg_id)

    # --- EMAIL NAČIN --- 
    # async def _handle_onboarding(self, sender: str, text: str, service: UserService):
    #     key = f"onboarding:{sender}"
    #     state = await self.redis.get(key)
    #     
    #     if state == "WAITING_EMAIL":
    #         if "@" not in text or len(text) < 5:
    #             await self.queue.enqueue(sender, "⚠️ Neispravan format e-maila. Molim pokušajte ponovo.")
    #             return
    #
    #         # Ovdje se prije prosljeđivao email servisu
    #         result = await service.onboard_user(sender, text)
    #         
    #         if result:
    #             name, vehicle_info = result
    #             await self.redis.delete(key)
    #             
    #             msg = (
    #                 f"✅ *Identitet potvrđen!*\n"
    #                 f"👤 {name}\n"
    #                 f"🚗 {vehicle_info}\n\n"
    #                 f"Sustav je spreman. Kako vam mogu pomoći?"
    #             )
    #             await self.queue.enqueue(sender, msg)
    #         else:
    #             await self.queue.enqueue(
    #                 sender, 
    #                 f"⛔ E-mail '{text}' nije pronađen. Kontaktirajte administratora."
    #             )
    #     else:
    #         welcome_msg = "👋 Dobrodošli! Molim upišite vašu službenu e-mail adresu."
    #         await self.queue.enqueue(sender, welcome_msg)
    #         await self.redis.setex(key, 900, "WAITING_EMAIL")


    async def _handle_business_logic(self, sender: str, text: str):
        async with AsyncSessionLocal() as session:
            user_service = UserService(session, self.gateway)
            
            # Prvi korak je provjera postoji li korisnik već u našem lokalnom cacheu.
            user = await user_service.get_active_identity(sender)
            
            # Ako korisnik nije pronađen, pokrećemo proces automatskog onboardinga.
            if not user:
                user = await self._perform_auto_onboard(sender, user_service)

            # Ako nakon pokušaja onboardinga korisnik i dalje ne postoji, prekidamo proces.
            if not user:
                return

            # U ovom trenutku sigurno imamo validnog korisnika i GUID, pa pripremamo AI kontekst.
            identity_context = (
                f"SYSTEM ENFORCEMENT: You are acting on behalf of '{user.display_name}'. "
                f"The Internal API User Identifier (GUID) is '{user.api_identity}'. "
                f"RULE: For EVERY tool call, you MUST set 'User' or 'PersonId' to '{user.api_identity}'."
            )
            
            # Spremanje poruke i pokretanje AI logike.
            await self.context.add_message(sender, "user", text)
            await self._run_ai_loop(sender, text, identity_context)

    async def _perform_auto_onboard(self, sender: str, service: UserService) -> Optional[UserMapping]:
        """Izdvojena logika onboardinga za čišći glavni flow."""
        logger.info("Unknown user, attempting auto-onboard", sender=sender)
        
        result = await service.try_auto_onboard(sender)
        
        # Ako onboarding nije uspio, šaljemo poruku odbijanja i vraćamo None.
        if not result:
            logger.warning("Access denied", sender=sender)
            await self.queue.enqueue(
                sender, 
                "⛔ Vaš broj mobitela nije pronađen u sustavu.\nMolimo kontaktirajte administratora flote."
            )
            return None

        # Ako je uspio, šaljemo dobrodošlicu.
        name, vehicle = result
        welcome_msg = f"👋 Bok {name}! Prepoznao sam tvoj broj.\nTvoje vozilo: {vehicle}\nKako ti mogu pomoći?"
        await self.queue.enqueue(sender, welcome_msg)
        
        # Vraćamo svježe učitani objekt korisnika iz baze.
        return await service.get_active_identity(sender)

    async def _run_ai_loop(self, sender, text, system_ctx):
        """AI Petlja: Optimizirana za brzinu."""
        
        for _ in range(3): 
            history = await self.context.get_history(sender)
            
            search_query = text
            if not search_query:
                for msg in reversed(history):
                    if msg['role'] == 'user':
                        search_query = msg['content']
                        break
            
            tools = await self.registry.find_relevant_tools(search_query or "help")
            
            decision = await analyze_intent(
                history, text, tools, 
                system_instruction=system_ctx
            )
            
            if decision.get("tool"):
                await self.context.add_message(
                    sender, "assistant", None, 
                    tool_calls=decision["raw_tool_calls"]
                )
                
                tool_name = decision["tool"]
                tool_def = self.registry.tools_map.get(tool_name)
                
                if tool_def:
                    logger.info("Tool exec", tool=tool_name, params=summarize_data(decision["parameters"]))
                    result = await self.gateway.execute_tool(tool_def, decision["parameters"])
                else:
                    result = {"error": "Tool not found"}
                
                await self.context.add_message(
                    sender, "tool", 
                    result, 
                    tool_call_id=decision["tool_call_id"],
                    name=tool_name
                )
                
                text = None 
            else:
                resp = decision.get("response_text")
                await self.context.add_message(sender, "assistant", resp)
                await self.queue.enqueue(sender, resp)
                break

    async def _check_rate_limit(self, sender: str) -> bool:
        """
        [MODIFIED] Provjera rate limita koristeći atomski Redis Pipeline.
        """
        key = f"rate:{sender}"
        
        async with self.redis.pipeline() as pipe:
            pipe.incr(key)
            pipe.expire(key, 60)
            results = await pipe.execute()
            
        count = results[0]
        return count <= 20

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
        await asyncio.sleep(15)  # [FAZA 1] Dajemo vremena za graceful shutdown
        
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