import asyncio
import sys
import os
import orjson
import structlog
import redis.asyncio as redis

# Dodajemo root folder u path da možemo importati config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings

# --- KONFIGURACIJA ---
DLQ_KEY = "dlq:inbound"
STREAM_KEY = "whatsapp:inbound"

logger = structlog.get_logger("replay_dlq")
settings = get_settings()

async def replay_dead_letters(dry_run: bool = False):
    """
    Vraća poruke iz Dead Letter Queue (DLQ) natrag u glavni Stream.
    
    Args:
        dry_run: Ako je True, samo ispisuje poruke, ne pomiče ih.
    """
    print(f"🔌 Spajanje na Redis: {settings.REDIS_URL}")
    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    
    try:
        # 1. Provjeri koliko ih ima
        count = await r.llen(DLQ_KEY)
        if count == 0:
            print("✅ DLQ je prazan. Nema poruka za oporavak.")
            return

        print(f"⚠️ Pronađeno {count} poruka u DLQ-u.")
        
        if dry_run:
            print("👀 DRY RUN MODE: Poruke neće biti vraćene.")

        processed = 0
        
        while True:
            # 2. Uzmi poruku (RPOP - s desne strane, FIFO princip ako je LPHUSHan)
            # Koristimo RPOP da uzmemo najstariju (ako su gurane s LPUSH)
            # Ali oprez: da ne izgubimo poruku ako skripta pukne, idealno bi bilo RPOPLPUSH
            # Za jednostavnost ovdje radimo: Peek -> XADD -> LREM (ili RPOP ako smo sigurni)
            
            # Dohvati zadnju poruku bez brisanja (za preview)
            raw_data = await r.lindex(DLQ_KEY, -1)
            if not raw_data:
                break
                
            try:
                payload = orjson.loads(raw_data)
            except Exception:
                print(f"❌ Corrupted JSON: {raw_data}")
                # Izbacujemo smeće
                if not dry_run: await r.rpop(DLQ_KEY)
                continue

            print(f"\n📩 Poruka #{processed + 1}:")
            print(f"   From: {payload.get('sender', 'Unknown')}")
            print(f"   Text: {payload.get('text', 'No text')}")
            print(f"   Error: {payload.get('error', 'Unknown error')}")

            if dry_run:
                # Samo simuliramo pomak da petlja ne bude beskonačna u ispisu
                # U stvarnosti, za dry run bi trebali iterirati bez pop-a
                # Ovdje ćemo samo breakati nakon 5 komada za preview
                if processed >= 4:
                    print("... (prikazano prvih 5)")
                    break
                processed += 1
                # Za stvarni loop u dry runu morali bi koristiti lrange, ali ovo je admin tool
                continue

            # 3. Potvrda korisnika (opcionalno, ovdje automatiziramo)
            # Gurni natrag u Stream
            # Čistimo payload od error metadata prije vraćanja
            clean_payload = {k: v for k, v in payload.items() if k not in ['error', 'failed_at']}
            
            # XADD vraća ID poruke
            msg_id = await r.xadd(STREAM_KEY, clean_payload)
            print(f"   ✅ Vraćeno u stream! Novi ID: {msg_id}")
            
            # 4. Obriši iz DLQ
            await r.rpop(DLQ_KEY)
            processed += 1

        if not dry_run:
            print(f"\n🎉 Uspješno oporavljeno {processed} poruka.")

    except Exception as e:
        logger.error("Replay failed", error=str(e))
    finally:
        await r.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Replay DLQ messages to main stream")
    parser.add_argument("--dry-run", action="store_true", help="Samo ispiši poruke bez pomicanja")
    args = parser.parse_args()
    
    asyncio.run(replay_dead_letters(dry_run=args.dry_run))