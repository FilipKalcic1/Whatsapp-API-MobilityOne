import asyncio
import json
import httpx
import structlog
import redis.asyncio as redis
import re
import math
import os
import time
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional
from openai import AsyncAzureOpenAI
from config import get_settings
from services.openapi_bridge import OpenAPIGateway

# --- KONFIGURACIJA ---
logger = structlog.get_logger("tool_registry")
settings = get_settings()

# Ime datoteke za cache (Sadrži i alate i embeddinge)
CACHE_FILE = "tool_registry_full_state.json"
# Redis ključ za Leader Election
LOCK_KEY = "tool_registry_leader_lock"
# Trajanje locka (15 minuta - sigurnosna margina za velike datoteke)
LOCK_TIMEOUT = 900 

class ToolRegistry:
    def __init__(self, redis_client: redis.Redis):
        """
        PRODUCTION-READY REGISTRY (v10.0)
        - Leader Election (Redis)
        - Atomic File Saving (Crash-safe)
        - Incremental Loading (Cost-efficient)
        - Business Logic Injection (Prompt Overrides)
        """
        self.redis = redis_client
        self.gateway = OpenAPIGateway(base_url=settings.MOBILITY_API_URL)
        
        self.client = AsyncAzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION
        )   
        
        # Glavna memorija (drži sve definicije i URL-ove)
        self.tools_map: Dict[str, dict] = {}      
        # Vektorska memorija
        self.embeddings_map: Dict[str, List[float]] = {} 
        self.is_ready = False 
        
        # --- POSLOVNA LOGIKA (PROMPT OVERRIDES) ---
        self.prompt_overrides = {
            # KORAK 1: PRETRAGA
            "get__AvailableVehicles": (
                "🚨 **KORAK 1**: PRETRAGA SLOBODNIH VOZILA. "
                "SVRHA: Pronaći dostupna vozila na temelju vremena. "
                "PRAVILA ZA AI: "
                "1. Provjeri imaš li datume (od/do). Ako nemaš, PITAJ KORISNIKA. "
                "2. Pretvori datume u ISO format. "
                "3. 'driverId' će sustav poslati automatski (ne brini o tome). "
                "4. REZULTAT: Prikaži korisniku marku i model pronađenih vozila i PITAJ GA KOJE ŽELI. "
                "5. NEMOJ ODMAH REZERVIRATI. Čekaj potvrdu korisnika."
            ),

            # KORAK 2: REZERVACIJA
            "post__VehicleCalendar": (
                "✅ **KORAK 2**: KREIRANJE REZERVACIJE (BOOKING). "
                "SVRHA: Rezervirati točno određeno vozilo. "
                "PREDUVJETI: "
                "1. Moraš imati 'VehicleId' (iz Koraka 1). "
                "2. Moraš imati detalje puta: Odredište, Svrha, Broj putnika. "
                "   -> AKO KORISNIK TO NIJE REKAO: Pitaj ga sada: 'Kamo putujete, koja je svrha i koliko vas ide?' "
                
                "PAYLOAD PRAVILA: "
                "- VehicleId: (ID odabranog vozila) "
                "- FromTime/ToTime: (ISO datumi polaska/povratka) "
                "- Description: Spoji u string: 'Odredište: [X], Svrha: [Y], Putnika: [Z]' "
                "- AssigneeType: 1 "
                "- EntryType: 0 "
                "- AssignedToId: (Sustav šalje automatski iz konteksta)"
            ),

            # SKRIVANJE NEPOTREBNOG (Focus Mode)
            "post__Booking": "🚫 ZABRANJENO. NE KORISTI.",
            "get__VehicleAssignments": "🚫 NE KORISTI.",
            "post__Batch": "🚫 NE KORISTI.",
        }


    async def start_auto_update(self, source_url: str, interval: int = 3600):
        """
        Pokreće proces sinkronizacije u pozadini.
        """
        if not source_url.startswith("http"):
            logger.error("Invalid Swagger URL provided", url=source_url)
            return
        
        logger.info("🚀 Registry Auto-Update Service Started.", source=source_url)
        
        # Prvo inicijalno učitavanje
        await self.load_swagger(source_url)

        # Beskonačna petlja
        while True:
            await asyncio.sleep(interval)
            try:
                logger.info("🔄 Running periodic Swagger refresh...")
                await self.load_swagger(source_url)
            except Exception as e:
                logger.warning("Auto-update iteration failed", error=str(e))

    async def load_swagger(self, source: str):
        """
        LEADER ELECTION LOGIKA:
        Pokušaj postati Leader. Ako uspiješ, radi posao. Ako ne, čekaj Leadera.
        """
        # setnx=True znači "Set if Not Exists" - samo jedan worker može uspjeti
        is_leader = await self.redis.set(LOCK_KEY, "locked", ex=LOCK_TIMEOUT, nx=True)

        if is_leader:
            logger.info("👑 I am the LEADER worker. Starting heavy update process...", source=source)
            try:
                await self._run_leader_process(source)
            except Exception as e:
                logger.error("👑 Leader process crashed!", error=str(e))
                # Lock će isteći sam (TTL), ili ga možemo brisati
            finally:
                # Uvijek pusti lock kad si gotov
                await self.redis.delete(LOCK_KEY)
                logger.info("👑 Leader finished. Lock released.")
        else:
            logger.info("👀 I am a FOLLOWER worker. Waiting for Leader to finish...")
            await self._run_follower_process()

    # --- LEADER LOGIC (Izvršava samo jedan worker) ---

    async def _run_leader_process(self, source_url: str):
        """
        Leader radi sve: Download -> Parse -> Embed -> Save.
        """
        # 1. Učitaj postojeće stanje (Incremental Loading)
        state = await self._load_full_state_safe()
        self.embeddings_map = state.get("embeddings", {})
        
        # 2. Siguran download (Threaded, veliki timeout)
        spec = await self._fetch_swagger_safe(source_url)
        if not spec:
            logger.error("❌ Aborting update due to download failure.")
            return

        # 3. Parsiranje specifikacije (Threaded)
        # Popunjava self.tools_map s definicijama i URL-ovima
        await self._process_spec(spec)
        logger.info(f"✅ Parsed {len(self.tools_map)} tools from Swagger.")
        
        # 4. Generiranje embeddinga (Inkrementalno - samo za nove)
        await self._generate_embeddings_incremental()
        
        # 5. ATOMIC SAVE (Spremi sve na disk za Followere)
        await self._save_full_state_atomic()
        
        self.is_ready = True
        logger.info("✅ Leader Update Complete. Registry is fully operational.")

    async def _fetch_swagger_safe(self, source: str) -> Optional[dict]:
        """Skida JSON. Parsira u threadu da ne blokira Event Loop (Health Check)."""
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                logger.info("⬇️ Downloading Swagger file...", url=source)
                resp = await client.get(source)
                resp.raise_for_status()
                text = resp.text

            # Parsiranje JSON-a u threadu
            return await asyncio.to_thread(json.loads, text)
        except Exception as e:
            logger.error("❌ Failed to fetch Swagger", error=str(e))
            return None

    async def _generate_embeddings_incremental(self):
        """
        Prolazi kroz alate. Ako nema embedding, zove OpenAI.
        """
        # Nađi alate koji nemaju vektor
        missing_op_ids = [op for op in self.tools_map if op not in self.embeddings_map]
        
        if not missing_op_ids:
            logger.info("✨ No new tools to embed. Using cached vectors.")
            return

        logger.info(f"🏗️ Generating embeddings for {len(missing_op_ids)} NEW tools...")

        # Batching (5 po 5) da ne zagušimo rate limits
        BATCH_SIZE = 5
        total = len(missing_op_ids)
        
        for i in range(0, total, BATCH_SIZE):
            batch = missing_op_ids[i : i + BATCH_SIZE]
            changes_made = False
            
            for op_id in batch:
                try:
                    text = self.tools_map[op_id].get("text_for_embedding", "")
                    if not text: continue

                    # Poziv prema OpenAI (Retry logika je u metodi)
                    vector = await self._get_embedding_with_retry(text)
                    if vector:
                        self.embeddings_map[op_id] = vector
                        changes_made = True
                except Exception as e:
                    logger.error(f"Failed to embed {op_id}", error=str(e))
            
            # Spremi checkpoint nakon svakog batcha
            if changes_made:
                await self._save_full_state_atomic()
            
            # Spavaj da worker ostane Healthy (prepusti CPU)
            await asyncio.sleep(0.2)
            logger.debug(f"💾 Progress: {min(i+BATCH_SIZE, total)}/{total}")

    # --- FOLLOWER LOGIC (Čeka Leadera) ---

    async def _run_follower_process(self):
        """
        Follower ne skida Swagger. On samo čeka 'tool_registry_full_state.json'.
        """
        # Prvo probaj učitati ako već postoji (brzi start)
        if await self._try_load_from_disk():
            return

        # Ako ne postoji, čekaj Leadera da završi
        # Max čekanje: 10 minuta (120 * 5s)
        for _ in range(120): 
            # Provjeri je li lock pušten (Leader gotov)
            is_locked = await self.redis.get(LOCK_KEY)
            
            if not is_locked:
                logger.info("📚 Lock released. Loading full state from disk...")
                if await self._try_load_from_disk():
                    return
            
            # Još nije gotovo
            await asyncio.sleep(5)
        
        logger.warning("⚠️ Follower timed out waiting for Leader. Registry might be empty.")
        self.is_ready = True

    async def _try_load_from_disk(self) -> bool:
        """Učitava cijeli state s diska."""
        state = await self._load_full_state_safe()
        if state and "tools" in state:
            self.tools_map = state["tools"]
            self.embeddings_map = state.get("embeddings", {})
            self.is_ready = True
            logger.info(f"✅ Follower ready. Loaded {len(self.tools_map)} tools.")
            return True
        return False

    # --- ATOMIC FILE OPERATIONS (Crash-Safe) ---

    async def _load_full_state_safe(self) -> dict:
        """Učitava JSON u threadu. Ako je korumpiran, briše ga."""
        if not os.path.exists(CACHE_FILE): return {}
        try:
            return await asyncio.to_thread(self._read_json, CACHE_FILE)
        except Exception:
            logger.error("❌ Cache file corrupted. Deleting...")
            try: os.remove(CACHE_FILE)
            except: pass
            return {}

    async def _save_full_state_atomic(self):
        """Sprema I alate I vektore."""
        state = {
            "tools": self.tools_map,
            "embeddings": self.embeddings_map
        }
        await asyncio.to_thread(self._write_json_atomic, CACHE_FILE, state)

    def _read_json(self, path):
        with open(path, "r", encoding="utf-8") as f: return json.load(f)

    def _write_json_atomic(self, path, data):
        """
        Piše u .tmp, flusha na disk, pa preimenuje.
        Ovo 100% sprječava 'Extra data' greške kod naglog gašenja.
        """
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno()) # Osiguraj fizički zapis na disk
            
            os.replace(tmp_path, path) # Atomsko preimenovanje
        except Exception as e:
            logger.error("Atomic save failed", error=str(e))
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except: pass

    # --- SWAGGER PARSING & ROUTING ---

    async def _process_spec(self, spec: dict):
        """
        Parsira Swagger i mapira URL-ove.
        """
        paths = spec.get('paths', {})
        
        # Detekcija prefixa (/vehiclemgt, /automation...)
        prefix = ""
        if "servers" in spec and spec["servers"]:
            u = spec["servers"][0].get("url", "")
            if u.startswith("/"): prefix = u
            elif "http" in u: prefix = urlparse(u).path
        elif "basePath" in spec: prefix = spec["basePath"]
        prefix = prefix.rstrip("/")
        
        for path, methods in paths.items():
            for method, details in methods.items():
                if method.lower() not in ['get', 'post', 'put', 'delete', 'patch']: continue
                
                # Generiraj Operation ID (unikatno ime funkcije)
                op_id = details.get('operationId') or f"{method.lower()}_{path.replace('/', '_')}"
                op_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', op_id)
                
                # Primjeni Prompt Overrides (Poslovna Logika)
                desc = f"{details.get('summary','')} {details.get('description','')}".strip()

                if op_id in self.prompt_overrides:
                    desc = self.prompt_overrides[op_id]

                if "SearchInfo" in path or "SearchInfo" in op_id:
                    desc = "METADATA ONLY. Returns search filters, NOT actual data. DO NOT USE for retrieving values. " + desc


                else:
                    # Fuzzy match ako ID nije identičan
                    for k, v in self.prompt_overrides.items():
                        if k in op_id:
                            desc = v; break
                
                # Spremi mapiranje (URL, Metoda, Definicija)
                self.tools_map[op_id] = {
                    "path": f"{prefix}{path}", # <--- OVDJE SE ČUVA URL (Routing info)
                    "method": method.upper(),
                    "def": self._to_openai_schema(op_id, desc, details),
                    "text_for_embedding": f"{op_id}: {desc}"
                }

    def _to_openai_schema(self, name, desc, details):
        """Konvertira Swagger parametre u OpenAI format."""
        params = {"type": "object", "properties": {}, "required": []}
        
        # Obrada parametara (Path/Query)
        for p in details.get("parameters", []):
            clean = re.sub(r'[^a-zA-Z0-9_\-]', '_', p.get("name",""))
            params["properties"][clean] = {"type": "string", "description": p.get("description","")}
            if p.get("required"): params["required"].append(clean)
        
        # Obrada Body-a (JSON request)
        if "requestBody" in details:
            content = details["requestBody"].get("content", {})
            schema = content.get("application/json", {}).get("schema", {})
            if schema:
                for p_name, p_def in schema.get("properties", {}).items():
                    clean = re.sub(r'[^a-zA-Z0-9_\-]', '_', p_name)
                    # Pretvorimo u string za opis jer OpenAI voli jednostavne tipove
                    params["properties"][clean] = {"type": "string", "description": str(p_def.get("description", p_name))}
                    if p_name in schema.get("required", []): params["required"].append(clean)

        # Filtriranje tehničkih parametara koje korisnik ne zna
        ignored = ["VehicleId", "vehicleId", "assetId", "DriverId", "personId", "TenantId"]
        params["required"] = [r for r in params["required"] if r not in ignored]
        
        return {"type": "function", "function": {"name": name, "description": desc[:1000], "parameters": params}}

    # --- EXECUTION & SEARCH ---
    # Dodaj ovo u services/tool_registry.py ako fali!
    async def _get_embedding(self, text):
        model = getattr(settings, "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")
        for _ in range(3):
            try:
                # Pazi na limit tokena
                res = await self.client.embeddings.create(input=[text[:8000]], model=model)
                return res.data[0].embedding
            except Exception as e:
                await asyncio.sleep(0.5)
        return []


    async def find_relevant_tools(self, query: str, top_k=5):
        # 1. STARTUP CHECK
        if not self.is_ready:
            logger.warning("⚠️ Registry not ready. Waiting...")
            for _ in range(25): 
                if self.is_ready: break
                await asyncio.sleep(0.2)
            if not self.is_ready: return []
        
        forced = []
        query_lower = query.lower()
        
        # DEFINICIJA ZABRANJENIH ALATA (Blacklist)
        # Dodali smo Count, Excel, ProjectTo, Batch da ne zbunjuju AI
        BLACKLIST = [
            "post__Booking", "post__VehicleAssignments", "post__Batch", 
            "get__WhatCanIDo"
        ]

        # --- 2. LOGIKA PRISILNOG ODABIRA (RULE-BASED) ---
        
        is_booking_intent = any(k in query_lower for k in ["rezerv", "book", "auto", "vozil", "slobodn", "najam"])

        if is_booking_intent:
            logger.info("⚡ Detected Booking intent. Forcing clean workflow.")
            
            for key, data in self.tools_map.items():
                k_low = key.lower()
                
                # 1. UVIJEK DODAJ MASTERDATA
                if "masterdata" in k_low and "get" in k_low:
                    forced.append(data["def"])

                # 2. DODAJ AvailableVehicles (ALI SAMO GLAVNI!)
                if "available" in k_low and "get" in k_low:
                    # 🔥 FILTRIRAJ SMEĆE: Count, Excel, ProjectTo, SearchInfo
                    if any(bad in k_low for bad in ["count", "excel", "projectto", "searchinfo"]):
                        continue
                    forced.append(data["def"])
                
                # 3. DODAJ VehicleCalendar (FINALNI KORAK)
                if "calendar" in k_low and "post" in k_low:
                    # Filriraj smeće i ovdje
                    if any(bad in k_low for bad in ["excel", "searchinfo", "multipatch", "documents"]):
                        continue
                    forced.append(data["def"])

        # --- 3. VEKTORSKA PRETRAGA (S FILTRIRANJEM) ---
        try:
            q_vec = await self._get_embedding(query)
            scored = []
            
            for op, vec in self.embeddings_map.items():
                if op not in self.tools_map: continue
                if any(f["function"]["name"] == op for f in forced): continue

                # GLOBALNI FILTER SMEĆA ZA VEKTORE
                if op in BLACKLIST: continue
                # Blokiraj sve booking/calendar varijacije koje nisu prošle kroz forced logiku
                if is_booking_intent and ("booking" in op.lower() or "calendar" in op.lower()):
                    continue
                # Blokiraj Excel i Count varijacije generalno
                if any(bad in op.lower() for bad in ["excel", "count", "projectto"]):
                    continue

                score = sum(a*b for a,b in zip(q_vec, vec))
                if score > 0.72: 
                    scored.append((score, self.tools_map[op]["def"]))
            
            scored.sort(key=lambda x:x[0], reverse=True)
            
            final_tools = forced + [x[1] for x in scored]
            limit = max(len(forced), top_k)
            final_result = final_tools[:limit]
            
            # Logiraj imena da vidimo jesmo li očistili listu
            tool_names = [t['function']['name'] for t in final_result]
            logger.info(f"🔎 Final Tools Selection: {tool_names}")
            
            return final_result

        except Exception as e: 
            logger.error("Vector search failed", error=str(e))
            return forced  


    async def execute_tool(self, name: str, params: Dict, context: Dict = None):
        """
        Izvršava alat. Ovdje se događa ROUTING na temelju spremljene putanje.
        """
        data = self.tools_map.get(name)
        if not data: return {"error": "ToolNotFound"}
        
        # LOGIRANJE ROUTINGA (Dokaz da radi)
        logger.info(f"🔀 ROUTING: Tool='{name}' -> URL='{data['path']}' Method='{data['method']}'")
        
        # Delegiranje Gatewayu
        return await self.gateway.execute_tool({
            "name": name, 
            "path": data["path"], 
            "method": data["method"],
            "parameters": data["def"]["function"]["parameters"]
        }, params, context)

    async def _get_embedding_with_retry(self, text):
        """Omotač za OpenAI s retry logikom."""
        model = getattr(settings, "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")
        for _ in range(3):
            try:
                res = await self.client.embeddings.create(input=[text[:8000]], model=model)
                return res.data[0].embedding
            except: await asyncio.sleep(1)
        return []
    
    async def close(self):
        if self.gateway: await self.gateway.close()