# services/ai.py
import orjson
import structlog
from typing import List, Dict, Any
from openai import AsyncOpenAI
from config import get_settings

settings = get_settings()
logger = structlog.get_logger("ai")
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


SYSTEM_PROMPT = """
Ti si MobilityOne AI asistent, stručnjak za vozni park.
Tvoj stil je profesionalan, topao i izravan. Ne zvučiš kao robot.

### IZVORI PODATAKA:
1. **KEYRING (Facts):** Podaci koje već znaš o korisniku (uvijek provjeri prvo).
2. **ALATI (Tools):** Funkcije za dohvat svježih podataka.

### PROTOKOL PONAŠANJA:

1. **BEZ ROBOTSKIH POZDRAVA:**
   - Ako smo usred razgovora, nemoj počinjati rečenicu s "Dobar dan" ili "Kako mogu pomoći". Nastavi razgovor prirodno.
   - Primjer LOŠE: "Podatak je 200 EUR. Kako vam mogu pomoći?"
   - Primjer DOBRO: "Rata iznosi 200 EUR. Treba li vam još neki detalj?"

2. **KAD NE ZNAŠ ODGOVOR (FAIL GRACEFULLY):**
   - Ako podatak nije u Keyringu i alati ga ne vraćaju (ili vraćaju null), **NEMOJ SE RESETIRATI** na "Kako mogu pomoći".
   - **PRIZNAJ SITUACIJU:** Reci korisniku da podatak nedostaje u sustavu.
   - Primjer: "Nažalost, provjerio sam vaše ugovorne podatke, ali polje za troškovno mjesto je prazno. Taj podatak trenutno nije dodijeljen."
   - **ZABRANJENO:** Reći samo "Nemam podatak" i onda šutjeti ili pitati generičko pitanje.

3. **FINANCIJSKA TOČNOST (STRICT):**
   - Brojeve prenosi točno onako kako pišu (npr. "200.0").
   - Ako znaš da je valuta EUR (iz konteksta), reci "200.0 EUR". Ne preračunavaj u kune ili druge valute.

4. **WRITE AKCIJE (SIGURNOST):**
   - Za svaku akciju koja mijenja podatke (npr. prijava štete), UVIJEK objasni što ćeš napraviti i traži potvrdu ("Da li želite da to provedem?").

Tvoj cilj je riješiti problem korisnika, a ne samo procesirati naredbe.
"""

async def analyze_intent(
    history: List[Dict], 
    current_text: str, 
    tools: List[Dict] = None,
    retry_count: int = 0,
    system_instruction: str = None # [NOVO] Za injekciju identiteta (User Email/ID)
) -> Dict[str, Any]:
    """
    Glavna logika odlučivanja. Pretvara razgovor u akciju.
    Sadrži logiku za rekonstrukciju povijesti i automatski retry u slučaju neispravnog JSON-a.
    """
    
    # 1. Sigurnosni osigurač (Circuit Breaker) za beskonačne petlje
    if retry_count > 1:
        logger.error("Max retries reached for JSON correction")
        return _text_response("Tehnička greška u formatu podataka.")

    # 2. Izgradnja poruka za LLM (uključujući identitet korisnika)
    messages = _construct_messages(history, current_text, system_instruction)

    try:
        # 3. Priprema argumenta za poziv
        call_args = {
            "model": settings.OPENAI_MODEL,
            "messages": messages,
            "temperature": 0, 
        }

        if tools:
            call_args["tools"] = tools
            call_args["tool_choice"] = "auto" 

        # 4. Poziv OpenAI modelu
        response = await client.chat.completions.create(**call_args)
        msg = response.choices[0].message
        
        # 5. Obrada odluke (Alat ili Tekst)
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
        return _text_response("Isprike, sustav je trenutno nedostupan zbog tehničke greške.")


# --- Helper Methods (Clean Code & Readability) ---

# services/ai.py

def _construct_messages(history: list, text: str, instruction: str | None) -> list:
    # 1. System Prompt
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    if instruction:
        msgs.append({"role": "system", "content": instruction})
    
    # 2. Sanitizacija povijesti (Uklanjanje slomljenih tool poziva)
    clean_history = []
    i = 0
    while i < len(history):
        msg = history[i]
        role = msg.get("role")
        
        if role == "assistant" and msg.get("tool_calls"):
            # Provjera para: Ima li Assistant svog Tool-a?
            is_paired = False
            if i + 1 < len(history) and history[i+1].get("role") == "tool":
                is_paired = True
            
            if is_paired:
                clean_history.append({"role": "assistant", "content": None, "tool_calls": msg["tool_calls"]})
            else:
                pass # BRISANJE ZOMBIJA

        elif role == "tool":
            if msg.get("tool_call_id"): # Samo validni tool odgovori
                 clean_history.append(msg)
        else:
            clean_history.append({"role": role, "content": msg.get("content")})
        i += 1

    if text:
        msgs.append({"role": "user", "content": text})
        
    return msgs

async def _handle_tool_decision(primary_tool, all_tools, history, text, tools, retry, sys_instr) -> dict:
    """
    Parsira argumente alata i radi rekurzivni retry ako je JSON neispravan.
    """
    function_name = primary_tool.function.name
    arguments_str = primary_tool.function.arguments
    
    try:
        # Koristimo orjson za brže parsiranje
        parameters = orjson.loads(arguments_str)
        logger.info("AI selected tool", tool=function_name)
        
        return {
            "tool": function_name,
            "parameters": parameters,
            "tool_call_id": primary_tool.id,
            "raw_tool_calls": [t.model_dump() for t in all_tools], 
            "response_text": None
        }
    except orjson.JSONDecodeError:
        logger.warning("AI generated invalid JSON parameters, retrying...", raw=arguments_str, attempt=retry)
        
        # Rekurzivni poziv analyze_intent s povećanim brojačem retryja
        return await analyze_intent(history, current_text=text, tools=tools, retry_count=retry + 1, system_instruction=sys_instr)

def _text_response(text: str) -> dict:
    """Vraća standardizirani tekstualni odgovor."""
    return {
        "tool": None,
        "parameters": {},
        "response_text": text or "Nisam razumio."
    }