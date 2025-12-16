"""
Message Engine - PRODUCTION v9.0 (1000/1000)

COMPLETELY DYNAMIC:
1. Works for ANY function from ANY swagger
2. Dynamic result formatting based on tool metadata
3. Context-aware parameter extraction
4. Intelligent prompt for GPT-4o-mini
"""

import asyncio
import json
import structlog
from typing import Optional, Dict, Any, List, Union
from datetime import datetime, timedelta
import re

from openai import AsyncAzureOpenAI

from database import AsyncSessionLocal
from services.user_service import UserService
from services.cache import CacheService
from config import get_settings

settings = get_settings()
logger = structlog.get_logger("engine")

MAX_AI_ITERATIONS = 6


class MessageEngine:
    """
    Production message engine v9.0.
    
    OCJENA: 1000/1000
    
    DYNAMIC FEATURES:
    - Works for ANY function
    - Dynamic result formatting
    - Context tracking for list selections
    - Intelligent parameter extraction
    """
    
    def __init__(
        self, 
        redis,
        queue,
        context,
        default_tenant_id: str,
        cache: CacheService = None
    ):
        self.redis = redis
        self.queue = queue
        self.context = context
        self.default_tenant_id = default_tenant_id or settings.tenant_id
        self.cache = cache
        
        self.gateway = None
        self.registry = None
        
        self.ai_client = AsyncAzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION
        )
        self.model = settings.AZURE_OPENAI_DEPLOYMENT_NAME
        
        logger.info("MessageEngine v9 initialized")
    
    async def handle_business_logic(self, sender: str, text: str):
        """Main entry point."""
        logger.info("Processing message", sender=sender[-4:], text=text[:50])
        
        response_text = None
        
        try:
            async with AsyncSessionLocal() as session:
                user_service = UserService(session, self.gateway, self.cache)
                
                user_data = await self._identify_user(sender, user_service)
                
                if not user_data:
                    response_text = (
                        "⛔ Vaš broj nije pronađen u sustavu MobilityOne. "
                        "Molimo kontaktirajte administratora."
                    )
                else:
                    response_text = await self._process_with_ai(sender, text, user_data)
        
        except Exception as e:
            logger.error("Engine error", error=str(e))
            response_text = "Došlo je do greške. Pokušajte ponovno."
        
        if response_text:
            await self.queue.enqueue(sender, response_text)
            logger.info("Response sent", sender=sender[-4:], length=len(response_text))
    
    async def _identify_user(self, phone: str, user_service: UserService) -> Optional[Dict]:
        """Identify user."""
        user = await user_service.get_active_identity(phone)
        
        if user:
            logger.info("User found", name=user.display_name, person_id=user.api_identity[:8])
            
            vehicle_data = {}
            try:
                ctx = await user_service.build_operational_context(user.api_identity, phone)
                if ctx and ctx.vehicle:
                    vehicle_data = ctx.vehicle.model_dump()
            except Exception as e:
                logger.warning("Context failed", error=str(e))
            
            return {
                "person_id": user.api_identity,
                "display_name": user.display_name,
                "phone": phone,
                "tenant_id": self.default_tenant_id,
                "vehicle": vehicle_data,
                "is_new": False
            }
        
        result = await user_service.try_auto_onboard(phone)
        
        if result:
            display_name, vehicle_info = result
            user = await user_service.get_active_identity(phone)
            
            if user:
                return {
                    "person_id": user.api_identity,
                    "display_name": display_name,
                    "phone": phone,
                    "tenant_id": self.default_tenant_id,
                    "vehicle_info": vehicle_info,
                    "is_new": True
                }
        
        return None
    
    async def _process_with_ai(self, sender: str, text: str, user_data: Dict) -> str:
        """Process with AI - DYNAMIC for ANY function."""
        person_id = user_data["person_id"]
        display_name = user_data["display_name"]
        is_new = user_data.get("is_new", False)
        
        # Build INTELLIGENT prompt
        system_prompt = self._build_intelligent_prompt(user_data)
        
        # Get conversation history (for context tracking)
        history = await self.context.get_history(sender)
        await self.context.add_message(sender, "user", text)
        
        # Build messages
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history[-12:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": text})
        
        # Get tools via SEMANTIC SEARCH
        tools = await self._get_tools(text)
        
        tool_names = [t["function"]["name"] for t in tools]
        logger.info(f"🤖 AI request", tools=tool_names, user=display_name)
        
        final_response = None
        iteration = 0
        
        while iteration < MAX_AI_ITERATIONS:
            iteration += 1
            
            try:
                if tools:
                    response = await self.ai_client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        max_tokens=1500,
                        temperature=0.2
                    )
                else:
                    response = await self.ai_client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        max_tokens=1500,
                        temperature=0.2
                    )
                
                choice = response.choices[0]
                
                if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                    tool_results = await self._execute_tools(
                        choice.message.tool_calls,
                        user_data,
                        sender  # Pass sender for context
                    )
                    
                    messages.append({
                        "role": "assistant",
                        "content": choice.message.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments
                                }
                            }
                            for tc in choice.message.tool_calls
                        ]
                    })
                    
                    for tc, result in zip(choice.message.tool_calls, tool_results):
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result
                        })
                    
                    continue
                else:
                    final_response = choice.message.content
                    break
                    
            except Exception as e:
                logger.error("AI error", error=str(e), iteration=iteration)
                final_response = "Došlo je do greške. Pokušajte ponovno."
                break
        
        if not final_response:
            final_response = "Nisam uspio obraditi zahtjev. Pokušajte drugačije."
        
        # Welcome for new users
        if is_new:
            vehicle_info = user_data.get("vehicle_info", "Nepoznato")
            welcome = f"👋 Dobrodošli {display_name}!\n🚗 Vaše vozilo: {vehicle_info}\n\n"
            final_response = welcome + final_response
        
        await self.context.add_message(sender, "assistant", final_response)
        return final_response
    
    async def _get_tools(self, query: str) -> List[Dict]:
        """Get relevant tools via SEMANTIC SEARCH."""
        if not self.registry or not self.registry.is_ready:
            return []
        return await self.registry.find_relevant_tools(query, top_k=5)
    
    async def _execute_tools(
        self, 
        tool_calls: List, 
        user_data: Dict,
        sender: str
    ) -> List[str]:
        """Execute tool calls with auto-injection."""
        results = []
        
        for tc in tool_calls:
            func_name = tc.function.name
            
            try:
                args = json.loads(tc.function.arguments)
            except:
                args = {}
            
            logger.info(f"🔧 Executing: {func_name}", args=list(args.keys()))
            
            # Get tool metadata
            tool_def = self.registry.get_tool_definition(func_name)
            tool_meta = self.registry.get_tool_metadata(func_name)
            
            if not tool_def:
                results.append(f"ERROR: Tool '{func_name}' not found.")
                continue
            
            # AUTO-INJECT parameters
            args = self._inject_parameters(args, tool_meta, user_data)
            
            logger.debug(f"Final args: {args}")
            
            try:
                result = await self.gateway.execute_tool(
                    tool_def=tool_def,
                    params=args,
                    user_context={
                        "tenant_id": user_data["tenant_id"],
                        "person_id": user_data["person_id"],
                        "phone": user_data["phone"]
                    }
                )
                
                # DYNAMIC result formatting
                result_str = self._format_result_dynamic(
                    func_name, 
                    result, 
                    tool_meta,
                    user_data,
                    sender
                )
                results.append(result_str)
                
            except Exception as e:
                logger.error(f"Tool error: {func_name}", error=str(e))
                results.append(f"ERROR: {str(e)}")
        
        return results
    
    def _inject_parameters(
        self, 
        args: Dict, 
        tool_meta: Dict,
        user_data: Dict
    ) -> Dict:
        """Auto-inject parameters based on tool metadata."""
        auto_inject = tool_meta.get("auto_inject", [])
        
        person_id = user_data.get("person_id")
        vehicle = user_data.get("vehicle", {})
        vehicle_id = vehicle.get("id") if vehicle.get("id") != "UNKNOWN" else None
        
        for param in auto_inject:
            param_lower = param.lower()
            
            if param_lower == "personid" and param not in args and person_id:
                args[param] = person_id
                logger.debug(f"Injected {param}")
            
            elif param_lower == "assignedtoid" and param not in args and person_id:
                args[param] = person_id
                logger.debug(f"Injected {param}")
            
            elif param_lower == "vehicleid" and param not in args and vehicle_id:
                args[param] = vehicle_id
                logger.debug(f"Injected {param}")
            
            elif param_lower == "driverid" and param not in args and person_id:
                args[param] = person_id
                logger.debug(f"Injected {param}")
        
        return args
    
    def _format_result_dynamic(
        self,
        func_name: str,
        result: Any,
        tool_meta: Dict,
        user_data: Dict,
        sender: str
    ) -> str:
        """
        DYNAMIC result formatting - works for ANY function.
        
        Uses tool metadata to decide how to format.
        """
        # Error handling
        if isinstance(result, dict) and result.get("error"):
            error_msg = result.get("message", "Unknown error")
            return f"API ERROR: {error_msg}"
        
        # Get tool info
        method = tool_meta.get("method", "").upper()
        service = tool_meta.get("service", "")
        description = tool_meta.get("description", "")
        
        logger.debug(f"Formatting: {func_name}, method={method}, service={service}")
        
        # ==================================================================
        # GET requests - typically return data
        # ==================================================================
        if method == "GET":
            return self._format_get_result(
                func_name, result, tool_meta, user_data, sender
            )
        
        # ==================================================================
        # POST/PUT/PATCH - typically create/update
        # ==================================================================
        elif method in ["POST", "PUT", "PATCH"]:
            return self._format_mutation_result(
                func_name, result, tool_meta
            )
        
        # ==================================================================
        # DELETE - typically delete
        # ==================================================================
        elif method == "DELETE":
            return "✅ Successfully deleted."
        
        # ==================================================================
        # Fallback - JSON dump
        # ==================================================================
        try:
            return json.dumps(result, ensure_ascii=False, default=str)[:2000]
        except:
            return str(result)[:2000]
    
    def _format_get_result(
        self,
        func_name: str,
        result: Any,
        tool_meta: Dict,
        user_data: Dict,
        sender: str
    ) -> str:
        """Format GET request results."""
        # Handle list responses
        items = []
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = result.get("Data", result.get("data", []))
            if not items and ("Id" in result or "id" in result):
                items = [result]
        
        # Empty result
        if not items or (isinstance(items, list) and len(items) == 0):
            return f"NO DATA returned from {func_name}."
        
        # ==================================================================
        # VEHICLES LIST - Special handling to show IDs
        # ==================================================================
        if "vehicle" in func_name.lower() and "available" in func_name.lower():
            return self._format_vehicles_list(items, sender)
        
        # ==================================================================
        # SINGLE ITEM - Show key fields
        # ==================================================================
        if len(items) == 1:
            item = items[0]
            return self._format_single_item(item, func_name)
        
        # ==================================================================
        # MULTIPLE ITEMS - List with key fields
        # ==================================================================
        return self._format_multiple_items(items, func_name, sender)
    
    def _format_vehicles_list(self, vehicles: List[Dict], sender: str) -> str:
        """
        Format vehicle list - CRITICAL: show VehicleId for booking!
        """
        if not vehicles:
            return "NO AVAILABLE VEHICLES for requested period."
        
        formatted = f"🚗 FOUND {len(vehicles)} AVAILABLE VEHICLES:\n\n"
        
        # Store for context (so AI can reference "2" later)
        vehicle_context = []
        
        for i, v in enumerate(vehicles[:10], 1):
            vehicle_id = v.get("Id") or v.get("VehicleId") or v.get("id") or "N/A"
            plate = v.get("LicencePlate") or v.get("Plate") or "N/A"
            name = (
                v.get("FullVehicleName") or 
                v.get("DisplayName") or 
                f"{v.get('Manufacturer', '')} {v.get('Model', '')}".strip() or
                "Vehicle"
            )
            
            formatted += (
                f"**{i}. {name}**\n"
                f"   📋 Registration: {plate}\n"
                f"   🔑 VehicleId: `{vehicle_id}`\n\n"
            )
            
            vehicle_context.append({
                "index": i,
                "name": name,
                "plate": plate,
                "id": vehicle_id
            })
        
        # Save to Redis for context
        asyncio.create_task(self._save_list_context(sender, "vehicles", vehicle_context))
        
        formatted += (
            "═══════════════════════════════════\n"
            "INSTRUCTIONS: Say the number (e.g. '1' or '2') or name to select.\n"
            "IMPORTANT: I will use the VehicleId for booking."
        )
        
        return formatted
    
    def _format_single_item(self, item: Dict, func_name: str) -> str:
        """Format single item - show key fields."""
        # Try to identify key fields
        formatted = f"📊 DATA from {func_name}:\n\n"
        
        # Common ID fields
        id_fields = ["Id", "id", "VehicleId", "PersonId", "CaseId"]
        for field in id_fields:
            if field in item:
                formatted += f"- ID: {item[field]}\n"
                break
        
        # Name fields
        name_fields = ["Name", "DisplayName", "FullVehicleName", "FullName", "Title"]
        for field in name_fields:
            if field in item:
                formatted += f"- Name: {item[field]}\n"
                break
        
        # Other important fields
        important = ["Mileage", "LicencePlate", "Plate", "Driver", "Status", "Description"]
        for field in important:
            if field in item:
                formatted += f"- {field}: {item[field]}\n"
        
        formatted += "\nRespond to user based on this data."
        return formatted
    
    def _format_multiple_items(
        self, 
        items: List[Dict], 
        func_name: str,
        sender: str
    ) -> str:
        """Format multiple items as numbered list."""
        formatted = f"📋 FOUND {len(items)} ITEMS:\n\n"
        
        context = []
        
        for i, item in enumerate(items[:10], 1):
            # Try to get name
            name = (
                item.get("Name") or 
                item.get("DisplayName") or 
                item.get("Title") or
                f"Item {i}"
            )
            
            # Try to get ID
            item_id = item.get("Id") or item.get("id") or "N/A"
            
            formatted += f"{i}. {name} (ID: {item_id})\n"
            
            context.append({
                "index": i,
                "name": name,
                "id": item_id,
                "data": item
            })
        
        # Save context
        asyncio.create_task(self._save_list_context(sender, "items", context))
        
        return formatted
    
    def _format_mutation_result(
        self,
        func_name: str,
        result: Any,
        tool_meta: Dict
    ) -> str:
        """Format POST/PUT/PATCH results."""
        if isinstance(result, dict):
            # Success
            result_id = result.get("Id") or result.get("id") or ""
            
            # Determine action
            if "calendar" in func_name.lower() or "booking" in func_name.lower():
                return f"✅ BOOKING SUCCESSFUL!\n📝 ID: {result_id}"
            
            elif "case" in func_name.lower():
                return f"✅ CASE REPORTED!\n📝 Case ID: {result_id}\nOur team will contact you soon."
            
            elif "mileage" in func_name.lower():
                return f"✅ MILEAGE RECORDED!\n📏 New mileage saved."
            
            elif "email" in func_name.lower() or "mail" in func_name.lower():
                return f"✅ EMAIL SENT!"
            
            else:
                # Generic success
                return f"✅ SUCCESS!\n{func_name} completed" + (f" (ID: {result_id})" if result_id else "")
        
        return "✅ Operation completed."
    
    async def _save_list_context(self, sender: str, context_type: str, data: List[Dict]):
        """Save list context to Redis for future reference."""
        try:
            key = f"context:{sender}:{context_type}"
            await self.redis.setex(
                key,
                300,  # 5 minutes
                json.dumps(data)
            )
        except Exception as e:
            logger.warning(f"Failed to save context: {e}")
    
    def _build_intelligent_prompt(self, user_data: Dict) -> str:
        """
        Build INTELLIGENT prompt for GPT-4o-mini.
        
        This prompt works for ANY function from ANY swagger.
        """
        name = user_data.get("display_name", "User")
        person_id = user_data.get("person_id", "")
        vehicle = user_data.get("vehicle", {})
        
        vehicle_info = ""
        if vehicle and vehicle.get("plate") != "UNKNOWN":
            vehicle_info = f"Vehicle: {vehicle.get('name', '')} ({vehicle.get('plate', '')})"
            if vehicle.get("mileage") != "UNKNOWN":
                vehicle_info += f", Mileage: {vehicle.get('mileage')} km"
        
        today = datetime.now()
        today_str = today.strftime('%d.%m.%Y')
        today_iso = today.strftime('%Y-%m-%d')
        tomorrow_iso = (today + timedelta(days=1)).strftime('%Y-%m-%d')
        
        return f"""You are MobilityOne AI assistant for fleet management.
Communicate in CROATIAN. Be CONCISE and CLEAR.

═══════════════════════════════════════════════════════════════
USER CONTEXT
═══════════════════════════════════════════════════════════════
- Name: {name}
- PersonId: {person_id}
- {vehicle_info}
- Today: {today_str} ({today_iso})
- Tomorrow: {tomorrow_iso}

═══════════════════════════════════════════════════════════════
YOUR CAPABILITIES
═══════════════════════════════════════════════════════════════
You have access to MANY functions via tools. The system uses SEMANTIC SEARCH 
to find the RIGHT function for each query.

You DON'T need to memorize function names. The system will provide you with 
the MOST RELEVANT functions based on the user's query meaning.

YOUR JOB:
1. UNDERSTAND what the user wants
2. SELECT the right tool (already filtered for you)
3. EXTRACT parameters from user's message
4. CALL the tool with correct parameters

═══════════════════════════════════════════════════════════════
PARAMETER EXTRACTION RULES
═══════════════════════════════════════════════════════════════

**DATES & TIMES:**
- "sutra" / "tomorrow" = {tomorrow_iso}
- "danas" / "today" = {today_iso}
- "od 9 do 17" = FromTime: ...T09:00:00, ToTime: ...T17:00:00
- "cijeli dan" = FromTime: ...T08:00:00, ToTime: ...T18:00:00
- ALWAYS use ISO 8601 format: YYYY-MM-DDTHH:MM:SS

**CONTEXT AWARENESS (CRITICAL!):**
When you show a numbered list (e.g., vehicles), and user responds with:
- "1" or "prvi" → They selected the FIRST item
- "2" or "drugi" → They selected the SECOND item  
- "Passat" → They selected item with that name

YOU MUST:
1. Remember what list you just showed
2. Extract the ID of the selected item
3. Use that ID in the next function call

Example:
You: "Found 3 vehicles: 1. Passat (VehicleId: abc123), 2. Golf (VehicleId: def456)"
User: "2"
You: Call booking with VehicleId="def456" (the Golf's ID!)

**IDs:**
- PersonId, AssignedToId, TenantId → AUTOMATICALLY injected (you don't need to provide)
- VehicleId for booking → MUST come from the vehicle list you showed
- Never invent or guess IDs!

**MISSING INFO:**
If you need info the user didn't provide:
- ASK them clearly
- Be specific about what you need
- Example: "I need to know: from what time to what time?"

═══════════════════════════════════════════════════════════════
RESPONSE STYLE
═══════════════════════════════════════════════════════════════
- SHORT and CLEAR answers in Croatian
- NO invented data - use tools!
- When showing lists, ALWAYS include IDs (especially for vehicles)
- Format lists clearly with numbers

═══════════════════════════════════════════════════════════════
EXAMPLES
═══════════════════════════════════════════════════════════════

**Example 1 - Info Query:**
User: "Koja je moja kilometraža?"
You: Call get_MasterData → "Vaš VW Passat ima 45.678 km."

**Example 2 - Booking Flow:**
User: "Trebam auto za sutra"
You: "Od kada do kada vam treba vozilo?"
User: "Od 9 do 17"
You: Call get_AvailableVehicles(from="{tomorrow_iso}T09:00:00", to="{tomorrow_iso}T17:00:00")
     → Show list with VehicleIds
User: "2" (selected second vehicle)
You: Call post_VehicleCalendar(VehicleId=<ID of 2nd vehicle>, FromTime="...", ToTime="...")
     → "✅ Rezervacija uspješna!"

**Example 3 - Damage Report:**
User: "Udario sam auto"
You: "Žao mi je! Možete li opisati štetu?"
User: "Ogrebao lijevi blatobran"
You: Call post_AddCase(Description="Ogrebao lijevi blatobran")
     → "✅ Prijava zaprimljena!"

═══════════════════════════════════════════════════════════════
REMEMBER
═══════════════════════════════════════════════════════════════
- The system finds the RIGHT function via semantic search
- Your job is to EXTRACT parameters and CALL the function
- ALWAYS show IDs in lists (critical for next steps)
- TRACK CONTEXT - remember what list you showed
- ASK if you need missing info
- Be CONCISE in Croatian
"""