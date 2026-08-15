# tron_adapter/tests/test_address.py
"""
Тесты конвертации адресов/хэшей Tron. Опорные значения — НЕ придуманы, а
взяты из реальной живой проверки в этой сессии: адрес контракта USDT0
(LayerZero OApp) на Tron mainnet, подтверждённый одновременно через
LayerZero Scan API, Tronscan API (label "UsdtOFT") и объём/баланс TRC-20
переводов. Так тест одновременно проверяет и код конвертации, и фиксирует
для будущих читателей, откуда взялся эталон.
"""

import pytest
from tron_adapter.address import (
    hex_to_base58,
    base58_to_hex,
    normalize_tron_address,
    normalize_tx_hash,
    addresses_equal,
    is_base58_address,
)

# USDT0 OApp контракт на Tron mainnet (см. bridge_registry.py) в трёх формах.
BASE58 = "TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt"
BARE_HEX = "3a08f76772e200653bb55c2a92998daca62e0e97"          # как отдаёт LayerZero Scan (без 0x41)
FULL_HEX = "413a08f76772e200653bb55c2a92998daca62e0e97"        # "родной" Tron hex (с 0x41)


def test_hex_to_base58_bare():
    assert hex_to_base58(BARE_HEX) == BASE58
    assert hex_to_base58("0x" + BARE_HEX) == BASE58


def test_hex_to_base58_full_prefix():
    assert hex_to_base58(FULL_HEX) == BASE58
    assert hex_to_base58("0x" + FULL_HEX) == BASE58


def test_base58_to_hex_roundtrip():
    assert base58_to_hex(BASE58) == BARE_HEX
    assert base58_to_hex(BASE58, with_prefix=True) == FULL_HEX


def test_normalize_tron_address_all_formats_converge():
    assert normalize_tron_address(BASE58) == BASE58
    assert normalize_tron_address(BARE_HEX) == BASE58
    assert normalize_tron_address("0x" + BARE_HEX) == BASE58
    assert normalize_tron_address(FULL_HEX) == BASE58


def test_addresses_equal_across_formats():
    assert addresses_equal(BASE58, "0x" + BARE_HEX) is True
    assert addresses_equal(BASE58, FULL_HEX) is True
    assert addresses_equal(BASE58, "TDifferentAddress1111111111111111") is False


def test_addresses_equal_handles_none():
    assert addresses_equal(None, BASE58) is False
    assert addresses_equal(BASE58, None) is False


def test_is_base58_address():
    assert is_base58_address(BASE58) is True
    assert is_base58_address(BARE_HEX) is False
    assert is_base58_address("0x" + BARE_HEX) is False


def test_base58_to_hex_rejects_bad_checksum():
    tampered = BASE58[:-1] + ("A" if BASE58[-1] != "A" else "B")
    with pytest.raises(ValueError):
        base58_to_hex(tampered)


def test_hex_to_base58_rejects_bad_length():
    with pytest.raises(ValueError):
        hex_to_base58("deadbeef")


def test_normalize_tx_hash_strips_0x_and_lowercases():
    real = "0xcae6f9052cc83b91a4688e83d616ada07c390df64289ae1c88f6b967982ce3d1"
    expected = "cae6f9052cc83b91a4688e83d616ada07c390df64289ae1c88f6b967982ce3d1"
    assert normalize_tx_hash(real) == expected
    assert normalize_tx_hash(expected) == expected
    assert normalize_tx_hash(real.upper()) == expected
