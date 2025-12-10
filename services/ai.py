import orjson
import structlog
from typing import List, Dict, Any
from openai import AsyncAzureOpenAI
from config import get_settings

settings = get_settings()
logger = structlog.get_logger("ai")

# 1. Inicijalizacija Azure Klijenta
client = AsyncAzureOpenAI(
    azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
    api_key=settings.AZURE_OPENAI_API_KEY,
    api_version=settings.AZURE_OPENAI_API_VERSION
)

# 2. Helper funkcija za siguran dump podataka (FIX ZA TVOJ ERROR)
def safe_dump(obj: Any) -> Any:
    """
    Robustno pretvara objekt u dictionary.
    Rješava grešku: 'dict object has no attribute model_dump'
    """
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return obj

# 3. System Prompt (Skraćeno radi preglednosti, ostavi svoj puni prompt ovdje)
SYSTEM_PROMPT = """
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
   - Tvoj cilj je popuniti parametre alata iz prirodnog govora korisnika.
   - **PRIMJER 1 (Prijava štete):**
     - Korisnik: "Prijavi da sam ogrebao branik na parkingu."
     - Tvoja logika: Alat `/AddCase` traži 'Subject' i 'Message'.
     - Tvoja akcija: Postavi 'Subject'="Prijava štete", 'Message'="Ogrebao branik na parkingu". PITAJ ZA POTVRDU.
   - **PRIMJER 2 (Kilometraža):**
     - Korisnik: "Trenutno stanje je 150000 km."
     - Tvoja logika: Alat `/AddMileage` traži 'Value'.
     - Tvoja akcija: Postavi 'Value'=150000. Izvrši (ili pitaj potvrdu).
   - **ZABRANA:** Nemoj pitati "Koji je razlog?" ako je korisnik već rekao razlog u prvoj rečenici.

2. **PROTOKOL IZVRŠAVANJA (READ vs WRITE):**
   - **READ (GET):** Ako korisnik pita "Kad mi ističe registracija?", pogledaj KEYRING (gore). Ako piše tamo, odgovori ODMAH. Ako piše 'UNKNOWN', tek onda zovi alat.
   - **WRITE (POST/PUT):** Za sve akcije koje nešto mijenjaju (Prijave, Zahtjevi), MORAŠ sažeti što ćeš napraviti i tražiti "DA" ili "POTVRĐUJEM".

3. **STIL KOMUNIKACIJE (FLEET MANAGER PERSONA):**
   - Jezik: Hrvatski.
   - Ton: Profesionalan, kratak, operativan.
   - **ZABRANJENO:** "Dobar dan" usred chata. Počni odgovor direktno informacijom.
   - **DOBRO:** "🚗 Vozilo: *Audi A4* (*ZG-1234-AB*)"

4. **VIZUALNA PREZENTACIJA (WHATSAPP FORMATIRANJE):**
   - **BOLDING:** Ključne podatke stavi unutar zvjezdica (npr. *ZG-1234-AB*).
   - **EMOJIS:** Koristi 1 emotikon po konceptu (🚗, 💶, 📅, ✅, ⚠️).
   - **LISTE:** Koristi natuknice (-).

5. **FINANCIJSKI INTEGRITET:**
   - Iznose prikazuj točno (npr. "*450.23 EUR*"). Ne konvertiraj valute.

6. **RJEŠAVANJE PROBLEMA (FALLBACK):**
   - Ako alat vrati grešku, reci: "⚠️ Trenutno ne mogu dohvatiti taj podatak."
   - Nemoj izmišljati datume ili iznose.

7. **RUKOVANJE POTVRDAMA (MEMORY CHECK - SUPER IMPORTANT):**
   - Ako korisnik kaže samo **"DA"**, **"MOŽE"**, **"POTVRĐUJEM"** ili **"OK"**:
   - **POGLEDAJ SVOJU ZADNJU PORUKU U POVIJESTI.**
   - Da li si upravo pitao za potvrdu akcije (npr. "Da li potvrđujete?")?
   - **AKO JESI:** ODMAH IZVRŠI TU AKCIJU s parametrima koje si sam predložio.
   - **ZABRANJENO:** Reći "Ne razumijem na što se odnosi DA". Moraš povezati kontekst.

Sada analiziraj povijest i pomozi korisniku {display_name}.
"""

async def analyze_intent(
    history: List[Dict], 
    current_text: str, 
    tools: List[Dict] = None,
    retry_count: int = 0,
    system_instruction: str = None 
) -> Dict[str, Any]:
    
    if retry_count > 1:
        logger.error("Max retries reached")
        return _text_response("Tehnička greška u formatu podataka.")

    messages = _construct_messages(history, current_text, system_instruction)

    try:
        call_args = {
            # Ovdje koristimo CHAT deployment name
            "model": settings.AZURE_OPENAI_DEPLOYMENT_NAME,
            "messages": messages,
            "temperature": 0, 
        }

        if tools:
            call_args["tools"] = tools
            call_args["tool_choice"] = "auto" 

        response = await client.chat.completions.create(**call_args)
        msg = response.choices[0].message
        
        if msg.tool_calls:
            return await _handle_tool_decision(
                msg.tool_calls[0], 
                msg.tool_calls, 
                history, 
                current_text, 
                tools, 
                retry_count, 
                system_instruction
            )

        return _text_response(msg.content)

    except Exception as e:
        logger.error("AI inference failed", error=str(e))
        return _text_response("Isprike, sustav je trenutno nedostupan (AI Error).")

# --- Helper Methods ---

def _construct_messages(history: list, text: str, instruction: str | None) -> list:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    if instruction:
        msgs.append({"role": "system", "content": instruction})
    
    i = 0
    while i < len(history):
        msg = history[i]
        role = msg.get("role")
        
        if role == "assistant" and msg.get("tool_calls"):
            is_paired = False
            if i + 1 < len(history) and history[i+1].get("role") == "tool":
                is_paired = True
            
            if is_paired:
                # [FIX] Koristimo safe_dump da se ne sruši
                raw_tools = msg["tool_calls"]
                safe_tools = [safe_dump(t) for t in raw_tools] if isinstance(raw_tools, list) else raw_tools
                
                msgs.append({"role": "assistant", "content": None, "tool_calls": safe_tools})
            else:
                pass 
        elif role == "tool":
            if msg.get("tool_call_id"):
                 msgs.append(msg)
        else:
            content = msg.get("content")
            if content:
                msgs.append({"role": role, "content": content})
        i += 1

    if text:
        msgs.append({"role": "user", "content": text})
    return msgs

async def _handle_tool_decision(primary_tool, all_tools, history, text, tools, retry, sys_instr) -> dict:
    function_name = primary_tool.function.name
    arguments_str = primary_tool.function.arguments
    
    try:
        parameters = orjson.loads(arguments_str)
        logger.info("AI selected tool", tool=function_name)
        
        # [FIX] Primjena sigurnog dumpa na listu alata
        safe_tool_calls = [safe_dump(t) for t in all_tools]

        return {
            "tool": function_name,
            "parameters": parameters,
            "tool_call_id": primary_tool.id,
            "raw_tool_calls": safe_tool_calls, # Sada je ovo sigurno
            "response_text": None
        }
    except orjson.JSONDecodeError:
        logger.warning("AI generated invalid JSON", raw=arguments_str)
        return await analyze_intent(history, current_text=text, tools=tools, retry_count=retry + 1, system_instruction=sys_instr)

def _text_response(text: str) -> dict:
    return {"tool": None, "parameters": {}, "response_text": text or "Nisam razumio."}