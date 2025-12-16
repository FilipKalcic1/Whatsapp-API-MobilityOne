"""
Pydantic schemas for MobilityOne v6.0
"""
from typing import Optional
from pydantic import BaseModel


class UserData(BaseModel):
    """User information."""
    person_id: str = "UNKNOWN"
    phone: str = ""
    display_name: str = "Korisnik"
    tenant_id: str = ""


class OrgData(BaseModel):
    """Organization info."""
    cost_center_id: str = "UNKNOWN"
    department_id: str = "UNKNOWN"


class VehicleData(BaseModel):
    """Vehicle information."""
    id: str = "UNKNOWN"
    plate: str = "UNKNOWN"
    name: str = "UNKNOWN"
    vin: str = "UNKNOWN"
    mileage: str = "UNKNOWN"  # Added!


class FinancialData(BaseModel):
    """Financial/contract info."""
    monthly_amount: str = "UNKNOWN"
    remaining_amount: str = "UNKNOWN"
    leasing_provider: str = "UNKNOWN"


class OperationalContext(BaseModel):
    """Complete user context for AI."""
    user: UserData
    org: OrgData
    vehicle: VehicleData
    contract: FinancialData
    
    def to_prompt_block(self) -> str:
        """Format for system prompt."""
        return (
            f"KORISNIK: {self.user.display_name} (ID: {self.user.person_id[:8]}...)\n"
            f"VOZILO: {self.vehicle.name} ({self.vehicle.plate})\n"
            f"KILOMETRAŽA: {self.vehicle.mileage} km\n"
            f"VIN: {self.vehicle.vin}"
        )

        
class InboundMessage(BaseModel):
    """Inbound WhatsApp message."""
    sender: str
    text: str
    message_id: str


class OutboundMessage(BaseModel):
    """Outbound WhatsApp message."""
    to: str
    text: str
    correlation_id: Optional[str] = None