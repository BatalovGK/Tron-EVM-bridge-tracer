# tron_adapter/adapter.py
"""
Публичный интерфейс tron_adapter — по духу аналогичен evm_adapter/adapter.py:
провенанс (_meta), кэш через синглтон, thin-функции поверх TronGridClient.

Важное отличие от evm_adapter: Tron mainnet — одна сеть, поэтому здесь нет
параметра chain_id и нет chains.yaml/_validate_chain_id — им просто нечего
валидировать. Если/когда понадобится Tron testnet (Nile/Shasta), это будет
отдельный base_url в клиенте, а не chain_id-переключатель, т.к. TronGrid не
использует чужую нумерацию сетей для этого (сравните с тем, как evm_adapter
принимает EIP-155 chain_id — у Tron такой размерности просто нет).

Этот модуль НЕ знает про мосты, LayerZero или Bridge Contract Registry — это
чистый network adapter (как и evm_adapter). Обнаружение депозита в мост
живёт в bridge_tracer.py (Layer 1 orchestrator) — НЕ через этот модуль:
первая версия фильтровала результат get_trc20_transfers() по адресу
известного bridge-контракта, но живой запуск нашёл на ней ложноотрицательный
случай (депозит через промежуточный router-контракт, не прямой TRC-20
Transfer), поэтому обнаружение депозита переведено на LayerZero Scan
(messages/wallet) — см. docstring _find_tron_bridge_deposit в
bridge_tracer.py. get_trc20_transfers() из этого модуля остаётся общей
функцией адаптера (используется в других сценариях), просто bridge_tracer.py
её для этой конкретной задачи больше не вызывает.
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any

from .client import TronGridClient
from .cache import Cache
from .address import normalize_tron_address, normalize_tx_hash

_cache_instance = None
_client_instance = None


def get_cache() -> Cache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = Cache()
    return _cache_instance


def get_client() -> TronGridClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = TronGridClient()
    return _client_instance


async def close_client():
    """Закрывает aiohttp-сессию синглтон-клиента, если он был создан.
    Вызывать один раз при завершении работы приложения/скрипта."""
    global _client_instance
    if _client_instance is not None:
        await _client_instance.close()
        _client_instance = None


def _add_meta(response: Dict[str, Any], cached: bool) -> Dict[str, Any]:
    res_copy = response.copy()
    res_copy["_meta"] = {
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "trongrid_api",
        "cached": cached,
    }
    return res_copy


async def _execute_request(
    endpoint: str,
    params: Dict[str, Any],
    ttl: int,
    client: Optional[TronGridClient] = None,
    cache: Optional[Cache] = None,
) -> Dict[str, Any]:
    cl = client or get_client()
    ch = cache or get_cache()

    cached_data = await ch.get(endpoint, params)
    if cached_data is not None:
        return _add_meta(cached_data, cached=True)

    network_data = await cl._make_request(endpoint, params)
    await ch.set(endpoint, params, network_data, ttl)

    return _add_meta(network_data, cached=False)


async def get_account_info(
    address: str,
    client: Optional[TronGridClient] = None,
    cache: Optional[Cache] = None,
) -> Dict[str, Any]:
    """Базовая информация об аккаунте Tron: баланс TRX, признак создания и т.д."""
    address = normalize_tron_address(address)
    endpoint = f"/v1/accounts/{address}"
    return await _execute_request(endpoint, {}, ttl=900, client=client, cache=cache)


async def get_trc20_transfers(
    address: str,
    contract_address: Optional[str] = None,
    only_from: Optional[bool] = None,
    only_to: Optional[bool] = None,
    only_confirmed: bool = True,
    min_timestamp: Optional[int] = None,
    max_timestamp: Optional[int] = None,
    fingerprint: Optional[str] = None,
    limit: int = 200,
    client: Optional[TronGridClient] = None,
    cache: Optional[Cache] = None,
) -> Dict[str, Any]:
    """
    Список TRC-20 событий (Transfer И Approval — TronGrid отдаёт оба типа в
    одном списке, фильтрация по `type == "Transfer"` — на стороне вызывающего
    кода, endpoint такого серверного фильтра не предоставляет) по адресу.

    Args:
        address: Tron-адрес в любом формате (base58 или hex — будет
            приведён к base58, который принимает TronGrid).
        contract_address: ограничить одним TRC-20 токеном (например, USDT).
        only_from / only_to: направление (TronGrid поддерживает оба как
            булевы query-параметры — подтверждено живым запросом).
        min_timestamp / max_timestamp: unix ms, диапазон по времени блока.
        fingerprint: курсор пагинации из предыдущего ответа
            (`meta.fingerprint` / `meta.links.next`).
        limit: элементов на страницу (по документации максимум 200).

    Returns:
        Сырой ответ TronGrid (data / success / meta.fingerprint /
        meta.links.next) + добавленный _meta (provenance).
    """
    address = normalize_tron_address(address)
    endpoint = f"/v1/accounts/{address}/transactions/trc20"
    params: Dict[str, Any] = {"limit": limit, "only_confirmed": only_confirmed}
    if contract_address:
        params["contract_address"] = normalize_tron_address(contract_address)
    if only_from is not None:
        params["only_from"] = only_from
    if only_to is not None:
        params["only_to"] = only_to
    if min_timestamp is not None:
        params["min_timestamp"] = min_timestamp
    if max_timestamp is not None:
        params["max_timestamp"] = max_timestamp
    if fingerprint:
        params["fingerprint"] = fingerprint
    # ttl=60: свежие переводы должны появляться в трейсере быстро (это не
    # неизменяемые исторические данные вроде блока в evm_adapter), но
    # достаточно, чтобы не бить по API повторно в пределах одной сессии
    # трейсинга одного и того же адреса.
    return await _execute_request(endpoint, params, ttl=60, client=client, cache=cache)


async def get_transaction_info(
    tx_hash: str,
    client: Optional[TronGridClient] = None,
    cache: Optional[Cache] = None,
) -> Dict[str, Any]:
    """Информация о транзакции по её id (без префикса "0x" — normalize_tx_hash
    делает это автоматически, если пришло с "0x", как из LayerZero Scan)."""
    tx_hash = normalize_tx_hash(tx_hash)
    endpoint = f"/v1/transactions/{tx_hash}"
    return await _execute_request(endpoint, {}, ttl=0, client=client, cache=cache)
