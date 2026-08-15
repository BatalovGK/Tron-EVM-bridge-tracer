"""
Офлайн-тесты bridge_tracer.py: TronGrid/LayerZero/Blockscout подменены
моками, повторяющими реальные схемы ответов (TronGrid trc20-transfers и
LayerZero Scan — проверены живыми запросами в этой сессии, см.
layerzero_tracer.py и tron_adapter/README.md; схема Blockscout Pro
token-transfers — проверена живым запросом к публичному eth.blockscout.com,
см. docstring bridge_tracer.py), чтобы проверить логику склейки без сети.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import bridge_tracer as bt

USDT0_OFT_TRON = "TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt"
TRON_SENDER = "TUPBaiCzVjnQdjSEwLgGNDeYqTEfuzigyj"
BINANCE_ETH = "0xf977814e90da44bfa03b6295a0616a897441acec"
UNISWAP_ETH = "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad"


def _tron_deposit_response(to_addr=USDT0_OFT_TRON, tx_id="cae6f9052cc83b91a4688e83d616ada07c390df64289ae1c88f6b967982ce3d1"):
    return {
        "data": [
            {
                "transaction_id": tx_id,
                "token_info": {"symbol": "USDT", "address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "decimals": 6},
                "block_timestamp": 1786746798000,
                "from": TRON_SENDER,
                "to": to_addr,
                "type": "Transfer",
                "value": "51282849736",
            }
        ]
    }


def _crossing_delivered(to_address="0x000000000000000000000000000000000000aa"):
    return {
        "found": True,
        "confidence": "HIGH",
        "guid": "0xguid",
        "message_status": "DELIVERED",
        "bridge_entry": {
            "chain": "Tron", "eid": 30420,
            "tx_hash": "0xcae6f9052cc83b91a4688e83d616ada07c390df64289ae1c88f6b967982ce3d1",
            "from_address": TRON_SENDER, "timestamp": 1786746798, "source_status": "SUCCEEDED",
        },
        "bridge_exit": {
            "chain": "Ethereum", "eid": 30101,
            "tx_hash": "0x67234ac732aa2d69fba744a4e132903b13ba8484acda1c3d213f2d4d8eea1b05",
            "to_address": to_address, "timestamp": 1786746923,
            "status": "DELIVERED", "raw_status": "SUCCEEDED",
        },
        "note": None,
    }


def _crossing_inflight():
    d = _crossing_delivered()
    d["message_status"] = "INFLIGHT"
    d["bridge_exit"]["tx_hash"] = None
    d["bridge_exit"]["status"] = "NOT DELIVERED YET"
    d["note"] = "Сообщение отправлено и проходит верификацию DVN..."
    return d


def _evm_transfer_item(from_addr, to_addr, tx_hash="0xhop1"):
    return {
        "from": {"hash": from_addr}, "to": {"hash": to_addr},
        "transaction_hash": tx_hash,
        "total": {"value": "1000000", "decimals": "6"},
        "token": {"symbol": "USDT"},
        "timestamp": "2026-08-15T01:00:00.000000Z",
    }


def _run(coro):
    return asyncio.run(coro)


def _patched(tron_data=None, crossing=None, evm_pages=None):
    """evm_pages: dict address(lower) -> {"items": [...]}."""
    m_tron = AsyncMock(return_value=tron_data)
    m_cross = MagicMock(return_value=crossing)

    async def _evm_side_effect(chain_id, address, token_standard=None, limit=None, **kw):
        return (evm_pages or {}).get(address.lower(), {"items": []})

    m_evm = AsyncMock(side_effect=_evm_side_effect)

    return patch.object(bt, "get_trc20_transfers", m_tron), \
        patch.object(bt.lz, "find_bridge_crossing", m_cross), \
        patch.object(bt, "get_token_transfers", m_evm)


def test_full_path_rests_at_exchange():
    recipient = BINANCE_ETH
    p1, p2, p3 = _patched(
        tron_data=_tron_deposit_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages={},
    )
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER, start_type="address"))

    assert result["final_status"] == "RESTED_AT_EXCHANGE"
    assert result["exchange_name"] == "Binance"
    assert result["final_chain"] == "Ethereum"
    assert result["final_address"] == recipient
    assert len(result["hops"]) == 2  # tron_deposit + bridge, no EVM hops (stopped immediately)
    assert result["hops"][0]["segment"] == "tron_deposit"
    assert result["hops"][1]["segment"] == "bridge"
    print("test_full_path_rests_at_exchange: OK")


def test_full_path_rests_at_dex_contract():
    recipient = "0x1111111111111111111111111111111111111a"
    p1, p2, p3 = _patched(
        tron_data=_tron_deposit_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages={recipient: {"items": [_evm_transfer_item(recipient, UNISWAP_ETH)]}},
    )
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "RESTED_AT_CONTRACT"
    assert result["contract_label"] == "Uniswap Universal Router"
    assert result["contract_type"] == "dex_swap"
    assert result["final_address"] == UNISWAP_ETH
    assert result["hops"][-1]["segment"] == "evm_hop"
    print("test_full_path_rests_at_dex_contract: OK")


def test_full_path_dead_end():
    recipient = "0x2222222222222222222222222222222222222b"
    p1, p2, p3 = _patched(
        tron_data=_tron_deposit_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages={},  # no outgoing transfers anywhere -> immediate dead end
    )
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "RESTED_AT_ADDRESS"
    assert result["final_address"] == recipient
    assert result["exchange_name"] is None
    assert result["contract_label"] is None
    print("test_full_path_dead_end: OK")


def test_full_path_max_hops_reached():
    addrs = [f"0x{i:040x}" for i in range(1, 6)]  # 5 addresses forming a chain
    recipient = addrs[0]
    pages = {addrs[i]: {"items": [_evm_transfer_item(addrs[i], addrs[i + 1], tx_hash=f"0xhop{i}")]}
              for i in range(len(addrs) - 1)}
    # last address also has an outgoing transfer, to guarantee max_hops (not dead end) triggers first
    pages[addrs[-1]] = {"items": [_evm_transfer_item(addrs[-1], "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")]}

    p1, p2, p3 = _patched(
        tron_data=_tron_deposit_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages=pages,
    )
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER, max_hops=3))

    assert result["final_status"] == "MAX_HOPS_REACHED"
    assert len([h for h in result["hops"] if h["segment"] == "evm_hop"]) == 3
    print("test_full_path_max_hops_reached: OK")


def test_full_path_stops_on_same_tx_multi_leg_swap():
    """Реальный случай, обнаруженный живым запуском: несколько Transfer-логов
    внутри ОДНОЙ tx (несколько legs свопа/арбитража через 2+ контракта) не
    должны читаться как отдельные последующие хопы — трейс должен
    остановиться на первом повторении tx_hash, а не зациклиться."""
    addr_a = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"
    addr_b = "0xd02bb053d506e31f91285fa9dc7c9e58179c17ee"
    recipient = "0x3333333333333333333333333333333333333c"
    shared_tx = "0xe327f75591a755ea072df754d2939d44867676a5c1429027a6193abd3b7dd9b5"
    pages = {
        recipient: {"items": [_evm_transfer_item(recipient, addr_a, tx_hash="0xhop1")]},
        addr_a: {"items": [_evm_transfer_item(addr_a, addr_b, tx_hash=shared_tx)]},
        addr_b: {"items": [_evm_transfer_item(addr_b, addr_a, tx_hash=shared_tx)]},
    }
    p1, p2, p3 = _patched(
        tron_data=_tron_deposit_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages=pages,
    )
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER, max_hops=10))

    assert result["final_status"] == "RESTED_AT_CONTRACT"
    assert result["final_address"] == addr_b
    assert result["contract_type"] == "dex_swap"
    evm_hops = [h for h in result["hops"] if h["segment"] == "evm_hop"]
    assert len(evm_hops) == 2  # recipient->addr_a, then addr_a->addr_b — stops before looping back to addr_a
    print("test_full_path_stops_on_same_tx_multi_leg_swap: OK")


def test_full_path_stops_on_burn_to_zero_address():
    """Реальный случай, обнаруженный живым запуском с платным ключом Blockscout
    Pro: Transfer(from, 0x0, amount) — burn — не должен читаться как хоп на
    реального следующего получателя. 0x0 не принадлежит никому: "исходящий
    перевод FROM 0x0" — это ЛЮБОЙ mint любого токена на сети, никак не
    связанный с нашими средствами."""
    recipient = "0x4444444444444444444444444444444444444d"
    zero = "0x" + "0" * 40
    pages = {recipient: {"items": [_evm_transfer_item(recipient, zero, tx_hash="0xburn1")]}}
    p1, p2, p3 = _patched(
        tron_data=_tron_deposit_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages=pages,
    )
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER, max_hops=10))

    assert result["final_status"] == "RESTED_AT_ADDRESS"
    assert result["final_address"] == zero
    assert result["contract_type"] == "burn"
    evm_hops = [h for h in result["hops"] if h["segment"] == "evm_hop"]
    assert len(evm_hops) == 1  # records the burn hop itself, then stops — never queries FROM 0x0
    print("test_full_path_stops_on_burn_to_zero_address: OK")


def test_no_bridge_deposit_found():
    p1, p2, p3 = _patched(tron_data={"data": []}, crossing=None, evm_pages={})
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "NO_BRIDGE_DEPOSIT_FOUND"
    assert result["hops"] == []
    print("test_no_bridge_deposit_found: OK")


def test_bridge_message_not_found():
    p1, p2, p3 = _patched(
        tron_data=_tron_deposit_response(),
        crossing={"found": False, "confidence": "UNRESOLVED", "note": "не найдено"},
        evm_pages={},
    )
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "BRIDGE_MESSAGE_NOT_FOUND"
    assert len(result["hops"]) == 1  # only the tron_deposit hop
    print("test_bridge_message_not_found: OK")


def test_in_transit_not_delivered():
    p1, p2, p3 = _patched(
        tron_data=_tron_deposit_response(),
        crossing=_crossing_inflight(),
        evm_pages={},
    )
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "IN_TRANSIT"
    assert result["final_tx_hash"] is None
    print("test_in_transit_not_delivered: OK")


def test_non_evm_destination_stops_cleanly():
    solana_crossing = _crossing_delivered(to_address="SoLanaRecipientAddr111111111111111111111")
    solana_crossing["bridge_exit"]["eid"] = 30168  # Solana
    solana_crossing["bridge_exit"]["chain"] = "Solana"
    p1, p2, p3 = _patched(tron_data=_tron_deposit_response(), crossing=solana_crossing, evm_pages={})
    with p1, p2, p3:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "RESTED_AT_ADDRESS"
    assert result["final_chain"] == "Solana"
    assert "не является EVM" in result["note"]
    print("test_non_evm_destination_stops_cleanly: OK")


def test_start_type_tx_hash_skips_tron_step():
    recipient = BINANCE_ETH
    p1, p2, p3 = _patched(
        tron_data=None,  # should never be called
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages={},
    )
    with p1, p2, p3:
        result = _run(bt.trace_full_path("0xcae6f9052cc8...", start_type="tx_hash"))

    assert result["final_status"] == "RESTED_AT_EXCHANGE"
    assert len(result["hops"]) == 1  # only the bridge hop, no tron_deposit hop
    assert result["hops"][0]["segment"] == "bridge"
    print("test_start_type_tx_hash_skips_tron_step: OK")


if __name__ == "__main__":
    test_full_path_rests_at_exchange()
    test_full_path_rests_at_dex_contract()
    test_full_path_dead_end()
    test_full_path_max_hops_reached()
    test_full_path_stops_on_same_tx_multi_leg_swap()
    test_full_path_stops_on_burn_to_zero_address()
    test_no_bridge_deposit_found()
    test_bridge_message_not_found()
    test_in_transit_not_delivered()
    test_non_evm_destination_stops_cleanly()
    test_start_type_tx_hash_skips_tron_step()
    print("\nВСЕ ОФЛАЙН-ТЕСТЫ bridge_tracer.py ПРОЙДЕНЫ")
