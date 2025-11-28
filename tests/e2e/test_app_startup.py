import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from main import app

def test_app_startup_and_healthcheck():
    """
    Testira podizanje aplikacije i Healthcheck endpoint.
    """
    mock_redis = MagicMock()
    mock_redis.close = AsyncMock()
    
    # [POPRAVAK] Patchamo samo ono što main.py stvarno koristi: Redis i Limiter
    with patch("main.redis.from_url", return_value=mock_redis), \
         patch("main.FastAPILimiter.init", new=AsyncMock()):
        
        with TestClient(app) as client:
            response = client.get("/health")
            
            assert response.status_code == 200
            assert response.json()["status"] == "ok"