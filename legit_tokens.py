#!/usr/bin/env python3
"""
legit_tokens.py — реестр официальных адресов контрактов легитимных
стейблкоинов (USDT/USDC) по EVM-сетям, для проверки token.address_hash в
_walk_evm (bridge_tracer.py) вместо token.symbol — символ подделать легко
(юникод-гомоглифы вроде "ÚSDТ"/"U5DT"/"ÚЅDТ", найдены живым запуском в этой
сессии), адрес контракта подделать нельзя: Blockscout резолвит symbol/name
из самого контракта, так что настоящий адрес физически не может отдавать
поддельный символ.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ЧАСТЬ known_contracts.py
--------------------------------------------------------
known_contracts.py размечает адреса, НА КОТОРЫХ трейс останавливается
(биржи/DEX/мосты — стоп-условие). Этот реестр — про другое: адреса, ЧЕРЕЗ
которые трейс МОЖЕТ продолжаться (легитимный токен, отбрасывать всё
остальное как подозрительного кандидата). Разная семантика — разные
модули, тот же принцип, что уже объяснён в bridge_registry.py.

ПОЧЕМУ ЗДЕСЬ НЕТ USDT НА ARBITRUM/POLYGON/OPTIMISM
------------------------------------------------------
Официальная страница Tether (tether.to/en/supported-protocols) перечисляет
только Ethereum для классического USD₮ — Arbitrum/Polygon/Optimism не
покрыты (это домен USDT0, отдельного продукта). Для этих трёх сетей
bridge_tracer._walk_evm сверяется НЕ с этим статическим реестром, а
дополнительно с bridge_registry.get_registry_for_evm_chain_id() (роль
"Token") — это уже существующий в проекте ЖИВОЙ источник (официальный
USDT0 Deployments API), а не догадка. Проверено живым запросом в этой
сессии: для Arbitrum API отдаёт под ролью "Token" адрес
0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9 — и реальный запрос к
Blockscout на живом USDT0-получателе (0xb7cc792b3af4bf6afc350538c9d5800c39e3d2c6,
использован в диагностике этой сессии) подтвердил точное совпадение
token.address_hash с этим адресом.

BNB CHAIN: USDT ЕСТЬ (Binance-Peg), USDC ОТСУТСТВУЕТ
----------------------------------------------------------
BNB Chain не покрыта ни официальной страницей Tether (USD₮ там не
перечислен вообще, только XAU₮), ни USDT0 Deployments API (пустой список
для chain_id=56, проверено живым запросом в этой сессии) — то есть у
Tether Ltd там нет СОБСТВЕННОГО выпуска USDT. Но де-факто каноническим
"USDT" на BNB Chain выступает Binance-Peg BSC-USD — контракт
0x55d398326f99059fF775485246999027B3197955, эмпирически подтверждено
через реальную входящую транзакцию с Bybit + проверку на BscScan
2026-08-16: официальные теги самого BscScan "Token Contract"/"Binance-Peg"/
"Stablecoin", TokenTracker-имя "Binance-Peg BSC-USD (BSC-USD)", "Source
Code Verified — Exact Match" (контракт BEP20USDT), аудит (Etherauthority,
апрель 2025), задеплоен ~6 лет назад. Тот же стандарт доказательности, что
уже применялся в known_contracts.py ("сверен живым поиском по публичным
name tags Etherscan" — здесь: BscScan). ВАЖНО: это НЕ выпуск самого Tether
Ltd, а пег-механизм Binance (Project Token Canal, по описанию самого
BscScan) — категориально отличается от Arbitrum/Polygon/Optimism-случая
(там источник — официальный протокольный реестр USDT0, first-party к
продукту), поэтому символ в реестре ниже помечен как "USDT (Binance-Peg)",
не просто "USDT".

USDC на BNB Chain НЕ включён: официальная документация Circle не
перечисляет BNB Chain среди сетей нативного выпуска — а раз Circle сам не
подтверждает выпуск, любой "USDC" на BSC тоже был бы сторонним пегом,
требующим ТОЙ ЖЕ отдельной проверки (реальная tx + explorer-тег), которая
для USDC пока не проводилась. Отсутствие фильтра — безопасный fallback
(фильтр просто не применяется для USDC на этой сети, как раньше). Внесение
неверного/непроверенного адреса было бы хуже: дало бы ложную уверенность.

ПОЧЕМУ BASE ОТСУТСТВУЕТ ДЛЯ USDT (НО ЕСТЬ ДЛЯ USDC)
--------------------------------------------------------
USDT0 Deployments API не содержит записей для chain_id=8453 (проверено
живым запросом в этой сессии) — USDT0 там не задеплоен. USDC на Base —
официально задокументирован Circle, включён.

ФОРМАТ
------
LEGIT_TOKEN_CONTRACTS: {evm_chain_id: {адрес в нижнем регистре: символ}}
Ключ — адрес (не символ), потому что фильтру нужно "есть ли этот КОНКРЕТНЫЙ
адрес в доверенном множестве для сети" — сверка по обратному направлению
(символ -> адрес -> сравнение) излишняя, раз Blockscout и так не может
соврать про symbol легитимного контракта.

ИСТОЧНИКИ (сверено живым запросом в этой сессии, не по памяти)
------------------------------------------------------------------
  - USDT Ethereum:        tether.to/en/supported-protocols -> etherscan.io/address/0xdac17f958d2ee523a2206206994597c13d831ec7
  - USDC Ethereum:        developers.circle.com/stablecoins/usdc-contract-addresses
  - USDC Polygon:         developers.circle.com/stablecoins/usdc-contract-addresses
  - USDC Arbitrum:        developers.circle.com/stablecoins/usdc-contract-addresses
  - USDC Optimism:        developers.circle.com/stablecoins/usdc-contract-addresses
  - USDC Base:            developers.circle.com/stablecoins/usdc-contract-addresses
  - USDT BNB Chain:       bscscan.com/token/0x55d398326f99059ff775485246999027b3197955
                          (Binance-Peg BSC-USD) — эмпирически подтверждено
                          реальной транзакцией с Bybit + проверкой тегов
                          BscScan, 2026-08-16.
Снято 2026-08-16.
"""

from typing import Optional

LEGIT_TOKEN_CONTRACTS: dict[int, dict[str, str]] = {
    1: {  # Ethereum mainnet
        "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    },
    56: {  # BNB Chain — только USDT (Binance-Peg), USDC не проверен, см. докстринг
        "0x55d398326f99059ff775485246999027b3197955": "USDT (Binance-Peg)",
    },
    137: {  # Polygon PoS
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",
    },
    42161: {  # Arbitrum One
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831": "USDC",
    },
    10: {  # Optimism
        "0x0b2c639c533813f4aa9d7837caf62653d097ff85": "USDC",
    },
    8453: {  # Base
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
    },
}


def is_legit_token_contract(evm_chain_id: int, address: Optional[str]) -> Optional[str]:
    """
    Возвращает символ токена (напр. "USDT"), если address — официальный
    контракт легитимного стейблкоина на данной EVM-сети, иначе None.
    None означает "не в реестре" — НЕ "точно подделка" (реестр покрывает
    только USDT/USDC на части сетей, см. докстринг модуля).
    """
    if address is None:
        return None
    chain_table = LEGIT_TOKEN_CONTRACTS.get(evm_chain_id)
    if not chain_table:
        return None
    return chain_table.get(address.lower())
