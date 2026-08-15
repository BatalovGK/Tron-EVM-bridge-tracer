# scripts/manual_check_attribution.py
"""
Ручная проверка Attribution & Labeling Engine против РЕАЛЬНЫХ внешних API —
сеть до treasury.gov / api.gopluslabs.io / api.opensanctions.org недоступна
из песочницы, где писался этот код, поэтому это нужно прогнать самостоятельно.

Использование:
    export POSTGRES_DSN="postgresql://user:pass@host:5432/aml_platform"
    export GOPLUS_API_KEY="..."          # опционально, выше лимиты
    export OPENSANCTIONS_API_KEY="..."   # опционально: НЕ нужен для bulk-проверки
                                          # ниже (bulk бесплатный, без ключа); ключ
                                          # нужен только для живого /match-резерва
                                          # (attribution.opensanctions.check_address_opensanctions),
                                          # который этот скрипт не вызывает
    PYTHONPATH=. python scripts/manual_check_attribution.py

Что делает:
1. Скачивает и парсит реальный OFAC SDN Advanced XML (~80+ МБ, может занять
   минуту-другую) и проверяет, что среди найденных адресов есть 4 ЗАВЕДОМО
   реальных sanctioned-адреса (SUEX OTC, официальный пресс-релиз OFAC от
   21.09.2021) — если их там нет, парсер сломан на реальной структуре,
   несмотря на то что проходит тесты на фрагменте XML.
1b. Скачивает и парсит targets.nested.json OpenSanctions (dataset=default,
    тоже может быть крупным файлом) и проверяет те же самые SUEX-адреса —
    они должны быть и в датасете OpenSanctions, не только в чистом OFAC XML
    (см. docstring attribution/opensanctions.py). Если структуры вложенности
    там на самом деле не такая, как предполагалось по документации, здесь
    это сразу будет видно (0 найденных адресов или несовпадение с известными).
2. Проверяет один из этих адресов через полный check_address() — должен
   получиться overall_risk = "high".
3. Проверяет заведомо обычный адрес (Vitalik Buterin) через GoPlus — должен
   получиться overall_risk = "low" или "medium", НЕ "high".
"""

import asyncio
import logging

# --- Windows/корпоративный антивирус часто делает HTTPS-инспекцию (расшифровывает
# трафик через свой собственный корневой сертификат) — Python по умолчанию доверяет
# только своему набору сертификатов (certifi), не сертификатам Windows/антивируса,
# отсюда `SSLCertVerificationError: self-signed certificate in certificate chain`.
# truststore подключает Python к тому же системному хранилищу доверенных
# сертификатов, что использует Windows/macOS/Linux — если у вас легитимная
# HTTPS-инспекция (антивирус, корпоративный прокси), это чинит проблему, не
# отключая проверку сертификатов вообще. Опционально: если пакет не установлен,
# просто продолжаем без патча (тогда сработает обычная проверка через certifi).
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from attribution.ofac import (
    refresh_ofac_sdn,
    KNOWN_SANCTIONED_ADDRESSES_FOR_SANITY_CHECK,
)
from attribution.opensanctions import refresh_opensanctions_bulk
from attribution.service import check_address
from common.db import close_pool

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

CLEAN_TEST_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"  # Vitalik Buterin


async def main():
    print("=" * 70)
    print("Шаг 1: скачиваем и парсим реальный OFAC SDN Advanced XML...")
    print("(файл большой, ~80+ МБ, может занять минуту-другую)")
    print("=" * 70)

    count = await refresh_ofac_sdn()
    print(f"\nНайдено и записано в label_cache: {count} EVM-адресов из OFAC SDN\n")

    print("Проверка на известных реальных sanctioned-адресах (SUEX OTC, 2021):")
    from common.evidence_store import get_labels
    all_found = True
    for addr in KNOWN_SANCTIONED_ADDRESSES_FOR_SANITY_CHECK:
        labels = await get_labels(addr)
        found = any(l["source"] == "ofac_sdn" for l in labels)
        status = "НАЙДЕН" if found else "НЕ НАЙДЕН (!)"
        print(f"  {addr}: {status}")
        all_found = all_found and found

    if all_found:
        print("\n✅ Все известные адреса найдены — парсер работает корректно на реальном файле.\n")
    else:
        print("\n❌ Не все известные адреса найдены — структура XML могла измениться, парсер нужно доработать.\n")

    print("=" * 70)
    print("Шаг 1b: скачиваем и парсим targets.nested.json OpenSanctions (bulk)...")
    print("(тоже может быть крупным файлом, зависит от размера датасета default)")
    print("=" * 70)

    os_count = await refresh_opensanctions_bulk()
    print(f"\nНайдено и записано в label_cache: {os_count} EVM-адресов из OpenSanctions bulk\n")

    print("Проверка тех же известных sanctioned-адресов уже через OpenSanctions:")
    os_all_found = True
    for addr in KNOWN_SANCTIONED_ADDRESSES_FOR_SANITY_CHECK:
        labels = await get_labels(addr)
        found = any(l["source"] == "opensanctions_bulk" for l in labels)
        status = "НАЙДЕН" if found else "НЕ НАЙДЕН"
        print(f"  {addr}: {status}")
        os_all_found = os_all_found and found

    if os_all_found:
        print("\n✅ Все известные адреса найдены и в OpenSanctions — структура вложенности targets.nested.json угадана верно.\n")
    else:
        print("\n⚠️  Не все известные адреса найдены в OpenSanctions bulk — это НЕ обязательно баг (OpenSanctions "
              "может не дублировать 100% адресов из чистого OFAC XML), но если найдено 0 из 4 — вероятно, "
              "структура вложенности CryptoWallet внутри targets.nested.json отличается от предположенной "
              "в attribution/opensanctions.py::_iter_nested_entities, и парсер нужно доработать по реальному файлу.\n")

    print("=" * 70)
    print("Шаг 2: полная проверка известного sanctioned-адреса через check_address()...")
    print("=" * 70)
    result = await check_address(KNOWN_SANCTIONED_ADDRESSES_FOR_SANITY_CHECK[0], chain_id=1)
    print(f"overall_risk = {result['overall_risk']} (ожидается 'high')")
    print(f"ofac_sanctioned = {result['ofac_sanctioned']}")
    print(f"goplus_flags = {result['goplus_flags']}")

    print("\n" + "=" * 70)
    print("Шаг 3: проверка обычного адреса (Vitalik Buterin) — не должен быть high-risk...")
    print("=" * 70)
    clean_result = await check_address(CLEAN_TEST_ADDRESS, chain_id=1)
    print(f"overall_risk = {clean_result['overall_risk']} (ожидается 'low' или 'medium', НЕ 'high')")
    print(f"goplus_flags = {clean_result['goplus_flags']}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
