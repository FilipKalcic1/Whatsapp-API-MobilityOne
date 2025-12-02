import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import UserMapping
from services.openapi_bridge import OpenAPIGateway
from config import get_settings


logger = structlog.get_logger("user_service")
settings = get_settings()


class UserService:
    def __init__(self, db: AsyncSession, gateway: OpenAPIGateway):
        self.db = db
        self.gateway = gateway

    async def get_active_identity(self, phone: str) -> UserMapping | None:
        stmt = select(UserMapping).where(UserMapping.phone_number == phone, UserMapping.is_active == True)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def onboard_user(self, phone: str, email_input: str) -> str | None:
        email_clean = email_input.strip().lower()
        
        profile = await self._validate_remote_profile(email_clean)
        if not profile:
            return None

        return await self._persist_mapping(phone, profile)

    async def _validate_remote_profile(self, email: str) -> dict | None:
        tool_def = {
            "path": settings.MOBILITY_USER_CHECK_ENDPOINT,
            "method": "GET",
            "description": "Check user existence",
            "openai_schema": {}, 
            "operationId": "GetPersonData"
        }

        logger.info("Validating identity", email=email)
        response = await self.gateway.execute_tool(tool_def, {"personIdOrEmail": email})

        if response.get("error"):
            logger.warning("Remote validation returned error", email=email, error=response)
            return None
        

        valid_email = (
            response.get("Email") or 
            response.get("ContactEmail") or 
            response.get("email")
        )
        
        if not valid_email:

            logger.error("Validation Error: API response missing email field", response_keys=list(response.keys()))
            return None
            

        response["_normalized_email"] = valid_email
        response["_normalized_name"] = response.get("FirstName") or response.get("Name") or "Korisnik"
        
        return response

    async def _persist_mapping(self, phone: str, profile: dict) -> str:

        email = profile["_normalized_email"]
        name = profile["_normalized_name"]

        try:
            existing = await self.get_active_identity(phone)
            if existing:
                existing.api_identity = email
                existing.display_name = name
            else:
                new_user = UserMapping(phone_number=phone, api_identity=email, display_name=name)
                self.db.add(new_user)
            
            await self.db.commit()
            return name
        except Exception as e:
            await self.db.rollback()
            logger.error("Database error", error=str(e))
            raise e