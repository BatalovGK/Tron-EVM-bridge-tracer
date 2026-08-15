# attribution/tests/test_goplus.py
"""
Тесты клиента GoPlus на моках aiohttp — реальная сеть до api.gopluslabs.io
недоступна из песочницы, где это писалось. Поля ответа взяты из официального
примера в документации/SDK (см. docstring attribution/goplus.py).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from attribution.goplus import check_address_goplus


def _mock_response(json_data, status=200):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


@pytest.mark.asyncio
async def test_clean_address_not_risky(monkeypatch):
    monkeypatch.delenv("GOPLUS_API_KEY", raising=False)
    clean_response = {
        "code": 1, "message": "OK",
        "result": {
            "blacklist_doubt": "0", "phishing_activities": "0", "stealing_attack": "0",
            "money_laundering": "0", "financial_crime": "0", "darkweb_transactions": "0",
            "cybercrime": "0", "fake_kyc": "0", "malicious_mining_activities": "0",
            "blackmail_activities": "0", "honeypot_related_address": "0",
        },
    }
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_mock_response(clean_response))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await check_address_goplus("0xClean", chain_id=1)

    assert result["is_risky"] is False
    assert result["risk_flags"] == []


@pytest.mark.asyncio
async def test_risky_address_flags_extracted(monkeypatch):
    monkeypatch.delenv("GOPLUS_API_KEY", raising=False)
    risky_response = {
        "code": 1, "message": "OK",
        "result": {
            "blacklist_doubt": "1", "phishing_activities": "1", "stealing_attack": "0",
            "money_laundering": "1", "financial_crime": "0", "darkweb_transactions": "0",
            "cybercrime": "0", "fake_kyc": "0", "malicious_mining_activities": "0",
            "blackmail_activities": "0", "honeypot_related_address": "0",
        },
    }
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_mock_response(risky_response))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await check_address_goplus("0xRisky", chain_id=1)

    assert result["is_risky"] is True
    assert set(result["risk_flags"]) == {"blacklisted", "phishing", "money_laundering"}


@pytest.mark.asyncio
async def test_missing_fields_default_to_not_risky(monkeypatch):
    """Если GoPlus не вернул какое-то поле вообще — считаем его не сработавшим,
    а не роняем проверку."""
    monkeypatch.delenv("GOPLUS_API_KEY", raising=False)
    sparse_response = {"code": 1, "message": "OK", "result": {}}
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_mock_response(sparse_response))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await check_address_goplus("0xSparse", chain_id=1)

    assert result["is_risky"] is False
