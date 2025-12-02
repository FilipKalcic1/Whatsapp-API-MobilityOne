import pytest
from unittest.mock import MagicMock, AsyncMock
from services.user_service import UserService

@pytest.mark.asyncio
async def test_onboard_user_missing_email_field():
    """Testira situaciju kad vanjski API vrati podatke bez emaila."""
    mock_db = AsyncMock()
    mock_gateway = MagicMock()
    
    mock_gateway.execute_tool = AsyncMock(return_value={
        "Name": "Pero Perić",
        "Id": "12345"
    })
    
    service = UserService(mock_db, mock_gateway)
    result = await service.onboard_user("38599123456", "pero@test.com")
    
    assert result is None
    mock_db.add.assert_not_called()

@pytest.mark.asyncio
async def test_onboard_user_api_error_response():
    """Testira situaciju kad vanjski API vrati grešku."""
    mock_db = AsyncMock()
    mock_gateway = MagicMock()
    
    mock_gateway.execute_tool = AsyncMock(return_value={"error": True, "message": "Not found"})
    
    service = UserService(mock_db, mock_gateway)
    result = await service.onboard_user("38599123456", "pero@test.com")
    
    assert result is None
    mock_db.commit.assert_not_called()

@pytest.mark.asyncio
async def test_onboard_user_success_flow():
    """
    [NOVO] Testira uspješan onboarding.
    Ovo pokriva _persist_mapping i veći dio user_service.py.
    """
    mock_db = AsyncMock()
    mock_gateway = MagicMock()
    
    # 1. Simuliramo da korisnik već ne postoji u bazi (get_active_identity vraća None)
    # execute vraća result objekt, čiji scalars().first() vraća None
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    # 2. Simuliramo uspješan odgovor vanjskog API-ja
    mock_gateway.execute_tool = AsyncMock(return_value={
        "Email": "valid@email.com",
        "FirstName": "Ivan",
        "Id": "999"
    })
    
    service = UserService(mock_db, mock_gateway)
    
    # Akcija
    result_name = await service.onboard_user("38599888888", "  valid@email.com  ") # Testiramo i trim
    
    # Provjere
    assert result_name == "Ivan"
    
    # Je li pozvan DB add?
    mock_db.add.assert_called_once()
    # Je li pozvan commit?
    mock_db.commit.assert_called_once()