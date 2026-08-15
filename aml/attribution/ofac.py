# attribution/ofac.py
"""
OFAC SDN List — пассивный источник (готовый список, раздел 4 архитектуры).

В отличие от GoPlus/OpenSanctions это НЕ API для проверки одного адреса —
OFAC публикует весь список целиком (~80+ МБ XML), поэтому правильный паттерн:
периодически (раз в несколько часов/раз в день) скачивать и парсить весь файл,
записывая найденные крипто-адреса в label_cache, а сами проверки одного адреса
делать через обычный SELECT к label_cache (см. attribution/service.py).

Официальный источник (подтверждён поиском на июль 2026):
    https://www.treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml

Формат XML подтверждён реальным фрагментом (UN Sanctions XML, версия 3):
    <FeatureType ID="345" FeatureTypeGroupID="1">Digital Currency Address - ETH</FeatureType>
    <Feature ID="36618" FeatureTypeID="345">
        <FeatureVersion ID="34337" ReliabilityID="1560">
            <Comment />
            <VersionDetail DetailTypeID="1432">0x901bb9583b24d97e995513c6778dc6888ab6870e</VersionDetail>
        </FeatureVersion>
        <IdentityReference IdentityID="21386" IdentityFeatureLinkTypeID="1" />
    </Feature>

Важно: тип фичи (`FeatureType`) — это отдельная таблица-подстановка по ID,
а не текст внутри самого Feature. Поэтому парсинг двухшаговый: сначала строим
словарь FeatureTypeID -> человекочитаемое название, затем ищем все Feature,
чей FeatureTypeID соответствует "Digital Currency Address".

ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ v1: разрешение человекочитаемого имени сущности
(entity name) требует ещё одного перехода по графу (IdentityReference ->
Identity -> Profile -> DistinctParty -> alias/name), что в этой схеме
устроено сложнее обычного плоского Entity и не проверялось на реальном
файле (сеть до treasury.gov недоступна из песочницы, где это писалось).
Чтобы не выдавать непроверенную логику за рабочую, v1 сознательно
ограничивается адресом + числовыми ID (IdentityID, FeatureID) без имени —
этого достаточно для записи в label_cache. Разрешение имени — TODO.
"""

import logging
import re
from typing import Dict, List
from xml.etree import ElementTree as ET

import aiohttp

from common.evidence_store import upsert_label

logger = logging.getLogger(__name__)

SDN_ADVANCED_XML_URL = "https://www.treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml"

# EVM-адреса всегда в формате 0x + 40 hex-символов, независимо от того, каким
# тикером (ETH/USDT/USDC/ARB/BSC) их пометил OFAC — это один и тот же ключ,
# просто засвеченный в разных сетях/токенах.
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Известные реально существующие sanctioned-адреса (SUEX OTC, назначение
# подтверждено официальным пресс-релизом OFAC от 21.09.2021) — использовать
# как sanity-check при первом реальном прогоне парсера, см.
# attribution/tests/test_ofac.py::test_known_real_addresses_note и README.
KNOWN_SANCTIONED_ADDRESSES_FOR_SANITY_CHECK = [
    "0x2f389ce8bd8ff92de3402ffce4691d17fc4f6535",
    "0x19aa5fe80d33a56d56c78e82ea5e50e5d80b4dff",
    "0xe7aa314c77f4233c18c6cc84384a9247c0cf367b",
    "0x308ed4b7b49797e1a98d3818bff6fe5385410370",
]


def _strip_ns(tag: str) -> str:
    """'{namespace}TagName' -> 'TagName'."""
    return tag.split("}")[-1] if "}" in tag else tag


def parse_sdn_advanced_xml(xml_bytes: bytes) -> List[Dict[str, str]]:
    """
    Парсит sdn_advanced.xml и возвращает список найденных EVM-адресов вида:
        {"address": "0x...", "feature_id": "...", "identity_id": "...", "asset_tag": "ETH"}

    Не привязано к сети (EVM-адреса валидны на любой EVM-сети) — chain_id
    проставляется как None при записи в label_cache.

    Реализовано без жёсткой привязки к namespace/родительским путям (кроме
    самого факта вложенности FeatureType/Feature/FeatureVersion/VersionDetail)
    — чтобы не сломаться на минорных изменениях структуры вокруг них.
    """
    root = ET.fromstring(xml_bytes)

    # Шаг 1: строим FeatureTypeID -> название (например "Digital Currency Address - ETH")
    feature_type_names: Dict[str, str] = {}
    for el in root.iter():
        if _strip_ns(el.tag) == "FeatureType" and el.get("ID"):
            feature_type_names[el.get("ID")] = (el.text or "").strip()

    digital_currency_type_ids = {
        fid for fid, name in feature_type_names.items() if "Digital Currency Address" in name
    }
    if not digital_currency_type_ids:
        logger.warning(
            "Не найдено ни одного FeatureType с 'Digital Currency Address' — "
            "возможно, структура XML изменилась. Проверьте вручную."
        )

    # Шаг 2: ищем все Feature с подходящим FeatureTypeID, достаём VersionDetail
    results: List[Dict[str, str]] = []
    for feature_el in root.iter():
        if _strip_ns(feature_el.tag) != "Feature":
            continue
        if feature_el.get("FeatureTypeID") not in digital_currency_type_ids:
            continue

        asset_tag = feature_type_names.get(feature_el.get("FeatureTypeID"), "")
        if "-" in asset_tag:
            asset_tag = asset_tag.split("-")[-1].strip()

        identity_id = ""
        for child in feature_el.iter():
            if _strip_ns(child.tag) == "IdentityReference" and child.get("IdentityID"):
                identity_id = child.get("IdentityID")
                break

        for child in feature_el.iter():
            if _strip_ns(child.tag) == "VersionDetail" and child.text:
                address = child.text.strip()
                if _EVM_ADDRESS_RE.match(address):
                    results.append({
                        "address": address.lower(),
                        "feature_id": feature_el.get("ID", ""),
                        "identity_id": identity_id,
                        "asset_tag": asset_tag,
                    })

    return results


async def download_sdn_advanced_xml() -> bytes:
    """Скачивает актуальный sdn_advanced.xml (файл большой, ~80+ МБ)."""
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(SDN_ADVANCED_XML_URL) as response:
            response.raise_for_status()
            return await response.read()


async def refresh_ofac_sdn() -> int:
    """
    Полный цикл обновления: скачать список -> распарсить -> записать все
    найденные EVM-адреса в label_cache. Возвращает число записанных адресов.

    Вызывать периодически (раз в несколько часов), а НЕ при каждой проверке
    одного адреса — см. docstring модуля.
    """
    xml_bytes = await download_sdn_advanced_xml()
    entries = parse_sdn_advanced_xml(xml_bytes)

    for entry in entries:
        await upsert_label(
            address=entry["address"],
            source="ofac_sdn",
            label_type="sanctioned",
            chain_id=None,
            confidence=1.0,
            raw_data={
                "feature_id": entry["feature_id"],
                "identity_id": entry["identity_id"],
                "asset_tag": entry["asset_tag"],
            },
        )

    logger.info(f"OFAC SDN: обновлено {len(entries)} EVM-адресов в label_cache")
    return len(entries)
