# flow_tracer/hop_tracer.py
"""
Flow & Hop Tracer (субагент 2, раздел 2 архитектуры) для EVM.

Идёт BFS-обходом по исходящим переводам от стартового адреса: обычный transfer
(native/ERC-20) — прямой хоп на адрес получателя; перевод на известный DEX-
контракт — резолвится через swap_resolver.resolve_swap (TheGraph), и уже
результат свопа (token_out, получатель) определяет, продолжается ли хоп.

Poison/taint-метод (согласовано для mode="incident_response", см. summary
сессии — раздел "Открытый вопрос №1 архитектуры — РЕШЁН"): любой адрес,
получивший средства от заражённого предка, целиком помечается заражённым
(tainted=True), независимо от доли/суммы. Для остальных mode taint не
проставляется (не их методология — aml_check использует бинарный contact-
check через label_cache, не полноценный forward-tracing).

ВАЖНО, ограничения v1 (зафиксированы явно, не по умолчанию):
- Мосты (bridges) НЕ детектируются отдельно в этой версии — согласовано вернуться
  к этому после остальных сетевых адаптеров (BTC/TRON). Перевод на bridge-контракт
  в v1 обрабатывается как обычный непрораспознанный transfer (не как своп, раз
  bridge не входит в dex_contracts.yaml) — хоп продолжится на сам bridge-контракт
  как на "получателя", что для форензик-смысла неверно (деньги ушли на другую
  сеть, а не осели на этом адресе), но явно не скрывается: такие узлы стоит
  считать подозрительными на "возможно мост, не распознан" при разборе руками.
- Поля ответов Blockscout Pro API (from/to как {"hash": ...}, amount как
  строка в minimal units) — по документированной v2-схеме; НЕ проверено на
  живом сервере в рамках этой сессии (в отличие от attribution/ модулей,
  которые проверялись на реальных серверах). Проверить через
  scripts/manual_check_flow_tracer.py на реальном Blockscout Pro API перед
  тем как доверять результатам этого модуля.
- max_branch ограничивает исходящие переводы по каждому адресу первыми
  `max_branch` штук по данным Blockscout (обычно уже отсортированы по времени/
  блоку) — это не "топ по сумме", а простое усечение, чтобы не взорвать граф
  на адресах с тысячами исходящих транзакций (напр. случайно затронутый CEX
  hot wallet). Если нужно топ-по-сумме — TODO на будущее.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from common.evidence_store import (
    get_labels,
    upsert_seed,
    write_evidence,
    write_flow_edge,
)
from evm_adapter.adapter import get_address_transactions, get_token_transfers
from flow_tracer.swap_resolver import identify_dex_protocol, resolve_swap
from flow_tracer.thegraph import TheGraphClient

logger = logging.getLogger(__name__)

DEFAULT_MAX_HOPS = {
    "incident_response": 5,   # высокий приоритет, глубокий трейс — раздел 2
    "aml_check": 1,           # средний приоритет — только непосредственные контрагенты
    "wallet_labeling": 0,     # низкий/не нужен по архитектуре — трейс не запускается
}
DEFAULT_MAX_BRANCH = 10


@dataclass
class HopNode:
    address: str
    hop_number: int
    tainted: bool


@dataclass
class TraceResult:
    investigation_id: uuid.UUID
    visited_addresses: List[str] = field(default_factory=list)
    edges_written: int = 0
    terminal_nodes: List[Dict[str, Any]] = field(default_factory=list)  # {"address", "reason"}


def _extract_address(field_value: Any) -> Optional[str]:
    """Blockscout v2 отдаёт from/to как {"hash": "0x..."} для EOA/контрактов;
    иногда как None (напр. контракт-деплой). Нормализует к строке или None."""
    if field_value is None:
        return None
    if isinstance(field_value, dict):
        return field_value.get("hash")
    if isinstance(field_value, str):
        return field_value
    return None


def _is_known_risky(labels: List[Dict[str, Any]]) -> Optional[str]:
    """Возвращает причину остановки, если среди меток есть санкции/известная
    биржа, иначе None. 'known_exchange' здесь — заготовка: пока проставляется
    только через будущий Attribution-флаг label_type='exchange' в label_cache
    (сейчас туда ничего с таким label_type не пишется — этот кейс появится
    вместе с Attribution Engine-side детекцией off-ramp)."""
    for label in labels:
        if label["source"] in ("ofac_sdn", "opensanctions_bulk"):
            return "sanctioned"
        if label.get("label_type") == "exchange":
            return "known_exchange"
    return None


async def trace_flow(
    start_address: str,
    chain_id: int,
    mode: str,
    investigation_id: Optional[uuid.UUID] = None,
    max_hops: Optional[int] = None,
    max_branch: int = DEFAULT_MAX_BRANCH,
    thegraph_client: Optional[TheGraphClient] = None,
    write_to_seed_registry: bool = True,
) -> TraceResult:
    """
    Обходит граф исходящих переводов от start_address, пишет рёбра в evidence_log
    (сырое) и flow_edges (структурированное), и — для mode="incident_response" —
    заводит новые адреса в seed_registry (согласовано: on-demand трейс пишет
    в Seed Registry так же, как и Watcher-триггер, раздел 5.3).

    Args:
        start_address: адрес, с которого начинается трейс.
        chain_id: ID EVM-сети.
        mode: "aml_check" | "incident_response" | "wallet_labeling" — определяет
            глубину обхода по умолчанию (см. DEFAULT_MAX_HOPS) и включение
            poison/taint-разметки (только incident_response).
        investigation_id: UUID расследования; генерируется, если не передан.
        max_hops: переопределяет глубину обхода по умолчанию для mode.
        max_branch: максимум исходящих переводов на обрабатываемый адрес (см.
            ограничение в докстринге модуля).
        thegraph_client: готовый клиент (для тестов); иначе создаётся новый.
        write_to_seed_registry: если False — не писать в seed_registry даже
            для incident_response (напр. для разовой ручной проверки без
            побочных эффектов на реестр).

    Returns:
        TraceResult со списком посещённых адресов, числом записанных рёбер и
        терминальными узлами (где обход остановился и почему).
    """
    if mode == "wallet_labeling":
        logger.info("wallet_labeling: Flow&Hop Tracer не нужен по приоритету режима (раздел 2 архитектуры), пропуск.")
        return TraceResult(investigation_id=investigation_id or uuid.uuid4())

    inv_id = investigation_id or uuid.uuid4()
    hops = max_hops if max_hops is not None else DEFAULT_MAX_HOPS.get(mode, 1)
    # Намеренно НЕ создаём TheGraphClient() здесь жадно: если по ходу трейса не
    # встретится ни одного свопа через известный DEX (обычный трейс из одних
    # transfer'ов), клиент вообще не нужен и не должен требовать THEGRAPH_API_KEY.
    # resolve_swap() сам лениво создаёт клиента по умолчанию, если он не передан.

    result = TraceResult(investigation_id=inv_id)
    visited: set = set()
    queue: List[HopNode] = [HopNode(address=start_address.lower(), hop_number=0, tainted=(mode == "incident_response"))]

    while queue:
        node = queue.pop(0)
        if node.address in visited:
            continue
        visited.add(node.address)
        result.visited_addresses.append(node.address)

        if node.hop_number >= hops:
            result.terminal_nodes.append({"address": node.address, "reason": "max_hops"})
            continue

        existing_labels = await get_labels(node.address, chain_id=chain_id)
        risky_reason = _is_known_risky(existing_labels)
        if risky_reason and node.hop_number > 0:
            # Стартовый адрес (hop_number=0) не останавливаем даже если сам помечен —
            # это адрес, который расследуют, а не найденный по ходу трейса контрагент.
            result.terminal_nodes.append({"address": node.address, "reason": risky_reason})
            continue

        next_hops = await _expand_address(
            address=node.address,
            chain_id=chain_id,
            hop_number=node.hop_number,
            tainted=node.tainted,
            investigation_id=inv_id,
            mode=mode,
            max_branch=max_branch,
            thegraph_client=thegraph_client,
            write_to_seed_registry=write_to_seed_registry and mode == "incident_response",
        )
        result.edges_written += len(next_hops)
        for child in next_hops:
            if child.address not in visited:
                queue.append(child)

    return result


async def _expand_address(
    address: str,
    chain_id: int,
    hop_number: int,
    tainted: bool,
    investigation_id: uuid.UUID,
    mode: str,
    max_branch: int,
    thegraph_client: Optional[TheGraphClient],
    write_to_seed_registry: bool,
) -> List[HopNode]:
    """Находит исходящие переводы с адреса, резолвит свопы через известные DEX,
    пишет рёбра и возвращает список следующих узлов для обхода."""
    next_nodes: List[HopNode] = []

    native_txs = await get_address_transactions(chain_id=chain_id, address=address, limit=max_branch)
    token_txs = await get_token_transfers(chain_id=chain_id, address=address, limit=max_branch)

    await write_evidence(
        investigation_id=investigation_id, mode=mode, subagent="flow_hop_tracer",
        action="get_outgoing_transfers", payload={"native": native_txs, "token": token_txs},
        source="blockscout_pro_api", chain_id=chain_id, cached=False,
    )

    all_items = list(native_txs.get("items", [])) + list(token_txs.get("items", []))
    seen_tx_hashes: set = set()

    for item in all_items[:max_branch]:
        from_addr = _extract_address(item.get("from"))
        if from_addr is None or from_addr.lower() != address:
            continue  # интересуют только исходящие переводы

        tx_hash = item.get("hash") or item.get("transaction_hash")
        if not tx_hash or tx_hash in seen_tx_hashes:
            continue
        seen_tx_hashes.add(tx_hash)

        to_addr = _extract_address(item.get("to"))
        if to_addr is None:
            continue  # напр. контракт-деплой без явного получателя

        edge_kind = "transfer"
        child_address = to_addr.lower()
        # token — всегда строка ('native' для нативного перевода, иначе адрес
        # контракта токена), не dict: колонка token в БД — TEXT.
        token_field = item.get("token")
        token: Optional[str] = token_field.get("address") if isinstance(token_field, dict) else "native"
        amount = _extract_amount(item)

        if identify_dex_protocol(chain_id, to_addr) is not None:
            swaps = await resolve_swap(chain_id, tx_hash, client=thegraph_client)
            if swaps is None:
                edge_kind = "swap_unresolved"
                # Оставляем child_address = адрес роутера/пула — трейс дальше по
                # этой ветке не имеет смысла продолжать (см. docstring модуля).
            else:
                for swap in swaps:
                    swap_recipient = (swap.get("recipient") or "").lower()
                    if swap_recipient and swap_recipient != address:
                        child_address = swap_recipient
                    edge_kind = "swap"
                    # token_out — dict {"id","symbol","decimals"} из swap_resolver
                    # (см. _normalize_v2/v3_swap), не строка — достаём символ/адрес.
                    token_out = swap.get("token_out") or {}
                    token = token_out.get("symbol") or token_out.get("id")
                    # amount_out — уже decimal-adjusted BigDecimal из подграфа
                    # (не сырые wei), но колонка amount в БД всё равно TEXT —
                    # приводим к строке явно, иначе asyncpg отклонит float.
                    swap_amount_out = swap.get("amount_out")
                    amount = str(swap_amount_out) if swap_amount_out is not None else None

        child_tainted = tainted  # poison-метод: заражение целиком, без пропорции

        await write_flow_edge(
            investigation_id=investigation_id, chain_id=chain_id, hop_number=hop_number + 1,
            parent_address=address, child_address=child_address, tx_hash=tx_hash,
            edge_kind=edge_kind, token=token, amount=amount, tainted=child_tainted,
        )

        if write_to_seed_registry and child_address != address:
            await upsert_seed(
                address=child_address, chain_id=chain_id,
                tag=f"incident_response_trace:{investigation_id}",
                seed_source=f"flow_hop_tracer:{investigation_id}",
                derived_from=address, confidence=0.7,
            )

        if child_address != address:
            next_nodes.append(HopNode(address=child_address, hop_number=hop_number + 1, tainted=child_tainted))

    return next_nodes


def _extract_amount(item: Dict[str, Any]) -> Optional[str]:
    """Blockscout отдаёт value/total как строку в minimal units (wei и т.п.).
    Намеренно НЕ конвертируем в float здесь: суммы в wei легко превышают
    точность double (2^53) уже на величинах порядка 1 ETH — для форензик-
    контекста это неприемлемая потеря точности, поэтому в БД (колонка TEXT)
    и дальше по коду сумма остаётся сырой строкой. Конвертация в human-
    readable с учётом decimals токена — TODO для Behavior Engine, если
    понадобится содержательно сравнивать суммы, а не просто хранить провенанс."""
    raw = item.get("value")
    if raw is None and isinstance(item.get("total"), dict):
        raw = item["total"].get("value")
    if raw is None:
        return None
    return str(raw)
