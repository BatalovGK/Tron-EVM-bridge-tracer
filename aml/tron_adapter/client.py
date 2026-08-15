# tron_adapter/client.py
"""HTTP-клиент к TronGrid API с rate limiting и провенансом — по духу
аналогичен evm_adapter/client.py (BlockscoutClient), но под особенности
TronGrid:

- Ключ ОПЦИОНАЛЕН (в отличие от Blockscout Pro): без TRONGRID_API_KEY
  запросы просто сильнее троттлятся публичным лимитом TronGrid, а не
  отклоняются. Поэтому здесь нет `required=True` при чтении секрета и нет
  CreditsExhaustedError — у TronGrid нет модели "кредитов", это чисто
  rate-limit по QPS/окну.
- Официальная документация TronGrid прямым текстом отказывается публиковать
  фиксированные цифры лимитов: "Specific quota and QPS may change depending
  on plan... Do not hard-code fixed limits into business logic"
  (developers.tron.network/reference/rate-limits, проверено в этой сессии
  прямым запросом). Поэтому rate_limit здесь — консервативное ЛОКАЛЬНОЕ
  значение по умолчанию (можно переопределить), а не воспроизведение
  какого-то официального числа — в отличие от заявленных в брифинге "100k/
  день, 15 QPS", которые при проверке источника не подтвердились и не
  упоминаются в актуальной документации.
- TronGrid (в отличие от Blockscout Pro) не отдаёт остаток лимита в
  заголовках ответа (`x-ratelimit-remaining` и т.п. отсутствуют — проверено
  живым запросом), поэтому здесь нет динамической подстройки token bucket
  под заголовки — только фиксированный локальный rate_limit + backoff на
  429/403.
"""

import asyncio
import logging
import time
import aiohttp
from typing import Optional, Dict, Any

from common.secrets import get_secret

logger = logging.getLogger(__name__)

TRONGRID_BASE_URL = "https://api.trongrid.io"


class TronGridClient:
    def __init__(self, api_key: Optional[str] = None, rate_limit: int = 5):
        """
        Инициализирует клиент TronGrid API.
        Ключ (если есть) читается из переменной окружения TRONGRID_API_KEY,
        но не обязателен — без него запросы просто идут по публичному,
        сильнее ограниченному лимиту TronGrid.
        """
        self.api_key = api_key if api_key is not None else get_secret("TRONGRID_API_KEY", required=False)

        self.rate_limit = rate_limit
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(10)

        self._tokens = float(rate_limit)
        self._last_token_update = time.monotonic()
        self._token_lock = asyncio.Lock()

    async def __aenter__(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Tron-Adapter/1.0"}
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        """Явно закрывает aiohttp-сессию. Нужно вызывать при использовании
        клиента как синглтона (через get_client()), иначе aiohttp будет
        ругаться на 'Unclosed client session' при выходе."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _mask_api_key(self) -> str:
        if not self.api_key:
            return ""
        return f"{self.api_key[:6]}***" if len(self.api_key) > 6 else "***"

    def _mask_text(self, text: str) -> str:
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, self._mask_api_key())
        return text

    async def _wait_for_token(self):
        """Token bucket rate limiting — тот же алгоритм, что в
        evm_adapter.client.BlockscoutClient._wait_for_token."""
        async with self._token_lock:
            now = time.monotonic()
            elapsed = now - self._last_token_update
            self._tokens = min(float(self.rate_limit), self._tokens + elapsed * self.rate_limit)
            self._last_token_update = now

            if self._tokens < 1.0:
                sleep_time = (1.0 - self._tokens) / self.rate_limit
                await asyncio.sleep(sleep_time)
                self._tokens = 0.0
                self._last_token_update = time.monotonic()
            else:
                self._tokens -= 1.0

    async def _make_request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Асинхронный GET-запрос к TronGrid API. `endpoint` — путь без базового
        URL, например "/v1/accounts/{address}/transactions/trc20".
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Tron-Adapter/1.0"}
            )

        url = f"{TRONGRID_BASE_URL}{endpoint}"

        # aiohttp/yarl отказывается сериализовать bool в query string
        # (TypeError: value should be str, int or float) — TronGrid ожидает
        # только_from/only_to и т.п. как "true"/"false" в самом URL, поэтому
        # приводим явно, а не полагаемся на неявную сериализацию.
        req_params: Dict[str, Any] = {}
        for k, v in (params or {}).items():
            req_params[k] = "true" if v is True else "false" if v is False else v
        req_headers = {}
        if self.api_key:
            req_headers["TRON-PRO-API-KEY"] = self.api_key

        retries = 0
        max_retries_429 = 3
        max_retries_network = 2

        while True:
            await self._wait_for_token()

            try:
                async with self._semaphore:
                    async with self._session.get(url, params=req_params, headers=req_headers) as response:
                        if response.status == 429:
                            logger.warning(self._mask_text(f"Rate limited on {endpoint}. Retries: {retries}"))
                            if retries < max_retries_429:
                                wait_time = 2 ** retries
                                await asyncio.sleep(wait_time)
                                retries += 1
                                continue
                            else:
                                raise Exception(f"Rate limited after {max_retries_429} attempts")

                        if response.status == 403:
                            text = await response.text()
                            raise Exception(self._mask_text(f"Authentication/access error (HTTP 403): {text}"))

                        if response.status >= 400:
                            text = await response.text()
                            raise Exception(self._mask_text(f"HTTP {response.status}: {text}"))

                        body = await response.json()
                        if isinstance(body, dict) and body.get("success") is False:
                            error = body.get("error", "unknown error")
                            raise Exception(self._mask_text(f"TronGrid API error: {error}"))
                        return body

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(self._mask_text(f"Network error on {endpoint}: {str(e)}"))
                if retries < max_retries_network:
                    wait_time = 2 ** retries
                    await asyncio.sleep(wait_time)
                    retries += 1
                    continue
                else:
                    raise Exception(self._mask_text(f"Network error after {retries} attempts: {str(e)}"))
            except Exception as e:
                msg = self._mask_text(str(e))
                logger.error(f"Unexpected error: {msg}")
                raise Exception(msg) from e
