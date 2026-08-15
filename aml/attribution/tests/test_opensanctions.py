# attribution/tests/test_opensanctions.py
"""
Тесты attribution.opensanctions.

- parse_targets_nested / _iter_nested_entities — офлайн, на синтетических
  фикстурах JSON Lines, собранных по документации FtM/OpenSanctions (см.
  docstring attribution/opensanctions.py). Сеть до data.opensanctions.org
  недоступна из песочницы, поэтому это НЕ проверка на реальном снапшоте —
  как и у OFAC, нужен ручной прогон перед продом (scripts/manual_check_attribution.py).
- refresh_opensanctions_bulk — на моках download_targets_nested/upsert_label.
- check_address_opensanctions (живой /match, опциональный резерв) — на
  моках aiohttp, как раньше.
"""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from attribution.opensanctions import (
    check_address_opensanctions,
    parse_targets_nested,
    refresh_opensanctions_bulk,
)


def _nested_line(
    target_id="NK-target1",
    caption="Some Sanctioned Entity",
    topics=None,
    wallet_addresses=None,
    wallet_schema="CryptoWallet",
):
    """Строит одну строку targets.nested.json: целевая сущность с вложенным
    криптокошельком где-то внутри properties (ключ произвольный — парсер
    рекурсивный и не привязан к конкретному имени свойства, см. docstring)."""
    topics = topics if topics is not None else ["sanction"]
    wallet_addresses = wallet_addresses if wallet_addresses is not None else ["0x2f389ce8bd8ff92de3402ffce4691d17fc4f6535"]
    return json.dumps({
        "id": target_id,
        "schema": "Person",
        "caption": caption,
        "datasets": ["us_ofac_sdn"],
        "properties": {
            "name": [caption],
            "topics": topics,
            "assets": [
                {
                    "id": f"{target_id}-wallet",
                    "schema": wallet_schema,
                    "properties": {
                        "publicKey": wallet_addresses,
                        "owner": [target_id],
                    },
                }
            ],
        },
    })


class TestParseTargetsNested:
    def test_extracts_evm_address_from_nested_wallet(self):
        data = _nested_line().encode("utf-8")
        results = parse_targets_nested(data)

        assert len(results) == 1
        assert results[0]["address"] == "0x2f389ce8bd8ff92de3402ffce4691d17fc4f6535"
        assert results[0]["target_id"] == "NK-target1"
        assert results[0]["target_topics"] == ["sanction"]

    def test_multiple_lines_all_parsed(self):
        lines = "\n".join([
            _nested_line(target_id="NK-1", wallet_addresses=["0x2f389ce8bd8ff92de3402ffce4691d17fc4f6535"]),
            _nested_line(target_id="NK-2", wallet_addresses=["0x19aa5fe80d33a56d56c78e82ea5e50e5d80b4dff"]),
        ])
        results = parse_targets_nested(lines.encode("utf-8"))
        addresses = {r["address"] for r in results}

        assert len(results) == 2
        assert addresses == {"0x2f389ce8bd8ff92de3402ffce4691d17fc4f6535", "0x19aa5fe80d33a56d56c78e82ea5e50e5d80b4dff"}

    def test_non_crypto_wallet_entities_ignored(self):
        line = json.dumps({
            "id": "NK-1", "schema": "Person", "caption": "Someone",
            "properties": {
                "topics": ["sanction"],
                "addresses": [{"id": "NK-addr", "schema": "Address", "properties": {"full": ["123 Main St"]}}],
            },
        })
        results = parse_targets_nested(line.encode("utf-8"))
        assert results == []

    def test_non_evm_wallet_address_skipped(self):
        """v1 ограничен EVM-адресами (см. docstring) — BTC/TRON форматы здесь
        сознательно не парсятся, чтобы не выдавать непроверенную логику за рабочую."""
        data = _nested_line(wallet_addresses=["bc1qxyz2ovv5f4example"]).encode("utf-8")
        results = parse_targets_nested(data)
        assert results == []

    def test_blank_and_malformed_lines_skipped_without_crashing(self):
        malformed = "not valid json{{{"
        good = _nested_line()
        data = f"\n{malformed}\n\n{good}\n".encode("utf-8")

        results = parse_targets_nested(data)

        assert len(results) == 1
        assert results[0]["target_id"] == "NK-target1"

    def test_no_sanction_topic_still_extracted_with_its_topics(self):
        """Парсер не решает, что считать риском — это делает
        refresh_opensanctions_bulk/label_type. Здесь просто честно передаём
        topics дальше, какие бы они ни были (напр. просто 'poi')."""
        data = _nested_line(topics=["poi"]).encode("utf-8")
        results = parse_targets_nested(data)
        assert results[0]["target_topics"] == ["poi"]


class TestRefreshOpenSanctionsBulk:
    @pytest.mark.asyncio
    async def test_writes_sanctioned_label_for_sanction_topic(self):
        data = _nested_line(topics=["sanction"]).encode("utf-8")
        with patch("attribution.opensanctions.download_targets_nested", new=AsyncMock(return_value=data)), \
             patch("attribution.opensanctions.upsert_label", new=AsyncMock()) as mock_upsert:
            count = await refresh_opensanctions_bulk()

        assert count == 1
        mock_upsert.assert_awaited_once()
        _, kwargs = mock_upsert.call_args
        assert kwargs["label_type"] == "sanctioned"
        assert kwargs["source"] == "opensanctions_bulk"

    @pytest.mark.asyncio
    async def test_non_sanction_topics_joined_as_label_type(self):
        data = _nested_line(topics=["poi", "role.pep"]).encode("utf-8")
        with patch("attribution.opensanctions.download_targets_nested", new=AsyncMock(return_value=data)), \
             patch("attribution.opensanctions.upsert_label", new=AsyncMock()) as mock_upsert:
            await refresh_opensanctions_bulk()

        _, kwargs = mock_upsert.call_args
        assert kwargs["label_type"] == "poi,role.pep"

    @pytest.mark.asyncio
    async def test_empty_bulk_file_writes_nothing(self):
        with patch("attribution.opensanctions.download_targets_nested", new=AsyncMock(return_value=b"")), \
             patch("attribution.opensanctions.upsert_label", new=AsyncMock()) as mock_upsert:
            count = await refresh_opensanctions_bulk()

        assert count == 0
        mock_upsert.assert_not_awaited()


# --- Живой /match (опциональный резерв, НЕ используется для проверки адресов
# в attribution/service.py — см. docstring модуля) ---

def _mock_response(json_data, status=200):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


@pytest.mark.asyncio
async def test_skipped_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENSANCTIONS_API_KEY", raising=False)
    result = await check_address_opensanctions("0xabc")
    assert result is None


@pytest.mark.asyncio
async def test_match_found_marks_risky(monkeypatch):
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "test_key_123")
    response_with_match = {
        "responses": {
            "q": {
                "results": [
                    {"caption": "Some Sanctioned Entity", "score": 0.95,
                     "properties": {"topics": ["sanction"]}}
                ]
            }
        }
    }
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_mock_response(response_with_match))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await check_address_opensanctions("0xabc")

    assert result["is_risky"] is True
    assert result["matches"][0]["caption"] == "Some Sanctioned Entity"


@pytest.mark.asyncio
async def test_low_score_match_not_risky(monkeypatch):
    """Слабое совпадение (ниже порога 0.7) не должно помечаться как риск —
    иначе много false positive на широком поиске по имени."""
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "test_key_123")
    weak_match_response = {
        "responses": {"q": {"results": [{"caption": "Weak Match", "score": 0.3, "properties": {}}]}}
    }
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_mock_response(weak_match_response))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await check_address_opensanctions("0xabc")

    assert result["is_risky"] is False


@pytest.mark.asyncio
async def test_no_results_not_risky(monkeypatch):
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "test_key_123")
    empty_response = {"responses": {"q": {"results": []}}}
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_mock_response(empty_response))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await check_address_opensanctions("0xabc")

    assert result["is_risky"] is False
    assert result["matches"] == []
