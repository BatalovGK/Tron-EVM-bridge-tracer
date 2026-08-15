# agent/tests/test_orchestrator.py
"""
Тесты цикла tool-calling (agent/orchestrator.py) с замоканным ollama.AsyncClient —
реального Ollama здесь нет, но логика диспетчеризации, обработки ошибок и
предохранителя от бесконечного цикла проверяется полностью.
"""

import json
import types
import pytest
from unittest.mock import AsyncMock, patch

from ollama._types import Message

from agent.orchestrator import run_agent_turn, MaxTurnsExceededError


def _make_response(content: str = "", tool_calls=None):
    """Собирает объект, похожий на то, что реально возвращает ollama.AsyncClient.chat()."""
    msg = Message(role="assistant", content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(message=msg)


def _make_tool_call(name: str, arguments: dict):
    return Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=arguments))


@pytest.mark.asyncio
async def test_single_tool_call_then_final_answer(monkeypatch):
    """Модель один раз вызывает тулзу, получает результат, отвечает финальным текстом."""
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_key_123456")

    fake_address_result = {"hash": "0xabc", "coin_balance": "1000", "_meta": {"cached": False}}

    with patch("agent.tools_evm._get_address_info", new=AsyncMock(return_value=fake_address_result)):
        mock_client = AsyncMock()
        mock_client.chat.side_effect = [
            _make_response(tool_calls=[_make_tool_call("get_address_info_tool", {"chain_id": 1, "address": "0xabc"})]),
            _make_response(content="Баланс адреса 0xabc: 1000."),
        ]

        result = await run_agent_turn(
            messages=[{"role": "user", "content": "Проверь баланс 0xabc в Ethereum"}],
            client=mock_client,
        )

    assert result == "Баланс адреса 0xabc: 1000."
    assert mock_client.chat.call_count == 2

    # Проверяем, что в историю сообщений реально добавился результат тулзы
    # в правильном формате (role=tool, tool_name=..., content=строка с данными)
    second_call_messages = mock_client.chat.call_args_list[1].kwargs["messages"]
    tool_messages = [m for m in second_call_messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_name"] == "get_address_info_tool"
    assert json.loads(tool_messages[0]["content"])["hash"] == "0xabc"


@pytest.mark.asyncio
async def test_tool_error_does_not_crash_loop(monkeypatch):
    """Если вызов тулзы падает с исключением, цикл не рушится — ошибка уходит
    обратно модели как содержимое tool-сообщения, и цикл продолжается."""
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_key_123456")

    with patch("agent.tools_evm._get_address_info", new=AsyncMock(side_effect=Exception("Daily Blockscout API credits exhausted."))):
        mock_client = AsyncMock()
        mock_client.chat.side_effect = [
            _make_response(tool_calls=[_make_tool_call("get_address_info_tool", {"chain_id": 1, "address": "0xabc"})]),
            _make_response(content="Не удалось получить данные: кредиты API исчерпаны."),
        ]

        result = await run_agent_turn(
            messages=[{"role": "user", "content": "Проверь баланс 0xabc"}],
            client=mock_client,
        )

    assert "исчерпаны" in result
    second_call_messages = mock_client.chat.call_args_list[1].kwargs["messages"]
    tool_messages = [m for m in second_call_messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert "credits exhausted" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_unknown_tool_name_handled_gracefully(monkeypatch):
    """Модель "придумала" несуществующую тулзу — не должно быть падения."""
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_key_123456")

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _make_response(tool_calls=[_make_tool_call("get_nonexistent_tool", {"foo": "bar"})]),
        _make_response(content="Извините, не смог выполнить запрос."),
    ]

    result = await run_agent_turn(
        messages=[{"role": "user", "content": "..."}],
        client=mock_client,
    )

    assert result == "Извините, не смог выполнить запрос."
    second_call_messages = mock_client.chat.call_args_list[1].kwargs["messages"]
    tool_messages = [m for m in second_call_messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert "не найдена" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_wrong_arguments_handled_gracefully(monkeypatch):
    """Модель передала неверные/лишние аргументы — TypeError ловится, а не падает наружу."""
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_key_123456")

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _make_response(tool_calls=[_make_tool_call("get_address_info_tool", {"chain_id": 1, "address": "0xabc", "unexpected_arg": 123})]),
        _make_response(content="Возникла ошибка при вызове инструмента."),
    ]

    result = await run_agent_turn(
        messages=[{"role": "user", "content": "..."}],
        client=mock_client,
    )

    assert result == "Возникла ошибка при вызове инструмента."


@pytest.mark.asyncio
async def test_max_turns_exceeded_raises(monkeypatch):
    """Если модель бесконечно запрашивает тулзы, цикл обязан остановиться
    по max_turns, а не висеть вечно."""
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_key_123456")

    with patch("agent.tools_evm._get_address_info", new=AsyncMock(return_value={"hash": "0xabc"})):
        mock_client = AsyncMock()
        # Всегда возвращает tool_calls, никогда не даёт финальный ответ
        mock_client.chat.return_value = _make_response(
            tool_calls=[_make_tool_call("get_address_info_tool", {"chain_id": 1, "address": "0xabc"})]
        )

        with pytest.raises(MaxTurnsExceededError):
            await run_agent_turn(
                messages=[{"role": "user", "content": "..."}],
                client=mock_client,
                max_turns=3,
            )

    assert mock_client.chat.call_count == 3


@pytest.mark.asyncio
async def test_no_tool_call_returns_immediately(monkeypatch):
    """Если модель сразу отвечает без вызова тулз — цикл завершается за 1 итерацию."""
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_key_123456")

    mock_client = AsyncMock()
    mock_client.chat.return_value = _make_response(content="Привет! Чем могу помочь?")

    result = await run_agent_turn(
        messages=[{"role": "user", "content": "привет"}],
        client=mock_client,
    )

    assert result == "Привет! Чем могу помочь?"
    assert mock_client.chat.call_count == 1
