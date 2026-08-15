# evm_adapter/tests/test_adapter.py
"""Unit tests with unit mocking for the EVM Blockscout Pro adapter module."""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock
from evm_adapter import (
    get_address_info,
    get_address_transactions,
    get_token_transfers,
    get_transaction_summary,
    get_block_by_number,
    BlockscoutClient,
    Cache
)
from evm_adapter.client import CreditsExhaustedError

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_secret_key_99999")

@pytest.fixture
def mock_cache():
    cache = MagicMock(spec=Cache)
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    return cache

@pytest.fixture
def mock_client():
    client = MagicMock(spec=BlockscoutClient)
    client._make_request = AsyncMock()
    return client

@pytest.mark.asyncio
async def test_get_address_info_network_success(mock_client, mock_cache):
    mock_response = {
        "hash": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        "is_contract": False,
        "coin_balance": "10000000"
    }
    mock_client._make_request.return_value = mock_response

    res = await get_address_info(1, "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", client=mock_client, cache=mock_cache)

    assert res["hash"] == "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    assert res["_meta"]["cached"] is False
    assert res["_meta"]["chain_id"] == 1
    assert res["_meta"]["source"] == "blockscout_pro_api"
    mock_client._make_request.assert_called_once_with(1, "/addresses/0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", {})

@pytest.mark.asyncio
async def test_get_token_transfers_with_cursor_and_type(mock_client, mock_cache):
    mock_response = {"items": [{"tx_hash": "0x123", "amount": "50"}]}
    mock_client._make_request.return_value = mock_response

    cursor_dict = {"block_number": 22107432, "index": 12, "items_count": 50}

    res = await get_token_transfers(
        chain_id=56,
        address="0xaddress",
        token_standard="ERC-20",
        cursor=cursor_dict,
        limit=25,
        client=mock_client,
        cache=mock_cache
    )

    assert res["items"][0]["tx_hash"] == "0x123"
    mock_client._make_request.assert_called_once_with(
        56,
        "/addresses/0xaddress/token-transfers",
        {"limit": 25, "type": "ERC-20", "block_number": 22107432, "index": 12, "items_count": 50}
    )

@pytest.mark.asyncio
async def test_cache_hit_prevents_network_call(mock_client, mock_cache):
    cached_data = {"hash": "0xabc", "summary": "Contract execution"}
    mock_cache.get.return_value = cached_data

    res = await get_transaction_summary(1, "0xabc", client=mock_client, cache=mock_cache)

    assert res["hash"] == "0xabc"
    assert res["_meta"]["cached"] is True
    mock_client._make_request.assert_not_called()

@pytest.mark.asyncio
async def test_invalid_chain_id_raises_value_error(mock_client, mock_cache):
    with pytest.raises(ValueError, match="не сконфигурирован"):
        await get_address_info(9999, "0xaddress", client=mock_client, cache=mock_cache)

@pytest.mark.asyncio
async def test_block_by_number_cached_forever(mock_client, mock_cache):
    """TTL для блока должен быть 0 (вечный кэш), не 900."""
    mock_response = {"number": 12345, "hash": "0xblockhash"}
    mock_client._make_request.return_value = mock_response

    await get_block_by_number(1, 12345, client=mock_client, cache=mock_cache)

    mock_cache.set.assert_called_once()
    call_args = mock_cache.set.call_args
    assert call_args.args[-1] == 0, "TTL для get_block_by_number должен быть 0 (вечный кэш)"


# --- Тесты для BlockscoutClient напрямую (rate limiting, credits guard, masking) ---

@pytest.mark.asyncio
async def test_credits_exhausted_raises_before_network_call(monkeypatch):
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_secret_123456789")
    client = BlockscoutClient(rate_limit=5)
    client._credits_remaining = 0

    # Подменяем _session, чтобы засечь, был ли вообще сетевой вызов
    fake_session = MagicMock()
    fake_session.get = MagicMock(side_effect=AssertionError("Network should not be called"))
    client._session = fake_session

    with pytest.raises(CreditsExhaustedError):
        await client._make_request(1, "/addresses/0xabc")


@pytest.mark.asyncio
async def test_rate_limiter_enforces_time_based_limit():
    """При лимите 2 запроса/сек и 6 последовательных потреблениях токенов
    суммарное время должно быть не меньше ~2 секунд (6 запросов, 2 сразу доступны,
    остальные 4 по 0.5с)."""
    import os
    os.environ["BLOCKSCOUT_API_KEY"] = "proapi_secret_123456789"
    client = BlockscoutClient(rate_limit=2)

    start = time.monotonic()
    for _ in range(6):
        await client._wait_for_token()
    elapsed = time.monotonic() - start

    assert elapsed >= 1.8, f"Ожидалось не менее ~2с при 2 RPS и 6 запросах, получено {elapsed:.2f}с"


def test_api_key_masked_in_text():
    import os as _os
    _os.environ["BLOCKSCOUT_API_KEY"] = "proapi_secret_123456789"
    client = BlockscoutClient()
    raw = "Error calling https://api.blockscout.com/1/api/v2/x?apikey=proapi_secret_123456789"
    masked = client._mask_text(raw)
    assert "proapi_secret_123456789" not in masked
    assert "proapi_s***" in masked or client._mask_api_key() in masked
