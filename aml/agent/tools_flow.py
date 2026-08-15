# agent/tools_flow.py
"""
Обёртка над flow_tracer.trace_flow как тулза для ollama (Execution Layer,
раздел 2-3 архитектуры: субагент 2, Flow & Hop Tracer).

В отличие от tools_evm.py/tools_attribution.py, здесь LLM не управляет
глубиной/деталями обхода напрямую — max_hops берётся по умолчанию из mode
(см. flow_tracer.hop_tracer.DEFAULT_MAX_HOPS), а не как параметр тулзы: это
осознанное архитектурное решение, а не глубина по вкусу модели, чтобы
избежать неконтролируемо дорогого обхода графа по прихоти LLM.
"""

import json
import uuid
from typing import Optional

from flow_tracer import trace_flow


async def trace_flow_tool(
    address: str,
    chain_id: int,
    mode: str,
    investigation_id: Optional[str] = None,
) -> str:
    """Проследить движение средств от адреса (Flow & Hop Tracer): обходит
    исходящие переводы, резолвит свопы через известные DEX, останавливается на
    известных санкционных/exchange-адресах или по достижении лимита глубины.

    Args:
        address: EVM-адрес, с которого начинается трейс.
        chain_id: ID сети (1 = Ethereum, 56 = BNB Chain, 137 = Polygon, 42161 = Arbitrum One, 10 = Optimism, 8453 = Base).
        mode: "incident_response" (полный многохоповый трейс — куда ушли средства),
              "aml_check" (только непосредственные контрагенты),
              "wallet_labeling" (тулза не выполняет обход, приоритет низкий по архитектуре).
        investigation_id: UUID текущего расследования (передаётся Оркестратором
            для связности evidence_log/flow_edges между вызовами разных
            субагентов в рамках одного расследования); если не передан — новый
            UUID генерируется внутри (тогда рёбра этого вызова не свяжутся с
            остальным evidence того же расследования).

    Returns:
        JSON-строка: {"visited_addresses": [...], "edges_written": N,
        "terminal_nodes": [{"address": ..., "reason": ...}, ...]}.
    """
    inv_uuid = uuid.UUID(investigation_id) if investigation_id else None
    result = await trace_flow(
        start_address=address, chain_id=chain_id, mode=mode, investigation_id=inv_uuid,
    )
    return json.dumps({
        "investigation_id": str(result.investigation_id),
        "visited_addresses": result.visited_addresses,
        "edges_written": result.edges_written,
        "terminal_nodes": result.terminal_nodes,
    }, ensure_ascii=False)


FLOW_TOOLS = [trace_flow_tool]
FLOW_TOOL_DISPATCH = {fn.__name__: fn for fn in FLOW_TOOLS}
