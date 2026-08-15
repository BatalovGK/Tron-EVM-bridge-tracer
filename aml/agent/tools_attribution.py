# agent/tools_attribution.py
"""Обёртка над attribution.service.check_address для использования Qwen3-Coder
как тулзы (аналогично agent/tools_evm.py — докстринг Google-style для
автогенерации схемы через ollama)."""

import json

from attribution.service import check_address as _check_address


async def check_sanctions_tool(address: str, chain_id: int) -> str:
    """Проверить адрес на санкции и вредоносную активность через OFAC, GoPlus
    и OpenSanctions (Attribution & Labeling Engine).

    Args:
        address: EVM-адрес в формате 0x..., копировать целиком, без сокращений.
        chain_id: ID сети (1 = Ethereum, 56 = BNB Chain, 137 = Polygon, 42161 = Arbitrum One, 10 = Optimism, 8453 = Base).

    Returns:
        JSON-строка с результатом: санкции OFAC, флаги риска GoPlus, совпадения
        OpenSanctions (из локального bulk-кэша, обновляется отдельным
        процессом — не живой запрос), итоговая оценка риска (low/medium/high).
    """
    result = await _check_address(address=address, chain_id=chain_id)
    return json.dumps(result, ensure_ascii=False, default=str)


ATTRIBUTION_TOOLS = [check_sanctions_tool]
ATTRIBUTION_TOOL_DISPATCH = {fn.__name__: fn for fn in ATTRIBUTION_TOOLS}
