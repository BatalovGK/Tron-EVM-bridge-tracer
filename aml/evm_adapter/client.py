# evm_adapter/client.py
"""HTTP Client for Blockscout Pro API with rate limiting, credits tracking, and retries."""

import asyncio
import logging
import time
import aiohttp
from typing import Optional, Dict, Any

from common.secrets import get_secret, SecretNotFoundError

logger = logging.getLogger(__name__)

class CreditsExhaustedError(Exception):
    """Raised when daily API credits are depleted."""
    pass

class BlockscoutClient:
    def __init__(self, api_key: Optional[str] = None, rate_limit: int = 5):
        """
        Initializes the Blockscout Pro API client.
        Reads the key from the BLOCKSCOUT_API_KEY environment variable if not passed explicitly.
        """
        try:
            self.api_key = api_key or get_secret("BLOCKSCOUT_API_KEY", required=True)
        except SecretNotFoundError as e:
            raise ValueError(str(e)) from e

        self.rate_limit = rate_limit
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(10)

        self._tokens = float(rate_limit)
        self._last_token_update = time.monotonic()
        self._token_lock = asyncio.Lock()

        self._credits_remaining = float('inf')

    @property
    def credits_remaining(self) -> float:
        return self._credits_remaining

    async def __aenter__(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "EVM-Adapter/1.0"}
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        """Явно закрывает aiohttp-сессию. Нужно вызывать при использовании
        клиента как синглтона (через get_client()), а не через `async with`,
        иначе aiohttp будет ругаться на 'Unclosed client session' при выходе."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _mask_api_key(self) -> str:
        """Returns masked API key for logging purposes."""
        if not self.api_key:
            return ""
        return f"{self.api_key[:8]}***" if len(self.api_key) > 8 else "***"

    def _mask_text(self, text: str) -> str:
        """Replaces raw API key with masked version in arbitrary text."""
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, self._mask_api_key())
        return text

    async def _wait_for_token(self):
        """Consumes a token for rate limiting (token bucket algorithm), waiting if necessary."""
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

    async def _make_request(
        self,
        chain_id: int,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Performs an asynchronous GET request to the Blockscout Pro API.
        Enforces strict time-based rate limiting.
        Handles retries for 429 status codes and network errors.
        """
        if self._credits_remaining <= 0:
            raise CreditsExhaustedError("Daily Blockscout API credits exhausted.")

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "EVM-Adapter/1.0"}
            )

        url = f"https://api.blockscout.com/{chain_id}/api/v2{endpoint}"

        req_params = {}
        if params:
            req_params.update(params)
        req_params["apikey"] = self.api_key

        retries = 0
        max_retries_429 = 3
        max_retries_network = 2

        while True:
            await self._wait_for_token()

            try:
                async with self._semaphore:
                    async with self._session.get(url, params=req_params) as response:
                        credits_hdr = response.headers.get("x-credits-remaining")
                        if credits_hdr is not None:
                            try:
                                self._credits_remaining = int(credits_hdr)
                            except ValueError:
                                pass

                        rl_remaining_hdr = response.headers.get("x-ratelimit-remaining")
                        if rl_remaining_hdr is not None:
                            try:
                                rl_rem = float(rl_remaining_hdr)
                                if rl_rem < self._tokens:
                                    self._tokens = rl_rem
                            except ValueError:
                                pass

                        if response.status == 429:
                            logger.warning(self._mask_text(f"Rate limited on {endpoint}. Retries: {retries}"))
                            if retries < max_retries_429:
                                wait_time = 2 ** retries
                                await asyncio.sleep(wait_time)
                                retries += 1
                                continue
                            else:
                                raise Exception(f"Rate limited after {max_retries_429} attempts")

                        if response.status in [402, 403]:
                            text = await response.text()
                            raise Exception(self._mask_text(f"Authentication error (HTTP {response.status}): {text}"))

                        if response.status >= 400:
                            text = await response.text()
                            raise Exception(self._mask_text(f"HTTP {response.status}: {text}"))

                        return await response.json()

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
                if not isinstance(e, CreditsExhaustedError):
                    msg = self._mask_text(str(e))
                    logger.error(f"Unexpected error: {msg}")
                    raise Exception(msg) from e
                raise
