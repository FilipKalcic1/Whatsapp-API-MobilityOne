import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Any, Tuple, Optional
from datetime import datetime

from models import UserMapping
from services.openapi_bridge import OpenAPIGateway
from config import get_settings
from schemas import OperationalContext, UserData, OrgData, VehicleData, FinancialData

logger = structlog.get_logger("user_service")
settings = get_settings()

class UserService:
    def __init__(self, db: AsyncSession, gateway: OpenAPIGateway):
        self.db = db
        self.gateway = gateway
        self.default_tenant_id = getattr(settings, "MOBILITY_TENANT_ID", None)

    async def get_active_identity(self, phone: str) -> Optional[UserMapping]:
        stmt = select(UserMapping).where(UserMapping.phone_number == phone, UserMapping.is_active == True)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def build_operational_context(self, person_guid: str, phone: str) -> OperationalContext:
        """
        Gradi kontekst koristeći Pydantic modele za tipsku sigurnost.
        
        Args:
            person_guid: GUID korisnika iz API-ja
            phone: Broj telefona za identifikaciju
            
        Returns:
            OperationalContext: Kompletan kontekst sa svim podacima
        """
        # Prvo dohvati display_name iz baze ako postoji
        existing_user = await self.get_active_identity(phone)
        display_name = existing_user.display_name if existing_user else "Korisnik"
        
        # 1. Inicijalizacija praznih modela
        user = UserData(
            person_id=person_guid, 
            phone=phone, 
            display_name=display_name, 
            tenant_id=self.default_tenant_id
        )
        org = OrgData()
        veh = VehicleData()
        fin = FinancialData()

        # Ako nema tenanta ili gateway-a, vrati prazno
        if not self.default_tenant_id or not self.gateway:
            return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)

        try:
            # 2. API Poziv za MasterData
            fake_ctx = {"tenant_id": self.default_tenant_id}
            response = await self.gateway.execute_tool(
                {"path": "/automation/MasterData", "method": "GET", "operationId": "GetMasterData"}, 
                {"personId": person_guid}, 
                user_context=fake_ctx
            )
            
            # 3. Obrada odgovora
            raw_data = response.get("Data", []) if isinstance(response, dict) else response
            target_item = raw_data[0] if isinstance(raw_data, list) and raw_data else raw_data
            
            if not isinstance(target_item, dict):
                return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)

            # --- 4. PARSIRANJE U OBJEKTE ---

            # A) User Data
            driver_str = self._find_val(target_item, ['Driver'])
            if driver_str:
                parts = driver_str.split(" - ")
                user.display_name = parts[-1].strip() if len(parts) > 1 else driver_str
            
            did = self._find_val(target_item, ['DriverId'])
            if did: 
                user.person_id = did  # Ažuriramo ID ako je DriverId točniji
            
            email = self._find_val(target_item, ['Email', 'EmailAddress'])
            if email: 
                user.email = email

            # B) Org Data
            cc = self._find_val(target_item, ['CostCenterId', 'CostCenter'])
            if cc: 
                org.cost_center_id = cc
            
            dept = self._find_val(target_item, ['OrgUnitId'])
            if dept: 
                org.department_id = dept
            
            mgr = self._find_val(target_item, ['ManagerId'])
            if mgr: 
                org.manager_id = mgr

            # C) Vehicle Data
            veh.id = self._find_val(target_item, ['Id', 'VehicleId', 'AssetId']) or "UNKNOWN"
            veh.plate = self._find_val(target_item, ['LicencePlate']) or "UNKNOWN"
            veh.name = self._find_val(target_item, ['FullVehicleName', 'DisplayName']) or "Nema vozila"
            veh.vin = self._find_val(target_item, ['VIN']) or "UNKNOWN"
            
            # Datum registracije (Nested logic)
            try:
                activities = target_item.get("PeriodicActivities", {})
                if activities and isinstance(activities, dict):
                    reg = activities.get("Registracija", {}).get("ExpiryDate")
                    if reg: 
                        veh.reg_expiry = self._parse_date(reg)
            except Exception:
                pass
            
            if veh.reg_expiry == "UNKNOWN":
                r_date = self._find_val(target_item, ['RegistrationExpirationDate', 'ContractEnd'])
                if r_date: 
                    veh.reg_expiry = self._parse_date(r_date)

            # D) Financial Data
            monthly = self._find_val(target_item, ['MonthlyAmount', 'Installment'])
            if monthly: 
                fin.monthly_amount = f"{monthly} EUR"
            
            rem = self._find_val(target_item, ['RemainingAmount', 'Debt'])
            if rem: 
                fin.remaining_amount = f"{rem} EUR"
            
            prov = self._find_val(target_item, ['SupplierName', 'ProviderName'])
            if prov: 
                fin.leasing_provider = prov

            # Vraćamo popunjeni glavni objekt
            return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)

        except Exception as e:
            logger.error("Failed to build operational context", error=str(e))
            # Vraćamo barem ono što imamo
            return OperationalContext(user=user, org=org, vehicle=veh, contract=fin)

    # --- HELPERI ---
    def _find_val(self, data: dict, keys: List[str]) -> Optional[str]:
        if not data: 
            return None
        data_lower = {k.lower(): v for k, v in data.items()}
        for key in keys:
            val = data_lower.get(key.lower())
            if val is not None and str(val).strip() != "":
                return str(val)
        return None

    def _parse_date(self, date_str: Any) -> str:
        if not date_str: 
            return "UNKNOWN"
        try:
            clean = str(date_str).replace('Z', '')
            return datetime.fromisoformat(clean).strftime("%d.%m.%Y")
        except Exception:
            return str(date_str)

    # --- ONBOARDING (originalni kod) ---
    async def try_auto_onboard(self, phone: str) -> Optional[Tuple[str, str]]:
        """Pokušaj automatskog onboardinga za broj telefona."""
        if not self.gateway:
            logger.error("Gateway not initialized")
            return None

        try:
            # 1. Pretraži osobu po telefonu
            search_tool = {"path": "/automation/PersonSearch", "method": "POST", "operationId": "SearchByPhone"}
            response = await self.gateway.execute_tool(
                search_tool,
                {"phone": phone},
                user_context={"tenant_id": self.default_tenant_id}
            )

            # 2. Provjeri rezultate
            persons = response.get("Data", []) if isinstance(response, dict) else response
            if not persons:
                logger.warning("No person found for phone", phone=phone)
                return None

            # 3. Uzmi prvu osobu
            person = persons[0] if isinstance(persons, list) else persons
            person_id = person.get("Id")
            display_name = person.get("DisplayName", "Korisnik")

            # 4. Dohvati vozilo
            vehicle_tool = {"path": "/automation/Vehicle", "method": "GET", "operationId": "GetVehicleByPerson"}
            vehicle_resp = await self.gateway.execute_tool(
                vehicle_tool,
                {"personId": person_id},
                user_context={"tenant_id": self.default_tenant_id}
            )

            vehicles = vehicle_resp.get("Data", []) if isinstance(vehicle_resp, dict) else vehicle_resp
            vehicle_info = "Nepoznato vozilo"
            if vehicles:
                vehicle = vehicles[0] if isinstance(vehicles, list) else vehicles
                plate = vehicle.get("LicencePlate", "NEPOZNATO")
                model = vehicle.get("Model", "")
                vehicle_info = f"{plate} ({model})" if model else plate

            # 5. Spremi u bazu
            await self._persist_mapping(phone, person_id, display_name)

            return (display_name, vehicle_info)

        except Exception as e:
            logger.error("Auto-onboard failed", phone=phone, error=str(e))
            return None

    async def _persist_mapping(self, phone: str, api_identity: str, display_name: str):
        """Spremi mapiranje u bazu."""
        existing = await self.get_active_identity(phone)
        if existing:
            existing.is_active = True
            existing.api_identity = api_identity
            existing.display_name = display_name
        else:
            new_mapping = UserMapping(
                phone_number=phone,
                api_identity=api_identity,
                display_name=display_name,
                is_active=True
            )
            self.db.add(new_mapping)
        await self.db.commit()