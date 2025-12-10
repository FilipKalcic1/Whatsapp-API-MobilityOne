import asyncio
import json
import httpx
import structlog
import redis.asyncio as redis
import re  # <--- NOVI IMPORT ZA REGEX
from typing import List, Dict, Any, Optional
from openai import AsyncAzureOpenAI
from config import get_settings

logger = structlog.get_logger("tool_registry")
settings = get_settings()

class ToolRegistry:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        
        self.client = AsyncAzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION
        )   
        
        self.tools_map: Dict[str, dict] = {}      
        self.is_ready = False 
        
        self.custom_tools = {
            "get_my_vehicle_info": {
                "type": "function",
                "function": {
                    "name": "get_my_vehicle_info",
                    "description": "Dohvaća informacije o vozilu trenutnog korisnika.",
                    "parameters": {
                        "type": "object",
                        "properties": {}, 
                        "required": []
                    }
                }
            }
        }
        self.tools_map.update(self.custom_tools)

    async def start_auto_update(self, source_url: str, interval: int = 3600):
        if not source_url.startswith("http"): return
        logger.info("Auto-update watchdog started", source=source_url)
        
        try:
            await self.load_swagger(source_url)
        except Exception as e:
            logger.error("Initial swagger load failed", error=str(e))

        while True:
            await asyncio.sleep(interval)
            try:
                await self.load_swagger(source_url)
            except Exception as e:
                logger.warning("Auto-update check failed", error=str(e))

    async def load_swagger(self, source: str):
        logger.info("Loading tools definition...", source=source)
        spec = None
        try:
            if source.startswith("http"):
                async with httpx.AsyncClient() as client:
                    resp = await client.get(source, timeout=30.0)
                    resp.raise_for_status()
                    spec = resp.json()
            else:
                with open(source, 'r', encoding='utf-8') as f:
                    spec = json.load(f)
        except Exception as e:
            logger.error("Failed to load Swagger", source=source, error=str(e))
            return

        if spec:
            await self._process_spec(spec)
            self.tools_map.update(self.custom_tools)
            self.is_ready = True
            logger.info("Tools loaded successfully", total_tools=len(self.tools_map))

    async def _process_spec(self, spec: dict):
        new_map = {}
        paths = spec.get('paths', {})
        for path, methods in paths.items():
            for method, details in methods.items():
                if method.lower() not in ['get', 'post', 'put', 'delete', 'patch']: continue
                
                op_id = details.get('operationId')
                if not op_id:
                    # Fallback ako nema ID-a: napravi ga iz putanje
                    clean_path = path.replace("/", "_").strip("_")
                    op_id = f"{method.lower()}_{clean_path}"
                    details['operationId'] = op_id

                # [FIX] REGEX SANITIZACIJA IMENA
                # Azure dopušta samo: a-z, A-Z, 0-9, _, -
                # Sve ostalo (npr. {}, :, razmaci) pretvaramo u _
                op_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', op_id)
                
                # Ukloni višestruke donje crte (npr. get___vehicle)
                op_id = re.sub(r'_+', '_', op_id)

                desc = f"{details.get('summary', '')} {details.get('description', '')}"
                if not desc.strip(): desc = f"{method.upper()} {path}"
                
                openai_schema = self._to_openai_schema(op_id, desc, details)
                
                new_map[op_id] = {
                    "path": path,
                    "method": method.upper(),
                    "def": openai_schema,
                    "server": spec.get("servers", [{"url": ""}])[0].get("url", "")
                }
        self.tools_map = new_map

    async def find_relevant_tools(self, query: str, top_k: int = 40) -> List[Dict]:
        """Keyword Search (Bez embeddinga)."""
        if not self.is_ready:
             return [t["def"] if "def" in t else t for t in self.custom_tools.values()]

        selected_tools = []
        for tool in self.custom_tools.values():
            val = tool
            if "def" in tool: val = tool["def"]
            selected_tools.append(val)

        query_parts = [word for word in query.lower().split() if len(word) > 2]
        scored_tools = []

        for op_id, tool_data in self.tools_map.items():
            if op_id in self.custom_tools: continue

            tool_def = tool_data["def"]
            tool_name = tool_def["function"]["name"].lower()
            tool_desc = tool_def["function"]["description"].lower() if tool_def["function"]["description"] else ""
            
            score = 0
            for part in query_parts:
                if part in tool_name: score += 5
                elif part in tool_desc: score += 1

            if score == 0 and "get" in tool_name and len(scored_tools) < 10:
                score = 0.1

            if score > 0:
                scored_tools.append((score, tool_def))

        scored_tools.sort(key=lambda x: x[0], reverse=True)
        for score, tool_def in scored_tools[:top_k]:
            selected_tools.append(tool_def)
        
        if len(selected_tools) < 2:
             fallback = [v["def"] for k,v in list(self.tools_map.items())[:10] if "get" in k.lower()]
             selected_tools.extend(fallback)

        logger.info(f"Selected {len(selected_tools)} tools for query: '{query}'")
        return selected_tools

    def _sanitize_property(self, prop_schema: dict, prop_name: str, depth=0) -> dict:
        if depth > 5: return {"type": "string"}
        t = prop_schema.get("type", "string")
        d = prop_schema.get("description", "") or prop_name
        res = {"type": t, "description": d}
        
        if t == "array":
            items = prop_schema.get("items", {})
            if not items: res["items"] = {"type": "string"}
            else: res["items"] = self._sanitize_property(items, f"{prop_name}_item", depth+1)
        elif t == "object":
            props = prop_schema.get("properties", {})
            if props:
                new_props = {}
                for k, v in props.items(): new_props[k] = self._sanitize_property(v, k, depth+1)
                res["properties"] = new_props
        return res

    def _to_openai_schema(self, name, desc, details):
        params = {"type": "object", "properties": {}, "required": []}
        
        for p in details.get("parameters", []):
            p_name = p.get("name")
            # [FIX] Imena parametara također moraju biti čista
            clean_p_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', p_name)
            
            p_schema = p.get("schema", {})
            params["properties"][clean_p_name] = self._sanitize_property(p_schema, clean_p_name)
            if p.get("required"): params["required"].append(clean_p_name)

        if "requestBody" in details:
            content = details["requestBody"].get("content", {})
            schema = content.get("application/json", {}).get("schema", {}) or \
                     content.get("application/x-www-form-urlencoded", {}).get("schema", {})
            
            for p_name, p_schema in schema.get("properties", {}).items():
                clean_p_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', p_name)
                params["properties"][clean_p_name] = self._sanitize_property(p_schema, clean_p_name)
                if p_name in schema.get("required", []):
                    params["required"].append(clean_p_name)

        ignored = ["VehicleId", "vehicleId", "assetId", "DriverId", "personId", "TenantId"]
        params["required"] = [r for r in params["required"] if r not in ignored]

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc[:1000],
                "parameters": params
            }
        }