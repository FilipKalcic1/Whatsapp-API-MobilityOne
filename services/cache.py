import orjson
import redis.asyncio as redis
from typing import Callable, Any, Optional
import structlog

logger = structlog.get_logger("cache")

class CacheService:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def get(self, key: str) -> Optional[str]:
        """
        Dohvaća raw vrijednost iz Redisa.
        Koristi se u UserService za dohvat JSON stringa.
        """
        try:
            return await self.redis.get(key)
        except Exception as e:
            logger.warning("Cache GET failed", key=key, error=str(e))
            return None

    async def set(self, key: str, value: Any, ttl: int = 60):
        """
        Sprema vrijednost u Redis s zadanim TTL-om (u sekundama).
        """
        try:
            # Ako je value dict/list, serijaliziraj ga (opcionalno, ali sigurnije)
            if not isinstance(value, (str, bytes, int, float)):
                value = orjson.dumps(value).decode('utf-8')
                
            await self.redis.set(key, value, ex=ttl)
        except Exception as e:
            logger.warning("Cache SET failed", key=key, error=str(e))

    async def get_or_compute(self, key: str, func: Callable, *args, ttl: int = 60) -> Any:
        """
        Dohvaća podatak iz cachea. Ako ne postoji, izvršava funkciju 'func' i sprema rezultat.
        Otporan na pad Redisa (nastavlja raditi bez cachea).
        """
        # 1. Pokušaj čitanja
        try:
            cached = await self.redis.get(key)
            if cached:
                return orjson.loads(cached)
        except Exception as e:
            logger.warning("Cache read failed (skipping)", key=key, error=str(e))

        # 2. Izračun (ako nema u cacheu ili je Redis pao)
        result = await func(*args)

        # 3. Pokušaj spremanja
        try:
            if result: 
                await self.set(key, result, ttl)
        except Exception as e:
            logger.warning("Cache write failed", key=key, error=str(e))

        return result