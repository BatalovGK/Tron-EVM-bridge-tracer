"""
Офлайн-тесты bridge_tracer.py: LayerZero Scan/Blockscout подменены моками,
повторяющими реальные схемы ответов (проверены живыми запросами в этой
сессии — см. layerzero_tracer.py и docstring bridge_tracer.py), чтобы
проверить логику склейки без сети.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bridge_tracer as bt

USDT0_OFT_TRON = "TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt"
USDT0_OFT_HEX = "0x" + bt.base58_to_hex(USDT0_OFT_TRON)
TRON_SENDER = "TUPBaiCzVjnQdjSEwLgGNDeYqTEfuzigyj"
TRON_SENDER_HEX = "0x" + bt.base58_to_hex(TRON_SENDER)
BINANCE_ETH = "0xf977814e90da44bfa03b6295a0616a897441acec"
UNISWAP_ETH = "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad"


def _wallet_messages_response(
    sender_hex=USDT0_OFT_HEX,
    from_hex=TRON_SENDER_HEX,
    tx_hash="0xcae6f9052cc83b91a4688e83d616ada07c390df64289ae1c88f6b967982ce3d1",
    src_eid=30420,
):
    """Синтетический ответ layerzero_tracer.find_messages_by_wallet() — по
    реальной схеме /v1/messages/wallet/{addr}, проверенной живым запросом.
    from_hex — source.tx.from (настоящий отправитель, каким бы путём —
    прямым или через промежуточный router-контракт — токены ни дошли до
    sender_hex/OApp): именно это поле теперь определяет депозит, а не
    прямой TRC-20 Transfer на известный контракт (см. историю в
    _find_tron_bridge_deposit)."""
    return [
        {
            "pathway": {
                "srcEid": src_eid, "dstEid": 30101,
                "sender": {"address": sender_hex, "chain": "tron"},
            },
            "source": {"tx": {"txHash": tx_hash, "from": from_hex, "blockTimestamp": 1786746798}},
        }
    ]


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
            "compose_status": "N/A", "compose_tx_hash": None,  # обычное сообщение, без Legacy Mesh hop2
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


def _crossing_compose_failed(to_address="0x000000000000000000000000000000000000aa"):
    """Legacy Mesh Hop 1 доставлен на хаб, но compose (Hop 2) НЕ исполнился
    успешно — реальный случай, найденный живым запуском (tx
    0x8f1497875d07…3f77f0, compose_status=SIMULATION_REVERTED). Отличие от
    _crossing_delivered(): compose_status присутствует и НЕ "N/A", но
    compose_tx_hash всё равно None (в отличие от обычного "нет Hop 2 вовсе",
    где compose_status == "N/A")."""
    d = _crossing_delivered(to_address=to_address)
    d["bridge_exit"]["compose_status"] = "SIMULATION_REVERTED"
    d["bridge_exit"]["compose_tx_hash"] = None
    return d


def _evm_transfer_item(from_addr, to_addr, tx_hash="0xhop1"):
    return {
        "from": {"hash": from_addr}, "to": {"hash": to_addr},
        "transaction_hash": tx_hash,
        "total": {"value": "1000000", "decimals": "6"},
        "token": {"symbol": "USDT"},
        "timestamp": "2026-08-15T01:00:00.000000Z",
    }


def _tron_transfer_item(from_addr, to_addr, tx_hash, block_timestamp_ms=1786800951000):
    """Синтетический элемент TronGrid /v1/accounts/{addr}/transactions/trc20
    — по реальной схеме, проверенной живым запросом в этой сессии (см.
    bridge_registry.py)."""
    return {
        "transaction_id": tx_hash,
        "token_info": {"symbol": "USDT", "address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                        "decimals": 6, "name": "Tether USD"},
        "block_timestamp": block_timestamp_ms,
        "from": from_addr,
        "to": to_addr,
        "type": "Transfer",
        "value": "1000000",
    }


def _run(coro):
    return asyncio.run(coro)


DEFAULT_TRON_REGISTRY = [
    {"address": USDT0_OFT_TRON, "chain_key": "Tron", "chain_id": None, "lz_eid": 30420,
     "protocol": "USDT0", "contract_role": "OFT", "type": "official_oft",
     "source": "usdt0_deployments_api", "verified_at": "2026-08-15", "evidence": [], "verification_note": None},
    {"address": "TWPziSAroSacAjDuL52ByQzU86s9mP2gPr", "chain_key": "Tron", "chain_id": None, "lz_eid": 30420,
     "protocol": "USDT0", "contract_role": "OftBridge (pool/router)", "type": "pool_router",
     "source": "empirical_verified", "verified_at": "2026-08-15",
     "evidence": ["https://tronscan.org/#/transaction/800fd19ab2b8ee417b999dad7943a2dc7d7d08b75597bae103ac91a332182b35"],
     "verification_note": "test fixture, mirrors bridge_registry.EMPIRICAL_VERIFIED_ENTRIES"},
]


def _patched(wallet_messages=None, crossing=None, evm_pages=None, tron_contracts=None,
             tron_registry=None, tron_trc20_transfers=None, evm_registry=None):
    """
    evm_pages: dict address(lower) -> {"items": [...]} (одна страница,
        поведение как раньше — cursor игнорируется, всегда одна и та же
        страница), ИЛИ dict address(lower) -> [page1, page2, ...] (список
        страниц — каждый следующий вызов get_token_transfers для этого
        адреса отдаёт следующий элемент списка по порядку, имитируя
        cursor-пагинацию; полезно для тестов SEARCH_DEPTH_EXCEEDED и
        "совпадение с реестром только на N-й странице"). Определяется по
        типу значения (list vs dict) — не нужно указывать явно, какой формат.
    crossing: либо один результат find_bridge_crossing() (всегда один и тот
        же ответ независимо от tx_hash — обычный случай), либо dict
        {tx_hash: результат} для тестов Legacy Mesh hop2-chase, где
        find_bridge_crossing() вызывается несколько раз подряд на РАЗНЫЕ
        tx_hash (Hop 1, затем compose_tx_hash Hop 2) и должен возвращать
        разное на каждый вызов.
    tron_contracts: словарь base58-адрес -> название для
        get_tron_oft_contracts() (по умолчанию — только настоящий USDT0 OFT;
        используется LayerZero-fallback путём).
    tron_registry: список записей bridge_registry.get_registry_for_tron()
        (по умолчанию — DEFAULT_TRON_REGISTRY, official_oft + pool_router).
    tron_trc20_transfers: сырой ответ get_trc20_transfers() (по умолчанию —
        {"data": []}, то есть TronGrid-путь ничего не находит и ВСЕГДА
        проваливается в LayerZero-fallback — так все существующие тесты,
        которые настраивают только wallet_messages/crossing, автоматически
        остаются тестами fallback-пути, не требуя правки).
    evm_registry: список записей bridge_registry.get_registry_for_evm_chain_id()
        (по умолчанию — [], то есть _walk_evm сверяется только с
        known_contracts.py, как и раньше — существующие тесты не требуют правки).
    """
    m_wallet = MagicMock(return_value=wallet_messages if wallet_messages is not None else [])
    m_tron_contracts = MagicMock(
        return_value=tron_contracts if tron_contracts is not None else {USDT0_OFT_TRON: "USDT0 OFT (Tron)"}
    )
    m_tron_registry = MagicMock(return_value=tron_registry if tron_registry is not None else DEFAULT_TRON_REGISTRY)
    m_evm_registry = MagicMock(return_value=evm_registry if evm_registry is not None else [])
    m_trc20 = AsyncMock(
        return_value=tron_trc20_transfers if tron_trc20_transfers is not None else {"data": [], "success": True}
    )

    if isinstance(crossing, dict) and "found" not in crossing:
        # tx_hash -> результат (multi-leg chase)
        def _cross_side_effect(tx_hash, testnet=False):
            return crossing[tx_hash]
        m_cross = MagicMock(side_effect=_cross_side_effect)
    else:
        m_cross = MagicMock(return_value=crossing)

    _evm_page_call_counts: dict[str, int] = {}

    async def _evm_side_effect(chain_id, address, token_standard=None, limit=None, cursor=None, **kw):
        key = address.lower()
        pages = (evm_pages or {}).get(key, {"items": []})
        if isinstance(pages, list):
            idx = _evm_page_call_counts.get(key, 0)
            _evm_page_call_counts[key] = idx + 1
            return pages[idx] if idx < len(pages) else {"items": []}
        return pages

    m_evm = AsyncMock(side_effect=_evm_side_effect)

    return patch.object(bt.lz, "find_messages_by_wallet", m_wallet), \
        patch.object(bt.lz, "find_bridge_crossing", m_cross), \
        patch.object(bt, "get_token_transfers", m_evm), \
        patch.object(bt, "get_tron_oft_contracts", m_tron_contracts), \
        patch.object(bt.bridge_registry, "get_registry_for_tron", m_tron_registry), \
        patch.object(bt.bridge_registry, "get_registry_for_evm_chain_id", m_evm_registry), \
        patch.object(bt, "get_trc20_transfers", m_trc20)


def test_full_path_rests_at_exchange():
    recipient = BINANCE_ETH
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages={},
    )
    with p1, p2, p3, p4, p5, p6, p7:
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
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages={recipient: {"items": [_evm_transfer_item(recipient, UNISWAP_ETH)]}},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "RESTED_AT_CONTRACT"
    assert result["contract_label"] == "Uniswap Universal Router"
    assert result["contract_type"] == "dex_swap"
    assert result["final_address"] == UNISWAP_ETH
    assert result["hops"][-1]["segment"] == "evm_hop"
    print("test_full_path_rests_at_dex_contract: OK")


def test_full_path_dead_end():
    recipient = "0x2222222222222222222222222222222222222b"
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages={},  # no outgoing transfers anywhere -> immediate dead end
    )
    with p1, p2, p3, p4, p5, p6, p7:
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

    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages=pages,
    )
    with p1, p2, p3, p4, p5, p6, p7:
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
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages=pages,
    )
    with p1, p2, p3, p4, p5, p6, p7:
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
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages=pages,
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER, max_hops=10))

    assert result["final_status"] == "RESTED_AT_ADDRESS"
    assert result["final_address"] == zero
    assert result["contract_type"] == "burn"
    evm_hops = [h for h in result["hops"] if h["segment"] == "evm_hop"]
    assert len(evm_hops) == 1  # records the burn hop itself, then stops — never queries FROM 0x0
    print("test_full_path_stops_on_burn_to_zero_address: OK")


def test_tron_deposit_uses_real_sender_not_shared_oapp_address():
    """Реальный случай, обнаруженный пользователем на живых данных: LayerZero
    Scan показывает pathway.sender.address = адрес OApp-контракта (общий для
    ВСЕХ отправителей через USDT0), а не настоящего депозитора — то же самое
    искажение, что и с получателем (см. layerzero_tracer._decode_oft_recipient),
    только на стороне source. source.tx.from — настоящий отправитель,
    независимо от того, сколько промежуточных router-контрактов было между
    ним и OApp (это и была причина ложноотрицательного NO_BRIDGE_DEPOSIT_FOUND
    на реальном Tron -> Arbitrum переводе)."""
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(sender_hex=USDT0_OFT_HEX, from_hex=TRON_SENDER_HEX),
        crossing=_crossing_delivered(),
        evm_pages={},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))

    deposit_hop = result["hops"][0]
    assert deposit_hop["segment"] == "tron_deposit"
    assert deposit_hop["from_address"] == TRON_SENDER_HEX
    assert deposit_hop["from_address"] != USDT0_OFT_HEX  # не спутали с общим OApp-контрактом
    assert deposit_hop["oapp"] == "USDT0 OFT (Tron)"
    print("test_tron_deposit_uses_real_sender_not_shared_oapp_address: OK")


def test_tron_deposit_found_via_trongrid_official_oft():
    """TronGrid-путь (ОСНОВНОЙ, см. историю в bridge_tracer._find_tron_bridge_deposit
    и провенанс в bridge_registry.py): исходящий TRC-20 Transfer напрямую на
    официальный OFT-адрес из реестра должен находиться БЕЗ обращения к
    LayerZero Scan messages/wallet (fallback вообще не должен вызываться —
    TronGrid быстрее и является приоритетным путём)."""
    trongrid_tx = "0xtrongridtx1"
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=None,  # не должен быть вызван — TronGrid находит депозит первым
        crossing=_crossing_delivered(),
        evm_pages={},
        tron_trc20_transfers={"data": [_tron_transfer_item(TRON_SENDER, USDT0_OFT_TRON, trongrid_tx)]},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))
        bt.lz.find_messages_by_wallet.assert_not_called()

    deposit_hop = result["hops"][0]
    assert deposit_hop["segment"] == "tron_deposit"
    assert deposit_hop["tx_hash"] == trongrid_tx
    assert deposit_hop["from_address"] == TRON_SENDER
    assert deposit_hop["detection_method"] == "trongrid_registry_match"
    assert deposit_hop["registry_entry_type"] == "official_oft"
    assert deposit_hop["registry_source"] == "usdt0_deployments_api"
    print("test_tron_deposit_found_via_trongrid_official_oft: OK")


def test_tron_deposit_found_via_trongrid_pool_router():
    """TronGrid-путь распознаёт депозит и через эмпирически подтверждённый
    pool/router-контракт (не только официальный OFT) — реестр расширяемый
    список, а не единственный жёстко заданный адрес (см. историю
    ложноотрицательного случая, из-за которого этот тип записи и появился,
    в bridge_registry.py)."""
    trongrid_tx = "0xtrongridtx2"
    pool_router = "TWPziSAroSacAjDuL52ByQzU86s9mP2gPr"
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=None,  # не должен быть вызван
        crossing=_crossing_delivered(),
        evm_pages={},
        tron_trc20_transfers={"data": [_tron_transfer_item(TRON_SENDER, pool_router, trongrid_tx)]},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))
        bt.lz.find_messages_by_wallet.assert_not_called()

    deposit_hop = result["hops"][0]
    assert deposit_hop["tx_hash"] == trongrid_tx
    assert deposit_hop["detection_method"] == "trongrid_registry_match"
    assert deposit_hop["registry_entry_type"] == "pool_router"
    assert deposit_hop["registry_source"] == "empirical_verified"
    print("test_tron_deposit_found_via_trongrid_pool_router: OK")


def test_trongrid_miss_falls_back_to_layerzero_scan():
    """Если ни один исходящий TRC-20 Transfer адреса не совпал с реестром
    (например, перевод через ещё не размеченный router-контракт), должен
    сработать fallback на LayerZero Scan messages/wallet — тот путь, который
    был единственным механизмом до появления TronGrid-пути (см. историю)."""
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_delivered(),
        evm_pages={},
        tron_trc20_transfers={"data": [
            _tron_transfer_item(TRON_SENDER, "TSomeUnknownRouterNotInRegistry1111", "0xnotmatching")
        ]},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))
        bt.lz.find_messages_by_wallet.assert_called_once()

    assert result["hops"][0]["segment"] == "tron_deposit"
    assert result["hops"][0]["detection_method"] == "layerzero_scan_wallet_fallback"
    print("test_trongrid_miss_falls_back_to_layerzero_scan: OK")


def test_trongrid_error_falls_back_to_layerzero_scan():
    """Сетевая ошибка/недоступность TronGrid (или USDT0 Deployments API) не
    должна ронять весь трейс — должен сработать тот же fallback, что и при
    простом отсутствии совпадения."""
    async def _raise(*a, **kw):
        raise RuntimeError("TronGrid недоступен (симулировано в тесте)")

    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_delivered(),
        evm_pages={},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        bt.get_trc20_transfers.side_effect = _raise
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["hops"][0]["detection_method"] == "layerzero_scan_wallet_fallback"
    print("test_trongrid_error_falls_back_to_layerzero_scan: OK")


def test_unknown_oapp_sender_treated_as_no_deposit():
    """Сообщение от адреса есть, но через OApp вне реестра
    TRON_BRIDGE_DEPOSIT_CONTRACTS (не USDT0) — вне scope MVP, должно
    читаться как отсутствие депозита, а не ложное совпадение."""
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(sender_hex="0x" + "ab" * 20),
        crossing=_crossing_delivered(),
        evm_pages={},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "NO_BRIDGE_DEPOSIT_FOUND"
    print("test_unknown_oapp_sender_treated_as_no_deposit: OK")


def test_incoming_message_to_tron_not_mistaken_for_deposit():
    """messages/wallet может вернуть и сообщения, где адрес — ПОЛУЧАТЕЛЬ на
    Tron (destination с другой сети), а не отправитель. srcEid != 30420
    должен быть отфильтрован, а не принят за исходящий депозит."""
    incoming = _wallet_messages_response(src_eid=30101)  # Ethereum -> Tron
    p1, p2, p3, p4, p5, p6, p7 = _patched(wallet_messages=incoming, crossing=_crossing_delivered(), evm_pages={})
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "NO_BRIDGE_DEPOSIT_FOUND"
    print("test_incoming_message_to_tron_not_mistaken_for_deposit: OK")


def test_full_path_chases_legacy_mesh_hop2():
    """Legacy Mesh (USDT0 Transfer Hub через Arbitrum как хаб): Hop 1 может
    доставиться на промежуточный хаб и автоматически инициировать Hop 2 через
    LayerZero compose — bridge_exit.compose_tx_hash сигнализирует об этом.
    Трейсер должен дочитать Hop 2 через повторный find_bridge_crossing(), а
    не остановиться на хабе как на финальной точке выхода из моста.

    Синтетические данные ниже смоделированы под структуру реального случая —
    механизм подтверждён живыми запросами именно на переводах С TRON (не
    только гипотетически): среди ~500 сообщений OApp USDT0-на-Tron нашлось
    30 реальных composed-переводов Tron -> Arbitrum -> дальше, например tx
    0x9574bb18862313c80b4d5476d75170ea567f76d038386d3af77c0cdc1d540050
    (-> Arbitrum, compose) -> compose-tx 0x1155a198e1ab7270f43841976946a728
    10661dd85be617e229d18288a663c917 (-> xlayer, DELIVERED) — прогнано и
    через find_bridge_crossing(), и через полный trace_full_path() на
    настоящем Tron-адресе отправителя (TG2ZWBeQtyqgBjbZjS4Y4rxqhBaHwCqq9q),
    см. историю сессии. См. также layerzero_tracer.find_bridge_crossing()
    докстринг для второго реального примера (Hop 2 на Polygon)."""
    hop1_tx = "0xhop1tx"
    hop2_tx = "0xhop2tx"
    final_recipient = "0x5555555555555555555555555555555555555e"

    hop1_crossing = _crossing_delivered(to_address="0xarbitrumhuboapp0000000000000000000000000")
    hop1_crossing["bridge_exit"]["chain"] = "Arbitrum"
    hop1_crossing["bridge_exit"]["eid"] = 30110
    hop1_crossing["bridge_exit"]["compose_status"] = "SUCCEEDED"
    hop1_crossing["bridge_exit"]["compose_tx_hash"] = hop2_tx

    hop2_crossing = _crossing_delivered(to_address=final_recipient)
    hop2_crossing["bridge_entry"]["chain"] = "Arbitrum"
    hop2_crossing["bridge_exit"]["chain"] = "Ethereum"
    hop2_crossing["bridge_exit"]["eid"] = 30101

    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(tx_hash=hop1_tx),
        crossing={hop1_tx: hop1_crossing, hop2_tx: hop2_crossing},
        evm_pages={},  # final_recipient has no outgoing transfers -> dead end after arrival
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))

    bridge_hops = [h for h in result["hops"] if h["segment"] == "bridge"]
    assert len(bridge_hops) == 2
    assert bridge_hops[0]["leg"] == 1 and bridge_hops[0]["to_chain"] == "Arbitrum"
    assert bridge_hops[1]["leg"] == 2 and bridge_hops[1]["to_chain"] == "Ethereum"
    assert result["final_status"] == "RESTED_AT_ADDRESS"
    assert result["final_chain"] == "Ethereum"  # не Arbitrum — хаб не финальная точка
    assert result["final_address"] == final_recipient
    print("test_full_path_chases_legacy_mesh_hop2: OK")


def test_bridge_hop_chase_capped_at_max_bridge_hops():
    """Защита от бесконечного цикла: если compose_tx_hash присутствует
    всегда (гипотетическая аномалия данных), обход должен остановиться на
    MAX_BRIDGE_HOPS, а не зависнуть."""
    call_log = []

    def _cross_side_effect(tx_hash, testnet=False):
        call_log.append(tx_hash)
        c = _crossing_delivered(to_address="0x6666666666666666666666666666666666666f")
        c["bridge_exit"]["compose_status"] = "SUCCEEDED"
        c["bridge_exit"]["compose_tx_hash"] = f"0xhop{len(call_log) + 1}"  # всегда есть следующий хоп
        return c

    p1, p2, p3, p4, p5, p6, p7 = _patched(wallet_messages=_wallet_messages_response(), crossing=None, evm_pages={})
    with p1, p2, p3, p4, p5, p6, p7:
        bt.lz.find_bridge_crossing.side_effect = _cross_side_effect
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert len(call_log) == bt.MAX_BRIDGE_HOPS
    bridge_hops = [h for h in result["hops"] if h["segment"] == "bridge"]
    assert len(bridge_hops) == bt.MAX_BRIDGE_HOPS
    print("test_bridge_hop_chase_capped_at_max_bridge_hops: OK")


def test_bridge_compose_failed_returns_explicit_status():
    """Реальный случай, найденный живым запуском (tx 0x8f1497875d07…3f77f0):
    Legacy Mesh Hop 1 доставлен на хаб, но compose (Hop 2) провалился
    (compose_status=SIMULATION_REVERTED, compose_tx_hash отсутствует). Без
    явной обработки трейсер молча продолжал бы как будто хаб — обычная
    финальная точка выхода из моста (та же ветка кода, что и "Hop 2 не
    настроен вовсе"), теряя сигнал "средства могли застрять на хабе, Hop 2 не
    состоялся" — важный для комплаенс-офицера случай. final_status должен
    отличаться от RESTED_AT_ADDRESS/RESTED_AT_CONTRACT, а не падать
    необработанным исключением."""
    hub_address = "0xarbitrumhuboapp0000000000000000000000000"
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_compose_failed(to_address=hub_address),
        evm_pages={},  # не должен вызываться вовсе — трейс должен остановиться на bridge-хопе
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))
        bt.get_token_transfers.assert_not_called()

    assert result["final_status"] == "BRIDGE_COMPOSE_FAILED"
    assert result["final_address"] == hub_address
    bridge_hops = [h for h in result["hops"] if h["segment"] == "bridge"]
    assert len(bridge_hops) == 1
    assert bridge_hops[0]["compose_status"] == "SIMULATION_REVERTED"
    assert "SIMULATION_REVERTED" in result["note"]
    print("test_bridge_compose_failed_returns_explicit_status: OK")


def test_no_bridge_deposit_found():
    p1, p2, p3, p4, p5, p6, p7 = _patched(wallet_messages=[], crossing=None, evm_pages={})
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "NO_BRIDGE_DEPOSIT_FOUND"
    assert result["hops"] == []
    print("test_no_bridge_deposit_found: OK")


def test_bridge_message_not_found():
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing={"found": False, "confidence": "UNRESOLVED", "note": "не найдено"},
        evm_pages={},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "BRIDGE_MESSAGE_NOT_FOUND"
    assert len(result["hops"]) == 1  # only the tron_deposit hop
    print("test_bridge_message_not_found: OK")


def test_in_transit_not_delivered():
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=_wallet_messages_response(),
        crossing=_crossing_inflight(),
        evm_pages={},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "IN_TRANSIT"
    assert result["final_tx_hash"] is None
    print("test_in_transit_not_delivered: OK")


def test_non_evm_destination_stops_cleanly():
    solana_crossing = _crossing_delivered(to_address="SoLanaRecipientAddr111111111111111111111")
    solana_crossing["bridge_exit"]["eid"] = 30168  # Solana
    solana_crossing["bridge_exit"]["chain"] = "Solana"
    p1, p2, p3, p4, p5, p6, p7 = _patched(wallet_messages=_wallet_messages_response(), crossing=solana_crossing, evm_pages={})
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path(TRON_SENDER))

    assert result["final_status"] == "RESTED_AT_ADDRESS"
    assert result["final_chain"] == "Solana"
    assert "не является EVM" in result["note"]
    print("test_non_evm_destination_stops_cleanly: OK")


def test_start_type_tx_hash_skips_tron_step():
    recipient = BINANCE_ETH
    p1, p2, p3, p4, p5, p6, p7 = _patched(
        wallet_messages=None,  # should never be called
        crossing=_crossing_delivered(to_address=recipient),
        evm_pages={},
    )
    with p1, p2, p3, p4, p5, p6, p7:
        result = _run(bt.trace_full_path("0xcae6f9052cc8...", start_type="tx_hash"))

    assert result["final_status"] == "RESTED_AT_EXCHANGE"
    assert len(result["hops"]) == 1  # only the bridge hop, no tron_deposit hop
    assert result["hops"][0]["segment"] == "bridge"
    print("test_start_type_tx_hash_skips_tron_step: OK")


# --- Тесты пагинации _walk_evm (реестр-чек против выбранного time-anchoring
# кандидатом, не против всех кандидатов страницы; SEARCH_DEPTH_EXCEEDED vs
# RESTED_AT_ADDRESS) — тестируют _walk_evm напрямую, без слоя Tron/bridge,
# т.к. это чисто EVM-механика одного сегмента пути. ---

def _page(items, next_page_params=None):
    return {"items": items, "next_page_params": next_page_params}


def _iso(unix_ts):
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def test_walk_evm_prefers_earliest_over_known_address_match():
    """Реальный сценарий из задания: у адреса 15 исходящих переводов, после
    time-фильтра остаётся 4 кандидата; 2 из них ведут на известные (по
    реестру) адреса, но не самый ранний по времени; самый ранний — на
    неизвестный адрес. Трейсер должен пойти через неизвестный адрес дальше,
    а не "срезать" на удобную известную остановку — приоритет "вероятное
    прямое продолжение этих денег" (earliest) выше признака "известный
    адрес" (см. докстринг _walk_evm)."""
    start = "0x1000000000000000000000000000000000000a"
    known_registry_addr = "0x2000000000000000000000000000000000000b"  # известен через bridge_registry
    known_contracts_addr = UNISWAP_ETH  # известен через known_contracts.py
    unknown_earliest = "0x4000000000000000000000000000000000000d"
    other_unknown = "0x5000000000000000000000000000000000000e"
    anchor = 1786800000.0

    old_items = [
        _evm_transfer_item(start, other_unknown, tx_hash=f"0xold{i}")
        for i in range(11)
    ]
    for i, it in enumerate(old_items):
        it["timestamp"] = _iso(anchor - 100 - i)  # строго ДО anchor -> отфильтруются

    candidates = [
        _evm_transfer_item(start, known_registry_addr, tx_hash="0xcand_known1"),
        _evm_transfer_item(start, known_contracts_addr, tx_hash="0xcand_known2"),
        _evm_transfer_item(start, unknown_earliest, tx_hash="0xcand_earliest"),
        _evm_transfer_item(start, other_unknown, tx_hash="0xcand_other"),
    ]
    candidates[0]["timestamp"] = _iso(anchor + 40)
    candidates[1]["timestamp"] = _iso(anchor + 30)
    candidates[2]["timestamp"] = _iso(anchor + 10)  # САМЫЙ РАННИЙ среди подходящих
    candidates[3]["timestamp"] = _iso(anchor + 20)

    page = _page(old_items + candidates)  # одна страница, 15 items всего
    registry_entry = {
        "address": known_registry_addr, "chain_key": "Ethereum", "chain_id": 1, "lz_eid": 30101,
        "protocol": "USDT0", "contract_role": "OFT Adapter", "type": "official_oft",
        "source": "usdt0_deployments_api", "verified_at": "2026-08-15", "evidence": [], "verification_note": None,
    }

    m_evm = AsyncMock(return_value=page)
    m_registry = MagicMock(return_value=[registry_entry])
    with patch.object(bt, "get_token_transfers", m_evm), \
         patch.object(bt.bridge_registry, "get_registry_for_evm_chain_id", m_registry):
        result = _run(bt._walk_evm(1, start, max_hops=5, after_timestamp=anchor))

    assert result["final_status"] != "RESTED_AT_EXCHANGE" and result["final_status"] != "RESTED_AT_CONTRACT"
    assert result["hops"][0]["to_address"] == unknown_earliest
    assert result["hops"][0]["tx_hash"] == "0xcand_earliest"
    print("test_walk_evm_prefers_earliest_over_known_address_match: OK")


def test_walk_evm_finds_match_on_later_page_stops_pagination():
    """Совпадение с реестром находится только на условной 4-й странице —
    остановка должна произойти именно на ней, страницы 5+ не запрашиваются
    (страниц в моке всего 4 — если бы код запросил 5-ю, m_evm вернул бы
    пустую страницу по умолчанию, и тест ниже это бы не поймал напрямую, но
    call_count фиксирует точное число обращений)."""
    start = "0x1000000000000000000000000000000000000a"
    target = UNISWAP_ETH
    anchor = 1786800000.0

    empty_before_anchor = [_page([], next_page_params={"cursor": "p2"})]  # стр.1: пусто после фильтра
    p2 = _page(
        [{"from": {"hash": start}, "to": {"hash": "0x9999999999999999999999999999999999999f"},
          "transaction_hash": "0xnope", "total": {"value": "1", "decimals": "6"}, "token": {"symbol": "USDT"},
          "timestamp": _iso(anchor - 500)}],  # ДО anchor -> не проходит фильтр
        next_page_params={"cursor": "p3"},
    )
    p3 = _page([], next_page_params={"cursor": "p4"})
    p4 = _page(
        [_evm_transfer_item(start, target, tx_hash="0xfound_on_page4")],
        next_page_params={"cursor": "p5"},  # есть ещё страницы, но их не должны спросить
    )
    for it in p4["items"]:
        it["timestamp"] = _iso(anchor + 10)

    pages = [empty_before_anchor[0], p2, p3, p4]
    m_evm = AsyncMock(side_effect=pages)
    m_registry = MagicMock(return_value=[])
    with patch.object(bt, "get_token_transfers", m_evm), \
         patch.object(bt.bridge_registry, "get_registry_for_evm_chain_id", m_registry):
        result = _run(bt._walk_evm(1, start, max_hops=5, after_timestamp=anchor, max_pages_per_hop=4))

    assert m_evm.call_count == 4
    assert result["final_status"] == "RESTED_AT_CONTRACT"
    assert result["final_address"] == target
    print("test_walk_evm_finds_match_on_later_page_stops_pagination: OK")


def test_walk_evm_search_depth_exceeded_when_no_match_within_page_cap():
    """Подходящих кандидатов нет вообще вплоть до потолка страниц (каждая
    просмотренная страница ещё имеет next_page_params, то есть данные не
    исчерпаны, просто не хватило глубины) — должен вернуться
    SEARCH_DEPTH_EXCEEDED, а НЕ RESTED_AT_ADDRESS (это разные вещи: "не
    хватило глубины поиска" vs "дальше правда некуда идти")."""
    start = "0x1000000000000000000000000000000000000a"
    anchor = 1786800000.0

    def _stale_page(cursor_out):
        return _page(
            [{"from": {"hash": start}, "to": {"hash": "0x9999999999999999999999999999999999999f"},
              "transaction_hash": "0xstale", "total": {"value": "1", "decimals": "6"}, "token": {"symbol": "USDT"},
              "timestamp": _iso(anchor - 999)}],  # всегда ДО anchor -> никогда не проходит фильтр
            next_page_params={"cursor": cursor_out},  # всегда "есть ещё"
        )

    pages = [_stale_page(f"p{i}") for i in range(1, 6)]  # с запасом больше потолка
    m_evm = AsyncMock(side_effect=pages)
    m_registry = MagicMock(return_value=[])
    with patch.object(bt, "get_token_transfers", m_evm), \
         patch.object(bt.bridge_registry, "get_registry_for_evm_chain_id", m_registry):
        result = _run(bt._walk_evm(1, start, max_hops=5, after_timestamp=anchor, max_pages_per_hop=3))

    assert m_evm.call_count == 3  # ровно потолок, не больше
    assert result["final_status"] == "SEARCH_DEPTH_EXCEEDED"
    assert result["pages_examined"] == 3
    assert result["final_status"] != "RESTED_AT_ADDRESS"
    print("test_walk_evm_search_depth_exceeded_when_no_match_within_page_cap: OK")


def test_walk_evm_rested_at_address_when_pages_genuinely_exhausted():
    """Контрольный тест на отличие от предыдущего: если Blockscout сам
    подтвердил конец данных (next_page_params пуст) ДО достижения потолка
    страниц — это RESTED_AT_ADDRESS (настоящий тупик), а не
    SEARCH_DEPTH_EXCEEDED."""
    start = "0x1000000000000000000000000000000000000a"
    anchor = 1786800000.0

    p1 = _page([], next_page_params={"cursor": "p2"})
    p2 = _page([], next_page_params=None)  # Blockscout подтверждает: страниц больше нет

    m_evm = AsyncMock(side_effect=[p1, p2])
    m_registry = MagicMock(return_value=[])
    with patch.object(bt, "get_token_transfers", m_evm), \
         patch.object(bt.bridge_registry, "get_registry_for_evm_chain_id", m_registry):
        result = _run(bt._walk_evm(1, start, max_hops=5, after_timestamp=anchor, max_pages_per_hop=4))

    assert m_evm.call_count == 2  # остановились сразу, как только next_page_params пуст
    assert result["final_status"] == "RESTED_AT_ADDRESS"
    print("test_walk_evm_rested_at_address_when_pages_genuinely_exhausted: OK")


if __name__ == "__main__":
    test_full_path_rests_at_exchange()
    test_full_path_rests_at_dex_contract()
    test_full_path_dead_end()
    test_full_path_max_hops_reached()
    test_full_path_stops_on_same_tx_multi_leg_swap()
    test_full_path_stops_on_burn_to_zero_address()
    test_tron_deposit_uses_real_sender_not_shared_oapp_address()
    test_tron_deposit_found_via_trongrid_official_oft()
    test_tron_deposit_found_via_trongrid_pool_router()
    test_trongrid_miss_falls_back_to_layerzero_scan()
    test_trongrid_error_falls_back_to_layerzero_scan()
    test_unknown_oapp_sender_treated_as_no_deposit()
    test_incoming_message_to_tron_not_mistaken_for_deposit()
    test_full_path_chases_legacy_mesh_hop2()
    test_bridge_hop_chase_capped_at_max_bridge_hops()
    test_bridge_compose_failed_returns_explicit_status()
    test_no_bridge_deposit_found()
    test_bridge_message_not_found()
    test_in_transit_not_delivered()
    test_non_evm_destination_stops_cleanly()
    test_start_type_tx_hash_skips_tron_step()
    test_walk_evm_prefers_earliest_over_known_address_match()
    test_walk_evm_finds_match_on_later_page_stops_pagination()
    test_walk_evm_search_depth_exceeded_when_no_match_within_page_cap()
    test_walk_evm_rested_at_address_when_pages_genuinely_exhausted()
    print("\nВСЕ ОФЛАЙН-ТЕСТЫ bridge_tracer.py ПРОЙДЕНЫ")
