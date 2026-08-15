#!/usr/bin/env python3
"""
known_contracts.py — минимальный реестр размеченных адресов (биржи / DEX /
другие мосты) для стоп-условий post-bridge walker'а в bridge_tracer.py.

ЗАЧЕМ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ
--------------------------
По духу — та же идея, что Bridge Contract Registry в layerzero_tracer.py
(LAYERZERO_EID_INFO): явная, читаемая таблица вместо магии/эвристики внутри
кода трейсера. bridge_tracer.py импортирует эту таблицу и ничего не решает
о разметке адресов сам — только сверяется с ней.

ЭТО НЕ PRODUCTION-ПОЛНОТА
--------------------------
Это НЕ полная база данных бирж/DEX (для этого есть коммерческие провайдеры
вроде Chainalysis/TRM, или можно тянуть name tags живым запросом из
Blockscout/Etherscan API — см. briefing_for_claude_code.md). Здесь —
вручную заполненный словарь на несколько самых частых адресов, достаточный
для демонстрации принципа стоп-условия в MVP: трейсер должен ОСТАНОВИТЬСЯ
и internally прочитать это распознавание, а не безуспешно попытаться
разрешить своп или следующий мост (это отдельная, не входящая в scope
MVP задача — см. limitations в архитектурном документе, раздел 5.2).

ИСТОЧНИК АДРЕСОВ
-----------------
Каждый адрес сверен живым веб-поиском по публичным name tags Etherscan
(не взят по памяти — после истории с ошибкой про Wormhole/Tron chain id
в layerzero_tracer.py принцип "не полагаться на память для конкретных
адресов/ID" применяется и здесь):
  - Binance 14:                 etherscan.io/address/0x28c6c06298d514db089934071355e5743bf21d60
  - Binance Hot Wallet 20:       etherscan.io/address/0xf977814e90da44bfa03b6295a0616a897441acec
  - Coinbase 12:                 etherscan.io/address/0x503828976d22510aad0201ac7ec88293211d23da
  - Uniswap: Universal Router:   etherscan.io/address/0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad
  - Uniswap V3: Router:          etherscan.io/address/0xe592427a0aece92de3edee1f18e0157c05861564
  - Wormhole: Portal Token Bridge: etherscan.io/address/0x3ee18b2214aff97000d974cf647e7c347e8fa585
Снято 2026-08-15.

ФОРМАТ
------
KNOWN_CONTRACTS: {evm_chain_id (EIP-155): {адрес в нижнем регистре: {...}}}
Ключ верхнего уровня — EIP-155 chain_id (та же нумерация, что использует
evm_adapter/chains.yaml), НЕ LayerZero eid и не что-либо ещё — см. принцип
"explicit translation table на границе модулей" в briefing_for_claude_code.md.
"""

from typing import Any, Optional

# type: "exchange" | "dex_swap" | "bridge"
KNOWN_CONTRACTS: dict[int, dict[str, dict[str, Any]]] = {
    1: {  # Ethereum mainnet
        "0x28c6c06298d514db089934071355e5743bf21d60": {"type": "exchange", "name": "Binance"},
        "0xf977814e90da44bfa03b6295a0616a897441acec": {"type": "exchange", "name": "Binance"},
        "0x503828976d22510aad0201ac7ec88293211d23da": {"type": "exchange", "name": "Coinbase"},
        "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": {"type": "dex_swap", "name": "Uniswap Universal Router"},
        "0xe592427a0aece92de3edee1f18e0157c05861564": {"type": "dex_swap", "name": "Uniswap V3 Router"},
        "0x3ee18b2214aff97000d974cf647e7c347e8fa585": {"type": "bridge", "name": "Wormhole Portal Token Bridge"},
    },
}


def lookup_contract(evm_chain_id: int, address: Optional[str]) -> Optional[dict[str, Any]]:
    """
    Возвращает {"type": ..., "name": ...} для известного адреса на данной
    EVM-сети (EIP-155 chain_id) или None, если адрес не размечен в этом
    MVP-реестре (это НЕ означает "адрес безопасен" — просто вне покрытия).
    """
    if address is None:
        return None
    chain_table = KNOWN_CONTRACTS.get(evm_chain_id)
    if not chain_table:
        return None
    return chain_table.get(address.lower())
