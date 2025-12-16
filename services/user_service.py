"""
User Service - Production Ready (v6.0)

FIXED:
1. Handles LIST response from MasterData
2. Uses gateway.get_master_data() convenience method
3. Proper UPSERT logic
"""

import structlog
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, Union, List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import UserMapping
from services.openapi_bridge import OpenAPIGateway
from services.cache import CacheService
from config import get_settings
from schemas import OperationalContext, UserData, OrgData, VehicleData, FinancialData

logger = structlog.get_logger("user_service")
settings = get_settings()


class UserService:
    """User identity management."""
    
    def __init__(
        self, 
        db: AsyncSession, 
        gateway: OpenAPIGateway, 
        cache_service: CacheService
    ):
        self.db = db
        self.gateway = gateway
        self.cache = cache_service
        self.default_tenant_id = settings.tenant_id
    
    async def get_active_identity(self, phone: str) -> Optional[UserMapping]:
        """Get user from local DB."""
        try:
            stmt = select(UserMapping).where(
                UserMapping.phone_number == phone,
                UserMapping.is_active == True
            )
            result = await self.db.execute(stmt)
            return result.scalars().first()
        except Exception as e:
            logger.error("DB lookup failed", error=str(e))
            return None
    
    async def try_auto_onboard(self, phone: str) -> Optional[Tuple[str, str]]:
        """
        Onboard new user.
        Returns (display_name, vehicle_info) if successful.
        """
        if not self.gateway:
            return None
        
        try:
            # 1. Lookup by phone
            person = await self.gateway.get_person_by_phone(phone)
            
            if not person:
                logger.warning("Person not found", phone_suffix=phone[-4:])
                return None
            
            # 2. Get PersonId
            person_id = person.get("Id") or person.get("id")
            if not person_id:
                return None
            
            # 3. Validate phone
            api_phone = str(person.get("Phone") or person.get("Mobile") or "")
            if not self._phones_match(phone, api_phone):
                logger.warning("Phone mismatch")
                return None
            
            # 4. Display name
            display_name = self._extract_display_name(person)
            
            logger.info("Person found", person_id=person_id[:8], name=display_name)
            
            # 5. Vehicle info
            vehicle_info = await self._get_vehicle_info(person_id)
            
            # 6. Save to DB
            await self._upsert_mapping(phone, person_id, display_name)
            
            return (display_name, vehicle_info)
            
        except Exception as e:
            logger.error("Auto-onboard failed", error=str(e))
            return None
    
    def _extract_display_name(self, person: Dict) -> str:
        """Extract clean display name."""
        name = (
            person.get("DisplayName") or 
            f"{person.get('FirstName', '')} {person.get('LastName', '')}".strip() or
            "Korisnik"
        )
        
        # "A-1 - Kalčić, Filip" → "Filip Kalčić"
        if " - " in name:
            parts = name.split(" - ")
            if len(parts) > 1:
                name_part = parts[-1].strip()
                if ", " in name_part:
                    surname, firstname = name_part.split(", ", 1)
                    name = f"{firstname} {surname}"
                else:
                    name = name_part
        
        return name
    
    def _phones_match(self, a: str, b: str) -> bool:
        """Compare phone numbers."""
        clean_a = "".join(c for c in a if c.isdigit())
        clean_b = "".join(c for c in b if c.isdigit())
        
        if clean_a == clean_b:
            return True
        
        if len(clean_a) >= 9 and len(clean_b) >= 9:
            return clean_a[-9:] == clean_b[-9:]
        
        return False
    
    async def _get_vehicle_info(self, person_id: str) -> str:
        """Get vehicle description."""
        try:
            # Use convenience method that handles list response
            data = await self.gateway.get_master_data(person_id)
            
            if data:
                plate = data.get("LicencePlate") or data.get("Plate")
                name = (
                    data.get("FullVehicleName") or 
                    data.get("DisplayName") or 
                    data.get("Model")
                )
                
                if plate:
                    return f"{name or 'Vozilo'} ({plate})"
                elif name:
                    return name
            
            return "Trenutno nema dodijeljenog vozila"
            
        except Exception as e:
            logger.warning("Vehicle info failed", error=str(e))
            return "Nepoznato"
    
    async def _upsert_mapping(self, phone: str, api_identity: str, display_name: str):
        """Save user mapping (UPSERT)."""
        try:
            stmt = pg_insert(UserMapping).values(
                phone_number=phone,
                api_identity=api_identity,
                display_name=display_name,
                is_active=True,
                updated_at=datetime.utcnow()
            ).on_conflict_do_update(
                index_elements=['phone_number'],
                set_={
                    'api_identity': api_identity,
                    'display_name': display_name,
                    'is_active': True,
                    'updated_at': datetime.utcnow()
                }
            )
            
            await self.db.execute(stmt)
            await self.db.commit()
            logger.info("User saved", phone_suffix=phone[-4:])
            
        except Exception as e:
            logger.error("Upsert failed", error=str(e))
            await self.db.rollback()
    
    async def build_operational_context(self, person_id: str, phone: str) -> OperationalContext:
        """Build context for AI."""
        if not person_id or person_id == "UNKNOWN":
            return self._empty_context(phone)
        
        # Cache check
        cache_key = f"context:{person_id}"
        cached = await self._load_cache(cache_key)
        if cached:
            return cached
        
        # Build from API
        context = await self._build_from_api(person_id, phone)
        
        # Cache if valid
        if context.vehicle.id != "UNKNOWN":
            await self._save_cache(cache_key, context)
        
        return context
    
    async def _load_cache(self, key: str) -> Optional[OperationalContext]:
        """Load from cache."""
        try:
            if self.cache:
                data = await self.cache.get(key)
                if data:
                    return OperationalContext.model_validate_json(data)
        except:
            pass
        return None
    
    async def _save_cache(self, key: str, ctx: OperationalContext):
        """Save to cache."""
        try:
            if self.cache:
                await self.cache.set(key, ctx.model_dump_json(), ttl=300)
        except:
            pass
    
    async def _build_from_api(self, person_id: str, phone: str) -> OperationalContext:
        """Build context from API."""
        user = UserData(
            person_id=person_id,
            phone=phone,
            display_name="Korisnik",
            tenant_id=self.default_tenant_id
        )
        org = OrgData()
        vehicle = VehicleData()
        finance = FinancialData()
        
        if not self.gateway:
            return OperationalContext(user=user, org=org, vehicle=vehicle, contract=finance)
        
        try:
            # Use convenience method
            data = await self.gateway.get_master_data(person_id)
            
            if not data:
                return OperationalContext(user=user, org=org, vehicle=vehicle, contract=finance)
            
            # User
            driver = data.get("Driver") or data.get("DriverName")
            if driver:
                user.display_name = self._extract_display_name({"DisplayName": driver})
            
            # Vehicle
            vehicle.id = data.get("Id") or data.get("VehicleId") or "UNKNOWN"
            vehicle.plate = data.get("LicencePlate") or data.get("Plate") or "UNKNOWN"
            vehicle.name = data.get("FullVehicleName") or data.get("DisplayName") or "Vozilo"
            vehicle.vin = data.get("VIN") or "UNKNOWN"
            
            # Mileage - important!
            if data.get("Mileage"):
                vehicle.mileage = str(data["Mileage"])
            elif data.get("CurrentMileage"):
                vehicle.mileage = str(data["CurrentMileage"])
            
            # Org
            org.cost_center_id = data.get("CostCenterId") or "UNKNOWN"
            org.department_id = data.get("OrgUnit") or "UNKNOWN"
            
            # Finance
            if data.get("MonthlyAmount"):
                finance.monthly_amount = f"{data['MonthlyAmount']} EUR"
            finance.leasing_provider = data.get("ProviderName") or "UNKNOWN"
            
            return OperationalContext(user=user, org=org, vehicle=vehicle, contract=finance)
            
        except Exception as e:
            logger.error("Context build failed", error=str(e))
            return OperationalContext(user=user, org=org, vehicle=vehicle, contract=finance)
    
    def _empty_context(self, phone: str) -> OperationalContext:
        """Empty context for unknown users."""
        return OperationalContext(
            user=UserData(person_id="UNKNOWN", phone=phone, display_name="Korisnik"),
            org=OrgData(),
            vehicle=VehicleData(),
            contract=FinancialData()
        )