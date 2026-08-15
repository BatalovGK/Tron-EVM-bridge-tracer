# tron_adapter/address.py
"""
Конвертация форматов адресов и хэшей транзакций Tron.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ
----------------------------
При живой проверке через LayerZero Scan API (layerzero_tracer.py) выяснилось,
что Tron-адреса там отдаются в "голом" 20-байтном hex-формате без префикса
0x41 (например "0x3a08f76772e200653bb55c2a92998daca62e0e97") — это
EVM-совместимое представление адреса на уровне TVM (Tron Virtual Machine).
TronGrid же принимает и возвращает адреса в base58check-формате (например
"TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt", начинается с "T") — так называемый
"Tron hex" формат отличается от него наличием служебного префикса 0x41
(байт version prefix) и checksum-кодировкой base58check, а не просто хексом.

Смешивать эти три представления напрямую («сравнить строки как есть») —
источник тихих багов: одна и та же транзакция/адрес не совпадёт по строке
между выводом layerzero_tracer.py и TronGrid API, если не привести к общему
формату явно. Это тот же принцип "explicit translation table на границе
модулей", что и для LAYERZERO_EID_TO_EVM_CHAIN_ID в layerzero_tracer.py —
здесь применяется к адресам, а не к chain id.

Аналогично для хэшей транзакций: TronGrid возвращает transaction_id БЕЗ
префикса "0x" (64 hex-символа), а LayerZero Scan API отдаёт хэш той же
транзакции С префиксом "0x" — normalize_tx_hash() приводит к общему виду.
"""

import hashlib
from typing import Optional

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

TRON_HEX_PREFIX = "41"  # version-byte префикс адресов Tron mainnet (аналог 0x00 у BTC mainnet)


def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + out


def _b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        if c not in _B58_INDEX:
            raise ValueError(f"Некорректный base58-символ '{c}' в адресе '{s}'")
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n > 0 else b""
    pad = 0
    for c in s:
        if c == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + body


def is_base58_address(address: str) -> bool:
    return isinstance(address, str) and address.startswith("T") and len(address) == 34


def hex_to_base58(hex_address: str) -> str:
    """
    Принимает Tron-адрес в hex-виде — с префиксом 0x41 (полный Tron hex,
    42 символа после удаления "0x") или без него (голый 20-байтный
    EVM-style адрес, 40 символов, как отдаёт LayerZero Scan API для Tron) —
    и возвращает канонический base58check-адрес ("T...").
    """
    h = hex_address.lower().removeprefix("0x")
    if h.startswith(TRON_HEX_PREFIX) and len(h) == 42:
        body_hex = h
    elif len(h) == 40:
        body_hex = TRON_HEX_PREFIX + h
    else:
        raise ValueError(
            f"Неожиданная длина hex-адреса Tron: '{hex_address}' "
            f"(ожидались 40 hex-символов без префикса 0x41 или 42 с ним)"
        )
    raw = bytes.fromhex(body_hex)
    checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    return _b58encode(raw + checksum)


def base58_to_hex(base58_address: str, with_prefix: bool = False) -> str:
    """
    Обратное преобразование: base58check ("T...") -> hex. По умолчанию
    возвращает 20-байтный EVM-style hex (без 0x41 и без "0x") — этот формат
    напрямую сравним с тем, что отдаёт LayerZero Scan API для адресов на
    Tron. С with_prefix=True возвращает полный Tron hex (0x41 + 20 байт).
    """
    raw = _b58decode(base58_address)
    if len(raw) != 25:
        raise ValueError(f"Некорректная длина декодированного адреса Tron: '{base58_address}'")
    body, checksum = raw[:21], raw[21:]
    expected_checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    if checksum != expected_checksum:
        raise ValueError(f"Некорректная checksum base58check-адреса Tron: '{base58_address}'")
    if not with_prefix:
        body = body[1:]  # отбросить версионный байт 0x41
    return body.hex()


def normalize_tron_address(address: str) -> str:
    """
    Приводит адрес Tron в ЛЮБОМ из трёх встречающихся форматов (base58
    "T...", полный Tron hex "0x41..."/"41...", голый EVM-style hex
    "0x..."/"..." без версионного байта — как в LayerZero Scan API) к
    единому каноническому base58-виду. Использовать на границе между
    tron_adapter/layerzero_tracer и любым кодом, который сравнивает адреса
    как строки (например, Bridge Contract Registry).
    """
    if is_base58_address(address):
        return address
    return hex_to_base58(address)


def normalize_tx_hash(tx_hash: str) -> str:
    """
    Приводит хэш транзакции Tron к каноническому виду без префикса "0x",
    в нижнем регистре — формат, который использует TronGrid
    (`transaction_id` в ответах API). LayerZero Scan API для той же
    транзакции на стороне Tron отдаёт хэш с префиксом "0x" — их нужно
    сравнивать только через эту нормализацию, не как сырые строки.
    """
    return tx_hash.lower().removeprefix("0x")


def addresses_equal(a: Optional[str], b: Optional[str]) -> bool:
    """Сравнивает два Tron-адреса вне зависимости от формата (base58/hex)."""
    if a is None or b is None:
        return False
    try:
        return normalize_tron_address(a) == normalize_tron_address(b)
    except ValueError:
        return False
