# flow_tracer/tests/test_hop_tracer.py
"""
Тесты алгоритма BFS-обхода на моках всех внешних зависимостей (evm_adapter,
resolve_swap, evidence_store) — сама логика обхода/остановки/taint здесь не
требует реального Postgres или Blockscout. Интеграционная проверка "пишется
ли реально в evidence_log/flow_edges/seed_registry" — на реальном Postgres,
см. common/tests/test_evidence_store.py (там проверены сами функции записи).
"""

import uuid
import pytest
from unittest.mock import AsyncMock, patch

from flow_tracer import hop_tracer


def _tx(from_addr, to_addr, tx_hash, value="1000000000000000000"):
    return {"from": {"hash": from_addr}, "to": {"hash": to_addr}, "hash": tx_hash, "value": value}


@pytest.fixture(autouse=True)
def patch_evidence_store(monkeypatch):
    monkeypatch.setattr(hop_tracer, "write_evidence", AsyncMock())
    monkeypatch.setattr(hop_tracer, "write_flow_edge", AsyncMock())
    monkeypatch.setattr(hop_tracer, "upsert_seed", AsyncMock())
    monkeypatch.setattr(hop_tracer, "get_labels", AsyncMock(return_value=[]))


@pytest.mark.asyncio
async def test_wallet_labeling_mode_skips_trace_entirely():
    result = await hop_tracer.trace_flow("0xstart", chain_id=1, mode="wallet_labeling")
    assert result.visited_addresses == []
    assert result.edges_written == 0


@pytest.mark.asyncio
async def test_simple_one_hop_transfer_incident_response():
    """0xstart -> 0xnext (обычный transfer), дальше 0xnext ничего не отправляет —
    трейс должен остановиться на max_hops, а не зациклиться."""
    with patch("flow_tracer.hop_tracer.get_address_transactions", new=AsyncMock(side_effect=[
        {"items": [_tx("0xstart", "0xnext", "0xtx1")]},
        {"items": []},
    ])), \
         patch("flow_tracer.hop_tracer.get_token_transfers", new=AsyncMock(return_value={"items": []})), \
         patch("flow_tracer.hop_tracer.identify_dex_protocol", return_value=None):

        result = await hop_tracer.trace_flow(
            "0xstart", chain_id=1, mode="incident_response", max_hops=2,
        )

    assert "0xstart" in result.visited_addresses
    assert "0xnext" in result.visited_addresses
    assert result.edges_written == 1
    hop_tracer.write_flow_edge.assert_called_once()
    call_kwargs = hop_tracer.write_flow_edge.call_args.kwargs
    assert call_kwargs["tainted"] is True  # incident_response -> poison-метод


@pytest.mark.asyncio
async def test_aml_check_mode_not_tainted():
    with patch("flow_tracer.hop_tracer.get_address_transactions", new=AsyncMock(side_effect=[
        {"items": [_tx("0xstart", "0xnext", "0xtx1")]},
        {"items": []},
    ])), \
         patch("flow_tracer.hop_tracer.get_token_transfers", new=AsyncMock(return_value={"items": []})), \
         patch("flow_tracer.hop_tracer.identify_dex_protocol", return_value=None):

        await hop_tracer.trace_flow("0xstart", chain_id=1, mode="aml_check")

    call_kwargs = hop_tracer.write_flow_edge.call_args.kwargs
    assert call_kwargs["tainted"] is False


@pytest.mark.asyncio
async def test_max_hops_stops_expansion():
    """max_hops=1: узел на hop_number=1 не должен разворачиваться дальше."""
    call_count = {"n": 0}

    async def fake_txs(chain_id, address, limit):
        call_count["n"] += 1
        if address == "0xstart":
            return {"items": [_tx("0xstart", "0xnext", "0xtx1")]}
        raise AssertionError("Не должно запрашивать транзакции для 0xnext при max_hops=1")

    with patch("flow_tracer.hop_tracer.get_address_transactions", new=fake_txs), \
         patch("flow_tracer.hop_tracer.get_token_transfers", new=AsyncMock(return_value={"items": []})), \
         patch("flow_tracer.hop_tracer.identify_dex_protocol", return_value=None):

        result = await hop_tracer.trace_flow("0xstart", chain_id=1, mode="incident_response", max_hops=1)

    assert {"address": "0xnext", "reason": "max_hops"} in result.terminal_nodes


@pytest.mark.asyncio
async def test_sanctioned_address_stops_expansion_but_not_start_address():
    """Стартовый адрес не останавливаем даже если он сам помечен (это и есть
    расследуемый адрес), но найденный по ходу трейса санкционный адрес — да."""

    async def fake_get_labels(address, chain_id=None):
        if address == "0xsanctioned":
            return [{"source": "ofac_sdn", "label_type": "sanctioned"}]
        return []

    with patch("flow_tracer.hop_tracer.get_address_transactions", new=AsyncMock(side_effect=[
        {"items": [_tx("0xstart", "0xsanctioned", "0xtx1")]},
    ])), \
         patch("flow_tracer.hop_tracer.get_token_transfers", new=AsyncMock(return_value={"items": []})), \
         patch("flow_tracer.hop_tracer.identify_dex_protocol", return_value=None), \
         patch("flow_tracer.hop_tracer.get_labels", new=fake_get_labels):

        result = await hop_tracer.trace_flow("0xstart", chain_id=1, mode="incident_response", max_hops=5)

    assert {"address": "0xsanctioned", "reason": "sanctioned"} in result.terminal_nodes


@pytest.mark.asyncio
async def test_swap_resolved_redirects_to_swap_recipient():
    """Перевод на известный DEX-роутер + успешный резолв свопа с другим
    получателем -> следующий хоп идёт на получателя свопа, не на роутер."""
    with patch("flow_tracer.hop_tracer.get_address_transactions", new=AsyncMock(side_effect=[
        {"items": [_tx("0xstart", "0xrouter", "0xtx1")]},
        {"items": []},
    ])), \
         patch("flow_tracer.hop_tracer.get_token_transfers", new=AsyncMock(return_value={"items": []})), \
         patch("flow_tracer.hop_tracer.identify_dex_protocol", return_value="uniswap_v3"), \
         patch("flow_tracer.hop_tracer.resolve_swap", new=AsyncMock(return_value=[{
             "recipient": "0xfinal",
             "token_out": {"id": "0xweth", "symbol": "WETH", "decimals": 18},
             "amount_out": 1.5,
         }])):

        result = await hop_tracer.trace_flow("0xstart", chain_id=1, mode="incident_response", max_hops=2)

    assert "0xfinal" in result.visited_addresses
    assert "0xrouter" not in result.visited_addresses
    call_kwargs = hop_tracer.write_flow_edge.call_args_list[0].kwargs
    assert call_kwargs["child_address"] == "0xfinal"
    assert call_kwargs["edge_kind"] == "swap"
    # Регрессия: token_out/amount_out из swap_resolver — dict/float, а колонка
    # flow_edges.token/.amount в БД — TEXT. write_flow_edge должен получать
    # уже нормализованные примитивы, иначе asyncpg упадёт на реальном Postgres.
    assert call_kwargs["token"] == "WETH"
    assert call_kwargs["amount"] == "1.5"
    assert isinstance(call_kwargs["token"], str)
    assert isinstance(call_kwargs["amount"], str)


@pytest.mark.asyncio
async def test_swap_unresolved_marks_edge_kind_and_stops_at_router():
    """Известный DEX, но resolve_swap вернул None (subgraph не настроен) —
    ребро помечается swap_unresolved, хоп продолжается на сам роутер как на
    тупиковый узел (см. ограничение в docstring модуля)."""
    with patch("flow_tracer.hop_tracer.get_address_transactions", new=AsyncMock(side_effect=[
        {"items": [_tx("0xstart", "0xrouter", "0xtx1")]},
        {"items": []},
    ])), \
         patch("flow_tracer.hop_tracer.get_token_transfers", new=AsyncMock(return_value={"items": []})), \
         patch("flow_tracer.hop_tracer.identify_dex_protocol", return_value="uniswap_v3"), \
         patch("flow_tracer.hop_tracer.resolve_swap", new=AsyncMock(return_value=None)):

        result = await hop_tracer.trace_flow("0xstart", chain_id=1, mode="incident_response", max_hops=2)

    call_kwargs = hop_tracer.write_flow_edge.call_args_list[0].kwargs
    assert call_kwargs["edge_kind"] == "swap_unresolved"
    assert call_kwargs["child_address"] == "0xrouter"


@pytest.mark.asyncio
async def test_incident_response_writes_to_seed_registry():
    with patch("flow_tracer.hop_tracer.get_address_transactions", new=AsyncMock(side_effect=[
        {"items": [_tx("0xstart", "0xnext", "0xtx1")]},
        {"items": []},
    ])), \
         patch("flow_tracer.hop_tracer.get_token_transfers", new=AsyncMock(return_value={"items": []})), \
         patch("flow_tracer.hop_tracer.identify_dex_protocol", return_value=None):

        await hop_tracer.trace_flow("0xstart", chain_id=1, mode="incident_response", max_hops=2)

    hop_tracer.upsert_seed.assert_called_once()
    seed_kwargs = hop_tracer.upsert_seed.call_args.kwargs
    assert seed_kwargs["address"] == "0xnext"
    assert seed_kwargs["derived_from"] == "0xstart"


@pytest.mark.asyncio
async def test_aml_check_does_not_write_to_seed_registry():
    with patch("flow_tracer.hop_tracer.get_address_transactions", new=AsyncMock(side_effect=[
        {"items": [_tx("0xstart", "0xnext", "0xtx1")]},
        {"items": []},
    ])), \
         patch("flow_tracer.hop_tracer.get_token_transfers", new=AsyncMock(return_value={"items": []})), \
         patch("flow_tracer.hop_tracer.identify_dex_protocol", return_value=None):

        await hop_tracer.trace_flow("0xstart", chain_id=1, mode="aml_check", max_hops=1)

    hop_tracer.upsert_seed.assert_not_called()


@pytest.mark.asyncio
async def test_investigation_id_generated_when_not_provided():
    result = await hop_tracer.trace_flow("0xstart", chain_id=1, mode="wallet_labeling")
    assert isinstance(result.investigation_id, uuid.UUID)


@pytest.mark.asyncio
async def test_write_to_seed_registry_false_disables_writes_even_for_incident_response():
    with patch("flow_tracer.hop_tracer.get_address_transactions", new=AsyncMock(side_effect=[
        {"items": [_tx("0xstart", "0xnext", "0xtx1")]},
        {"items": []},
    ])), \
         patch("flow_tracer.hop_tracer.get_token_transfers", new=AsyncMock(return_value={"items": []})), \
         patch("flow_tracer.hop_tracer.identify_dex_protocol", return_value=None):

        await hop_tracer.trace_flow(
            "0xstart", chain_id=1, mode="incident_response", max_hops=2,
            write_to_seed_registry=False,
        )

    hop_tracer.upsert_seed.assert_not_called()
