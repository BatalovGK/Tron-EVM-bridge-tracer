"""EVM Adapter for Blockscout Pro API."""

from .adapter import (
    get_address_info,
    get_address_transactions,
    get_internal_transactions,
    get_token_transfers,
    get_transaction_summary,
    get_transaction_raw_trace,
    get_block_by_number,
    close_client,
    CHAIN_CONFIG,
)
from .client import BlockscoutClient, CreditsExhaustedError
from .cache import Cache

__all__ = [
    "get_address_info",
    "get_address_transactions",
    "get_internal_transactions",
    "get_token_transfers",
    "get_transaction_summary",
    "get_transaction_raw_trace",
    "get_block_by_number",
    "close_client",
    "CHAIN_CONFIG",
    "BlockscoutClient",
    "CreditsExhaustedError",
    "Cache",
]
