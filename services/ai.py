import orjson
import structlog
import uuid
import json
from typing import List, Dict, Any, Optional
from openai import AsyncAzureOpenAI
from config import get_settings

settings = get_settings()
logger = structlog.get_logger("ai")

client = AsyncAzureOpenAI(
    azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
    api_key=settings.AZURE_OPENAI_API_KEY,
    api_version=settings.AZURE_OPENAI_API_VERSION
)

# --- SYSTEM PROMPT (Template) ---
# Ovo se koristi samo ako engine.py ne pošalje svoj prompt
DEFAULT_SYSTEM_PROMPT = """
SYSTEM DATA SNAPSHOT:
You are the MobilityOne Assistant for {display_name}.

### 🔐 KEYRING (INTERNAL KNOWLEDGE):
The following data is ALREADY KNOWN. Do NOT ask the user for it.
{facts}

### 🔧 SYSTEM VARIABLES (FOR TOOL CALLS):
When a tool requires an ID, use these values immediately:
1. User.MobilePhone: "{phone}"
2. Vehicle.LicencePlate: "{plate}"
3. Vehicle.RegExpiry: "{reg_expiry}"
4. TOOL PARAMETER 'personId' or 'driverId' -> USE: "{person_id}"
5. TOOL PARAMETER 'vehicleId' or 'assetId' -> USE: "{vehicle_id}"

---------------------------------------------------

### GLAVNE DIREKTIVE (CORE BEHAVIOR):

1. **SMART PARAMETER EXTRACTION (KRITIČNO):**
   - Cilj: Popuniti parametre iz govora.
   - Primjer: "Prijavi štetu na braniku" -> Subject="Prijava štete", Message="Šteta na braniku".

2. **PROTOKOL IZVRŠAVANJA:**
   - READ: Ako je podatak u KEYRING-u, odgovori odmah.
   - WRITE: Za akcije traži potvrdu ("DA" ili "POTVRĐUJEM").

3. **STIL:**
   - Hrvatski jezik. Kratko. Profesionalno.
   - Bez "Dobar dan" svako malo.

4. **FORMATIRANJE:**
   - Boldaj ključno (*ZG-1234*). Emoji po potrebi.

5. **POTVRDE:**
   - Ako korisnik kaže "DA", izvrši akciju predloženu u prošloj poruci.

Sada analiziraj povijest i pomozi korisniku {display_name}.
"""

async def analyze_intent(
    history: List[Dict], 
    current_text: str, 
    tools: List[Dict] = None,
    system_instruction: str = None
) -> Dict[str, Any]:
    """
    Glavna funkcija za komunikaciju s OpenAI.
    Priprema povijest, šalje alate i vraća odluku.
    """
    
    # 1. Priprema poruka (FLATTENING)
    # Ovo pretvara kompleksne tool-call strukture iz prošlosti u običan tekst
    # kako bi izbjegli 400 Bad Request greške zbog "broken chain-a".
    messages = _construct_safe_messages(history, current_text, system_instruction)

    try:
        # 2. Priprema alata za OpenAI format
        openai_tools = []
        if tools:
            for t in tools:
                # Osiguraj da je format ispravan
                if t.get("type") == "function" and "function" in t:
                    openai_tools.append(t)
                else:
                    # Fallback ako je došao samo dict funkcije
                    openai_tools.append({"type": "function", "function": t})
        
        call_args = {
            "model": settings.AZURE_OPENAI_DEPLOYMENT_NAME,
            "messages": messages,
            "temperature": 0.0, # Deterministički odgovori
        }

        if openai_tools:
            call_args["tools"] = openai_tools
            call_args["tool_choice"] = "auto" 
            logger.info(f"🧠 Sending {len(openai_tools)} tools to OpenAI")

        # 3. HTTP Poziv prema OpenAI
        response = await client.chat.completions.create(**call_args)
        
        # Validacija odgovora
        if not response.choices:
            logger.error("❌ OpenAI returned empty choices list")
            return _text_response("Greška: Prazan odgovor od AI servisa.")

        msg = response.choices[0].message
        
        # --- DEBUG LOGGING ---
        # Koristimo 'getattr' da ne pukne ako je content None
        content_safe = getattr(msg, 'content', '') or ''
        tool_calls_count = len(msg.tool_calls) if msg.tool_calls else 0
        logger.info(f"🤖 RAW OPENAI RESPONSE: content='{content_safe[:50]}...' tool_calls={tool_calls_count}")
        # ---------------------

        # 4. Obrada odluke (Tool vs Text)
        if msg.tool_calls:
            # AI želi zvati alat
            tool_call = msg.tool_calls[0]
            try:
                args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                logger.warning("⚠️ AI generated invalid JSON arguments", raw=tool_call.function.arguments)
                args = {} # Fallback

            return {
                "tool": tool_call.function.name,
                "parameters": args,
                "tool_call_id": tool_call.id,
                "raw_tool_calls": msg.tool_calls, # Ovo vraćamo da engine može spremiti u povijest
                "response_text": None
            }

        # AI samo odgovara tekstom
        return _text_response(msg.content)

    except Exception as e:
        logger.error("🔥 AI Critical Failure", error=str(e))
        return _text_response("Isprike, sustav je trenutno nedostupan (AI Error).")

# --- HELPER METHODS ---

def _construct_safe_messages(history: list, text: str, instruction: str | None) -> list:
    """
    Gradi sigurnu povijest poruka.
    Pretvara prošle 'tool_calls' u tekstualne opise kako bi se izbjegle greške u API pozivima.
    """
    base_prompt = instruction if instruction else DEFAULT_SYSTEM_PROMPT
    msgs = [{"role": "system", "content": base_prompt}]
    
    for msg in history:
        role = msg.get("role")
        content = msg.get("content")
        
        # 1. User poruke prolaze netaknute
        if role == "user":
            msgs.append({"role": "user", "content": content or ""})
        
        # 2. Assistant poruke
        elif role == "assistant":
            # Ako je poruka imala tool_calls, pretvori ih u tekst
            if "tool_calls" in msg and msg["tool_calls"]:
                tool_name = msg["tool_calls"][0]["function"]["name"]
                # Simuliramo da je AI rekao "Zovem alat X..."
                msgs.append({"role": "assistant", "content": f"🛠️ [System Action: Calling tool '{tool_name}']"})
            else:
                msgs.append({"role": "assistant", "content": content or ""})
        
        # 3. Tool poruke (Rezultati)
        elif role == "tool":
            # Pretvori rezultat alata u System poruku s kontekstom
            # Ovo je trik: OpenAI bolje razumije rezultate ako su dio konteksta, a ne "broken chain" tool poruka
            msgs.append({"role": "system", "content": f"🛠️ [Tool Result]: {content}"})
            
    # Dodaj trenutni upit korisnika
    if text:
        msgs.append({"role": "user", "content": text})
        
    return msgs

def _text_response(text: str) -> dict:
    return {
        "tool": None, 
        "parameters": {}, 
        "response_text": text or "Nisam razumio.",
        "tool_call_id": None
    }