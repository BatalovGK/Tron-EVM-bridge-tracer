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

ПОЧЕМУ BNB CHAIN ОТСУТСТВУЕТ ПОЛНОСТЬЮ (ни USDT, ни USDC)
--------------------------------------------------------------
Не "не хватило времени проверить" — явно исключено по первоисточникам:
  - USDT: официальная страница Tether не перечисляет USD₮ для BNB Smart
    Chain вообще (только XAU₮). USDT0 Deployments API тоже не содержит
    записей для chain_id=56 (проверено живым запросом в этой сессии).
  - USDC: официальная документация Circle (developers.circle.com/
    stablecoins/usdc-contract-addresses) не перечисляет BNB Chain среди
    сетей нативного выпуска USDC.
Отсутствие фильтра для сети — безопасный fallback (фильтр там просто не
применяется, как раньше). Внесение неверного адреса было бы хуже: дало бы
ложную уверенность и могло отфильтровать легитимный перевод как поддельный
или пропустить подделку как легитимную.

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
  - USDT Ethereum:  tether.to/en/supported-protocols -> etherscan.io/address/0xdac17f958d2ee523a2206206994597c13d831ec7
  - USDC Ethereum:  developers.circle.com/stablecoins/usdc-contract-addresses
  - USDC Polygon:   developers.circle.com/stablecoins/usdc-contract-addresses
  - USDC Arbitrum:  developers.circle.com/stablecoins/usdc-contract-addresses
  - USDC Optimism:  developers.circle.com/stablecoins/usdc-contract-addresses
  - USDC Base:      developers.circle.com/stablecoins/usdc-contract-addresses
Снято 2026-08-16.
"""

from typing import Optional

LEGIT_TOKEN_CONTRACTS: dict[int, dict[str, str]] = {
    1: {  # Ethereum mainnet
        "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
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
    # 56 (BNB Chain) намеренно отсутствует целиком — см. докстринг модуля.
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
