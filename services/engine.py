import asyncio
import orjson
import structlog
from typing import Optional, Dict, Any
from sentry_sdk import capture_exception

from models import UserMapping
from database import AsyncSessionLocal
from services.user_service import UserService
from services.ai import analyze_intent

logger = structlog.get_logger("engine")

class MessageEngine:
    def __init__(self, redis, queue, context, default_tenant_id):
        self.redis = redis
        self.queue = queue
        self.context = context
        self.default_tenant_id = default_tenant_id
        self.gateway = None
        self.registry = None

    async def handle_business_logic(self, sender: str, text: str):
        async with AsyncSessionLocal() as session:
            user_service = UserService(session, self.gateway)
            
            # 1. Identifikacija
            user = await user_service.get_active_identity(sender)
            if not user:
                user = await self.perform_auto_onboard(sender, user_service)
            if not user:
                return

            # 2. Dohvat svih podataka kao OperationalContext objekt
            ctx = await user_service.build_operational_context(user.api_identity, sender)
            
            # 3. Request Context (Za Tenant ID)
            request_context = {
                "tenant_id": self.default_tenant_id,
                "user_guid": user.api_identity,
                "phone": sender
            }

            # 4. SYSTEM PROMPT - koristimo Pydantic metode
            facts = ctx.to_prompt_block()  # 👈 OVA METODA JE IZ SCHEMAS.PY!

            identity_context = (
                f"SYSTEM DATA SNAPSHOT:\n"
                f"You are the assistant for {ctx.user.display_name}.\n\n"
                f"### KNOWN FACTS (KEYRING):\n"
                f"{facts}\n"
                f"---------------------------------------------------\n"
                f"RULES:\n"
                f"1. Phone Number: {ctx.user.phone}. (Answer if asked)\n"
                f"2. Vehicle Plate: {ctx.vehicle.plate}. (Answer if asked)\n"
                f"3. Registration Expiry: {ctx.vehicle.reg_expiry}. (Answer if asked)\n"
                f"4. FOR TOOLS: Use 'User.PersonId' ({ctx.user.person_id}) for personId parameters.\n"
                f"5. FOR TOOLS: Use 'Vehicle.Id' ({ctx.vehicle.id}) for vehicleId parameters.\n"
                f"6. If a Fact is 'UNKNOWN', you MUST ask the user for it or call a Tool to find it.\n"
            )
            
            await self.context.add_message(sender, "user", text)
            await self.run_ai_loop(sender, text, identity_context, request_context)

    async def perform_auto_onboard(self, sender: str, service: UserService) -> Optional[UserMapping]:
        """Izdvojena logika onboardinga za čišći glavni flow."""
        logger.info("Unknown user, attempting auto-onboard", sender=sender)
        
        result = await service.try_auto_onboard(sender)
        
        if not result:
            logger.warning("Access denied", sender=sender)
            await self.queue.enqueue(
                sender, 
                "⛔ Vaš broj mobitela nije pronađen u sustavu.\nMolimo kontaktirajte administratora flote."
            )
            return None

        name, vehicle = result
        welcome_msg = f"👋 Bok {name}! Prepoznao sam tvoj broj.\nTvoje vozilo: {vehicle}\nKako ti mogu pomoći?"
        await self.queue.enqueue(sender, welcome_msg)
        
        return await service.get_active_identity(sender)

    async def run_ai_loop(self, sender, text, system_ctx, request_context=None):
        for _ in range(3): 
            # Paralelno
            h_task = self.context.get_history(sender)
            t_task = self.registry.find_relevant_tools(text or "help")
            history, tools = await asyncio.gather(h_task, t_task)
            
            decision = await analyze_intent(history, text, tools, system_instruction=system_ctx)
            
            if decision.get("tool"):
                await self.context.add_message(sender, "assistant", None, tool_calls=decision["raw_tool_calls"])
                
                tool_name = decision["tool"]
                tool_def = self.registry.tools_map.get(tool_name)
                result = None
                
                try:
                    if tool_name == "get_my_vehicle_info":
                        result = "Podaci o vozilu su već navedeni u FACTS sekciji na početku poruke. Pročitaj ih."
                    elif tool_def:
                        logger.info("Executing Swagger Tool", tool=tool_name)
                        result = await self.gateway.execute_tool(tool_def, decision["parameters"], user_context=request_context)
                    else:
                        result = {"error": f"Tool '{tool_name}' not found."}
                except Exception as e:
                    logger.error("Tool failed", tool=tool_name, error=str(e))
                    result = f"Error: {str(e)}"

                # Serijalizacija rezultata
                if not isinstance(result, str):
                    try:
                        result = orjson.dumps(result).decode()
                    except:
                        result = str(result)

                await self.context.add_message(sender, "tool", result, tool_call_id=decision["tool_call_id"], name=tool_name)
                text = None 
            else:
                # Kraj
                resp = decision.get("response_text")
                await self.context.add_message(sender, "assistant", resp)
                await self.queue.enqueue(sender, resp)
                break

    async def check_rate_limit(self, sender: str) -> bool:
        """Provjera rate limita koristeći atomski Redis Pipeline."""
        key = f"rate:{sender}"
        
        async with self.redis.pipeline() as pipe:
            pipe.incr(key)
            pipe.expire(key, 60)
            results = await pipe.execute()
            
        count = results[0]
        return count <= 20