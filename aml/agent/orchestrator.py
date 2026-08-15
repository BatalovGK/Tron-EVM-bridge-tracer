# agent/orchestrator.py
"""
Минимальный цикл tool-calling поверх Ollama для проверки связки:
Qwen3-Coder (через нативный tool calling Ollama) <-> тулзы evm_adapter.

Это НЕ полный Оркестратор из архитектуры (там ещё Валидатор циклов как отдельная
чистая функция, Rolling Dump, эскалация по off-ramp/санкциям и т.д.) — это первый
сквозной прогон: убедиться, что модель реально вызывает тулзы, тулзы реально
дёргают evm_adapter, а результат реально возвращается модели и модель даёт
финальный ответ.

`max_turns` здесь — это грубый, временный предохранитель от бесконечного цикла
(если модель зациклится на вызовах тулз). Это НЕ замена полноценному Валидатору
циклов из раздела 3 архитектуры — просто чтобы прототип не завис намертво.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import ollama

from agent.tools_evm import EVM_TOOLS, EVM_TOOL_DISPATCH
from agent.tools_attribution import ATTRIBUTION_TOOLS, ATTRIBUTION_TOOL_DISPATCH
from agent.tools_flow import FLOW_TOOLS, FLOW_TOOL_DISPATCH

ALL_TOOLS = EVM_TOOLS + ATTRIBUTION_TOOLS + FLOW_TOOLS
ALL_TOOL_DISPATCH = {**EVM_TOOL_DISPATCH, **ATTRIBUTION_TOOL_DISPATCH, **FLOW_TOOL_DISPATCH}

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("ORCHESTRATOR_MODEL", "qwen3-coder")


class MaxTurnsExceededError(Exception):
    """Цикл tool-calling не завершился за отведённое число итераций."""
    pass


async def _execute_tool_call(tool_call) -> str:
    """
    Безопасно исполняет один вызов тулзы: ловит любые исключения (включая
    CreditsExhaustedError и т.п. из evm_adapter/client.py) и возвращает текст,
    понятный модели, вместо падения всего цикла.
    """
    name = tool_call.function.name
    arguments = dict(tool_call.function.arguments)

    fn = ALL_TOOL_DISPATCH.get(name)
    if fn is None:
        logger.warning(f"Модель запросила неизвестную тулзу: {name}")
        return json.dumps({"error": f"Тулза '{name}' не найдена"}, ensure_ascii=False)

    try:
        result = await fn(**arguments)
        return result
    except TypeError as e:
        # Модель передала не те аргументы (например, лишний/отсутствующий параметр)
        logger.warning(f"Неверные аргументы для {name}: {arguments} — {e}")
        return json.dumps({"error": f"Неверные аргументы вызова: {e}"}, ensure_ascii=False)
    except Exception as e:
        # Любая другая ошибка (сеть, credits exhausted, rate limit после всех
        # ретраев и т.д.) — не роняем цикл, отдаём модели как результат тулзы
        logger.error(f"Ошибка при вызове тулзы {name}({arguments}): {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def run_agent_turn(
    messages: List[Dict[str, Any]],
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_OLLAMA_HOST,
    max_turns: int = 8,
    client: Optional["ollama.AsyncClient"] = None,
) -> str:
    """
    Прогоняет messages через Qwen3-Coder с тулзами EVM-адаптера, обрабатывая
    tool_calls до тех пор, пока модель не вернёт финальный текстовый ответ
    без запроса тулз, либо пока не будет исчерпан max_turns.

    Args:
        messages: История диалога в формате ollama messages (role/content).
        model: Имя модели в Ollama (по умолчанию из ORCHESTRATOR_MODEL или "qwen3-coder").
        host: Адрес Ollama (по умолчанию из OLLAMA_HOST, в docker-compose это
              обычно "http://ollama:11434", а не "http://localhost:11434").
        max_turns: Предохранитель от зацикливания на вызовах тулз.
        client: Готовый ollama.AsyncClient (передаётся в тестах для мока;
                в проде создаётся автоматически).

    Returns:
        Финальный текстовый ответ модели.

    Raises:
        MaxTurnsExceededError: если за max_turns итераций модель так и не
            вернула финальный ответ без tool_calls.
    """
    cl = client or ollama.AsyncClient(host=host)
    messages = list(messages)  # не мутируем то, что передал вызывающий код

    for turn in range(max_turns):
        response = await cl.chat(model=model, messages=messages, tools=ALL_TOOLS)
        messages.append(response.message)

        if not response.message.tool_calls:
            return response.message.content or ""

        for tool_call in response.message.tool_calls:
            output = await _execute_tool_call(tool_call)
            messages.append({
                "role": "tool",
                "tool_name": tool_call.function.name,
                "content": output,
            })

    raise MaxTurnsExceededError(
        f"Цикл не завершился за {max_turns} итераций — возможно, модель зациклилась "
        f"на вызовах тулз. Последнее сообщение: {messages[-1] if messages else None}"
    )
