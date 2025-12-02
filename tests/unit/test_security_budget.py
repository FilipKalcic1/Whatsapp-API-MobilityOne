import pytest
from unittest.mock import MagicMock, AsyncMock
from services.context import ContextService
from worker import sanitize_log_data, SENSITIVE_KEYS

def test_sanitize_sensitive_data():
    raw_data = {"email": "test@test.com", "meta": {"oib": "123", "iban": "HR123"}}
    sanitized = sanitize_log_data(raw_data)
    assert sanitized["email"] == "***MASKED***"
    assert sanitized["meta"]["oib"] == "***MASKED***"

def test_sensitive_keys_completeness():
    for key in ['oib', 'iban', 'jmbg', 'card', 'pin']:
        assert key in SENSITIVE_KEYS

@pytest.mark.asyncio
async def test_budget_shield_activation():
    mock_redis = AsyncMock()
    mock_redis.llen.return_value = 20
    mock_redis.lrange.return_value = [b'{"role":"user","content":"msg"}' for _ in range(20)]
    mock_redis.get.return_value = "6" # Iznad limita

    service = ContextService(mock_redis)
    service._count_tokens = MagicMock(return_value=3000)
    service._generate_summary = AsyncMock()

    await service._enforce_token_limit("ctx:spammer", "spammer")
    service._generate_summary.assert_not_called()

@pytest.mark.asyncio
async def test_normal_budget_behaviour():
    mock_redis = AsyncMock()
    mock_redis.llen.return_value = 20
    mock_redis.lrange.return_value = [b'{"role":"user","content":"msg"}' for _ in range(20)]
    mock_redis.get.return_value = "2" # Ispod limita
    
    # [POPRAVAK] Mockamo pipeline da ne pukne
    mock_redis.pipeline = MagicMock()
    mock_pipeline = MagicMock()
    mock_pipeline.execute = AsyncMock()
    mock_redis.pipeline.return_value.__aenter__.return_value = mock_pipeline

    service = ContextService(mock_redis)
    service._count_tokens = MagicMock(return_value=3000)
    service._generate_summary = AsyncMock(return_value="Summary")

    await service._enforce_token_limit("ctx:normal", "normal")

    service._generate_summary.assert_called()
    
    # [PROVJERA] Tražimo 'incr' metodu u pozivima
    # Ovo će ispisati sve pozive ako padne, pa ćemo vidjeti što se događa
    calls = [str(c) for c in mock_pipeline.method_calls]
    has_incr = any("incr" in c for c in calls)
    
    assert has_incr, f"Nije pozvan incr! Pozvano je: {calls}"