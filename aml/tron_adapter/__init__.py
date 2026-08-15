"""Tron Adapter for TronGrid API."""

from .adapter import (
    get_account_info,
    get_trc20_transfers,
    get_transaction_info,
    close_client,
)
from .client import TronGridClient
from .cache import Cache
from .address import (
    normalize_tron_address,
    normalize_tx_hash,
    hex_to_base58,
    base58_to_hex,
    addresses_equal,
)

__all__ = [
    "get_account_info",
    "get_trc20_transfers",
    "get_transaction_info",
    "close_client",
    "TronGridClient",
    "Cache",
    "normalize_tron_address",
    "normalize_tx_hash",
    "hex_to_base58",
    "base58_to_hex",
    "addresses_equal",
]
