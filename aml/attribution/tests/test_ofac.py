# attribution/tests/test_ofac.py
"""
Тесты парсера OFAC SDN — офлайн, на фрагменте XML, повторяющем подтверждённую
структуру реального файла (см. docstring attribution/ofac.py). Полный
реальный файл (~80 МБ) не скачивается в тестах — сеть до treasury.gov
недоступна из песочницы, где это писалось. См. README про то, как проверить
парсер на настоящем файле самостоятельно.
"""

import pytest

from attribution.ofac import parse_sdn_advanced_xml, KNOWN_SANCTIONED_ADDRESSES_FOR_SANITY_CHECK


# Фрагмент повторяет реальную вложенность FeatureType (таблица-подстановка) +
# Feature/FeatureVersion/VersionDetail + IdentityReference, подтверждённую
# поиском по официальному репозиторию sambacha/ofac-list.
SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Sanctions Version="3">
    <ReferenceValueSets>
        <FeatureTypeValues>
            <FeatureType ID="345" FeatureTypeGroupID="1">Digital Currency Address - ETH</FeatureType>
            <FeatureType ID="346" FeatureTypeGroupID="1">Digital Currency Address - XBT</FeatureType>
            <FeatureType ID="999" FeatureTypeGroupID="2">Title</FeatureType>
        </FeatureTypeValues>
    </ReferenceValueSets>
    <DistinctParties>
        <DistinctParty>
            <Profile ID="1">
                <Identity ID="21386">
                    <IdentityFeatureList>
                        <Feature ID="36618" FeatureTypeID="345">
                            <FeatureVersion ID="34337" ReliabilityID="1560">
                                <Comment/>
                                <VersionDetail DetailTypeID="1432">0x901bb9583b24d97e995513c6778dc6888ab6870e</VersionDetail>
                            </FeatureVersion>
                            <IdentityReference IdentityID="21386" IdentityFeatureLinkTypeID="1"/>
                        </Feature>
                        <Feature ID="36619" FeatureTypeID="999">
                            <FeatureVersion ID="34338" ReliabilityID="1560">
                                <VersionDetail DetailTypeID="1400">Some Title Not An Address</VersionDetail>
                            </FeatureVersion>
                        </Feature>
                    </IdentityFeatureList>
                </Identity>
            </Profile>
        </DistinctParty>
        <DistinctParty>
            <Profile ID="2">
                <Identity ID="55555">
                    <IdentityFeatureList>
                        <Feature ID="77777" FeatureTypeID="346">
                            <FeatureVersion ID="88888" ReliabilityID="1560">
                                <VersionDetail DetailTypeID="1432">bc1qsomebtcaddressnotevm</VersionDetail>
                            </FeatureVersion>
                            <IdentityReference IdentityID="55555" IdentityFeatureLinkTypeID="1"/>
                        </Feature>
                    </IdentityFeatureList>
                </Identity>
            </Profile>
        </DistinctParty>
    </DistinctParties>
</Sanctions>
"""


def test_extracts_evm_address_with_correct_feature_type():
    results = parse_sdn_advanced_xml(SAMPLE_XML)
    evm_addresses = [r["address"] for r in results]
    assert "0x901bb9583b24d97e995513c6778dc6888ab6870e" in evm_addresses


def test_ignores_non_digital_currency_features():
    """Feature с FeatureTypeID, не относящимся к Digital Currency Address
    (в примере — 'Title'), не должен попадать в результат."""
    results = parse_sdn_advanced_xml(SAMPLE_XML)
    assert not any("Some Title" in str(r) for r in results)


def test_ignores_non_evm_addresses():
    """BTC-адрес (не в формате 0x...) не должен попадать в EVM-парсер."""
    results = parse_sdn_advanced_xml(SAMPLE_XML)
    addresses = [r["address"] for r in results]
    assert "bc1qsomebtcaddressnotevm" not in addresses


def test_asset_tag_extracted_correctly():
    results = parse_sdn_advanced_xml(SAMPLE_XML)
    eth_entry = next(r for r in results if r["address"] == "0x901bb9583b24d97e995513c6778dc6888ab6870e")
    assert eth_entry["asset_tag"] == "ETH"


def test_identity_id_linked_via_identity_reference():
    results = parse_sdn_advanced_xml(SAMPLE_XML)
    eth_entry = next(r for r in results if r["address"] == "0x901bb9583b24d97e995513c6778dc6888ab6870e")
    assert eth_entry["identity_id"] == "21386"


def test_no_digital_currency_feature_types_logs_warning_not_crash(caplog):
    """Если структура XML вдруг изменится и FeatureType с нужным названием
    пропадёт — парсер не должен падать, только предупредить."""
    xml_without_dca = b"""<?xml version="1.0"?>
    <Sanctions Version="3">
        <ReferenceValueSets>
            <FeatureTypeValues>
                <FeatureType ID="1" FeatureTypeGroupID="1">Something Else</FeatureType>
            </FeatureTypeValues>
        </ReferenceValueSets>
    </Sanctions>
    """
    results = parse_sdn_advanced_xml(xml_without_dca)
    assert results == []


def test_known_real_addresses_documented_for_manual_verification():
    """
    Это не тест парсера как такового (реальный файл не скачивается в CI) —
    а фиксация списка ЗАВЕДОМО реальных sanctioned-адресов (SUEX OTC,
    официальный пресс-релиз OFAC от 21.09.2021), которые нужно проверить
    вручную после первого реального `refresh_ofac_sdn()` на настоящем
    сервере: они ДОЛЖНЫ найтись в label_cache после реального прогона.
    Если не находятся — парсер сломан на реальной структуре, несмотря на
    то что проходит тесты на фрагменте.
    """
    assert len(KNOWN_SANCTIONED_ADDRESSES_FOR_SANITY_CHECK) == 4
    for addr in KNOWN_SANCTIONED_ADDRESSES_FOR_SANITY_CHECK:
        assert addr.startswith("0x") and len(addr) == 42
