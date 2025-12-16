import structlog
from typing import Optional, Any
from config import get_settings

# --- CONSTANTS ---
# Use the encoded parentheses format which is known to work with the specific firewall/gateway
ODATA_FILTER_TEMPLATE = "Phone%28=%29{}" 
API_ENDPOINT = "/tenantmgt/Persons"

# --- LOGGING SETUP ---
logger = structlog.get_logger("identity_service")
settings = get_settings()

async def resolve_identity(gateway: Any, phone_number: str) -> Optional[str]:
    """
    Resolves a raw phone number to a persistent PersonID (GUID) using the MobilityOne API.
    
    This function acts as the "Source of Truth" for identity, bypassing local caches
    to handle cases where a user's ID has changed in the backend (e.g. vehicle re-assignment).
    
    Args:
        gateway: Instance of OpenAPIGateway (must have _do_request_with_retry method)
        phone_number: Raw phone number string (e.g., "+38591...")
        
    Returns:
        str: The canonical PersonID (GUID) if found.
        None: If the user does not exist or API fails.
    """
    # 1. Input Validation & Sanitization
    if not gateway:
        logger.error("identity_resolution_failed", reason="Gateway instance is None")
        return None
        
    if not phone_number:
        logger.warning("identity_resolution_skipped", reason="Empty phone number")
        return None

    # Remove '+' and whitespace to match API expectations
    clean_phone = phone_number.replace("+", "").strip().replace(" ", "")
    
    # 2. Configuration Retrieval
    # We must search within the specific tenant context
    tenant_id = getattr(settings, "DEFAULT_TENANT_ID", None)
    if not tenant_id:
        # Fallback to hardcoded ID only if absolutely necessary (Defensive default)
        tenant_id = "dee707eb-66ad-42e1-92f2-068be031f18a"
        logger.warning("identity_service_missing_tenant_config", using_fallback=tenant_id)

    # 3. Request Construction
    # We manually construct the query string to ensure OData filter syntax is preserved exactly.
    # Pattern: Filter=Phone(=)12345 -> encoded as Phone%28=%2912345
    filter_query = ODATA_FILTER_TEMPLATE.format(clean_phone)
    url = f"{gateway.base_url}{API_ENDPOINT}?Filter={filter_query}"
    
    headers = {
        "x-tenant": tenant_id,
        "Accept": "application/json"
    }

    log = logger.bind(phone=clean_phone, tenant_id=tenant_id)

    try:
        log.debug("identity_resolving_start", url=url)
        
        # 4. Execution
        # We access the protected method _do_request_with_retry to leverage the 
        # Gateway's existing authentication, token refresh, and connection pooling logic.
        response = await gateway._do_request_with_retry(
            method="GET",
            url=url,
            headers=headers
        )

        # 5. Error Handling (HTTP Level)
        # _do_request_with_retry returns a dict. If it has "error", something went wrong.
        if "error" in response:
            log.warning("identity_api_error", error_details=response)
            return None

        # 6. Response Parsing (Defensive)
        # Expected JSON: {"Items": [{"Id": "...", "DisplayName": "..."}], "Count": 1}
        items = response.get("Items")
        
        if not isinstance(items, list):
            log.warning("identity_unexpected_format", response_keys=list(response.keys()))
            return None
            
        if not items:
            log.info("identity_not_found", count=0)
            return None

        # 7. Success Extraction
        user = items[0]
        person_id = user.get("Id")
        display_name = user.get("DisplayName", "Unknown")

        if not person_id:
            log.error("identity_missing_id_field", user_data=user)
            return None

        log.info("identity_resolved_success", person_id=person_id, name=display_name)
        return person_id

    except Exception as e:
        log.error("identity_resolution_exception", error=str(e), exc_info=True)
        return None