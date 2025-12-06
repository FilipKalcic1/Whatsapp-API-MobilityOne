import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Tuple, Optional

from models import UserMapping
from services.openapi_bridge import OpenAPIGateway
from config import get_settings

logger = structlog.get_logger("user_service")
settings = get_settings()

class UserService:
    def __init__(self, db: AsyncSession, gateway: OpenAPIGateway):
        self.db = db
        self.gateway = gateway
        # Učitavamo Tenant ID iz konfiguracije (.env) jer je obavezan za API pozive
        self.default_tenant_id = getattr(settings, "MOBILITY_TENANT_ID", None)

    async def get_active_identity(self, phone: str) -> UserMapping | None:
        """
        Dohvaća aktivnog korisnika iz lokalne baze (cache).
        Ako korisnik postoji ovdje, znači da je već onboardan.
        """
        stmt = select(UserMapping).where(UserMapping.phone_number == phone, UserMapping.is_active == True)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def try_auto_onboard(self, phone: str) -> Tuple[str, str] | None:
        """
        Pokušava identificirati korisnika isključivo na temelju broja telefona.
        Ne traži nikakav input od korisnika (zero-touch).
        
        Returns:
            Tuple (Ime Prezime, Naziv Vozila) ako je uspješno.
            None ako korisnik nije pronađen u sustavu.
        """
        
        # 1. Čišćenje i priprema broja
        # Micanje razmaka, pluseva i crtica da dobijemo čiste znamenke
        clean_phone = "".join(filter(str.isdigit, phone))
        
        # Strategija pretraživanja: API podržava 'contains', pa tražimo
        # zadnjih 8 ili 7 znamenki kako bismo zaobišli razlike u formatima (091 vs 38591)
        search_terms = []
        if len(clean_phone) >= 8:
            search_terms.append(clean_phone[-8:])
        if len(clean_phone) >= 7:
            search_terms.append(clean_phone[-7:])

        person_data = None
        
        # Iteriramo kroz termine dok ne nađemo pogodak
        for term in search_terms:
            person_data = await self._find_person_by_phone_fragment(term)
            if person_data:
                break
        
        # Ako osoba nije pronađena ni s jednim terminom, prekidamo
        if not person_data:
            logger.warning("Onboarding failed: Phone not found in registry", phone=phone)
            return None

        # 2. Ekstrakcija ključnih podataka
        # GUID (Id) je obavezan za daljnji rad sustava
        person_id = person_data.get("Id")
        if not person_id:
            logger.error("Data integrity error: Person found but missing GUID", phone=phone)
            return None

        first_name = person_data.get('FirstName', '').strip()
        last_name = person_data.get('LastName', '').strip()
        full_name = f"{first_name} {last_name}".strip() or "Korisnik"

        # 3. Dohvat vozila (Kontekstualno obogaćivanje)
        # Ovo je opcionalno - ako ne uspije, ne blokiramo ulaz korisniku
        vehicle_name = await self._resolve_vehicle_name(person_id)
        #### 
        # 4. Spremanje u lokalnu bazu
        # Povezujemo broj mobitela (WhatsApp ID) s GUID-om osobe (MobilityOne ID)
        await self._persist_mapping(phone, person_id, full_name)
        
        logger.info("User successfully onboarded", uid=person_id, vehicle=vehicle_name)
        return full_name, vehicle_name

    async def _find_person_by_phone_fragment(self, fragment: str) -> dict | None:
        """
        Izvodi API upit prema Tenant Management servisu tražeći osobu po dijelu broja telefona.
        """
        if not self.default_tenant_id:
            logger.error("Missing configuration: MOBILITY_TENANT_ID is not set")
            return None

        tool_def = {
            "path": "/tenantmgt/Persons",
            "method": "GET",
            "description": "Find person",
            "operationId": "GetPersons"
        }
        
        # Filter sintaksa: Phone(contains)1234567
        # x-tenant je obavezan header
        params = {
            "Filter": [f"Phone(contains){fragment}"], 
            "Rows": 1,
            "x-tenant": self.default_tenant_id
        }
        
        try:
            response = await self.gateway.execute_tool(tool_def, params)
            data = response.get("Data", [])
            # Vraćamo prvi rezultat ako lista nije prazna
            return data[0] if data and isinstance(data, list) else None
        except Exception as e:
            logger.error("API Lookup Error", error=str(e))
            return None

    async def _resolve_vehicle_name(self, person_guid: str) -> str:
            if not self.default_tenant_id:
                return "Nema dodijeljenog vozila"

            try:
                tool_def = {
                    "path": "/automation/MasterData",
                    "method": "GET",
                    "description": "Get person master data",
                    "operationId": "GetMasterData"
                }
                
                # Parametri samo za query
                params = {"personId": person_guid}
                
                # [POPRAVAK] Tenant ide u kontekst, ne u params
                fake_context = {"tenant_id": self.default_tenant_id}
                
                # Poziv sada ide kroz popravljeni openapi_bridge koji dodaje Token + Tenant
                response = await self.gateway.execute_tool(tool_def, params, user_context=fake_context)
                
                # Obrada rezultata
                raw_data = response.get("Data", []) if isinstance(response, dict) else response
                items = raw_data if isinstance(raw_data, list) else [raw_data]

                if not items: return "Nema dodijeljenog vozila"
                
                # Filtriranje
                candidates = [i for i in items if isinstance(i, dict) and (i.get('LicencePlate') or i.get('VehicleLicencePlate'))]
                if not candidates: return "Nema dodijeljenog vozila"

                veh = candidates[0]
                brand = veh.get('Manufacturer') or veh.get('VehicleManufacturer', '')
                model = veh.get('Model') or veh.get('VehicleModel', '')
                plate = veh.get('LicencePlate') or veh.get('VehicleLicencePlate', '')
                
                full = f"{brand} {model}".strip()
                return f"{full} ({plate})" if plate else full
                
            except Exception as e:
                logger.warning(f"Vehicle lookup failed", person=person_guid, error=str(e))
                return "Nema dodijeljenog vozila"

    async def _persist_mapping(self, phone: str, guid: str, name: str) -> None:
        """
        Sprema ili ažurira korisnika u lokalnoj bazi.
        Ključna promjena: api_identity je sada GUID.
        """
        try:
            existing = await self.get_active_identity(phone)
            
            if existing:
                existing.api_identity = guid
                existing.display_name = name
            else:
                self.db.add(UserMapping(phone_number=phone, api_identity=guid, display_name=name))
                
            await self.db.commit()
        except Exception as e:
            await self.db.rollback()
            logger.error("Database persistence error", error=str(e))
            raise e