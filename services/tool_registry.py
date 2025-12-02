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

# [NOVO] Vrijeme trajanja cachea: 30 dana (u sekundama)
CACHE_TTL = 60 * 60 * 24 * 30 

class ToolRegistry:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        
        self.tools_map: Dict[str, dict] = {}      
        self.tool_embeddings: List[dict] = []  
        self.is_ready = False 
        self.current_hash = None 

    def _calculate_tool_hash(self, tool_def: dict) -> str:
        """Kreira jedinstveni otisak alata."""
        json_str = json.dumps(tool_def, sort_keys=True)
        return hashlib.sha256(json_str.encode("utf-8")).hexdigest()

    async def start_auto_update(self, source_url: str, interval: int = 300):
        if not source_url.startswith("http"): return

        logger.info("Auto-update watchdog started", source=source_url)
        while True:
            await asyncio.sleep(interval)
            try:
                async with httpx.AsyncClient() as client:
                    try:
                        response = await client.head(source_url)
                        if response.status_code >= 400:
                            raise httpx.RequestError("HEAD not supported")
                    except httpx.RequestError:
                        response = await client.get(source_url)
                    
                    remote_hash = response.headers.get("ETag") or response.headers.get("Last-Modified")
                    
                    # Osvježi ako se promijenilo ILI ako je prošlo puno vremena (da refreshamo TTL)
                    if (remote_hash and remote_hash != self.current_hash) or (interval > 3600):
                        logger.info("Checking/Reloading Swagger...", remote_hash=remote_hash)
                        await self.load_swagger(source_url)
                        self.current_hash = remote_hash
            except Exception as e:
                logger.warning("Auto-update check failed", error=str(e))

    async def load_swagger(self, source: str):
        logger.info("Loading tools definition...", source=source)
        spec = None

        try:
            if source.startswith("http"):
                async with httpx.AsyncClient() as client:
                    resp = await client.get(source, timeout=10.0)
                    resp.raise_for_status()
                    spec = resp.json()
                    self.current_hash = resp.headers.get("ETag")
            else:
                with open(source, 'r', encoding='utf-8') as f:
                    spec = json.load(f)
        except Exception as e:
            logger.error("Failed to load Swagger", source=source, error=str(e))
            if not self.is_ready: 
                logger.critical("Starting without tools due to swagger error!")
            return

        if spec:
            await self._process_spec(spec)
            self.is_ready = True
            logger.info("Tools loaded successfully", count=len(self.tool_embeddings))

    async def _process_spec(self, spec: dict):
        new_map = {}
        new_embeddings = []

        paths = spec.get('paths', {})
        for path, methods in paths.items():
            for method, details in methods.items():
                if method.lower() not in ['get', 'post', 'put', 'delete']: continue
                
                op_id = details.get('operationId')
                if not op_id:
                    clean_path = path.replace("{", "").replace("}", "").replace("/", "_").strip("_")
                    op_id = f"{method.lower()}_{clean_path}"
                    details['operationId'] = op_id

                desc = f"{details.get('summary', '')} {details.get('description', '')}"
                if not desc.strip():
                    desc = f"{method.upper()} {path}" 
                
                openai_schema = self._to_openai_schema(op_id, desc, details)
                
                # --- REDIS CACHE LOGIKA ---
                tool_hash = self._calculate_tool_hash(openai_schema)
                embedding = None
                
                hash_key = f"{REDIS_TOOL_HASH_PREFIX}{op_id}"
                embed_key = f"{REDIS_TOOL_EMBED_PREFIX}{op_id}"
                
                cached_hash = await self.redis.get(hash_key)
                
                if cached_hash == tool_hash:
                    # HIT! Imamo embedding.
                    cached_embed_str = await self.redis.get(embed_key)
                    if cached_embed_str:
                        embedding = json.loads(cached_embed_str)
                        
                        # [NOVO] Refreshamo TTL (produžujemo život za 30 dana)
                        async with self.redis.pipeline() as pipe:
                            pipe.expire(hash_key, CACHE_TTL)
                            pipe.expire(embed_key, CACHE_TTL)
                            await pipe.execute()
                
                # Ako nema (MISS), zovi OpenAI
                if not embedding:
                    if cached_hash:
                        logger.info("Tool changed, re-calculating", tool=op_id)
                        
                    embedding = await self._get_embedding(f"{op_id}: {desc}")
                    if embedding:
                        # Spremi s TTL-om
                        async with self.redis.pipeline() as pipe:
                            pipe.set(hash_key, tool_hash, ex=CACHE_TTL)
                            pipe.set(embed_key, json.dumps(embedding), ex=CACHE_TTL)
                            await pipe.execute()
                        
                        await asyncio.sleep(0.05) 

                if embedding:
                    new_map[op_id] = {
                        "path": path,
                        "method": method,
                        "openai_schema": openai_schema
                    }
                    new_embeddings.append({
                        "id": op_id,
                        "embedding": embedding,
                        "def": openai_schema, 
                        "hash": tool_hash
                    })

        self.tools_map = new_map
        self.tool_embeddings = new_embeddings

    async def find_relevant_tools(self, query: str, top_k: int = 3) -> List[Dict]:
        if not self.is_ready or not self.tool_embeddings: return []

        try:
            query_vec = await self._get_embedding(query)
            if query_vec is None: return []

            vectors = [t["embedding"] for t in self.tool_embeddings]
            if not vectors: return []
            
            # Cosine similarity
            scores = np.dot(vectors, query_vec)
            top_indices = np.argsort(scores)[-top_k:][::-1]
            
            results = []
            for idx in top_indices:
                if scores[idx] > 0.25: 
                    results.append(self.tool_embeddings[idx]["def"])
            return results
            
        except Exception as e:
            logger.error("Tool search error", error=str(e))
            return []

    async def _get_embedding(self, text: str) -> Optional[List[float]]:
        try:
            text = text.replace("\n", " ")
            resp = await self.client.embeddings.create(
                input=[text], 
                model="text-embedding-3-small"
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.error("Embedding generation failed", error=str(e))
            return None

    def _to_openai_schema(self, name, desc, details):
        params = {"type": "object", "properties": {}, "required": []}
        for p in details.get("parameters", []):
            p_name = p.get("name")
            p_type = p.get("schema", {}).get("type", "string")
            params["properties"][p_name] = {"type": p_type, "description": p.get("description", "")}
            if p.get("required"): params["required"].append(p_name)

        if "requestBody" in details:
            content = details["requestBody"].get("content", {})
            schema = content.get("application/json", {}).get("schema", {}) or \
                     content.get("application/x-www-form-urlencoded", {}).get("schema", {})
            
            for p_name, p_schema in schema.get("properties", {}).items():
                params["properties"][p_name] = {
                    "type": p_schema.get("type", "string"),
                    "description": p_schema.get("description", "")
                }
                if p_name in schema.get("required", []):
                    params["required"].append(p_name)

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc[:1000], 
                "parameters": params
            }
        }