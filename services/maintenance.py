import structlog
import time
from datetime import datetime, timedelta
from sqlalchemy import delete
from database import AsyncSessionLocal
from models import UserMapping
from config import get_settings

logger = structlog.get_logger("maintenance")
settings = get_settings()

# GDPR Pravilo: Briši korisnike neaktivne duže od 1 godine
RETENTION_DAYS = 365 

class MaintenanceService:
    def __init__(self):
        self.last_run = 0
        # Pokreni se jednom svaka 24 sata (86400 sekundi)
        self.interval = 86400 

    async def run_daily_cleanup(self):
        """
        Provjerava je li prošao dan. Ako je, pokreće čišćenje.
        Ova metoda se poziva često iz workera, ali izvršava teški posao samo jednom dnevno.
        """
        now = time.time()
        
        # Ako nije prošao puni dan od zadnjeg čišćenja, ne radi ništa
        if now - self.last_run < self.interval:
            return

        logger.info("🧹 Starting Daily Maintenance & GDPR Cleanup...")
        
        try:
            await self._cleanup_inactive_users()
            
            # Zabilježi da smo uspjeli
            self.last_run = now
            logger.info("✅ Daily Maintenance completed successfully.")
            
        except Exception as e:
            logger.error("Maintenance failed", error=str(e))

    async def _cleanup_inactive_users(self):
        """
        Fizički briše zapise korisnika iz baze koji su stariji od 1 godine.
        Ovo osigurava da ne čuvaš osobne podatke vječno.
        """
        cutoff_date = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
        
        async with AsyncSessionLocal() as session:
            try:
                # Brišemo redove iz tablice UserMapping
                stmt = delete(UserMapping).where(UserMapping.created_at < cutoff_date)
                result = await session.execute(stmt)
                
                await session.commit()
                
                if result.rowcount > 0:
                    logger.info("Deleted inactive users (GDPR)", count=result.rowcount)
            except Exception as e:
                await session.rollback()
                # Ponovno dižemo grešku da je 'run_daily_cleanup' može logirati
                raise e