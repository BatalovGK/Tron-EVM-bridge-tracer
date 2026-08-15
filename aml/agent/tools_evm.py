# agent/tools_evm.py
"""
Тонкие обёртки над evm_adapter, предназначенные для передачи в ollama как тулзы.

Важно: ollama-python умеет сама генерировать JSON-schema тулзы прямо из Python-
функции, если у неё Google-style докстринг (Args:/Returns:). Поэтому здесь не
пишется отдельная JSON-schema руками — она собирается автоматически из этих
докстрингов при `client.chat(..., tools=[get_address_info_tool, ...])`.

Отличия от прямых функций evm_adapter:
- Без параметра `cursor` (пагинация) — это внутренний механизм, LLM не должно
  им управлять напрямую в первой версии; при необходимости можно добавить
  отдельную тулзу "get_next_page" позже.
- Без параметров client/cache — тулзы всегда используют дефолтные синглтоны
  из evm_adapter.adapter (get_client()/get_cache()).
- Каждая функция возвращает JSON-сериализуемую строку (а не dict), т.к. именно
  строку ожидает поле `content` сообщения с ролью "tool".
"""

import json
from typing import Optional

from evm_adapter import (
    get_address_info as _get_address_info,
    get_address_transactions as _get_address_transactions,
    get_internal_transactions as _get_internal_transactions,
    get_token_transfers as _get_token_transfers,
    get_transaction_summary as _get_transaction_summary,
    get_block_by_number as _get_block_by_number,
)


async def get_address_info_tool(chain_id: int, address: str) -> str:
    """Получить базовую информацию об EVM-адресе: баланс, признак контракта.

    Args:
        chain_id: ID сети (1 = Ethereum, 56 = BNB Chain, 137 = Polygon, 42161 = Arbitrum One, 10 = Optimism, 8453 = Base).
        address: EVM-адрес в формате 0x..., копировать целиком, без сокращений.

    Returns:
        JSON-строка с данными об адресе (баланс, тип, метаданные источника).
    """
    result = await _get_address_info(chain_id=chain_id, address=address)
    return json.dumps(result, ensure_ascii=False)


async def get_address_transactions_tool(chain_id: int, address: str, limit: int = 50) -> str:
    """Получить список обычных транзакций по EVM-адресу.

    Args:
        chain_id: ID сети (1 = Ethereum, 56 = BNB Chain, 137 = Polygon, 42161 = Arbitrum One, 10 = Optimism, 8453 = Base).
        address: EVM-адрес в формате 0x..., копировать целиком, без сокращений.
        limit: Сколько транзакций вернуть за один вызов (от 1 до 50, по умолчанию 50).

    Returns:
        JSON-строка со списком транзакций (первая страница).
    """
    result = await _get_address_transactions(chain_id=chain_id, address=address, limit=limit)
    return json.dumps(result, ensure_ascii=False)


async def get_internal_transactions_tool(chain_id: int, address: str, limit: int = 50) -> str:
    """Получить внутренние транзакции по EVM-адресу (переводы внутри вызовов контрактов).

    Args:
        chain_id: ID сети (1 = Ethereum, 56 = BNB Chain, 137 = Polygon, 42161 = Arbitrum One, 10 = Optimism, 8453 = Base).
        address: EVM-адрес в формате 0x..., копировать целиком, без сокращений.
        limit: Сколько записей вернуть за один вызов (от 1 до 50, по умолчанию 50).

    Returns:
        JSON-строка со списком внутренних транзакций (первая страница).
    """
    result = await _get_internal_transactions(chain_id=chain_id, address=address, limit=limit)
    return json.dumps(result, ensure_ascii=False)


async def get_token_transfers_tool(
    chain_id: int,
    address: str,
    token_standard: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Получить список переводов токенов (ERC-20/721/1155) по EVM-адресу.

    Args:
        chain_id: ID сети (1 = Ethereum, 56 = BNB Chain, 137 = Polygon, 42161 = Arbitrum One, 10 = Optimism, 8453 = Base).
        address: EVM-адрес в формате 0x..., копировать целиком, без сокращений.
        token_standard: Стандарт токена для фильтрации: "ERC-20", "ERC-721" или "ERC-1155". Можно не указывать, тогда вернутся все стандарты.
        limit: Сколько переводов вернуть за один вызов (от 1 до 50, по умолчанию 50).

    Returns:
        JSON-строка со списком переводов токенов (первая страница).
    """
    result = await _get_token_transfers(
        chain_id=chain_id, address=address, token_standard=token_standard, limit=limit
    )
    return json.dumps(result, ensure_ascii=False)


async def get_transaction_summary_tool(chain_id: int, tx_hash: str) -> str:
    """Получить декодированное summary одной транзакции: что произошло по-человечески.

    Args:
        chain_id: ID сети (1 = Ethereum, 56 = BNB Chain, 137 = Polygon, 42161 = Arbitrum One, 10 = Optimism, 8453 = Base).
        tx_hash: Хэш транзакции в формате 0x..., копировать целиком, без сокращений.

    Returns:
        JSON-строка с декодированным описанием транзакции.
    """
    result = await _get_transaction_summary(chain_id=chain_id, tx_hash=tx_hash)
    return json.dumps(result, ensure_ascii=False)


async def get_block_by_number_tool(chain_id: int, block_number: int) -> str:
    """Получить информацию о блоке по его номеру (время, хэш, число транзакций).

    Args:
        chain_id: ID сети (1 = Ethereum, 56 = BNB Chain, 137 = Polygon, 42161 = Arbitrum One, 10 = Optimism, 8453 = Base).
        block_number: Номер блока.

    Returns:
        JSON-строка с данными о блоке.
    """
    result = await _get_block_by_number(chain_id=chain_id, block_number=block_number)
    return json.dumps(result, ensure_ascii=False)


# Полный список тулз EVM-адаптера для передачи в ollama.chat(tools=EVM_TOOLS)
EVM_TOOLS = [
    get_address_info_tool,
    get_address_transactions_tool,
    get_internal_transactions_tool,
    get_token_transfers_tool,
    get_transaction_summary_tool,
    get_block_by_number_tool,
]

# Диспетчер имя_функции -> сама функция, для вызова по response.message.tool_calls
EVM_TOOL_DISPATCH = {fn.__name__: fn for fn in EVM_TOOLS}
