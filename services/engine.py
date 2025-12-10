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
        Glavni mozak sustava. Koordinira bazu, AI i vanjske alate.
        Sada prima i Cache servis za optimizaciju.
        """
        self.redis = redis
        self.queue = queue
        self.context = context
        self.default_tenant_id = default_tenant_id
        self.cache = cache  # 👈 SPREMAMO CACHE (Ključno za UserService)
        self.gateway = None
        self.registry = None
    
    async def handle_business_logic(self, sender: str, text: str):
        """
        Orkestrira cijeli proces:
        1. Identificira korisnika (UserService).
        2. Priprema kontekst (Facts & Rules).
        3. Šalje upit AI-u.
        """
        async with AsyncSessionLocal() as session:
            # 👈 OVDJE JE FIX: Inicijaliziramo UserService s Cache servisom!
            user_service = UserService(session, self.gateway, self.cache)
            
            # 1. Identifikacija korisnika
            user = await user_service.get_active_identity(sender)
            if not user:
                # Ako ne znamo tko je, probaj ga naći u MobilityOne (Onboarding)
                user = await self.perform_auto_onboard(sender, user_service)
            
            if not user:
                # Ako i dalje ne znamo, odustani (korisnik je već dobio poruku odbijanja)
                return

            # 2. Dohvat "Šalabahtera" (Context)
            # Ovo povlači podatke iz Cache-a ili API-ja (ovisno o stanju)
            ctx = await user_service.build_operational_context(user.api_identity, sender)
            
            # 3. Priprema konteksta zahtjeva (za HTTP headere kao x-tenant)
            request_context = {
                "tenant_id": self.default_tenant_id,
                "user_guid": user.api_identity,
                "phone": sender
            }

            # 4. Generiranje SYSTEM PROMPTA
            # Koristimo metodu iz schemas.py koja lijepo formatira podatke
            facts = ctx.to_prompt_block()

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
                # --- SIGURNOSNO PRAVILO ---
                f"7. DO NOT GUESS PARAMETERS. If a tool requires a parameter (like 'reason', 'amount', 'date') "
                f"and it is not in the user's message, you MUST ASK the user for it. Do not invent values.\n"
            )
            
            # Spremi korisnikovu poruku u povijest razgovora
            await self.context.add_message(sender, "user", text)
            
            # Pokreni glavnu AI petlju (ReAct)
            await self.run_ai_loop(sender, text, identity_context, request_context)

    async def perform_auto_onboard(self, sender: str, service: UserService) -> Optional[UserMapping]:
        """
        Posebna logika za nove korisnike.
        Pokušava ih pronaći u MobilityOne sustavu ako nisu u našoj bazi.
        """
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
        
        # Vrati novokreirani zapis iz baze
        return await service.get_active_identity(sender)

    def compact_json(self, data: Any) -> Any:
        """
        Rekurzivno čisti JSON od praznih vrijednosti (null, "", {}, []).
        Ovo drastično smanjuje potrošnju tokena kod velikih API odgovora.
        """
        if isinstance(data, dict):
            cleaned = {}
            for k, v in data.items():
                res = self.compact_json(v)
                # Zadrži samo ako rezultat nije None/Prazan
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
        
    async def run_ai_loop(self, sender, text, system_ctx, request_context=None):
        """
        ReAct Loop: User -> AI -> Tool -> AI -> Response
        Vrti se do 3 puta kako bi omogućio složene akcije (npr. dohvati pa analiziraj).
        """
        for _ in range(3): 
            # Paralelno dohvaćamo povijest i tražimo alate (optimizacija vremena)
            h_task = self.context.get_history(sender)
            t_task = self.registry.find_relevant_tools(text or "help")
            history, tools = await asyncio.gather(h_task, t_task)
            
            # AI Analiza (odlučuje zvati alat ili odgovoriti)
            decision = await analyze_intent(history, text, tools, system_instruction=system_ctx)
            
            if decision.get("tool"):
                # AI je odlučio pozvati alat
                tool_name = decision["tool"]
                logger.info("AI decided to call tool", tool=tool_name)

                # Zabilježi namjeru u povijest (bez teksta odgovora, samo tool call)
                await self.context.add_message(sender, "assistant", None, tool_calls=decision["raw_tool_calls"])
                
                tool_def = self.registry.tools_map.get(tool_name)
                result = None
                
                try:
                    # Poseban hardkodirani alat (za svaki slučaj, ako AI zabrije)
                    if tool_name == "get_my_vehicle_info":
                        result = "Podaci o vozilu su već navedeni u FACTS sekciji na početku poruke. Pročitaj ih."
                    
                    elif tool_def:
                        # IZVRŠAVANJE PRAVOG API POZIVA (OPENAPI BRIDGE)
                        logger.info("Executing Swagger Tool", tool=tool_name)
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

                # Serijalizacija i čišćenje rezultata (Compact JSON)
                if not isinstance(result, str):
                    try:
                        # Koristimo self.compact_json metodu
                        clean_data = self.compact_json(result)
                        result = orjson.dumps(clean_data).decode()
                    except Exception as e:
                        logger.warning("Result serialization failed", error=str(e))
                        result = str(result)

                # Vraćamo rezultat alata AI-u kao "Tool Message"
                await self.context.add_message(
                    sender, "tool", result, 
                    tool_call_id=decision["tool_call_id"], 
                    name=tool_name
                )
                
                # Brišemo 'text' jer u idućem krugu AI treba generirati odgovor na temelju alata, ne starog pitanja
                text = None 
            else:
                # AI je generirao konačni odgovor (nije alat)
                resp = decision.get("response_text")
                if resp:
                    await self.context.add_message(sender, "assistant", resp)
                    await self.queue.enqueue(sender, resp)
                break

    async def check_rate_limit(self, sender: str) -> bool:
        """
        Provjera rate limita (max 20 poruka u minuti).
        Koristi Redis Pipeline za atomsku operaciju.
        """
        key = f"rate:{sender}"
        
        async with self.redis.pipeline() as pipe:
            pipe.incr(key)
            pipe.expire(key, 60)
            results = await pipe.execute()
            
        count = results[0]
        return count <= 20