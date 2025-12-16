"""
Cache Service - Production Ready (v2.0)

Simple Redis-based caching for:
1. User context
2. API responses
3. Token storage
"""

import orjson
import structlog
import redis.asyncio as redis
from typing import Callable, Any, Optional

logger = structlog.get_logger("cache")


class CacheService:
    """
    Simple cache wrapper around Redis.
    
    Provides:
    - get/set with TTL
    - get_or_compute pattern
    - Error resilience (cache miss on failure)
    """
    
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
    
    async def get(self, key: str) -> Optional[str]:
        """
        Get value from cache.
        
        Returns None if key doesn't exist or on error.
        """
        try:
            return await self.redis.get(key)
        except Exception as e:
            logger.warning("Cache GET failed", key=key, error=str(e))
            return None
    
    async def set(self, key: str, value: Any, ttl: int = 300):
        """
        Set value in cache with TTL (seconds).
        
        Automatically serializes dicts/lists to JSON.
        """
        try:
            # Serialize if needed
            if not isinstance(value, (str, bytes)):
                value = orjson.dumps(value).decode("utf-8")
            
            await self.redis.setex(key, ttl, value)
            
        except Exception as e:
            logger.warning("Cache SET failed", key=key, error=str(e))
    
    async def delete(self, key: str):
        """Delete key from cache."""
        try:
            await self.redis.delete(key)
        except Exception as e:
            logger.warning("Cache DELETE failed", key=key, error=str(e))
    
    async def get_or_compute(
        self,
        key: str,
        compute_func: Callable,
        *args,
        ttl: int = 300
    ) -> Any:
        """
        Get from cache or compute if missing.
        
        Pattern:
        1. Try cache
        2. If miss, call compute_func(*args)
        3. Cache result
        4. Return value
        
        Resilient to Redis failures.
        """
        # Try cache
        try:
            cached = await self.redis.get(key)
            if cached:
                return orjson.loads(cached)
        except Exception as e:
            logger.debug("Cache read failed", key=key, error=str(e))
        
        # Compute
        result = await compute_func(*args)
        
        # Cache result
        if result is not None:
            try:
                await self.set(key, result, ttl)
            except Exception as e:
                logger.debug("Cache write failed", key=key, error=str(e))
        
        return result