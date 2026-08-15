# evm_adapter/adapter.py
"""Main adapter interfaces and functions providing configuration tracking and unified metadata injection."""

import os
import yaml
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from .client import BlockscoutClient
from .cache import Cache

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "chains.yaml")
_cache_instance = None
_client_instance = None

def get_cache() -> Cache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = Cache()
    return _cache_instance

def get_client() -> BlockscoutClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = BlockscoutClient()
    return _client_instance

async def close_client():
    """Закрывает aiohttp-сессию синглтон-клиента, если он был создан.
    Вызывать один раз при завершении работы приложения/скрипта, чтобы не
    оставлять 'Unclosed client session' в логах."""
    global _client_instance
    if _client_instance is not None:
        await _client_instance.close()
        _client_instance = None

def _load_chain_config() -> Dict[int, str]:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return {int(k): v for k, v in config.items()} if config else {}

try:
    CHAIN_CONFIG = _load_chain_config()
except Exception:
    CHAIN_CONFIG = {}

def _validate_chain_id(chain_id: int):
    if chain_id not in CHAIN_CONFIG:
        available_chains = ", ".join(map(str, CHAIN_CONFIG.keys()))
        raise ValueError(
            f"chain_id={chain_id} не сконфигурирован, добавьте в chains.yaml. "
            f"Доступные chain_id: {available_chains}"
        )

def _add_meta(response: Dict[str, Any], chain_id: int, cached: bool) -> Dict[str, Any]:
    res_copy = response.copy()
    res_copy["_meta"] = {
        "chain_id": chain_id,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "blockscout_pro_api",
        "cached": cached
    }
    return res_copy

async def _execute_request(
    chain_id: int,
    endpoint: str,
    params: Dict[str, Any],
    ttl: int,
    client: Optional[BlockscoutClient] = None,
    cache: Optional[Cache] = None
) -> Dict[str, Any]:
    _validate_chain_id(chain_id)

    cl = client or get_client()
    ch = cache or get_cache()

    cached_data = await ch.get(chain_id, endpoint, params)
    if cached_data is not None:
        return _add_meta(cached_data, chain_id, cached=True)

    network_data = await cl._make_request(chain_id, endpoint, params)
    await ch.set(chain_id, endpoint, params, network_data, ttl)

    return _add_meta(network_data, chain_id, cached=False)

async def get_address_info(
    chain_id: int,
    address: str,
    client: Optional[BlockscoutClient] = None,
    cache: Optional[Cache] = None
) -> Dict[str, Any]:
    endpoint = f"/addresses/{address}"
    return await _execute_request(chain_id, endpoint, {}, ttl=900, client=client, cache=cache)

async def get_address_transactions(
    chain_id: int,
    address: str,
    cursor: Optional[Dict[str, Any]] = None,
    limit: int = 50,
    client: Optional[BlockscoutClient] = None,
    cache: Optional[Cache] = None
) -> Dict[str, Any]:
    endpoint = f"/addresses/{address}/transactions"
    params = {"limit": limit}
    if cursor:
        params.update(cursor)
    return await _execute_request(chain_id, endpoint, params, ttl=900, client=client, cache=cache)

async def get_internal_transactions(
    chain_id: int,
    address: str,
    cursor: Optional[Dict[str, Any]] = None,
    limit: int = 50,
    client: Optional[BlockscoutClient] = None,
    cache: Optional[Cache] = None
) -> Dict[str, Any]:
    endpoint = f"/addresses/{address}/internal-transactions"
    params = {"limit": limit}
    if cursor:
        params.update(cursor)
    return await _execute_request(chain_id, endpoint, params, ttl=900, client=client, cache=cache)

async def get_token_transfers(
    chain_id: int,
    address: str,
    token_standard: Optional[str] = None,
    cursor: Optional[Dict[str, Any]] = None,
    limit: int = 50,
    client: Optional[BlockscoutClient] = None,
    cache: Optional[Cache] = None
) -> Dict[str, Any]:
    endpoint = f"/addresses/{address}/token-transfers"
    params = {"limit": limit}
    if token_standard:
        params["type"] = token_standard
    if cursor:
        params.update(cursor)
    return await _execute_request(chain_id, endpoint, params, ttl=900, client=client, cache=cache)

async def get_transaction_summary(
    chain_id: int,
    tx_hash: str,
    client: Optional[BlockscoutClient] = None,
    cache: Optional[Cache] = None
) -> Dict[str, Any]:
    endpoint = f"/transactions/{tx_hash}/summary"
    return await _execute_request(chain_id, endpoint, {}, ttl=0, client=client, cache=cache)

async def get_transaction_raw_trace(
    chain_id: int,
    tx_hash: str,
    client: Optional[BlockscoutClient] = None,
    cache: Optional[Cache] = None
) -> Dict[str, Any]:
    endpoint = f"/transactions/{tx_hash}/raw-trace"
    return await _execute_request(chain_id, endpoint, {}, ttl=0, client=client, cache=cache)

async def get_block_by_number(
    chain_id: int,
    block_number: int,
    client: Optional[BlockscoutClient] = None,
    cache: Optional[Cache] = None
) -> Dict[str, Any]:
    endpoint = f"/blocks/{block_number}"
    return await _execute_request(chain_id, endpoint, {}, ttl=0, client=client, cache=cache)
