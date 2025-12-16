"""
Context Service - Production Ready (v2.0)

Manages conversation history in Redis:
1. Per-user message storage
2. Automatic expiry (24 hours)
3. History length management
4. Summarization for long conversations
"""

import time
import orjson
import structlog
import redis.asyncio as redis
from typing import List, Dict, Optional, Any

from openai import AsyncAzureOpenAI
from config import get_settings

logger = structlog.get_logger("context")
settings = get_settings()

# Configuration
CONTEXT_TTL = 3600 * 24  # 24 hours
MAX_HISTORY_LENGTH = 20  # Max messages before summarization


class ContextService:
    """
    Manages conversation context for each user.
    
    Storage format in Redis:
    - Key: "chat_history:{phone}"
    - Value: List of JSON-encoded messages
    """
    
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        
        # OpenAI client for summarization
        self.client = AsyncAzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION
        )
    
    def _get_key(self, user_id: str) -> str:
        """Generate Redis key for user."""
        return f"chat_history:{user_id}"
    
    async def get_history(self, user_id: str) -> List[Dict]:
        """
        Get conversation history for user.
        
        Returns list of message dicts with role, content, etc.
        """
        key = self._get_key(user_id)
        
        try:
            raw_messages = await self.redis.lrange(key, 0, -1)
            
            history = []
            for raw in raw_messages:
                try:
                    msg = orjson.loads(raw)
                    history.append(msg)
                except:
                    continue
            
            return history
            
        except Exception as e:
            logger.warning("Failed to get history", user=user_id[-4:], error=str(e))
            return []
    
    async def add_message(
        self,
        user_id: str,
        role: str,
        content: Optional[str],
        tool_calls: List[Dict] = None,
        tool_call_id: str = None,
        name: str = None
    ):
        """
        Add a message to conversation history.
        
        Args:
            user_id: User identifier (phone number)
            role: "user", "assistant", "tool", or "system"
            content: Message content
            tool_calls: Tool call objects (for assistant messages)
            tool_call_id: ID of tool call this is a result for
            name: Tool name (for tool results)
        """
        key = self._get_key(user_id)
        
        # Build message object
        message = {
            "role": role,
            "timestamp": time.time()
        }
        
        if content:
            message["content"] = content
        
        if tool_calls:
            # Ensure tool_calls are serializable
            safe_calls = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    safe_calls.append(tc)
                elif hasattr(tc, "model_dump"):
                    safe_calls.append(tc.model_dump())
                elif hasattr(tc, "dict"):
                    safe_calls.append(tc.dict())
            message["tool_calls"] = safe_calls
        
        if tool_call_id:
            message["tool_call_id"] = tool_call_id
        
        if name:
            message["name"] = name
        
        try:
            # Add to list
            await self.redis.rpush(key, orjson.dumps(message))
            
            # Set expiry
            await self.redis.expire(key, CONTEXT_TTL)
            
            # Check if summarization needed
            length = await self.redis.llen(key)
            if length > MAX_HISTORY_LENGTH:
                await self._summarize_if_needed(user_id)
                
        except Exception as e:
            logger.warning("Failed to add message", user=user_id[-4:], error=str(e))
    
    async def clear_history(self, user_id: str):
        """Clear conversation history for user."""
        key = self._get_key(user_id)
        
        try:
            await self.redis.delete(key)
        except Exception as e:
            logger.warning("Failed to clear history", user=user_id[-4:], error=str(e))
    
    async def _summarize_if_needed(self, user_id: str):
        """
        Summarize old messages to keep context manageable.
        Keeps recent messages, summarizes older ones.
        """
        key = self._get_key(user_id)
        
        try:
            history = await self.get_history(user_id)
            
            if len(history) <= MAX_HISTORY_LENGTH:
                return
            
            # Keep last 5 messages
            recent = history[-5:]
            older = history[:-5]
            
            if not older:
                return
            
            # Build text for summarization
            text_parts = []
            for msg in older:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if content:
                    text_parts.append(f"{role}: {content[:200]}")
            
            text_to_summarize = "\n".join(text_parts)
            
            # Call OpenAI for summary
            try:
                response = await self.client.chat.completions.create(
                    model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                    messages=[
                        {
                            "role": "system",
                            "content": "Summarize this conversation in 2-3 sentences, focusing on key facts and decisions."
                        },
                        {
                            "role": "user",
                            "content": text_to_summarize
                        }
                    ],
                    temperature=0.3,
                    max_tokens=200
                )
                
                summary = response.choices[0].message.content
                
            except Exception as e:
                logger.warning("Summarization API failed", error=str(e))
                summary = f"[Prethodni razgovor od {len(older)} poruka]"
            
            # Create summary message
            summary_msg = {
                "role": "system",
                "content": f"Sažetak prethodnog razgovora: {summary}",
                "timestamp": time.time()
            }
            
            # Replace history atomically
            await self.redis.delete(key)
            await self.redis.rpush(key, orjson.dumps(summary_msg))
            
            for msg in recent:
                await self.redis.rpush(key, orjson.dumps(msg))
            
            await self.redis.expire(key, CONTEXT_TTL)
            
            logger.info("History summarized", 
                       user=user_id[-4:], 
                       old_count=len(older),
                       new_count=len(recent) + 1)
            
        except Exception as e:
            logger.error("Summarization failed", user=user_id[-4:], error=str(e))