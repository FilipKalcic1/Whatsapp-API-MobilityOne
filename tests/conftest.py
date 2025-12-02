import pytest
import pytest_asyncio
import fnmatch
from unittest.mock import MagicMock, AsyncMock
from httpx import AsyncClient, ASGITransport
from fastapi_limiter import FastAPILimiter

from main import app
from services.queue import QueueService
from services.cache import CacheService
from services.context import ContextService
from routers.webhook import get_queue

class FakeRedis:
    def __init__(self):
        self.data = {}    
        self.lists = {}    
        self.sets = {}
        self.streams = {} 

    async def get(self, key): return self.data.get(key)
    async def set(self, key, value, *args, **kwargs): self.data[key] = value; return True
    async def setex(self, key, time, value): self.data[key] = value; return True
    async def delete(self, key): 
        if key in self.data: del self.data[key]
        return 1
    
    async def incr(self, key):
        val = int(self.data.get(key, 0)) + 1
        self.data[key] = str(val)
        return val

    async def keys(self, pattern="*"):
        return [k for k in self.data.keys() if fnmatch.fnmatch(k, pattern)]

    # List operacije
    async def rpush(self, key, value):
        if key not in self.lists: self.lists[key] = []
        self.lists[key].append(value)
        return len(self.lists[key])
    
    async def lpop(self, key):
        if key in self.lists and self.lists[key]: return self.lists[key].pop(0)
        return None

    async def llen(self, key): return len(self.lists.get(key, []))
    
    async def lrange(self, key, start, end): 
        lst = self.lists.get(key, [])
        if end == -1: return lst[start:]
        return lst[start:end+1]

    async def ltrim(self, key, start, end):
        if key not in self.lists: return True
        lst = self.lists[key]
        if end == -1: self.lists[key] = lst[start:]
        else: self.lists[key] = lst[start:end+1]
        return True

    async def blpop(self, key, timeout=0):
        if key in self.lists and self.lists[key]: return [key, self.lists[key].pop(0)]
        return None
    
    # Stream operacije
    async def xadd(self, stream, fields):
        if stream not in self.streams: self.streams[stream] = []
        msg_id = f"{len(self.streams[stream])}-0"
        self.streams[stream].append((msg_id, fields))
        return msg_id

    async def xreadgroup(self, groupname, consumername, streams, count=1, block=None):
        result = []
        for stream_key, start_id in streams.items():
            if stream_key in self.streams and self.streams[stream_key]:
                result.append([stream_key, self.streams[stream_key]])
                self.streams[stream_key] = [] 
        return result

    async def xack(self, stream, group, id): return 1
    async def xdel(self, stream, id): return 1
    async def xgroup_create(self, stream, group, id="$", mkstream=False): return True

    # [NOVO] Dodano za podršku testiranja worker recovery logike
    async def xautoclaim(self, name, groupname, consumername, min_idle_time=0, start_id="0-0", count=1):
        # Vraća prazan rezultat jer u testovima obično mockamo povratnu vrijednost
        # ili koristimo patch, ali metoda mora postojati da bi patch.object radio.
        return "0-0", [], []

    async def expire(self, key, time): return True
    async def zadd(self, key, mapping): return 1
    async def zrangebyscore(self, key, min, max, start=None, num=None): return [] 
    async def zrem(self, key, member): return 1
    
    # Metode koje trebaju za Rate Limiter
    async def eval(self, *args, **kwargs): return 0
    async def evalsha(self, *args, **kwargs): return 0
    async def script_load(self, script): return "dummy_sha"

    # Pipeline podrška (vraća self, simulira ponašanje bez transakcija)
    def pipeline(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc_val, exc_tb): pass
    async def execute(self): return []
    async def close(self): pass
    async def aclose(self): pass

@pytest.fixture
def redis_client():
    return FakeRedis()

@pytest_asyncio.fixture
async def async_client(redis_client):
    queue_service = QueueService(redis_client)
    
    # Overrideamo queue dependency jer ga API koristi
    app.dependency_overrides[get_queue] = lambda: queue_service
    
    await FastAPILimiter.init(redis_client)
    
    app.state.redis = redis_client
    app.state.queue = queue_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    
    app.dependency_overrides = {}