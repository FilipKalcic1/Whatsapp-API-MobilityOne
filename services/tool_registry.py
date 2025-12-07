import asyncio
import json
import hashlib
import httpx
import structlog
import numpy as np
import redis.asyncio as redis
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI
from config import get_settings

logger = structlog.get_logger("tool_registry")
settings = get_settings()

# Prefiksi ključeva u Redisu
REDIS_TOOL_HASH_PREFIX = "tool:hash:"
REDIS_TOOL_EMBED_PREFIX = "tool:embed:"

# Vrijeme trajanja cachea: 30 dana
CACHE_TTL = 60 * 60 * 24 * 30 

class ToolRegistry:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        
        self.tools_map: Dict[str, dict] = {}      
        self.tool_embeddings: List[dict] = []   
        self.is_ready = False 
        self.current_hash = None 
        
    async def load_swagger(self, url_or_path: str):
        """Učitava Swagger/OpenAPI definiciju i pretvara je u OpenAI alate."""
        try:
            content = ""
            if url_or_path.startswith("http"):
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url_or_path, timeout=10)
                    resp.raise_for_status()
                    content = resp.text
            else:
                with open(url_or_path, "r", encoding="utf-8") as f:
                    content = f.read()

            spec = json.loads(content)
            count = 0
            
            for path, methods in spec.get("paths", {}).items():
                for method, details in methods.items():
                    if method not in ["get", "post", "put", "delete"]:
                        continue
                    
                    op_id = details.get("operationId")
                    if not op_id:
                        continue
                        
                    # Filtriramo samo sigurne metode ili one koje želimo
                    # Ovdje učitavamo SVE, a AI odlučuje što zvati
                    
                    tool_desc = details.get("summary") or details.get("description") or op_id
                    openai_tool = self._to_openai_schema(op_id, tool_desc, details)
                    
                    # Spremanje u mapu za brzo izvršavanje
                    self.tools_map[op_id] = {
                        "def": openai_tool,
                        "path": path,
                        "method": method.upper(),
                        "server": spec.get("servers", [{"url": ""}])[0].get("url", "")
                    }
                    count += 1
            
            logger.info("Swagger loaded", source=url_or_path, tools_count=count)
            # Nakon učitavanja, osvježi embeddinge
            await self._refresh_embeddings()
            
        except Exception as e:
            logger.error("Failed to load swagger", source=url_or_path, error=str(e))

    def _to_openai_schema(self, name, desc, details):
        """
        Pretvara Swagger operaciju u OpenAI Function Schema.
        Sadrži 'Auto-Fix' logiku za neispravne Swaggere (poput array missing items).
        """
        params = {"type": "object", "properties": {}, "required": []}
        
        # 1. Obrada Query/Path parametara
        for p in details.get("parameters", []):
            p_name = p.get("name")
            p_schema = p.get("schema", {})
            p_type = p_schema.get("type", "string")
            p_desc = p.get("description", "") or p_name

            prop_def = {
                "type": p_type, 
                "description": p_desc
            }

            # [FIX] OpenAI Crash Prevention: Array mora imati 'items'
            if p_type == "array":
                if "items" in p_schema:
                    # Kopiramo postojeći items definition
                    prop_def["items"] = p_schema["items"]
                else:
                    # Fallback: Ako fali, pretpostavljamo array of strings
                    prop_def["items"] = {"type": "string"}

            params["properties"][p_name] = prop_def
            
            if p.get("required"): 
                params["required"].append(p_name)

        # 2. Obrada Request Body-a (za POST/PUT)
        if "requestBody" in details:
            content = details["requestBody"].get("content", {})
            # Tražimo JSON ili Form data
            schema = content.get("application/json", {}).get("schema", {}) or \
                     content.get("application/x-www-form-urlencoded", {}).get("schema", {})
            
            for p_name, p_schema in schema.get("properties", {}).items():
                p_type = p_schema.get("type", "string")
                
                prop_def = {
                    "type": p_type,
                    "description": p_schema.get("description", "") or p_name
                }

                # [FIX] OpenAI Crash Prevention za Body
                if p_type == "array":
                    if "items" in p_schema:
                        prop_def["items"] = p_schema["items"]
                    else:
                        prop_def["items"] = {"type": "string"}

                params["properties"][p_name] = prop_def
                
                if p_name in schema.get("required", []):
                    params["required"].append(p_name)

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc[:1024], # OpenAI limit
                "parameters": params
            }
        }

    async def _refresh_embeddings(self):
        """Generira embeddinge samo za nove/promijenjene alate."""
        new_embeddings = []
        
        for name, tool_data in self.tools_map.items():
            tool_def = tool_data["def"]
            # Kreiramo hash alata da vidimo je li se promijenio
            tool_json = json.dumps(tool_def, sort_keys=True)
            tool_hash = hashlib.md5(tool_json.encode()).hexdigest()
            
            cached_hash = await self.redis.get(f"{REDIS_TOOL_HASH_PREFIX}{name}")
            
            if cached_hash == tool_hash:
                # Nije se promijenio, učitaj embedding iz cachea
                cached_embed = await self.redis.get(f"{REDIS_TOOL_EMBED_PREFIX}{name}")
                if cached_embed:
                    new_embeddings.append({
                        "tool": tool_def,
                        "embedding": json.loads(cached_embed)
                    })
                    continue
            
            # Promijenio se ili je nov -> Generiraj Embedding
            desc = tool_def["function"]["description"]
            # Embedding radimo na temelju Imena i Opisa
            embedding = await self._generate_embedding(f"{name}: {desc}")
            
            if embedding:
                # Spremi u Redis
                await self.redis.set(f"{REDIS_TOOL_HASH_PREFIX}{name}", tool_hash, ex=CACHE_TTL)
                await self.redis.set(f"{REDIS_TOOL_EMBED_PREFIX}{name}", json.dumps(embedding), ex=CACHE_TTL)
                
                new_embeddings.append({
                    "tool": tool_def,
                    "embedding": embedding
                })
        
        self.tool_embeddings = new_embeddings
        self.is_ready = True
        logger.info("Tool Registry refreshed", total_tools=len(self.tools_map))

    async def _generate_embedding(self, text: str) -> List[float]:
        try:
            resp = await self.client.embeddings.create(
                input=text,
                model="text-embedding-3-small"
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.error("Embedding generation failed", error=str(e))
            return None

    async def find_relevant_tools(self, query: str, top_k: int = 5) -> List[dict]:
        """Semantička pretraga alata."""
        if not self.is_ready or not self.tool_embeddings:
            # Fallback ako nismo spremni
            return [t["def"] for t in list(self.tools_map.values())[:top_k]]
            
        query_embedding = await self._generate_embedding(query)
        if not query_embedding:
            return []

        # Računanje kosinusne sličnosti
        scores = []
        for item in self.tool_embeddings:
            similarity = np.dot(query_embedding, item["embedding"])
            scores.append((similarity, item["tool"]))
            
        # Sortiranje i vraćanje najboljih
        scores.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scores[:top_k]]

    async def start_auto_update(self, url: str):
        """Periodički osvježava Swagger (npr. svakih sat vremena)."""
        while True:
            await asyncio.sleep(3600)
            await self.load_swagger(url)