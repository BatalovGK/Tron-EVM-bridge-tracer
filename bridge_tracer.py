#!/usr/bin/env python3
"""
bridge_tracer.py — Layer 1: сквозной трейсер полного пути транзакции через
мост LayerZero. Склеивает три сетевых адаптера, Bridge Contract Registry и
LayerZero Scan API в один самодостаточный async-модуль:

    TronGrid (bridge_registry.py, известные bridge-адреса Tron — поиск
        депозита, ОСНОВНОЙ путь), fallback на LayerZero Scan
        (layerzero_tracer.py, messages/wallet, если TronGrid ничего не
        нашёл) -> LayerZero Scan (find_bridge_crossing — сопоставление
        входа/выхода моста) -> Blockscout Pro (aml/evm_adapter, любая
        EVM-сеть, куда пришёл мост)

Шаг 1 — двухуровневая детекция (см. _find_tron_bridge_deposit ниже и
историю решения в bridge_registry.py): TronGrid как основной, более быстрый
путь (сверка исходящих TRC-20-переводов с генерик-реестром известных
bridge-адресов), LayerZero Scan messages/wallet как fallback для депозитов
через ещё не размеченный в реестре промежуточный контракт.

Единая входная точка — trace_full_path(). Модуль не знает, кто его вызывает
(CLI ниже — просто одна из обёрток, наравне с будущими agent/tools_bridge.py
и MCP-сервером — см. "Архитектурный принцип: сначала чистая логика, потом
обёртки" в briefing_for_claude_code.md). Вся сложность склейки живёт здесь
один раз.

ПОЧЕМУ НЕ ПЕРЕИСПОЛЬЗУЕТСЯ aml/flow_tracer/hop_tracer.py
----------------------------------------------------------
В кодовой базе уже есть Flow & Hop Tracer (BFS по исходящим переводам,
poison/taint-разметка) — но он рассчитан на другой сценарий: пишет рёбра в
PostgreSQL (evidence_log/flow_edges/seed_registry), резолвит свопы через
TheGraph (нужен THEGRAPH_API_KEY), стоп-условие "known_exchange" читает из
label_cache, которую заполняет Attribution Engine. Для ЭТОГО тестового
задания — самодостаточный Layer 1 без внешних БД/сервисов, стоп-условия
по маленькому статическому реестру (known_contracts.py) — сознательно
переиспользовать hop_tracer.py было бы неверно: он тянет за собой
Postgres + TheGraph там, где задача явно просит "MVP, без production-
грейда полноты". Пост-bridge обход здесь реализован заново, проще и без
внешних зависимостей, кроме тех же трёх адаптеров.

ОГРАНИЧЕНИЯ ЭТОГО MVP (осознанно, см. definition of done в брифинге)
-----------------------------------------------------------------------
- Шаг 1 (TronGrid, основной путь) распознаёт депозит только по адресам,
  ПОПАВШИМ в bridge_registry.get_registry_for_tron() — официальный OFT из
  USDT0 Deployments API плюс небольшой эмпирический список вручную
  подтверждённых pool/router-контрактов (сейчас один). Перевод через
  ЛЮБОЙ другой, ещё не размеченный там контракт TronGrid-путь не найдёт —
  для него подхватывает fallback на LayerZero Scan messages/wallet, который
  не ограничен реестром (спрашивает у LayerZero Scan напрямую, какие
  сообщения адрес реально отправил), но при этом сам ограничен реестром
  известных Tron-OApp из того же usdt0_deployments.py (фильтр по
  pathway.sender.address — сейчас там один адрес, USDT0 OFT на Tron).
  Сообщения через другие OApp/протоколы пропускаются как вне scope MVP.
- Шаг 3 (evm_adapter) — линейный обход "один хоп = один самый ранний
  подходящий исходящий ERC-20-перевод", НЕ amount-aware taint-трейсинг
  (это другая, более объёмная задача — см. hop_tracer.py выше). Схема
  ответа Blockscout (from/to как {"hash": ...}, total.value/decimals,
  transaction_hash, next_page_params) подтверждена живым запросом к
  публичному eth.blockscout.com (тот же v2 API, что и Blockscout Pro)
  в этой сессии — 2026-08-15.
- Второй мост/DEX-своп на пути НЕ резолвится — трейсер останавливается на
  нём (RESTED_AT_CONTRACT), это отдельная задача вне scope (см. раздел 5.2
  архитектурного документа).
- Ошибки нижележащих адаптеров (сетевые, CreditsExhaustedError, отсутствие
  BLOCKSCOUT_API_KEY/TRONGRID_API_KEY) намеренно НЕ проглатываются здесь —
  Layer 1 пробрасывает их вызывающему коду как есть; user-friendly
  форматирование ошибок — забота Layer 2 (обёрток).

КАК ПРОВЕРИТЬ САМОМУ
----------------------
python3 bridge_tracer.py --start <Tron-адрес отправителя> --max-hops 10
или, если уже есть хэш депозитной транзакции на Tron:
python3 bridge_tracer.py --start <tx_hash> --start-type tx_hash
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime
from typing import Any, Literal, Optional

# Без этого на Windows вывод кириллицы ломается в консолях с не-UTF8 кодовой
# страницей (cp866/cp1251) — та же проблема и тот же фикс, что в
# layerzero_tracer.py, проверено живым запуском в этой сессии.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# aml/-пакеты (tron_adapter, evm_adapter) написаны в расчёте на то, что сам
# aml/ лежит на sys.path (внутренние импорты вида "from common.secrets import
# ..." без префикса "aml."), а не на пакет "aml.tron_adapter" — поэтому
# добавляем aml/ в sys.path явно, а не переписываем их импорты.
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_AML_DIR = os.path.join(_REPO_ROOT, "aml")
if _AML_DIR not in sys.path:
    sys.path.insert(0, _AML_DIR)

import layerzero_tracer as lz  # noqa: E402
import bridge_registry  # noqa: E402
import observed_name_tags  # noqa: E402
from known_contracts import lookup_contract  # noqa: E402
from tron_adapter import base58_to_hex, get_trc20_transfers, get_account_info as get_tron_account_info, close_client as _close_tron_client  # noqa: E402
from evm_adapter import get_token_transfers, get_address_info, close_client as _close_evm_client  # noqa: E402
from usdt0_deployments import get_tron_oft_contracts  # noqa: E402

DEFAULT_MAX_HOPS = 10
MAX_EVM_TRANSFERS_PER_PAGE = 50
ZERO_ADDRESS = "0x" + "0" * 40
# Legacy Mesh (USDT0 Transfer Hub) документирован как ровно 2 хопа
# LayerZero-сообщений (source -> Arbitrum-хаб -> реальная сеть) — предел
# защитный, а не наблюдаемое значение, см. цикл в trace_full_path().
MAX_BRIDGE_HOPS = 3


async def close_clients() -> None:
    """Закрывает aiohttp-сессии evm_adapter И tron_adapter. Вызывать один раз
    по завершении работы с trace_full_path (см. main() ниже). tron_adapter
    теперь снова используется как сетевой клиент — TronGrid-путь детекции
    депозита, см. _find_tron_bridge_deposit_via_trongrid ниже."""
    await _close_evm_client()
    await _close_tron_client()


async def trace_full_path(
    start: str,
    start_type: Literal["address", "tx_hash"] = "address",
    mode: str = "incident_response",
    max_hops: int = DEFAULT_MAX_HOPS,
    max_evm_pages_per_hop: int = 4,
) -> dict[str, Any]:
    """
    Единая входная точка: прослеживает полный путь средств от адреса/tx на
    Tron через мост LayerZero до точки, где трейс останавливается (биржа,
    DEX/другой мост, тупик или max_hops).

    Args:
        start: Tron-адрес отправителя (start_type="address") или хэш
            транзакции депозита в мост на Tron (start_type="tx_hash") —
            во втором случае шаг 1 (поиск депозита через LayerZero Scan)
            пропускается.
        start_type: "address" | "tx_hash".
        mode: пробрасывается в метаданные результата для совместимости с
            остальной платформой (aml/agent/tools_*.py тоже принимают mode);
            в этой версии MVP поведение самого трейсера от mode не зависит —
            все режимы используют одинаковую глубину/логику обхода.
        max_hops: защита от бесконечной рекурсии/циклов на EVM-сегменте.
        max_evm_pages_per_hop: мягкий потолок страниц Blockscout НА ОДИН хоп
            пост-bridge обхода (см. _walk_evm) — не хардкод, параметр с
            дефолтом; действует, только пока не найден подходящий по
            time-anchoring кандидат на текущем хопе.

    Returns:
        Плоский вердикт по формату из briefing_for_claude_code.md
        (final_status/final_chain/final_address/final_tx_hash/exchange_name/
        contract_label/contract_type/hops/note).
    """
    hops: list[dict[str, Any]] = []

    if start_type == "address":
        deposit = await _find_tron_bridge_deposit(start)
        if deposit is None:
            return _flat_result(
                final_status="NO_BRIDGE_DEPOSIT_FOUND",
                hops=hops,
                note=(
                    f"LayerZero Scan не нашёл ни одного сообщения, отправленного "
                    f"(source) с адреса {start} через известный OApp (проверяется "
                    "реестр из официального USDT0 Deployments API — сейчас в нём "
                    "только USDT0 OFT на Tron). Либо адрес не связан с переводом "
                    "через LayerZero, либо использован OApp/протокол вне текущего "
                    "реестра."
                ),
            )
        hops.append(deposit["hop"])
        bridge_tx_hash = deposit["tx_hash"]
    elif start_type == "tx_hash":
        bridge_tx_hash = start
    else:
        raise ValueError(f"Неизвестный start_type: {start_type!r} (ожидались 'address' или 'tx_hash')")

    # find_bridge_crossing — синхронная функция (requests, не aiohttp) по
    # дизайну layerzero_tracer.py (независимый standalone-скрипт для BitOK).
    # Заворачиваем в отдельный поток, чтобы не блокировать event loop, в
    # котором крутятся асинхронные tron_adapter/evm_adapter.
    #
    # Legacy Mesh (USDT0 Transfer Hub через Arbitrum как хаб) может состоять
    # из 2 сообщений LayerZero подряд: Hop 1 (source -> Arbitrum-хаб) и,
    # если на хабе автоматически исполнился compose-вызов, Hop 2 (хаб ->
    # реальная целевая сеть) — см. докстринг find_bridge_crossing() про
    # compose_status/compose_tx_hash, подтверждено живым запросом в этой
    # сессии. Без этого цикла трейсер останавливался бы на хабе как на
    # "выходе из моста", хотя это середина пути. MAX_BRIDGE_HOPS — защитный
    # предел (Legacy Mesh документирован ровно как 2 хопа), а не наблюдаемое
    # значение — просто чтобы не зациклиться на аномальных данных.
    current_bridge_tx_hash = bridge_tx_hash
    crossing: dict[str, Any] = {}
    for leg in range(1, MAX_BRIDGE_HOPS + 1):
        crossing = await asyncio.to_thread(lz.find_bridge_crossing, current_bridge_tx_hash)
        if not crossing["found"]:
            return _flat_result(final_status="BRIDGE_MESSAGE_NOT_FOUND", hops=hops, note=crossing["note"])

        compose_status = crossing["bridge_exit"].get("compose_status")
        hops.append({
            "segment": "bridge",
            "leg": leg,
            "protocol": "LayerZero",
            "from_chain": crossing["bridge_entry"]["chain"],
            "from_address": crossing["bridge_entry"]["from_address"],
            "from_tx_hash": crossing["bridge_entry"]["tx_hash"],
            "to_chain": crossing["bridge_exit"]["chain"],
            "to_address": crossing["bridge_exit"]["to_address"],
            "to_tx_hash": crossing["bridge_exit"]["tx_hash"],
            "guid": crossing["guid"],
            "confidence": crossing["confidence"],
            "status": crossing["message_status"],
            # "N/A" (или отсутствует) — Legacy Mesh Hop 2 не настроен для этого
            # сообщения вовсе; "SUCCEEDED" — Hop 2 исполнился (см. compose_tx_hash
            # ниже); любое другое значение (например, "SIMULATION_REVERTED") —
            # Hop 2 был ЗАПУЩЕН, но НЕ исполнился успешно (см. BRIDGE_COMPOSE_FAILED
            # ниже) — оставляем это поле в каждом bridge-хопе явно, а не только
            # внутри логики ветвления, чтобы оно было видно в hops[] независимо
            # от исхода.
            "compose_status": compose_status,
        })

        if crossing["bridge_exit"]["tx_hash"] is None:
            return _flat_result(
                final_status="IN_TRANSIT",
                hops=hops,
                final_chain=crossing["bridge_exit"]["chain"],
                final_address=crossing["bridge_exit"]["to_address"],
                note=crossing["note"],
            )

        compose_tx_hash = crossing["bridge_exit"].get("compose_tx_hash")
        if not compose_tx_hash:
            if compose_status and compose_status != "N/A":
                # Compose был ЗАПУЩЕН (compose_status присутствует и не "N/A"),
                # но НЕ дал compose_tx_hash — то есть Hop 2 не исполнился успешно
                # (например, "SIMULATION_REVERTED": исполнитель не смог вызвать
                # Composer-контракт на хабе). Обнаружено живым запуском на
                # реальном примере: без этой проверки трейсер молча продолжал
                # как будто хаб — обычная финальная точка выхода из моста,
                # теряя сигнал "средства могли застрять на хабе" — важный для
                # комплаенс-офицера случай, не то же самое, что "Hop 2 не
                # настроен для этого сообщения вовсе" (compose_status == "N/A").
                return _flat_result(
                    final_status="BRIDGE_COMPOSE_FAILED",
                    hops=hops,
                    final_chain=crossing["bridge_exit"]["chain"],
                    final_address=crossing["bridge_exit"]["to_address"],
                    final_tx_hash=crossing["bridge_exit"]["tx_hash"],
                    note=(
                        f"Legacy Mesh Hop 2 (compose) был запущен на "
                        f"{crossing['bridge_exit']['chain']}, но НЕ исполнился "
                        f"успешно (compose_status={compose_status!r}) — средства "
                        "пришли на хаб (Hop 1 доставлен), но автоматический "
                        "перевод дальше НЕ состоялся. Возможно, требуется ручное "
                        "вмешательство/повторная попытка на стороне моста — вне "
                        "scope этого трейсера. Трейс корректно остановлен на "
                        "точке выхода из Hop 1, а не молча продолжен как будто "
                        "это обычная финальная точка."
                    ),
                )
            break  # финальный хоп: Hop 2 не настроен для этого сообщения вовсе
        current_bridge_tx_hash = compose_tx_hash

    dest_chain_name = crossing["bridge_exit"]["chain"]
    recipient = crossing["bridge_exit"]["to_address"]
    dest_tx_hash = crossing["bridge_exit"]["tx_hash"]

    dest_eid = crossing["bridge_exit"]["eid"]
    if not lz.is_evm_chain(dest_eid):
        return _flat_result(
            final_status="RESTED_AT_ADDRESS",
            hops=hops,
            final_chain=dest_chain_name,
            final_address=recipient,
            final_tx_hash=dest_tx_hash,
            note=(
                f"Целевая сеть моста ({dest_chain_name}) не является EVM-"
                "совместимой в реестре LayerZero (LAYERZERO_EID_INFO) — "
                "автоматический пост-bridge трейсинг через evm_adapter для неё "
                "принципиально не подходит (нужен отдельный network adapter "
                "под конкретную не-EVM сеть, вне scope этого MVP). Трейс "
                "корректно остановлен на точке выхода из моста, а не упал "
                "с ошибкой."
            ),
        )

    evm_chain_id = lz.LAYERZERO_EID_TO_EVM_CHAIN_ID[dest_eid]
    walk = await _walk_evm(
        evm_chain_id, recipient, max_hops=max_hops,
        after_timestamp=crossing["bridge_exit"]["timestamp"],
        max_pages_per_hop=max_evm_pages_per_hop,
    )
    hops.extend(walk["hops"])

    exchange_name, contract_label, contract_type = _label_fields(walk["label"])
    return _flat_result(
        final_status=walk["final_status"],
        hops=hops,
        final_chain=dest_chain_name,
        final_address=walk["final_address"],
        final_tx_hash=walk["final_tx_hash"] or dest_tx_hash,
        exchange_name=exchange_name,
        contract_label=contract_label,
        contract_type=contract_type,
        note=walk.get("note"),
        observed_name=walk.get("observed_name"),
        observed_name_source=walk.get("observed_name_source"),
    )


async def _find_tron_bridge_deposit(tron_address: str) -> Optional[dict[str, Any]]:
    """
    Находит депозит в мост LayerZero, отправленный с данного Tron-адреса —
    двухуровневая детекция:
      1. TronGrid + Bridge Contract Registry (ОСНОВНОЙ путь, один запрос к
         TronGrid) — см. _find_tron_bridge_deposit_via_trongrid.
      2. LayerZero Scan messages/wallet (FALLBACK — если TronGrid ничего не
         нашёл в реестре или недоступен) — см.
         _find_tron_bridge_deposit_via_layerzero_scan.

    ИСТОРИЯ РЕШЕНИЯ (два разворота за одну сессию, оба — по живым данным,
    ни один не по памяти/предположению)
    -----------------------------------------------------------------------
    Первая версия использовала ТОЛЬКО TronGrid: эвристика "прямой transfer
    на единственный известный OFT-адрес". Живой запуск нашёл ложноотрицательный
    случай — депозит шёл через промежуточный router-контракт, прямого
    Transfer на OFT просто не было, хотя сообщение LayerZero было реально
    отправлено и доставлено. Заменена на LayerZero Scan messages/wallet как
    ЕДИНСТВЕННЫЙ механизм (см. историю в
    _find_tron_bridge_deposit_via_layerzero_scan ниже).

    Позже пользователь вручную нашёл на Tronscan именно такой router-
    контракт и подтвердил двумя транзакциями, что средства через него в той
    же атомарной tx доходят до официального OFT (см.
    bridge_registry.EMPIRICAL_VERIFIED_ENTRIES). Вместо возврата к старой
    "угадывающей" эвристике TronGrid-путь теперь сверяется с РАСШИРЯЕМЫМ
    Bridge Contract Registry (bridge_registry.py — официальные адреса из
    USDT0 Deployments API + эмпирически подтверждённые вручную), а не с
    одним жёстко заданным адресом — это не тот же баг под новым названием:
    реестр можно пополнять по мере обнаружения новых router-контрактов, не
    трогая логику детекции. LayerZero Scan остаётся полноценным fallback'ом
    для всего, что в реестр ещё не попало — а не отбрасывается.
    """
    primary = await _find_tron_bridge_deposit_via_trongrid(tron_address)
    if primary is not None:
        return primary
    return await _find_tron_bridge_deposit_via_layerzero_scan(tron_address)


async def _find_tron_bridge_deposit_via_trongrid(tron_address: str) -> Optional[dict[str, Any]]:
    """
    TronGrid-путь (ОСНОВНОЙ, см. историю решения в _find_tron_bridge_deposit
    и провенанс адресов в bridge_registry.py): проверяет исходящие TRC-20
    Transfer-события адреса на прямое совпадение получателя ("to") с
    известным bridge-адресом Tron из bridge_registry.get_registry_for_tron()
    — официальные OFT-контракты (USDT0 Deployments API) И эмпирически
    подтверждённые pool/router-контракты.

    Один запрос к TronGrid вместо двух последовательных к разным API
    (быстрее fallback-пути), но принципиально ограничен известным реестром:
    перевод через ЕЩЁ не размеченный там контракт здесь не найдётся — для
    него подхватывает _find_tron_bridge_deposit_via_layerzero_scan.

    Не фильтрует по конкретному TRC-20 token contract_address (например,
    USDT), потому что bridge_registry.py принципиально не привязан к одному
    токену (USDT0 — не единственный протокол, который он может описывать в
    будущем) — сверяет ПОЛУЧАТЕЛЯ каждого исходящего Transfer-события с
    реестром, не тип токена.

    Известное ограничение (тот же класс, что и MAX_EVM_TRANSFERS_PER_PAGE в
    _walk_evm): читается только первая страница TronGrid (limit=200,
    сортировка не гарантирована документацией, но по наблюдению — свежие
    первыми); при очень высокой активности адреса релевантный перевод может
    не попасть на страницу — тогда TronGrid-путь молча вернёт None, и
    сработает fallback на LayerZero Scan (не БАГ, а РАЗУМНАЯ деградация -
    результат в итоге всё равно найдётся, просто медленнее).
    """
    registry_entries: Any
    transfers: Any
    registry_entries, transfers = await asyncio.gather(
        asyncio.to_thread(bridge_registry.get_registry_for_tron),
        get_trc20_transfers(address=tron_address, only_from=True),
        return_exceptions=True,
    )
    if isinstance(registry_entries, BaseException) or isinstance(transfers, BaseException):
        return None  # TronGrid или USDT0 Deployments API недоступны — fallback разберётся

    registry_by_address = {entry["address"]: entry for entry in registry_entries}
    if not registry_by_address:
        return None

    for item in transfers.get("data", []):
        if item.get("type") != "Transfer":
            continue  # TronGrid отдаёт Transfer и Approval в одном списке
        entry = registry_by_address.get(item.get("to"))
        if entry is None:
            continue
        raw_tx_hash = item.get("transaction_id")
        if not raw_tx_hash:
            continue
        # TronGrid отдаёт transaction_id БЕЗ префикса "0x" (голый hex) — а
        # LayerZero Scan API (fetch_message -> /v1/messages/tx/{txHash})
        # требует префикс "0x" (без него отдаёт "не найдено" даже на
        # существующем сообщении, подтверждено живым запросом в этой
        # сессии на реальной tx 800fd19a...). LayerZero Scan сам всегда
        # возвращает txHash С префиксом (см. find_messages_by_wallet) —
        # нормализуем на границе между двумя API, а не полагаемся на то,
        # что оба формата совпадут случайно.
        tx_hash = raw_tx_hash if raw_tx_hash.startswith("0x") else "0x" + raw_tx_hash

        block_ts = item.get("block_timestamp")  # TronGrid отдаёт unix ms
        depositor = item.get("from")
        hop: dict[str, Any] = {
            "segment": "tron_deposit",
            "chain": "Tron",
            "from_address": depositor,
            "oapp": entry["contract_role"],
            "tx_hash": tx_hash,
            "timestamp": block_ts // 1000 if isinstance(block_ts, int) else None,
            "detection_method": "trongrid_registry_match",
            "registry_entry_type": entry["type"],
            "registry_source": entry["source"],
        }
        observed = await _annotate_tron_observed_name(depositor) if depositor else None
        if observed is not None:
            hop["from_address_observed_name"] = observed["name"]
            hop["from_address_observed_name_source"] = observed["source"]
        return {"tx_hash": tx_hash, "hop": hop}
    return None


async def _find_tron_bridge_deposit_via_layerzero_scan(tron_address: str) -> Optional[dict[str, Any]]:
    """
    LayerZero Scan-путь (FALLBACK — вызывается только если
    _find_tron_bridge_deposit_via_trongrid ничего не нашёл в реестре или
    TronGrid/USDT0 Deployments API были недоступны).

    ПОЧЕМУ ЭТОТ ПУТЬ ВООБЩЕ НУЖЕН (история, обнаружено живым запуском)
    -----------------------------------------------------------
    TronGrid-путь ограничен ИЗВЕСТНЫМ реестром bridge-адресов — перевод
    через ещё не размеченный там промежуточный router/aggregator-контракт
    он не найдёт в принципе, сколько бы записей в реестр ни добавили (это
    было исходной причиной ложноотрицательного результата, из-за которой
    этот путь и появился — см. историю в _find_tron_bridge_deposit).
    layerzero_tracer.find_messages_by_wallet() устраняет саму необходимость
    угадывать промежуточные контракты: LayerZero Scan уже знает итоговый tx
    хэш и настоящего отправителя (source.tx.from, то же поле, что
    find_bridge_crossing() предпочитает pathway.sender.address — см. его
    докстринг) независимо от того, сколько контрактов было между
    пользователем и OFT.

    Реестр известных Tron-OApp (фильтр "это сообщение через USDT0, а не
    какой-то другой протокол вне scope MVP") — живым запросом (с диск-
    кэшем) к официальному USDT0 Deployments API через
    usdt0_deployments.get_tron_oft_contracts() — см. этот модуль, почему.
    """
    hex_addr = "0x" + base58_to_hex(tron_address)
    messages, tron_contracts = await asyncio.gather(
        asyncio.to_thread(lz.find_messages_by_wallet, hex_addr),
        asyncio.to_thread(get_tron_oft_contracts),
    )
    tron_contracts_hex = {("0x" + base58_to_hex(addr)).lower(): name for addr, name in tron_contracts.items()}

    for msg in messages:
        pathway = msg.get("pathway") or {}
        if pathway.get("srcEid") != 30420:
            continue  # это сообщение НА Tron (адрес — receiver), не депозит С Tron

        sender_hex = ((pathway.get("sender") or {}).get("address") or "").lower()
        oapp_name = tron_contracts_hex.get(sender_hex)
        if oapp_name is None:
            continue  # сообщение через OApp вне текущего реестра — вне scope MVP

        source_tx = (msg.get("source") or {}).get("tx") or {}
        tx_hash = source_tx.get("txHash")
        if not tx_hash:
            continue

        depositor = source_tx.get("from")
        hop: dict[str, Any] = {
            "segment": "tron_deposit",
            "chain": "Tron",
            "from_address": depositor,
            "oapp": oapp_name,
            "tx_hash": tx_hash,
            "timestamp": source_tx.get("blockTimestamp"),
            "detection_method": "layerzero_scan_wallet_fallback",
        }
        observed = await _annotate_tron_observed_name(depositor) if depositor else None
        if observed is not None:
            hop["from_address_observed_name"] = observed["name"]
            hop["from_address_observed_name_source"] = observed["source"]
        return {"tx_hash": tx_hash, "hop": hop}
    return None


def _parse_iso_timestamp(ts: Optional[str]) -> Optional[float]:
    """Blockscout отдаёт timestamp как ISO8601 с 'Z' ("2026-08-14T23:59:23.000000Z").
    Возвращает unix-время (секунды, float) или None, если распарсить не удалось."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def _annotate_evm_observed_name(chain_id: int, address: str) -> Optional[dict[str, Any]]:
    """
    Читает/пишет observed_name_tags.py для EVM-адреса — Blockscout Pro
    `get_address_info().name`, заполняется ТОЛЬКО для verified-контрактов
    (см. докстринг observed_name_tags.py, почему это не используется как
    основание для стоп-условия). Кэш-хит не делает сетевого запроса вовсе.
    """
    chain_key = f"evm:{chain_id}"
    cached = observed_name_tags.get_cached(chain_key, address)
    if cached is not None:
        return cached
    try:
        info = await get_address_info(chain_id=chain_id, address=address)
    except Exception:
        return None  # не критично — аннотация информационная, best-effort
    name = info.get("name")
    if not name:
        return None
    return observed_name_tags.write_through(chain_key, address, name, observed_name_tags.SOURCE_BLOCKSCOUT)


async def _annotate_tron_observed_name(address: str) -> Optional[dict[str, Any]]:
    """
    То же самое для Tron — TronGrid `get_account_info().data[0].account_name`
    (hex-encoded on-chain имя аккаунта, см. observed_name_tags.py)."""
    chain_key = "tron"
    cached = observed_name_tags.get_cached(chain_key, address)
    if cached is not None:
        return cached
    try:
        info = await get_tron_account_info(address)
    except Exception:
        return None
    accounts = info.get("data") or []
    raw_name = accounts[0].get("account_name") if accounts else None
    if not raw_name:
        return None
    try:
        name = bytes.fromhex(raw_name).decode("utf-8", errors="replace")
    except ValueError:
        return None
    if not name:
        return None
    return observed_name_tags.write_through(chain_key, address, name, observed_name_tags.SOURCE_TRONGRID)


async def _with_observed_evm_name(result: dict[str, Any], chain_id: int) -> dict[str, Any]:
    """Аннотирует result['final_address'] самоуказанным именем — ТОЛЬКО
    когда у результата нет курируемой метки (result['label'] is None, т.е.
    RESTED_AT_ADDRESS/MAX_HOPS_REACHED/SEARCH_DEPTH_EXCEEDED). Чисто
    информационно — final_status не меняется."""
    if result.get("label") is not None or result.get("final_address") is None:
        return result
    observed = await _annotate_evm_observed_name(chain_id, result["final_address"])
    if observed is not None:
        result["observed_name"] = observed["name"]
        result["observed_name_source"] = observed["source"]
    return result


def _known_evm_label(
    chain_id: int, address: Optional[str], registry_by_address: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """
    Единая точка "известен ли этот EVM-адрес" — сверяется с ДВУМЯ реестрами:
    known_contracts.py (биржи/DEX/мосты, вручную заполненный реестр меток
    для стоп-условий пост-bridge обхода) и bridge_registry.py (generic
    Bridge Contract Registry — используется здесь как задел на будущее, см.
    докстринг bridge_registry.get_registry_for_evm_chain_id(): сейчас там нет
    записей ни для одной EVM-сети (официальный реестр USDT0 Deployments API
    для EVM даёт только "native"/"legacyMesh" OFT/Composer-адреса — они и
    правда bridge-related, поэтому логично считать их стоп-условием того же
    рода, что и known_contracts.py type="bridge"). Совпадение в ЛЮБОМ из
    двух реестров — известный адрес; known_contracts.py проверяется первым
    (более специфичная, вручную курируемая метка).
    """
    label = lookup_contract(chain_id, address)
    if label is not None:
        return label
    if address is None:
        return None
    entry = registry_by_address.get(address.lower())
    if entry is None:
        return None
    return {"type": "bridge", "name": f"{entry['protocol']} {entry['contract_role']} (bridge_registry)"}


async def _walk_evm(
    chain_id: int, start_address: str, max_hops: int, after_timestamp: Optional[int] = None,
    max_pages_per_hop: int = 4,
) -> dict[str, Any]:
    """
    Линейный пост-bridge обход исходящих ERC-20 переводов от адреса-
    получателя на целевой EVM-сети (chain_id — EIP-155, прокинут насквозь от
    точки выхода из моста, см. LAYERZERO_EID_TO_EVM_CHAIN_ID — evm_adapter
    здесь не завязан на конкретную сеть, работает так же для Arbitrum/Base/
    Polygon и т.д.).

    Один хоп = САМЫЙ РАННИЙ исходящий перевод СРЕДИ ПОДХОДЯЩИХ, случившийся
    НЕ РАНЬШЕ after_timestamp (для первого хопа — время прихода моста; для
    следующих — время предыдущего хопа). Без этого ограничения по времени
    можно случайно выбрать перевод, который физически произошёл ДО того, как
    средства вообще пришли на адрес — обнаружено живым запуском в этой
    сессии: реальный адрес-получатель оказался высокоактивным (свежий
    исходящий перевод примерно раз в 15-20 минут), и без time-фильтра
    "последний элемент страницы" оказался переводом за несколько дней ДО
    прихода трейсящихся средств — очевидно неверным. Это НЕ amount-aware
    taint-трейсинг (см. ограничения в docstring модуля) — просто "первое,
    что случилось после" по времени, без учёта суммы.

    ПАГИНАЦИЯ (до max_pages_per_hop страниц НА ОДИН ХОП)
    -----------------------------------------------------
    Blockscout отдаёт страницу по убыванию времени (свежие первыми) — то
    есть более поздние страницы содержат БОЛЕЕ СТАРЫЕ переводы. Если на
    текущей странице после time-фильтра не осталось ни одного подходящего
    кандидата — переходим к следующей странице через cursor (next_page_params
    предыдущего ответа), а не сразу сдаёмся на RESTED_AT_ADDRESS. Как только
    на КАКОЙ-ТО странице находится хотя бы один подходящий кандидат —
    дальнейшие страницы для этого хопа не запрашиваются: "самый ранний
    подходящий на этой странице" побеждает, даже если на более старой
    странице (которую мы уже не смотрим) мог бы найтись кандидат с ещё более
    ранним временем, ведущий на известный адрес — приоритет "это, вероятно,
    прямое продолжение именно этих денег" выше, чем удобство найти
    терминальный статус пораньше (см. следующий абзац и тест
    test_walk_evm_prefers_earliest_over_known_address_match).

    Известный кандидат (проверка ПОСЛЕ выбора, не вместо него)
    -------------------------------------------------------------
    Как только "самый ранний подходящий" кандидат выбран на своей странице,
    его АДРЕС-ПОЛУЧАТЕЛЬ (не все кандидаты страницы!) сверяется с реестрами
    известных адресов (см. _known_evm_label — known_contracts.py +
    bridge_registry.py). Если известен — это финальный хоп сразу здесь (без
    лишней итерации цикла на "текущий = известный адрес" — тот же итог, что
    и раньше, просто без одного лишнего прохода). Если неизвестен — обычное
    продолжение обхода. Реестр НЕ влияет на то, какой кандидат выбирается
    (это делает только time-anchoring) — только на то, останавливаемся мы
    на нём или продолжаем.

    Если после max_pages_per_hop страниц НИ на одной не нашлось подходящего
    кандидата, но у Blockscout ещё оставались более старые страницы (последняя
    просмотренная страница имела непустой next_page_params) — это
    SEARCH_DEPTH_EXCEEDED, а НЕ RESTED_AT_ADDRESS: не хватило глубины
    поиска, а не "дальше правда некуда идти" (тот второй случай — когда
    Blockscout сам подтвердил конец данных, next_page_params пуст, до
    исчерпания max_pages_per_hop). Различие важно: тихий ложный тупик хуже
    честного "не досмотрели".
    """
    hops: list[dict[str, Any]] = []
    current = start_address
    current_tx_hash: Optional[str] = None
    not_before = after_timestamp

    try:
        registry_entries = await asyncio.to_thread(bridge_registry.get_registry_for_evm_chain_id, chain_id)
    except Exception:
        registry_entries = []  # USDT0 Deployments API недоступен — деградация до known_contracts.py-only
    registry_by_address = {e["address"].lower(): e for e in registry_entries}

    for hop_number in range(1, max_hops + 1):
        label = _known_evm_label(chain_id, current, registry_by_address)
        if label is not None:
            status = "RESTED_AT_EXCHANGE" if label["type"] == "exchange" else "RESTED_AT_CONTRACT"
            return {"hops": hops, "final_status": status, "final_address": current,
                    "final_tx_hash": current_tx_hash, "label": label}

        next_transfer = None
        cursor: Optional[dict[str, Any]] = None
        pages_fetched = 0
        hit_natural_end = False
        while pages_fetched < max_pages_per_hop:
            page = await get_token_transfers(
                chain_id=chain_id, address=current, token_standard="ERC-20",
                limit=MAX_EVM_TRANSFERS_PER_PAGE, cursor=cursor,
            )
            pages_fetched += 1
            outgoing = [
                item for item in page.get("items", [])
                if ((item.get("from") or {}).get("hash") or "").lower() == current.lower()
            ]
            if not_before is not None:
                outgoing = [
                    item for item in outgoing
                    if (ts := _parse_iso_timestamp(item.get("timestamp"))) is not None and ts >= not_before
                ]
            if outgoing:
                # Самый ранний среди подходящих (после time-фильтра подмножество
                # может быть не строго упорядочено с "последним элементом = самым ранним").
                next_transfer = min(outgoing, key=lambda it: _parse_iso_timestamp(it.get("timestamp")) or float("inf"))
                break

            next_cursor = page.get("next_page_params")
            if not next_cursor:
                hit_natural_end = True
                break
            cursor = next_cursor

        if next_transfer is None:
            if hit_natural_end:
                return await _with_observed_evm_name(
                    {"hops": hops, "final_status": "RESTED_AT_ADDRESS", "final_address": current,
                     "final_tx_hash": current_tx_hash, "label": None},
                    chain_id,
                )
            return await _with_observed_evm_name(
                {
                    "hops": hops, "final_status": "SEARCH_DEPTH_EXCEEDED", "final_address": current,
                    "final_tx_hash": current_tx_hash, "label": None, "pages_examined": pages_fetched,
                    "note": (
                        f"Не хватило глубины поиска: просмотрено {pages_fetched} страниц(ы) Blockscout "
                        f"(лимит max_pages_per_hop={max_pages_per_hop}), ни на одной не нашлось исходящего "
                        "перевода не раньше времени прихода средств — но у Blockscout ещё оставались более "
                        "старые страницы (next_page_params не был пуст на последней просмотренной). Это НЕ "
                        "означает тупик — увеличьте max_pages_per_hop, если нужно досмотреть глубже."
                    ),
                },
                chain_id,
            )

        to_addr = (next_transfer.get("to") or {}).get("hash")
        if to_addr is None:
            # напр. burn/контракт без явного получателя — дальше трейсить некуда
            return await _with_observed_evm_name(
                {"hops": hops, "final_status": "RESTED_AT_ADDRESS", "final_address": current,
                 "final_tx_hash": current_tx_hash, "label": None},
                chain_id,
            )

        tx_hash = next_transfer.get("transaction_hash")

        if to_addr.lower() == ZERO_ADDRESS:
            # Burn: Transfer(from, 0x0, amount) — токен уничтожен, адрес 0x0 не
            # принадлежит никому и не может быть "следующим получателем" для
            # дальнейшего обхода. Обнаружено живым запуском с платным ключом
            # Blockscout Pro (более полные данные, чем на бесплатном тире, где
            # эта ветка ни разу не встретилась): без этой проверки трейсер брал
            # "следующий исходящий перевод FROM 0x0" — а это ЛЮБОЙ mint любого
            # токена на всей сети (Transfer(0x0, кто угодно, что угодно)), никак
            # не связанный с трейсящимися средствами — результат был откровенно
            # бессмысленным ("путь" внезапно утыкался в случайный чужой mint).
            total = next_transfer.get("total") or {}
            token = next_transfer.get("token") or {}
            hops.append({
                "segment": "evm_hop", "chain_id": chain_id, "hop_number": hop_number,
                "from_address": current, "to_address": to_addr, "tx_hash": tx_hash,
                "token_symbol": token.get("symbol"), "value_raw": total.get("value"),
                "value_decimals": total.get("decimals"), "timestamp": next_transfer.get("timestamp"),
            })
            return {"hops": hops, "final_status": "RESTED_AT_ADDRESS", "final_address": to_addr,
                    "final_tx_hash": tx_hash,
                    "label": {"type": "burn", "name": "Burned (Transfer to null address)"}}

        if tx_hash is not None and tx_hash == current_tx_hash:
            # Тот же tx_hash, что у предыдущего хопа, — значит это ДРУГОЙ
            # Transfer-лог ВНУТРИ той же атомарной транзакции (напр. несколько
            # legs одного свопа/арбитража через 2+ пула), а не отдельное
            # последующее движение средств. Обнаружено живым запуском: адрес-
            # получатель на реальных данных 4 раза подряд "прыгал" между теми
            # же двумя контрактами внутри одной tx (USDT/LINK туда-обратно) —
            # без этой проверки трейс превращался в бессмысленный цикл. Резолвить
            # сам своп — вне scope MVP (см. docstring модуля), поэтому просто
            # останавливаемся здесь как на нераспознанном контракте.
            return {"hops": hops, "final_status": "RESTED_AT_CONTRACT", "final_address": current,
                    "final_tx_hash": current_tx_hash,
                    "label": {"type": "dex_swap", "name": "Unresolved swap-like contract (multiple transfers in one tx)"}}
        total = next_transfer.get("total") or {}
        token = next_transfer.get("token") or {}
        hops.append({
            "segment": "evm_hop",
            "chain_id": chain_id,
            "hop_number": hop_number,
            "from_address": current,
            "to_address": to_addr,
            "tx_hash": tx_hash,
            "token_symbol": token.get("symbol"),
            "value_raw": total.get("value"),
            "value_decimals": total.get("decimals"),
            "timestamp": next_transfer.get("timestamp"),
        })

        # Проверка ПОСЛЕ выбора кандидата (см. докстринг функции) — тот же
        # итог, что дал бы top-of-loop label-чек на следующей итерации (см.
        # выше), просто на один проход раньше и без лишнего API-запроса.
        arrival_label = _known_evm_label(chain_id, to_addr, registry_by_address)
        if arrival_label is not None:
            status = "RESTED_AT_EXCHANGE" if arrival_label["type"] == "exchange" else "RESTED_AT_CONTRACT"
            return {"hops": hops, "final_status": status, "final_address": to_addr,
                    "final_tx_hash": tx_hash, "label": arrival_label}

        current = to_addr
        current_tx_hash = tx_hash
        not_before = _parse_iso_timestamp(next_transfer.get("timestamp"))

    return await _with_observed_evm_name(
        {"hops": hops, "final_status": "MAX_HOPS_REACHED", "final_address": current,
         "final_tx_hash": current_tx_hash, "label": None},
        chain_id,
    )


def _label_fields(label: Optional[dict[str, Any]]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if label is None:
        return None, None, None
    if label["type"] == "exchange":
        return label["name"], None, None
    return None, label["name"], label["type"]


def _flat_result(
    final_status: str,
    hops: list[dict[str, Any]],
    final_chain: Optional[str] = None,
    final_address: Optional[str] = None,
    final_tx_hash: Optional[str] = None,
    exchange_name: Optional[str] = None,
    contract_label: Optional[str] = None,
    contract_type: Optional[str] = None,
    note: Optional[str] = None,
    observed_name: Optional[str] = None,
    observed_name_source: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "final_status": final_status,
        "final_chain": final_chain,
        "final_address": final_address,
        "final_tx_hash": final_tx_hash,
        "exchange_name": exchange_name,
        "contract_label": contract_label,
        "contract_type": contract_type,
        "hops": hops,
        "note": note,
        # Самоуказанное имя адреса (Blockscout Pro `name`, только verified-
        # контракты) — ИНФОРМАЦИОННО, не влияет на final_status/маршрут, не
        # то же самое, что contract_label (курируемая метка). См.
        # observed_name_tags.py, почему разделены.
        "observed_name": observed_name,
        "observed_name_source": observed_name_source,
    }


def print_result(result: dict[str, Any]) -> None:
    print("=" * 70)
    print("ПОЛНЫЙ ПУТЬ ТРАНЗАКЦИИ")
    print("=" * 70)
    for i, hop in enumerate(result["hops"], start=1):
        print(f"[{i}] {hop}")
    print()
    print(f"Итоговый статус:   {result['final_status']}")
    print(f"Итоговая сеть:     {result['final_chain']}")
    print(f"Итоговый адрес:    {result['final_address']}")
    print(f"Итоговый tx hash:  {result['final_tx_hash']}")
    if result["exchange_name"]:
        print(f"Биржа:             {result['exchange_name']}")
    if result["contract_label"]:
        print(f"Контракт:          {result['contract_label']} ({result['contract_type']})")
    if result["note"]:
        print(f"\nПримечание: {result['note']}")


async def _main_async(args: argparse.Namespace) -> None:
    try:
        result = await trace_full_path(args.start, start_type=args.start_type, max_hops=args.max_hops)
        print_result(result)
    finally:
        await close_clients()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сквозной трейсер пути транзакции: LayerZero Scan (Tron) -> LayerZero Scan (bridge) -> Blockscout Pro (EVM)"
    )
    parser.add_argument("--start", required=True, help="Tron-адрес отправителя или хэш депозитной tx на Tron")
    parser.add_argument("--start-type", choices=["address", "tx_hash"], default="address")
    parser.add_argument("--max-hops", type=int, default=DEFAULT_MAX_HOPS)
    args = parser.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
