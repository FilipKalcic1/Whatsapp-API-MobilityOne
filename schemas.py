# schemas.py
from pydantic import BaseModel, Field
from typing import Optional

class UserData(BaseModel):
    person_id: str = Field(..., description="GUID korisnika iz MasterData")
    phone: str
    display_name: str
    email: str = "UNKNOWN"
    tenant_id: Optional[str] = None

class OrgData(BaseModel):
    cost_center_id: str = "UNKNOWN"
    department_id: str = "UNKNOWN"
    manager_id: str = "UNKNOWN"

class VehicleData(BaseModel):
    id: str = "UNKNOWN"
    plate: str = "UNKNOWN"
    name: str = "Nema vozila"
    reg_expiry: str = "UNKNOWN"
    vin: str = "UNKNOWN"

class FinancialData(BaseModel):
    monthly_amount: str = "UNKNOWN"
    remaining_amount: str = "UNKNOWN"
    leasing_provider: str = "UNKNOWN"

class OperationalContext(BaseModel):
    """
    Jedan objekt koji drži SVE podatke.
    Ovo je tvoj 'Keyring'.
    """
    user: UserData
    org: OrgData
    vehicle: VehicleData
    contract: FinancialData

    def to_prompt_block(self) -> str:
        """Pretvara objekt u lijepi string za AI Prompt."""
        lines = []
        # User
        lines.append(f"- User.PersonId: {self.user.person_id}")
        lines.append(f"- User.DisplayName: {self.user.display_name}")
        lines.append(f"- User.Phone: {self.user.phone}")
        
        # Vehicle - [POPRAVAK] Dodano ime vozila!
        lines.append(f"- Vehicle.Id: {self.vehicle.id}")
        lines.append(f"- Vehicle.Name: {self.vehicle.name}")  # <--- OVO JE FALILO
        lines.append(f"- Vehicle.Plate: {self.vehicle.plate}")
        lines.append(f"- Vehicle.RegExpiry: {self.vehicle.reg_expiry}")
        
        # Org
        lines.append(f"- Org.CostCenterId: {self.org.cost_center_id}")
        
        # Finance
        lines.append(f"- Contract.MonthlyAmount: {self.contract.monthly_amount}")
        lines.append(f"- Contract.LeasingProvider: {self.contract.leasing_provider}") # Dodao sam i ovo da zna tko je leasing
        
        return "\n".join(lines)