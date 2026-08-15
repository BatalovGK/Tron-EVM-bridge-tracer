# flow_tracer/thegraph.py
"""
Тонкий HTTP-клиент к TheGraph decentralized network (не hosted-сервис — тот
депрекейтнут ещё в 2024-м). Используется ТОЛЬКО для резолва свопов (see
swap_resolver.py) — не для общей аналитики пулов/цен, чтобы не тратить
бесплатную квоту (100k запросов/месяц) впустую.

Ключевые отличия от evm_adapter.client.BlockscoutClient:
- Один и тот же ключ действителен для запроса ЛЮБОГО подграфа в сети — не для
  конкретной сети/протокола, поэтому rate limiting здесь по ключу глобально,
  а не per-chain.
- Эндпоинт строится как gateway.thegraph.com/api/<KEY>/subgraphs/id/<SUBGRAPH_ID>,
  где SUBGRAPH_ID берётся из flow_tracer/config/dex_subgraphs.yaml.
- Это GraphQL (POST c телом {"query": ..., "variables": ...}), а не REST GET.

Кэш переиспользует evm_adapter.cache.Cache (тот же принцип: ключ на основе
(namespace, endpoint, params), TTL). "chain_id" здесь используется как поле
namespace-ключа — в него кладём subgraph_id, чтобы не путать кэш разных
подграфов между собой (сама Cache не знает о семантике этого поля).
"""

import logging
from typing import Any, Dict, Optional

import aiohttp

from common.secrets import get_secret, SecretNotFoundError
from evm_adapter.cache import Cache

logger = logging.getLogger(__name__)

GATEWAY_BASE_URL = "https://gateway.thegraph.com/api"


class TheGraphQueryError(Exception):
    """GraphQL-запрос вернул errors[] или сеть/сервер отказали после ретраев."""
    pass


class TheGraphClient:
    def __init__(self, api_key: Optional[str] = None):
        """Читает ключ из THEGRAPH_API_KEY, если не передан явно.
        Ключ создаётся в Subgraph Studio (нужен Web3-кошелёк для подключения к
        студии — см. README флоу-трейсера), сам ключ — обычная строка."""
        try:
            self.api_key = api_key or get_secret("THEGRAPH_API_KEY", required=True)
        except SecretNotFoundError as e:
            raise ValueError(str(e)) from e
        self._session: Optional[aiohttp.ClientSession] = None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _mask_text(self, text: str) -> str:
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, f"{self.api_key[:6]}***")
        return text

    async def query(
        self,
        subgraph_id: str,
        graphql_query: str,
        variables: Optional[Dict[str, Any]] = None,
        cache: Optional[Cache] = None,
        ttl: int = 300,
    ) -> Dict[str, Any]:
        """
        Выполняет один GraphQL-запрос к указанному подграфу.

        Args:
            subgraph_id: ID подграфа из dex_subgraphs.yaml (НЕ адрес контракта).
            graphql_query: тело GraphQL-запроса.
            variables: переменные запроса (напр. {"tx_hash": "0x..."}).
            cache: опциональный кэш (по умолчанию — новый Cache() на файл по
                   умолчанию; в проде стоит передавать общий инстанс).
            ttl: время жизни кэша в секундах. On-chain swap-данные по
                 конкретной завершённой транзакции неизменны, поэтому долгий
                 TTL безопасен — но по умолчанию оставлен умеренным (5 мин),
                 чтобы не мешать при разработке/тестах.

        Returns:
            Распарсенный JSON-ответ (["data"] уже проверено на отсутствие
            errors — при errors кидается TheGraphQueryError).
        """
        variables = variables or {}
        ch = cache or Cache()

        cache_params = {"query": graphql_query, "variables": variables}
        cached = await ch.get(chain_id=0, endpoint=subgraph_id, params=cache_params)
        if cached is not None:
            return cached

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "flow-tracer/1.0"},
            )

        url = f"{GATEWAY_BASE_URL}/{self.api_key}/subgraphs/id/{subgraph_id}"
        body = {"query": graphql_query, "variables": variables}

        try:
            async with self._session.post(url, json=body) as response:
                if response.status >= 400:
                    text = await response.text()
                    raise TheGraphQueryError(
                        self._mask_text(f"HTTP {response.status} от TheGraph gateway: {text}")
                    )
                payload = await response.json()
        except aiohttp.ClientError as e:
            raise TheGraphQueryError(self._mask_text(f"Сетевая ошибка при запросе к TheGraph: {e}")) from e

        if "errors" in payload and payload["errors"]:
            raise TheGraphQueryError(
                self._mask_text(f"TheGraph GraphQL errors для subgraph_id={subgraph_id}: {payload['errors']}")
            )

        data = payload.get("data", {})
        await ch.set(chain_id=0, endpoint=subgraph_id, params=cache_params, value=data, ttl=ttl)
        return data
