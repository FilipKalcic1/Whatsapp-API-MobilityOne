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
            """
            Dohvaća AKTIVNO vozilo koristeći /automation/MasterData endpoint.
            """
            if not self.default_tenant_id:
                return "Nema dodijeljenog vozila"

            try:
                tool_def = {
                    "path": "/automation/MasterData",
                    "method": "GET",
                    "description": "Get person master data",
                    "operationId": "GetMasterData"
                }
                
                params = {
                    "personId": person_guid,
                    "x-tenant": self.default_tenant_id
                }
                
                response = await self.gateway.execute_tool(tool_def, params)
                
                # Normalizacija odgovora u listu radi lakše obrade
                raw_data = response.get("Data", []) if "Data" in response else response
                items = raw_data if isinstance(raw_data, list) else [raw_data]
                
                if not items:
                    return "Nema dodijeljenog vozila"

                # 1. KORAK: Filtriranje (Tražimo najrelevantniji zapis)
                # Ako API vraća listu, moramo naći onaj koji je AKTIVAN.
                # (Ovdje pretpostavljam da postoji neki indikator, npr. 'IsActive' ili datum. 
                # Ako nema statusa, uzimamo zadnji ili prvi, ali svjesno).
                
                selected_vehicle = None
                
                # Primjer logike: Preferiraj onaj koji ima podatke o tablici
                # Možeš dodati i provjeru: if item.get('Status') == 'Active'
                


                candidates = [
                    item for item in items 
                    if (item.get('LicencePlate') or item.get('VehicleLicencePlate'))
                ]

                if not candidates:
                    return "Nema dodijeljenog vozila"

                # Ako ima više kandidata, idealno bi bilo sortirati po datumu dodjele 
                # Ovdje uzimamo prvi iz filtrirane liste (ili zadnji ako API vraća kronološki)
                selected_vehicle = candidates[0] 

                # 2. KORAK: Ekstrakcija podataka
                brand = selected_vehicle.get('Manufacturer') or selected_vehicle.get('VehicleManufacturer', '')
                model = selected_vehicle.get('Model') or selected_vehicle.get('VehicleModel', '')
                plate = selected_vehicle.get('LicencePlate') or selected_vehicle.get('VehicleLicencePlate', '')
                
                # Clean up strings (strip whitespace)
                brand = str(brand).strip()
                model = str(model).strip()
                plate = str(plate).strip()

                if not brand and not plate:
                    return "Nema dodijeljenog vozila"

                # Formatiranje ispisa
                full_name = f"{brand} {model}".strip()
                if plate:
                    full_name = f"{full_name} ({plate})"
                
                return full_name.strip()
                
            except Exception as e:
                # Dodaj person_guid u log da znaš točno KOME nije našao vozilo
                logger.warning(f"Vehicle lookup failed for person {person_guid}", error=str(e))
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