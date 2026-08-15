# flow_tracer/tests/test_thegraph.py
"""Тесты TheGraphClient на моках aiohttp — без реального обращения к сети.
Живая проверка против настоящего TheGraph gateway — отдельным manual_check
скриптом (аналог attribution: manual_check_attribution.py), т.к. требует
реального API-ключа из Subgraph Studio."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from flow_tracer.thegraph import TheGraphClient, TheGraphQueryError


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("THEGRAPH_API_KEY", "test_key_123456789")


@pytest.fixture
def mock_cache():
    cache = MagicMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    return cache


def _fake_response(status=200, json_payload=None, text_payload=""):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_payload or {})
    resp.text = AsyncMock(return_value=text_payload)
    return resp


class _FakeSessionCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_query_returns_data_on_success(mock_cache, monkeypatch):
    client = TheGraphClient()
    fake_resp = _fake_response(200, {"data": {"swaps": [{"id": "1"}]}})
    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.post = MagicMock(return_value=_FakeSessionCtx(fake_resp))
    client._session = fake_session

    data = await client.query("some_subgraph_id", "query { swaps { id } }", cache=mock_cache)

    assert data == {"swaps": [{"id": "1"}]}
    mock_cache.set.assert_called_once()


@pytest.mark.asyncio
async def test_query_returns_cached_value_without_network(mock_cache):
    mock_cache.get.return_value = {"swaps": []}
    client = TheGraphClient()
    fake_session = MagicMock()
    fake_session.post = MagicMock(side_effect=AssertionError("не должно быть сетевого вызова"))
    client._session = fake_session

    data = await client.query("subgraph_x", "query { swaps { id } }", cache=mock_cache)

    assert data == {"swaps": []}


@pytest.mark.asyncio
async def test_query_raises_on_graphql_errors(mock_cache):
    client = TheGraphClient()
    fake_resp = _fake_response(200, {"errors": [{"message": "bad query"}]})
    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.post = MagicMock(return_value=_FakeSessionCtx(fake_resp))
    client._session = fake_session

    with pytest.raises(TheGraphQueryError, match="bad query"):
        await client.query("subgraph_x", "query { bad }", cache=mock_cache)


@pytest.mark.asyncio
async def test_query_raises_on_http_error_and_masks_key(mock_cache):
    client = TheGraphClient(api_key="secretkey123456")
    fake_resp = _fake_response(403, text_payload="forbidden for key secretkey123456")
    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.post = MagicMock(return_value=_FakeSessionCtx(fake_resp))
    client._session = fake_session

    with pytest.raises(TheGraphQueryError) as exc_info:
        await client.query("subgraph_x", "query { x }", cache=mock_cache)

    assert "secretkey123456" not in str(exc_info.value)


def test_api_key_required_error(monkeypatch):
    monkeypatch.delenv("THEGRAPH_API_KEY", raising=False)
    with pytest.raises(ValueError):
        TheGraphClient(api_key=None)  # без THEGRAPH_API_KEY в env должно упасть
