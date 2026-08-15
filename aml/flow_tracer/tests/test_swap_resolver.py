# flow_tracer/tests/test_swap_resolver.py
"""Тесты swap_resolver на моках: реального Blockscout/TheGraph здесь не трогаем."""

import pytest
from unittest.mock import AsyncMock, patch

from flow_tracer import swap_resolver
from flow_tracer.swap_resolver import (
    identify_dex_protocol,
    resolve_swap,
    _normalize_v3_swap,
    _normalize_v2_swap,
)


@pytest.fixture(autouse=True)
def patch_configs(monkeypatch):
    """Подменяем загруженные конфиги на управляемые тестовые данные, чтобы не
    зависеть от реального состояния YAML-файлов (там часть значений — плейсхолдеры)."""
    monkeypatch.setattr(swap_resolver, "_DEX_CONTRACTS", {
        56: {"0xrouter": "uniswap_v3"},
    })
    monkeypatch.setattr(swap_resolver, "_DEX_SUBGRAPHS", {
        56: {"uniswap_v3": "real_subgraph_id_123"},
        137: {"uniswap_v3": "ЗАПОЛНИТЬ_ВРУЧНУЮ"},
    })


def test_identify_dex_protocol_known_contract():
    assert identify_dex_protocol(56, "0xROUTER") == "uniswap_v3"


def test_identify_dex_protocol_unknown_contract():
    assert identify_dex_protocol(56, "0xnotarouter") is None


def test_normalize_v3_swap_token_in_out_by_sign():
    raw = {
        "amount0": "100.0", "amount1": "-0.05", "amountUSD": "100.0",
        "sender": "0xs", "recipient": "0xr",
        "pool": {"id": "0xpool", "token0": {"id": "0xusdc"}, "token1": {"id": "0xweth"}},
    }
    normalized = _normalize_v3_swap(raw)
    assert normalized["token_in"] == {"id": "0xusdc"}
    assert normalized["amount_in"] == 100.0
    assert normalized["token_out"] == {"id": "0xweth"}
    assert normalized["amount_out"] == 0.05


def test_normalize_v2_swap_token_in_out():
    raw = {
        "amount0In": "0", "amount1In": "10", "amount0Out": "500", "amount1Out": "0",
        "sender": "0xs", "to": "0xr",
        "pair": {"id": "0xpair", "token0": {"id": "0xusdc"}, "token1": {"id": "0xweth"}},
    }
    normalized = _normalize_v2_swap(raw)
    assert normalized["token_in"] == {"id": "0xweth"}
    assert normalized["amount_in"] == 10.0
    assert normalized["token_out"] == {"id": "0xusdc"}
    assert normalized["amount_out"] == 500.0
    assert normalized["recipient"] == "0xr"


@pytest.mark.asyncio
async def test_resolve_swap_returns_none_for_non_dex_contract():
    with patch("flow_tracer.swap_resolver.get_transaction_summary", new=AsyncMock(
        return_value={"to": {"hash": "0xNotARouter"}}
    )):
        result = await resolve_swap(chain_id=56, tx_hash="0xabc")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_swap_returns_none_when_subgraph_not_configured():
    """Известный DEX-контракт, но subgraph_id для него — незаполненный плейсхолдер."""
    with patch.object(swap_resolver, "_DEX_SUBGRAPHS", {56: {"uniswap_v3": "ЗАПОЛНИТЬ_ВРУЧНУЮ_чтобы_проверить"}}):
        with patch("flow_tracer.swap_resolver.get_transaction_summary", new=AsyncMock(
            return_value={"to": {"hash": "0xRouter"}}
        )):
            result = await resolve_swap(chain_id=56, tx_hash="0xabc")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_swap_calls_thegraph_and_normalizes():
    fake_client = AsyncMock()
    fake_client.query = AsyncMock(return_value={"swaps": [{
        "id": "1", "sender": "0xs", "recipient": "0xr",
        "origin": "0xs", "amount0": "100.0", "amount1": "-0.05", "amountUSD": "100.0",
        "pool": {"id": "0xpool", "token0": {"id": "0xusdc"}, "token1": {"id": "0xweth"}},
    }]})

    with patch("flow_tracer.swap_resolver.get_transaction_summary", new=AsyncMock(
        return_value={"to": {"hash": "0xRouter"}}
    )):
        result = await resolve_swap(chain_id=56, tx_hash="0xabc", client=fake_client)

    assert result is not None
    assert len(result) == 1
    assert result[0]["protocol"] == "uniswap_v3"
    assert result[0]["amount_in"] == 100.0
    fake_client.query.assert_called_once()
    call_kwargs = fake_client.query.call_args
    assert call_kwargs.args[0] == "real_subgraph_id_123"


@pytest.mark.asyncio
async def test_resolve_swap_returns_none_on_empty_swaps_list():
    fake_client = AsyncMock()
    fake_client.query = AsyncMock(return_value={"swaps": []})

    with patch("flow_tracer.swap_resolver.get_transaction_summary", new=AsyncMock(
        return_value={"to": {"hash": "0xRouter"}}
    )):
        result = await resolve_swap(chain_id=56, tx_hash="0xabc", client=fake_client)

    assert result is None
