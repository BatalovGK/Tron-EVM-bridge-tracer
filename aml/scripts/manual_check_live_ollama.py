# scripts/manual_check_live_ollama.py
"""
Ручная проверка связки Qwen3-Coder <-> тулзы evm_adapter против ВАШЕЙ реальной
Ollama (не мок). Запускать внутри той же docker-сети, где крутится ollama,
или локально с OLLAMA_HOST, указывающим на неё.

Использование:

    export BLOCKSCOUT_API_KEY="proapi_ваш_ключ"
    export OLLAMA_HOST="http://ollama:11434"      # или http://localhost:11434
    export ORCHESTRATOR_MODEL="qwen3-coder"       # имя модели, как в `ollama list`
    python scripts/manual_check_live_ollama.py

Что должно произойти, если всё работает:
1. В консоли появится строка "--- Модель запрашивает тулзу: get_address_info_tool ..."
   — значит, модель реально решила вызвать инструмент, а не просто угадала ответ.
2. Появится строка с результатом от evm_adapter (реальные данные с Blockscout).
3. В конце — финальный текстовый ответ модели с балансом адреса.

Если шаг 1 не появился — модель ответила без вызова инструмента (либо не
поддерживает tool calling, либо не поняла, что нужно вызвать тулзу — тогда
стоит проверить версию Ollama (`ollama --version`, нужна 0.3.0+) и системный
промпт/формулировку вопроса).
"""

import asyncio
import logging

from agent.orchestrator import run_agent_turn, _execute_tool_call
from evm_adapter import close_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Реальный, известный адрес Vitalik Buterin (Ethereum) — безопасно для теста,
# публичные данные, ничего расследовательского.
TEST_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


async def main():
    original_execute = _execute_tool_call

    async def _logged_execute(tool_call):
        print(f"\n--- Модель запрашивает тулзу: {tool_call.function.name}({dict(tool_call.function.arguments)}) ---")
        result = await original_execute(tool_call)
        print(f"--- Результат тулзы (первые 300 символов): {result[:300]} ---\n")
        return result

    import agent.orchestrator as orch_module
    orch_module._execute_tool_call = _logged_execute

    messages = [
        {
            "role": "user",
            "content": (
                f"Проверь баланс и тип адреса {TEST_ADDRESS} в сети Ethereum (chain_id=1). "
                "Используй доступные инструменты, не придумывай данные сам."
            ),
        }
    ]

    final_answer = await run_agent_turn(messages=messages)
    print("=" * 60)
    print("ФИНАЛЬНЫЙ ОТВЕТ МОДЕЛИ:")
    print(final_answer)
    print("=" * 60)

    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
