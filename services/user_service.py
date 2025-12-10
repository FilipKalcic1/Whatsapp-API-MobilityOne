import structlog
import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Any, Tuple, Optional
from datetime import datetime



# Importi modela i konfiguracije
from models import UserMapping
from services.openapi_bridge import OpenAPIGateway
from services.cache import CacheService 
from config import get_settings
from schemas import OperationalContext, UserData, OrgData, VehicleData, FinancialData

logger = structlog.get_logger("user_service")
settings = get_settings()

class UserService:
    def __init__(self, db: AsyncSession, gateway: OpenAPIGateway, cache_service: CacheService):
        self.db = db
        self.gateway = gateway
        self.cache = cache_service
        self.default_tenant_id = getattr(settings, "MOBILITY_TENANT_ID", None)

    async def get_active_identity(self, phone: str) -> Optional[UserMapping]:
        """Dohvaća mapiranje korisnika iz lokalne baze."""
        stmt = select(UserMapping).where(UserMapping.phone_number == phone, UserMapping.is_active == True)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def build_operational_context(self, person_guid: str, phone: str) -> OperationalContext:
        """
        Gradi kontekst za AI.
        Hijerarhija: Cache -> API -> Cache
        """
        # Sigurnosna provjera: Ako nemamo GUID, ne možemo ništa
        if not person_guid:
            return self._create_empty_context(phone)

        cache_key = f"context:{person_guid}"

        # --- 1. BRZI PUT: Provjeri Cache ---
        try:
            cached_data = await self.cache.get(cache_key)
            if cached_data:
                try:
                    # Pokušaj za Pydantic V2
                    ctx = OperationalContext.model_validate_json(cached_data)
                    logger.info("Context loaded from cache (Hit)", user=person_guid)
                    return ctx
                except AttributeError:
                    # Fallback za Pydantic V1
                    ctx = OperationalContext.parse_raw(cached_data)
                    logger.info("Context loaded from cache (Hit - V1)", user=person_guid)
                    return ctx
        except Exception as e:
            logger.warning("Cache read warning (proceeding to API)", error=str(e))

        # --- 2. SPORI PUT: Dohvati svježe podatke s API-ja ---
        context = await self._fetch_from_api_securely(person_guid, phone)

        # --- 3. SPREMANJE: Spremi u Cache ---
        if context.vehicle.id != "UNKNOWN":
            try:
                try:
                    # Pydantic V2
                    context_json = context.model_dump_json()
                except AttributeError:
                    # Pydantic V1
                    context_json = context.json()
                
                await self.cache.set(cache_key, context_json, ttl=300)
            except Exception as e:
                logger.warning("Failed to cache context", error=str(e))

        return context

    async def _fetch_from_api_securely(self, person_guid: str, phone: str) -> OperationalContext:
        """Interna metoda za dohvat podataka s API-ja."""
        
        # Ako Tenant ID fali, nema smisla zvati API
        if not self.default_tenant_id:
            logger.error("Missing MOBILITY_TENANT_ID in settings")
            return self._create_empty_context(phone)

        # Dohvati ime iz baze kao fallback
        existing_user = await self.get_active_identity(phone)
        display_name = existing_user.display_name if existing_user else "Korisnik"

        # Pripremi početne objekte
        user = UserData(
            person_id=person_guid, 
            phone=phone, 
            display_name=display_name, 
            tenant_id=self.default_tenant_id
        )
        org = OrgData()
        veh = VehicleData()
        fin = FinancialData()

        if not self.gateway:
            return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)

        try:
            # API Poziv
            fake_ctx = {"tenant_id": self.default_tenant_id}
            response = await self.gateway.execute_tool(
                {"path": "/automation/MasterData", "method": "GET", "operationId": "GetMasterData"}, 
                {"personId": person_guid}, 
                user_context=fake_ctx
            )
            
            raw_data = response.get("Data", []) if isinstance(response, dict) else response
            target_item = None

            # --- LOGIKA ODABIRA VOZILA (Sigurnosna petlja) ---
            if isinstance(raw_data, list):
                for item in raw_data:
                    # Traži po DriverId ili PersonId
                    api_driver_id = self._find_val(item, ['DriverId', 'PersonId'])
                    api_person_id_direct = str(item.get('Id', '')).lower()
                    target_guid = str(person_guid).lower()

                    if api_driver_id and str(api_driver_id).lower() == target_guid:
                        target_item = item
                        break
                    
                    if api_person_id_direct == target_guid:
                         target_item = item
                         break
                
                if not target_item and raw_data:
                    logger.warning("SECURITY: API data mismatch.", requested=person_guid)
                    return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)
            
            elif isinstance(raw_data, dict):
                api_driver_id = self._find_val(raw_data, ['DriverId'])
                if api_driver_id and str(api_driver_id).lower() != str(person_guid).lower():
                     return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)
                target_item = raw_data

            if not target_item:
                return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)

            # --- POPUNJAVANJE ---
            
            # User
            driver_str = self._find_val(target_item, ['Driver', 'DriverName'])
            if driver_str:
                parts = driver_str.split(" - ")
                user.display_name = parts[-1].strip() if len(parts) > 1 else driver_str
            
            email = self._find_val(target_item, ['Email', 'EmailAddress'])
            if email: user.email = email

            # Org
            org.cost_center_id = self._find_val(target_item, ['CostCenterId', 'CostCenter']) or "UNKNOWN"
            org.department_id = self._find_val(target_item, ['OrgUnit', 'OrgUnitName']) or "UNKNOWN"
            org.manager_id = self._find_val(target_item, ['ManagerId']) or "UNKNOWN"

            # Vehicle
            veh.id = self._find_val(target_item, ['Id', 'VehicleId', 'AssetId']) or "UNKNOWN"
            veh.plate = self._find_val(target_item, ['LicencePlate']) or "UNKNOWN"
            veh.name = self._find_val(target_item, ['FullVehicleName', 'DisplayName', 'Model']) or "Vozilo"
            veh.vin = self._find_val(target_item, ['VIN']) or "UNKNOWN"
            
            reg_date = self._find_nested_val(target_item, ["PeriodicActivities", "Registracija", "ExpiryDate"])
            if not reg_date:
                 reg_date = self._find_val(target_item, ['RegistrationExpirationDate', 'ContractEnd'])
            veh.reg_expiry = self._parse_date_safe(reg_date)

            # Finance
            monthly = self._find_val(target_item, ['MonthlyAmount', 'Installment'])
            if monthly: fin.monthly_amount = f"{monthly} EUR"
            
            rem = self._find_val(target_item, ['RemainingAmount', 'Debt'])
            if rem: fin.remaining_amount = f"{rem} EUR"
            
            fin.leasing_provider = (
                self._find_val(target_item, ['ProviderName']) or 
                self._find_val(target_item, ['SupplierName']) or 
                "UNKNOWN"
            )

            return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)

        except Exception as e:
            logger.error("API Fetch failed", error=str(e))
            return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)

    def _create_empty_context(self, phone: str) -> OperationalContext:
        """Vraća prazan kontekst da se kod ne ruši."""
        return OperationalContext(
            user=UserData(person_id="UNKNOWN", phone=phone, display_name="Korisnik"),
            org=OrgData(),
            vehicle=VehicleData(),
            contract=FinancialData()
        )

    # --- POMOĆNE METODE ---
    def _find_val(self, data: dict, keys: List[str]) -> Optional[str]:
        if not data: return None
        data_lower = {k.lower(): v for k, v in data.items()}
        for key in keys:
            val = data_lower.get(key.lower())
            if val is not None and str(val).strip() != "":
                return str(val)
        return None

    def _find_nested_val(self, data: dict, path: List[str]) -> Optional[Any]:
        current = data
        for step in path:
            if isinstance(current, dict):
                found = False
                for k, v in current.items():
                    if k.lower() == step.lower():
                        current = v
                        found = True
                        break
                if not found: return None
            else:
                return None
        return current

    def _parse_date_safe(self, date_input: Any) -> str:
        if not date_input: return "UNKNOWN"
        try:
            date_str = str(date_input).split("T")[0]
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.strftime("%d.%m.%Y")
        except Exception:
            return str(date_input)[:10]

    # --- ONBOARDING (SA SIGURNOSNOM ZAKRPOM) ---
    async def try_auto_onboard(self, phone: str) -> Optional[Tuple[str, str]]:
        if not self.gateway: return None
        try:
            # 1. Tražimo osobu po telefonu
            response = await self.gateway.execute_tool(
                {"path": "/automation/PersonSearch", "method": "POST", "operationId": "SearchByPhone"}, 
                {"phone": phone}, 
                user_context={"tenant_id": self.default_tenant_id}
            )
            persons = response.get("Data", []) if isinstance(response, dict) else response
            
            if isinstance(persons, list) and persons:
                target = persons[0]

                # ==========================================
                # [NOVO] CRITICAL SECURITY CHECK: PHONE MATCH
                # ==========================================
                # Ako API slučajno vrati krivu osobu (npr. direktora) jer nije našao nas,
                # moramo provjeriti da li se broj u API-ju slaže s brojem koji zove (sender).
                
                api_phone = str(target.get("Mobile") or target.get("Phone") or "")
                
                # Normalizacija (micanje + i razmaka) za usporedbu
                clean_input = phone.replace("+", "").replace(" ", "").strip()
                clean_api = api_phone.replace("+", "").replace(" ", "").strip()

                # Ako clean_input nije u clean_api (i obrnuto), vjerojatno je kriva osoba
                if clean_input not in clean_api and clean_api not in clean_input:
                    logger.warning(
                        "SECURITY ALERT: Onboarding phone mismatch prevented.", 
                        requested_phone=phone, 
                        api_returned_phone=api_phone,
                        api_returned_name=target.get("DisplayName")
                    )
                    return None
                # ==========================================

                person_id = target.get("Id")
                display_name = target.get("DisplayName", "Korisnik")
                
                veh_info = "Nepoznato vozilo"
                try:
                    v_resp = await self.gateway.execute_tool(
                        {"path": "/automation/Vehicle", "method": "GET", "operationId": "GetVehicle"},
                        {"personId": person_id},
                        user_context={"tenant_id": self.default_tenant_id}
                    )
                    v_data = v_resp.get("Data", []) if isinstance(v_resp, dict) else v_resp
                    if v_data and isinstance(v_data, list):
                        veh_info = v_data[0].get("LicencePlate", "Vozilo")
                except Exception:
                    pass

                await self._persist_mapping(phone, person_id, display_name)
                return display_name, veh_info
            
            return None
        except Exception as e:
            logger.warning("Auto-onboard failed", error=str(e))
            return None

    async def _persist_mapping(self, phone: str, api_identity: str, display_name: str):
        try:
            existing = await self.get_active_identity(phone)
            if existing:
                existing.is_active = True
                existing.api_identity = api_identity
                existing.display_name = display_name
            else:
                new_mapping = UserMapping(
                    phone_number=phone, api_identity=api_identity, display_name=display_name, is_active=True
                )
                self.db.add(new_mapping)
            await self.db.commit()
        except Exception as e:
            logger.error("Database persist failed", error=str(e))
            await self.db.rollback()