import time
import orjson
import structlog
import redis.asyncio as redis
from typing import List, Dict, Optional, Any
from openai import AsyncAzureOpenAI
from config import get_settings

logger = structlog.get_logger("context_service")
settings = get_settings()

CONTEXT_TTL = 3600 * 24  # 24 sata
MAX_HISTORY_LEN = 10     # Koliko poruka pamtimo prije sažimanja

class ContextService:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        
        # Azure Client
        self.client = AsyncAzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION 
        )

    async def get_history(self, user_id: str) -> List[Dict]:
        """Vraća povijest razgovora za korisnika."""
        key = f"chat_history:{user_id}"
        raw_data = await self.redis.lrange(key, 0, -1)
        
        history = []
        for item in raw_data:
            try:
                msg = orjson.loads(item)
                history.append(msg)
            except:
                continue
        return history

    async def add_message(self, user_id: str, role: str, content: str, tool_calls=None, tool_call_id=None, name=None):
        """
        Dodaje poruku u Redis.
        SADRŽI FIX ZA 'dict object has no attribute model_dump'.
        """
        key = f"chat_history:{user_id}"
        
        msg = {
            "role": role,
            "timestamp": time.time()
        }
        
        if content:
            msg["content"] = content
            
        # [FIX] Robusna obrada tool_calls
        if tool_calls:
            safe_calls = []
            for t in tool_calls:
                # Ako je Pydantic objekt (ima model_dump), pretvori ga.
                # Ako je već dict (jer ga je ai.py pretvorio), ostavi ga.
                if hasattr(t, "model_dump"):
                    safe_calls.append(t.model_dump())
                else:
                    safe_calls.append(t)
            msg["tool_calls"] = safe_calls

        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        if name:
            msg["name"] = name

        # Spremi u Redis
        await self.redis.rpush(key, orjson.dumps(msg))
        await self.redis.expire(key, CONTEXT_TTL)
        
        # Provjera dužine i sažimanje
        list_len = await self.redis.llen(key)
        if list_len > MAX_HISTORY_LEN:
            logger.info("Context limit exceeded, summarizing...", user_id=user_id)
            # Pokrećemo sažimanje (fire and forget ili await, ovisno o želji)
            try:
                await self._summarize_conversation(user_id)
            except Exception as e:
                logger.error("Async summarize error", error=str(e))

    async def _summarize_conversation(self, user_id: str):
        """Sažima stari dio razgovora koristeći Azure OpenAI."""
        key = f"chat_history:{user_id}"
        
        raw_history = await self.get_history(user_id)
        if not raw_history: return

        # Odvoji zadnje 3 poruke
        recent_msgs = raw_history[-3:]
        older_msgs = raw_history[:-3]
        
        if not older_msgs: return

        text_block = "\n".join([f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in older_msgs])
        
        try:
            response = await self.client.chat.completions.create(
                model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                messages=[
                    {"role": "system", "content": "Summarize the key facts from this conversation concisely in 2 sentences."},
                    {"role": "user", "content": text_block}
                ],
                temperature=0.3
            )
            
            summary = response.choices[0].message.content
            
            summary_msg = {
                "role": "system", 
                "content": f"Previous conversation summary: {summary}",
                "timestamp": time.time()
            }
            
            # Atomski zamijeni povijest
            await self.redis.delete(key)
            await self.redis.rpush(key, orjson.dumps(summary_msg))
            
            for m in recent_msgs:
                await self.redis.rpush(key, orjson.dumps(m))
                
            await self.redis.expire(key, CONTEXT_TTL)
            
        except Exception as e:
            logger.error("Summarization failed", error=str(e))
            # Fallback: samo izbaci najstariju poruku
            await self.redis.lpop(key)