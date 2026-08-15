#!/usr/bin/env python3
"""
bridge_registry.py — единый, generic Bridge Contract Registry: адрес ->
(сеть, протокол, тип контракта, источник данных с провенансом).

ЗАЧЕМ ЭТОТ МОДУЛЬ (история решения)
------------------------------------
До этого bridge_tracer.py находил депозит в мост на Tron ТОЛЬКО через
LayerZero Scan (messages/wallet) — потому что первая версия, TronGrid-
эвристика "прямой TRC-20 Transfer на единственный известный OFT-адрес",
давала ложноотрицательный результат на переводах через промежуточный
pool/router-контракт (см. историю в bridge_tracer.py, раздел 9.6
архитектурного документа).

Пользователь вручную нашёл на Tronscan конкретный такой router-контракт
(TWPziSAroSacAjDuL52ByQzU86s9mP2gPr) и подтвердил tx hash'ами, что USDT,
пришедший на него, в ТОЙ ЖЕ атомарной транзакции пересылается (за вычетом
комиссии пула) на официальный OFT — то есть это легитимный промежуточный
контракт того же моста, не что-то постороннее. Проверено живым запросом в
этой сессии:
  tx 800fd19ab2b8ee417b999dad7943a2dc7d7d08b75597bae103ac91a332182b35:
    TW37rD2LrsURThJB3ohNztWUUKtJ49KjJb -[27525.000000 USDT]-> TWPziS...gPr
    TWPziS...gPr                        -[27505.437809 USDT]-> TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt (официальный OFT)
  tx 85769a682fd6b2801797e4c583a087f69763f2ae1579b36719ec99a4c5d98729:
    TMTkbTwMgkPTXngZeZm5tprgTTmbR3sXvn -[500.000000 USDT]->   TWPziS...gPr
    TWPziS...gPr                        -[499.650000 USDT]->   TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt
  Контракт верифицирован на Tronscan под именем "OftBridge" (публичной
  метки/тега нет, но исходник верифицирован и назван самим разработчиком).
Так вместо возврата к "прямому transfer на единственный адрес" появляется
РЕЕСТР известных bridge-адресов (официальных + эмпирически проверенных),
против которого TronGrid-путь может сверяться напрямую — не как полное
решение (любой ЕЩЁ не размеченный router по-прежнему не найдётся), а как
основной, более быстрый путь, с LayerZero Scan messages/wallet как fallback
для всего, что в реестр не попало (см. _find_tron_bridge_deposit в
bridge_tracer.py).

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ЧАСТЬ bridge_tracer.py
-------------------------------------------------------
Реестр должен быть переиспользуем не только для Tron/TronGrid: в будущем
evm_adapter должен уметь так же спросить "какие bridge-адреса известны на
этой EVM-сети" (например, если понадобится обнаруживать депозит В мост
со стороны EVM, а не только пост-bridge обход после него) — без
переписывания логики детекции с нуля под каждую новую сеть/адаптер. Отсюда
`get_registry_for_tron()` и `get_registry_for_evm_chain_id()` как два
тонких фильтра над одним и тем же общим реестром, а не два независимых
списка.

ФОРМАТ ЗАПИСИ РЕЕСТРА
----------------------
{
    "address": str,          # родной формат сети: base58 для Tron, hex (0x...) для EVM
    "chain_key": str,        # человекочитаемое имя сети, как в USDT0 Deployments API ("Tron", "Ethereum", ...)
    "chain_id": Optional[int],   # EIP-155, None для не-EVM сетей (Tron, Solana, TON, ...)
    "lz_eid": Optional[int],     # LayerZero V2 endpoint id
    "protocol": str,          # "USDT0" (пока единственный поддерживаемый)
    "contract_role": str,     # человекочитаемая роль ("OFT", "OFT Adapter", "Composer", "pool_router", ...)
    "type": str,              # "official_oft" | "pool_router" — см. TYPE_* константы
    "source": str,            # "usdt0_deployments_api" | "empirical_verified" — см. SOURCE_* константы
    "verified_at": str,       # ISO-дата проверки (для official — дата последнего пересмотра ЭТОГО модуля;
                               # для empirical — дата, когда конкретный адрес был вручную подтверждён)
    "evidence": list[str],    # для empirical: ссылки/tx hash, подтверждающие запись; для official — пусто
    "verification_note": Optional[str],  # для empirical: как именно проверено
}

Тот же принцип провенанса (источник + дата), что уже используется в
Attribution & Labeling Engine платформы для меток OFAC/OpenSanctions
(label_cache: source + fetched_at) — здесь применён к bridge-адресам.
"""

from typing import Any, Optional

import usdt0_deployments

TYPE_OFFICIAL_OFT = "official_oft"
TYPE_POOL_ROUTER = "pool_router"

SOURCE_USDT0_API = "usdt0_deployments_api"
SOURCE_EMPIRICAL = "empirical_verified"

# Дата, когда структура USDT0 Deployments API (используемая get_official_entries
# ниже) была в последний раз сверена живым запросом — НЕ дата данных (те всегда
# свежие через usdt0_deployments.py, с диск-кэшем на 24ч), а дата, когда
# формат ответа был подтверждён соответствующим текущему коду.
OFFICIAL_SOURCE_LAST_VERIFIED = "2026-08-15"

# --- Эмпирический слой: адреса, которых НЕТ в официальном USDT0 Deployments
# API, найденные и подтверждённые вручную. Отдельная, явно поименованная
# структура (не просто комментарий в коде) — источник виден в самих данных,
# а не только в докстринге. Каждая запись обязана иметь evidence и
# verification_note — без них запись сюда не добавляется.
EMPIRICAL_VERIFIED_ENTRIES: list[dict[str, Any]] = [
    {
        "address": "TWPziSAroSacAjDuL52ByQzU86s9mP2gPr",
        "chain_key": "Tron",
        "chain_id": None,
        "lz_eid": 30420,
        "protocol": "USDT0",
        "contract_role": "OftBridge (pool/router, имя из верифицированного исходника на Tronscan)",
        "type": TYPE_POOL_ROUTER,
        "source": SOURCE_EMPIRICAL,
        "verified_at": "2026-08-15",
        "evidence": [
            "https://tronscan.org/#/transaction/800fd19ab2b8ee417b999dad7943a2dc7d7d08b75597bae103ac91a332182b35",
            "https://tronscan.org/#/transaction/85769a682fd6b2801797e4c583a087f69763f2ae1579b36719ec99a4c5d98729",
            "https://tronscan.org/#/address/TWPziSAroSacAjDuL52ByQzU86s9mP2gPr",
        ],
        "verification_note": (
            "В обеих транзакциях-примерах USDT сначала приходит на этот адрес, "
            "а внутри ТОЙ ЖЕ атомарной транзакции пересылается (за вычетом "
            "небольшой комиссии пула — 27525.0 -> 27505.437809 USDT и "
            "500.0 -> 499.65 USDT соответственно) на официальный Tron OFT "
            "TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt (см. официальный реестр, "
            "get_official_entries). Контракт верифицирован на Tronscan под "
            "именем 'OftBridge' — публичного тега/метки нет, имя взято из "
            "имени верифицированного исходника. Проверено live-запросом к "
            "Tronscan API (apilist.tronscanapi.com/api/transaction-info) в "
            "этой сессии, не по памяти."
        ),
    },
]


def get_official_entries(token: str = "usdt0", **kwargs: Any) -> list[dict[str, Any]]:
    """
    Строит записи реестра из официального USDT0 Deployments API — ВСЕ сети
    сразу (секции "native" и "legacyMesh"), не только Tron. kwargs
    пробрасываются в usdt0_deployments.fetch_deployments() (timeout,
    force_refresh).
    """
    data = usdt0_deployments.fetch_deployments(**kwargs)
    entries: list[dict[str, Any]] = []
    for section in ("native", "legacyMesh"):
        for net in data.get(token, {}).get(section, []):
            chain_id = net.get("chainId")
            lz_eid_raw = net.get("lzEid")
            lz_eid = int(lz_eid_raw) if lz_eid_raw is not None else None
            chain_key = net.get("name")
            for c in net.get("contracts", []):
                addr = c.get("address")
                if not addr:
                    continue
                entries.append({
                    "address": addr,
                    "chain_key": chain_key,
                    "chain_id": chain_id,
                    "lz_eid": lz_eid,
                    "protocol": token.upper(),
                    "contract_role": c.get("name", "OFT"),
                    "type": TYPE_OFFICIAL_OFT,
                    "source": SOURCE_USDT0_API,
                    "verified_at": OFFICIAL_SOURCE_LAST_VERIFIED,
                    "evidence": [],
                    "verification_note": None,
                })
    return entries


def get_registry(token: str = "usdt0", **kwargs: Any) -> list[dict[str, Any]]:
    """Полный реестр: официальные записи (все сети) + эмпирические (сейчас
    только Tron pool/router). kwargs пробрасываются в get_official_entries."""
    return get_official_entries(token=token, **kwargs) + EMPIRICAL_VERIFIED_ENTRIES


def get_registry_for_tron(token: str = "usdt0", **kwargs: Any) -> list[dict[str, Any]]:
    """Записи реестра для сети Tron — используется TronGrid-путём детекции
    депозита в bridge_tracer.py."""
    return [e for e in get_registry(token=token, **kwargs) if e["chain_key"] == "Tron"]


def get_registry_for_evm_chain_id(chain_id: int, token: str = "usdt0", **kwargs: Any) -> list[dict[str, Any]]:
    """
    Записи реестра для конкретной EVM-сети (EIP-155 chain_id) — задел на
    будущее для evm_adapter (см. докстринг модуля), сейчас bridge_tracer.py
    её не вызывает (пост-bridge обход на EVM использует known_contracts.py,
    не этот реестр — это ДРУГОЙ тип адресов, "куда трейс останавливается",
    а не "куда приходит депозит В мост").
    """
    return [e for e in get_registry(token=token, **kwargs) if e["chain_id"] == chain_id]
