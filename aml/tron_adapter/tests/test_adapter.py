# tron_adapter/tests/test_adapter.py
"""Офлайн-тесты tron_adapter (адаптер + клиент), по образцу
evm_adapter/tests/test_adapter.py — тот же стиль мокирования."""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock
from tron_adapter import (
    get_account_info,
    get_trc20_transfers,
    get_transaction_info,
    TronGridClient,
    Cache,
)


@pytest.fixture
def mock_cache():
    cache = MagicMock(spec=Cache)
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    return cache


@pytest.fixture
def mock_client():
    client = MagicMock(spec=TronGridClient)
    client._make_request = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_get_account_info_network_success(mock_client, mock_cache):
    mock_client._make_request.return_value = {
        "data": [{"address": "413a08f76772e200653bb55c2a92998daca62e0e97", "balance": 1000}],
        "success": True,
    }

    res = await get_account_info("TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt", client=mock_client, cache=mock_cache)

    assert res["success"] is True
    assert res["_meta"]["cached"] is False
    assert res["_meta"]["source"] == "trongrid_api"
    mock_client._make_request.assert_called_once_with(
        "/v1/accounts/TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt", {}
    )


@pytest.mark.asyncio
async def test_get_account_info_normalizes_hex_address(mock_client, mock_cache):
    """Адрес в 'голом' EVM-hex формате (как отдаёт LayerZero Scan для Tron)
    должен приводиться к base58 ДО обращения к TronGrid."""
    mock_client._make_request.return_value = {"data": [], "success": True}

    await get_account_info("0x3a08f76772e200653bb55c2a92998daca62e0e97", client=mock_client, cache=mock_cache)

    called_endpoint = mock_client._make_request.call_args.args[0]
    assert called_endpoint == "/v1/accounts/TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt"


@pytest.mark.asyncio
async def test_get_trc20_transfers_builds_params(mock_client, mock_cache):
    mock_client._make_request.return_value = {"data": [], "success": True, "meta": {}}

    await get_trc20_transfers(
        address="TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt",
        contract_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        only_from=True,
        limit=100,
        fingerprint="abc123",
        client=mock_client,
        cache=mock_cache,
    )

    endpoint, params = mock_client._make_request.call_args.args
    assert endpoint == "/v1/accounts/TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt/transactions/trc20"
    assert params["only_from"] is True
    assert params["limit"] == 100
    assert params["fingerprint"] == "abc123"
    assert params["contract_address"] == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@pytest.mark.asyncio
async def test_cache_hit_prevents_network_call(mock_client, mock_cache):
    mock_cache.get.return_value = {"data": [], "success": True}

    res = await get_trc20_transfers("TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt", client=mock_client, cache=mock_cache)

    assert res["_meta"]["cached"] is True
    mock_client._make_request.assert_not_called()


@pytest.mark.asyncio
async def test_get_transaction_info_strips_0x_prefix(mock_client, mock_cache):
    mock_client._make_request.return_value = {"ret": [], "txID": "abc"}

    await get_transaction_info(
        "0xcae6f9052cc83b91a4688e83d616ada07c390df64289ae1c88f6b967982ce3d1",
        client=mock_client,
        cache=mock_cache,
    )

    called_endpoint = mock_client._make_request.call_args.args[0]
    assert called_endpoint == "/v1/transactions/cae6f9052cc83b91a4688e83d616ada07c390df64289ae1c88f6b967982ce3d1"


# --- Тесты для TronGridClient напрямую (rate limiting, bool-параметры, masking) ---

@pytest.mark.asyncio
async def test_rate_limiter_enforces_time_based_limit():
    client = TronGridClient(rate_limit=2)

    start = time.monotonic()
    for _ in range(6):
        await client._wait_for_token()
    elapsed = time.monotonic() - start

    assert elapsed >= 1.8, f"Ожидалось не менее ~2с при 2 RPS и 6 запросах, получено {elapsed:.2f}с"


def test_api_key_masked_in_text():
    client = TronGridClient(api_key="trongrid_secret_123456789")
    raw = "Error calling https://api.trongrid.io/v1/x?key=trongrid_secret_123456789"
    masked = client._mask_text(raw)
    assert "trongrid_secret_123456789" not in masked


def test_client_works_without_api_key(monkeypatch):
    """В отличие от BlockscoutClient, TronGridClient не должен требовать
    ключ — TronGrid работает и без него (просто медленнее)."""
    monkeypatch.delenv("TRONGRID_API_KEY", raising=False)
    client = TronGridClient()
    assert client.api_key is None


@pytest.mark.asyncio
async def test_bool_query_params_serialized_as_lowercase_strings():
    """Регрессионный тест: aiohttp/yarl бросает TypeError на bool в query
    params ('value should be str, int or float'). Раньше это ловилось
    только живым запросом к TronGrid — здесь фиксируем офлайн."""
    client = TronGridClient(api_key="testkey")

    captured = {}

    class FakeResponse:
        status = 200

        async def json(self):
            return {"data": [], "success": True}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        closed = False

        def get(self, url, params=None, headers=None):
            captured["params"] = params
            return FakeResponse()

    client._session = FakeSession()

    await client._make_request("/v1/accounts/Tabc/transactions/trc20", {"only_from": True, "only_to": False, "limit": 5})

    assert captured["params"]["only_from"] == "true"
    assert captured["params"]["only_to"] == "false"
    assert captured["params"]["limit"] == 5
