#!/usr/bin/env python3
"""
bridge_tracer.py — Layer 1: сквозной трейсер полного пути транзакции через
мост LayerZero. Склеивает три независимых сетевых адаптера в один
самодостаточный async-модуль:

    TronGrid (aml/tron_adapter) -> LayerZero Scan (layerzero_tracer.py)
        -> Blockscout Pro (aml/evm_adapter, любая EVM-сеть, куда пришёл мост)

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
- Шаг 1 (TronGrid) распознаёт депозит в мост только по статическому
  реестру TRON_BRIDGE_DEPOSIT_CONTRACTS ниже — сейчас там один адрес
  (USDT0 OFT на Tron), подтверждённый живым запросом в этой сессии
  (см. docstring find_real_tx в истории сессии и aml/tron_adapter/README.md).
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
from known_contracts import lookup_contract  # noqa: E402
from tron_adapter import (  # noqa: E402
    get_trc20_transfers,
    close_client as _close_tron_client,
    addresses_equal,
    normalize_tx_hash,
)
from evm_adapter import get_token_transfers, close_client as _close_evm_client  # noqa: E402

# Известные адреса LayerZero OApp-контрактов на исходных НЕ-EVM сетях —
# нужны, чтобы распознать "этот исходящий TRC-20 Transfer — депозит в мост",
# а не случайный перевод куда-то ещё. Для этого тестового задания достаточно
# одного адреса (единственный сценарий, который демонстрируется — TRON ->
# Ethereum через USDT0), но структура словаря готова к расширению на другие
# OApp/сети без изменения кода.
TRON_BRIDGE_DEPOSIT_CONTRACTS: dict[str, str] = {
    # USDT0 OFT на Tron mainnet. Адрес подтверждён дважды живыми запросами в
    # этой сессии: (1) docs.usdt0.to/api/deployments -> lzEid=30420, (2)
    # обратное сопоставление через LayerZero Scan API
    # /v1/messages/oapp/30420/{hex-адрес} нашло реальные DELIVERED-сообщения
    # Tron<->Ethereum с этим контрактом как sender/receiver.
    "TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt": "USDT0 OFT (Tron)",
}

DEFAULT_MAX_HOPS = 10
MAX_EVM_TRANSFERS_PER_PAGE = 50
ZERO_ADDRESS = "0x" + "0" * 40


async def close_clients() -> None:
    """Закрывает aiohttp-сессии tron_adapter и evm_adapter. Вызывать один раз
    по завершении работы с trace_full_path (см. main() ниже)."""
    await _close_tron_client()
    await _close_evm_client()


async def trace_full_path(
    start: str,
    start_type: Literal["address", "tx_hash"] = "address",
    mode: str = "incident_response",
    max_hops: int = DEFAULT_MAX_HOPS,
) -> dict[str, Any]:
    """
    Единая входная точка: прослеживает полный путь средств от адреса/tx на
    Tron через мост LayerZero до точки, где трейс останавливается (биржа,
    DEX/другой мост, тупик или max_hops).

    Args:
        start: Tron-адрес отправителя (start_type="address") или хэш
            транзакции депозита в мост на Tron (start_type="tx_hash") —
            во втором случае шаг 1 (TronGrid) пропускается.
        start_type: "address" | "tx_hash".
        mode: пробрасывается в метаданные результата для совместимости с
            остальной платформой (aml/agent/tools_*.py тоже принимают mode);
            в этой версии MVP поведение самого трейсера от mode не зависит —
            все режимы используют одинаковую глубину/логику обхода.
        max_hops: защита от бесконечной рекурсии/циклов на EVM-сегменте.

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
                    f"Среди исходящих TRC-20 переводов с адреса {start} не найден "
                    "депозит на известный bridge-контракт LayerZero (проверяется "
                    "только реестр TRON_BRIDGE_DEPOSIT_CONTRACTS — сейчас в нём "
                    "только USDT0 OFT). Либо адрес не связан с переводом через "
                    "LayerZero, либо использован bridge-контракт вне текущего "
                    "реестра, либо перевод старше окна выборки TronGrid."
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
    crossing = await asyncio.to_thread(lz.find_bridge_crossing, bridge_tx_hash)
    if not crossing["found"]:
        return _flat_result(final_status="BRIDGE_MESSAGE_NOT_FOUND", hops=hops, note=crossing["note"])

    hops.append({
        "segment": "bridge",
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
    })

    dest_chain_name = crossing["bridge_exit"]["chain"]
    recipient = crossing["bridge_exit"]["to_address"]
    dest_tx_hash = crossing["bridge_exit"]["tx_hash"]

    if dest_tx_hash is None:
        return _flat_result(
            final_status="IN_TRANSIT",
            hops=hops,
            final_chain=dest_chain_name,
            final_address=recipient,
            note=crossing["note"],
        )

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
    )


async def _find_tron_bridge_deposit(tron_address: str) -> Optional[dict[str, Any]]:
    """
    Ищет среди исходящих TRC-20 Transfer-событий адреса перевод на известный
    bridge-контракт (TRON_BRIDGE_DEPOSIT_CONTRACTS). TronGrid по умолчанию
    отдаёт события по убыванию времени, поэтому первое совпадение — самый
    свежий депозит.
    """
    result = await get_trc20_transfers(address=tron_address, only_from=True, limit=200)
    for item in result.get("data", []):
        if item.get("type") != "Transfer":
            continue  # TronGrid отдаёт в одном списке и Transfer, и Approval
        to_addr = item.get("to")
        if to_addr is None:
            continue
        matched_name = next(
            (name for bridge_addr, name in TRON_BRIDGE_DEPOSIT_CONTRACTS.items()
             if addresses_equal(to_addr, bridge_addr)),
            None,
        )
        if matched_name is None:
            continue
        tx_hash = "0x" + normalize_tx_hash(item["transaction_id"])
        token_info = item.get("token_info") or {}
        return {
            "tx_hash": tx_hash,
            "hop": {
                "segment": "tron_deposit",
                "chain": "Tron",
                "from_address": item.get("from"),
                "to_address": to_addr,
                "to_label": matched_name,
                "tx_hash": tx_hash,
                "token_symbol": token_info.get("symbol"),
                "value_raw": item.get("value"),
                "value_decimals": token_info.get("decimals"),
                "timestamp": item.get("block_timestamp"),
            },
        }
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


async def _walk_evm(
    chain_id: int, start_address: str, max_hops: int, after_timestamp: Optional[int] = None,
) -> dict[str, Any]:
    """
    Линейный пост-bridge обход исходящих ERC-20 переводов от адреса-
    получателя на целевой EVM-сети (chain_id — EIP-155, прокинут насквозь от
    точки выхода из моста, см. LAYERZERO_EID_TO_EVM_CHAIN_ID — evm_adapter
    здесь не завязан на конкретную сеть, работает так же для Arbitrum/Base/
    Polygon и т.д.).

    Один хоп = САМЫЙ РАННИЙ исходящий перевод СРЕДИ вернувшейся страницы
    Blockscout, случившийся НЕ РАНЬШЕ after_timestamp (для первого хопа —
    время прихода моста; для следующих — время предыдущего хопа). Без этого
    ограничения по времени можно случайно выбрать перевод, который физически
    произошёл ДО того, как средства вообще пришли на адрес — обнаружено
    живым запуском в этой сессии: реальный адрес-получатель оказался
    высокоактивным (свежий исходящий перевод примерно раз в 15-20 минут), и
    без time-фильтра "последний элемент страницы" оказался переводом за
    несколько дней ДО прихода трейсящихся средств — очевидно неверным. Это
    НЕ amount-aware taint-трейсинг (см. ограничения в docstring модуля) —
    просто "первое, что случилось после" по времени, без учёта суммы.

    Известное ограничение: если после after_timestamp у адреса было больше
    MAX_EVM_TRANSFERS_PER_PAGE исходящих ERC-20 переводов ДО следующего
    релевантного, страница может не дотянуться — тогда это ошибочно
    прочитается как RESTED_AT_ADDRESS (тупик), хотя на самом деле просто не
    хватило глубины одной страницы. Для MVP не пагинируется дальше одной
    страницы (см. max_branch-ограничение с тем же компромиссом в
    aml/flow_tracer/hop_tracer.py).
    """
    hops: list[dict[str, Any]] = []
    current = start_address
    current_tx_hash: Optional[str] = None
    not_before = after_timestamp

    for hop_number in range(1, max_hops + 1):
        label = lookup_contract(chain_id, current)
        if label is not None:
            status = "RESTED_AT_EXCHANGE" if label["type"] == "exchange" else "RESTED_AT_CONTRACT"
            return {"hops": hops, "final_status": status, "final_address": current,
                    "final_tx_hash": current_tx_hash, "label": label}

        page = await get_token_transfers(
            chain_id=chain_id, address=current, token_standard="ERC-20", limit=MAX_EVM_TRANSFERS_PER_PAGE,
        )
        outgoing = [
            item for item in page.get("items", [])
            if ((item.get("from") or {}).get("hash") or "").lower() == current.lower()
        ]
        if not_before is not None:
            outgoing = [
                item for item in outgoing
                if (ts := _parse_iso_timestamp(item.get("timestamp"))) is not None and ts >= not_before
            ]
        if not outgoing:
            return {"hops": hops, "final_status": "RESTED_AT_ADDRESS", "final_address": current,
                    "final_tx_hash": current_tx_hash, "label": None}

        # Самый ранний среди подходящих (Blockscout отдаёт страницу по
        # убыванию времени, но после time-фильтра подмножество может быть
        # не строго упорядочено с "последним элементом = самым ранним").
        next_transfer = min(outgoing, key=lambda it: _parse_iso_timestamp(it.get("timestamp")) or float("inf"))
        to_addr = (next_transfer.get("to") or {}).get("hash")
        if to_addr is None:
            # напр. burn/контракт без явного получателя — дальше трейсить некуда
            return {"hops": hops, "final_status": "RESTED_AT_ADDRESS", "final_address": current,
                    "final_tx_hash": current_tx_hash, "label": None}

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
        current = to_addr
        current_tx_hash = tx_hash
        not_before = _parse_iso_timestamp(next_transfer.get("timestamp"))

    return {"hops": hops, "final_status": "MAX_HOPS_REACHED", "final_address": current,
            "final_tx_hash": current_tx_hash, "label": None}


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
        description="Сквозной трейсер пути транзакции: TronGrid -> LayerZero -> Blockscout Pro (EVM)"
    )
    parser.add_argument("--start", required=True, help="Tron-адрес отправителя или хэш депозитной tx на Tron")
    parser.add_argument("--start-type", choices=["address", "tx_hash"], default="address")
    parser.add_argument("--max-hops", type=int, default=DEFAULT_MAX_HOPS)
    args = parser.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
