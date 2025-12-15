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
    def __init__(self, redis, queue, context, default_tenant_id, cache):
        """
        MessageEngine (v10.0)
        Glavni orkestrator sustava. Povezuje User Service, AI i Tool Registry.
        Odgovoran za:
        1. Identifikaciju korisnika (PersonID).
        2. Pripremu konteksta za alate (Booking & Inspector).
        3. Izvršavanje AI petlje.
        """
        self.redis = redis
        self.queue = queue
        self.context = context
        self.default_tenant_id = default_tenant_id
        self.cache = cache
        
        # Dependency Injection (postavlja se iz workera)
        self.gateway = None
        self.registry = None
    
    async def check_rate_limit(self, sender: str) -> bool:
        """Sprečava spam (Max 20 poruka u 60 sekundi)."""
        key = f"rate:{sender}"
        async with self.redis.pipeline() as pipe:
            pipe.incr(key)
            pipe.expire(key, 60)
            results = await pipe.execute()
        count = results[0]
        return count <= 20

    async def handle_business_logic(self, sender: str, text: str):
        """
        Glavna logika obrade poruke.
        """
        async with AsyncSessionLocal() as session:
            user_service = UserService(session, self.gateway, self.cache)
            
            # 1. Identifikacija korisnika
            user = await user_service.get_active_identity(sender)
            
            # Ako korisnik ne postoji, probaj auto-onboard
            if not user:
                user = await self.perform_auto_onboard(sender, user_service)
            
            # Ako i dalje ne postoji (nema prava), prekini
            if not user:
                return

            # 2. Dohvat operativnog konteksta (Vozilo, Ime, Stanje)
            ctx = await user_service.build_operational_context(user.api_identity, sender)
            
            # 3. PRIPREMA KONTEKSTA ZA ALATE (CRITICAL STEP)
            # Ovdje pakiramo sve što OpenAPIGateway treba da bi Booking i Inspector radili.
            request_context = {
                "tenant_id": self.default_tenant_id,
                "user_guid": user.api_identity,
                
                # [BOOKING FIX] Ovdje eksplicitno šaljemo PersonID za 'AssignedToId'
                "person_id": user.api_identity, 
                
                # [AI INSPECTOR FIX] Ovdje šaljemo upit da Inspector zna što tražiti u velikom JSON-u
                "last_user_message": text,      
                
                "phone": sender
            }

            # 4. Priprema System Prompta (Instrukcije za AI)
            facts = ctx.to_prompt_block()

            identity_context = (
                f"SYSTEM DATA SNAPSHOT:\n"
                f"You are the intelligent assistant for {ctx.user.display_name}.\n"
                f"Your mission is to help the user by calling the correct tools based on their intent.\n\n"
                
                f"### KEYRING (KNOWN FACTS):\n"
                f"{facts}\n"
                f"Your internal PersonID is: {user.api_identity}\n"
                f"---------------------------------------------------\n"
                
                f"### CRITICAL OPERATIONAL RULES:\n"
                f"1. **PARAMETER HONESTY**: Do NOT invent parameter values. If a tool requires a parameter and it is NOT in the user's message or KEYRING, you MUST ASK the user for it.\n"
                
                # [AUTO-FILL PRAVILA]
                f"2. **AUTO-FILL (PERSON)**: Always use '{user.api_identity}' for 'personId' OR 'AssignedToId' arguments. Do NOT ask the user for their ID.\n"
                f"3. **AUTO-FILL (VEHICLE)**: Use '{ctx.vehicle.id}' for 'vehicleId' or 'assetId' arguments (unless user explicitly selects a different vehicle from a list).\n"
                
                f"4. **LANGUAGE**: Speak Croatian. Be concise and professional.\n"
                f"5. **AMBIGUITY**: If multiple tools look similar, ask for clarification.\n"
            )
            
            # Spremi korisnikovu poruku u povijest
            await self.context.add_message(sender, "user", text)
            
            # Pokreni AI petlju
            await self.run_ai_loop(sender, text, identity_context, request_context)

    async def run_ai_loop(self, sender, text, system_ctx, request_context=None):
        for i in range(3): 
            h_task = self.context.get_history(sender)
            t_task = self.registry.find_relevant_tools(text or "help")
            
            history, tools = await asyncio.gather(h_task, t_task)
            
            # --- DEBUG LOG (OVO ĆE NAM REĆI ISTINU) ---
            tool_names = [t['function']['name'] for t in tools]
            logger.info(f"🐛 AI LOOP [{i}]: Found tools", count=len(tools), names=tool_names)
            # ------------------------------------------

            decision = await analyze_intent(history, text, tools, system_instruction=system_ctx)
            
            if decision.get("tool"):
                tool_name = decision["tool"]
                logger.info("AI decided to call tool", tool=tool_name)

                await self.context.add_message(sender, "assistant", None, tool_calls=decision["raw_tool_calls"])
                
                tool_def = self.registry.tools_map.get(tool_name)
                result = None
                
                try:
                    if tool_name == "get_my_vehicle_info":
                        result = "Podaci o vozilu su već navedeni u KEYRING (FACTS) sekciji system prompta. Koristi te podatke."
                    elif tool_def:
                        result = await self.gateway.execute_tool(
                            tool_def, 
                            decision["parameters"], 
                            user_context=request_context
                        )
                    else:
                        result = {"error": f"Tool '{tool_name}' not found in registry."}
                        
                except Exception as e:
                    logger.error("Tool execution failed", tool=tool_name, error=str(e))
                    result = f"Error executing tool: {str(e)}"

                if not isinstance(result, str):
                    try:
                        clean_data = self.compact_json(result)
                        result = orjson.dumps(clean_data).decode()
                    except Exception as e:
                        logger.warning("Result serialization failed", error=str(e))
                        result = str(result)

                await self.context.add_message(
                    sender, "tool", result, 
                    tool_call_id=decision["tool_call_id"], 
                    name=tool_name
                )
                text = None 
            else:
                resp = decision.get("response_text")
                if resp:
                    await self.context.add_message(sender, "assistant", resp)
                    await self.queue.enqueue(sender, resp)
                break

    async def perform_auto_onboard(self, sender: str, service: UserService) -> Optional[UserMapping]:
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

    def compact_json(self, data: Any) -> Any:
        """
        Rekurzivno uklanja prazne vrijednosti iz JSON-a radi uštede tokena.
        """
        if isinstance(data, dict):
            cleaned = {}
            for k, v in data.items():
                res = self.compact_json(v)
                if res not in [None, "", [], {}, "null", "None"]:
                    cleaned[k] = res
            return cleaned if cleaned else None
        elif isinstance(data, list):
            cleaned = []
            for v in data:
                res = self.compact_json(v)
                if res not in [None, "", [], {}, "null", "None"]:
                    cleaned.append(res)
            return cleaned if cleaned else None
        else:
            return data