"""
Офлайн-тест layerzero_tracer.py: подменяем requests.get синтетическим ответом,
повторяющим реальную схему LayerZero Scan API (/v1/messages/tx/{tx}, см.
https://docs.layerzero.network/v2/tools/api/scan/testnet), чтобы проверить
логику парсинга без доступа к сети (в этой песочнице сеть отключена).
"""
from unittest.mock import patch, MagicMock
import layerzero_tracer as lz


def _build_oft_payload(recipient_hex: str, amount_sd: int) -> str:
    """Строит синтетический OFT-payload в реальном формате (см.
    layerzero_tracer._decode_oft_recipient): 2 байта типа + 32-байтный
    padded-адрес + 8-байтный amountSD."""
    addr_bytes = bytes.fromhex(recipient_hex.removeprefix("0x"))
    payload = b"\x00\x02" + (b"\x00" * 12 + addr_bytes) + amount_sd.to_bytes(8, "big")
    return "0x" + payload.hex()


# Синтетический реальный получатель, зашифрованный в payload — ОТЛИЧАЕТСЯ от
# pathway.receiver.address ниже (адрес OApp-контракта), как и в реальных
# данных: см. _decode_oft_recipient и live-проверку в этой сессии
# (tx 0xcae6f9052cc8...), где эти два адреса тоже разошлись.
OFT_REAL_RECIPIENT = "0x" + ("00" * 19) + "ab"  # 20-байтный EVM-адрес
OFT_AMOUNT_SD = 123456789

# Синтетическое сообщение по схеме LayerZero Scan API, смоделировано под
# перевод TRON (eid=30420) -> Ethereum (eid=30101), статус DELIVERED.
FAKE_MESSAGE_DELIVERED = {
    "data": [
        {
            "pathway": {
                "srcEid": 30420,
                "dstEid": 30101,
                "sender": {"address": "TXYZ1234567890abcdefTronSenderAddress", "chain": "tron"},
                "receiver": {"address": "0xUSDT0OAppAddressOnEthereum", "chain": "ethereum"},
                "id": "30420-30101-0xUSDT0OAppAddress",
                "nonce": 42,
            },
            "source": {
                "status": "SUCCEEDED",
                "tx": {
                    "txHash": "tron_source_tx_hash",
                    "blockNumber": "12345678",
                    "blockTimestamp": 1786000000,
                    "from": "TXYZ1234567890abcdefTronSenderAddress",
                    "value": "0",
                    "payload": _build_oft_payload(OFT_REAL_RECIPIENT, OFT_AMOUNT_SD),
                },
            },
            "destination": {
                "status": "SUCCEEDED",
                "tx": {
                    "txHash": "0xdeadbeefdeadbeef_eth_dest_tx",
                    "blockNumber": 21000000,
                    "blockTimestamp": 1786000300,
                },
            },
            "verification": {
                "dvn": {"status": "SUCCEEDED"},
                "sealer": {"status": "SUCCEEDED"},
            },
            "guid": "0xguid1234567890abcdef",
            "status": {"name": "DELIVERED", "message": "Delivered and executed on the destination chain"},
        }
    ]
}

FAKE_MESSAGE_INFLIGHT = {
    "data": [
        {
            "pathway": {
                "srcEid": 30420,
                "dstEid": 30101,
                "sender": {"address": "TAnotherTronSender", "chain": "tron"},
                "receiver": {"address": "0xExpectedRecipient", "chain": "ethereum"},
                "id": "30420-30101-0xUSDT0OAppAddress",
                "nonce": 43,
            },
            "source": {
                "status": "VALIDATING_TX",
                "tx": {
                    "txHash": "tron_source_tx_inflight",
                    "blockTimestamp": 1786000500,
                    "from": "TAnotherTronSender",
                },
            },
            "destination": {"status": "WAITING", "tx": {}},
            "verification": {"dvn": {"status": "WAITING"}},
            "guid": "0xguid_inflight",
            "status": {"name": "INFLIGHT", "message": "Waiting for verification or execution"},
        }
    ]
}


def _mock_get(payload, status_code=200):
    def _get(url, timeout=None, headers=None):
        m = MagicMock()
        m.status_code = status_code
        m.json.return_value = payload
        m.text = str(payload)
        return m
    return _get


def test_delivered_case():
    with patch("requests.get", new=_mock_get(FAKE_MESSAGE_DELIVERED)):
        result = lz.find_bridge_crossing("tron_source_tx_hash")
    assert result["found"] is True
    assert result["confidence"] == "HIGH"
    assert result["bridge_entry"]["chain"] == "Tron"
    assert result["bridge_exit"]["chain"] == "Ethereum"
    assert result["bridge_exit"]["tx_hash"] == "0xdeadbeefdeadbeef_eth_dest_tx"
    assert result["message_status"] == "DELIVERED"
    print("test_delivered_case: OK")
    lz.print_result(result)


def test_oft_payload_decoding_prefers_real_recipient():
    """Когда payload — валидный OFT send-пакет, to_address должен быть
    декодированным реальным получателем, а НЕ адресом OApp-контракта
    (pathway.receiver) — см. _decode_oft_recipient и живую проверку в
    докстринге модуля, где эти два адреса разошлись на реальных данных."""
    with patch("requests.get", new=_mock_get(FAKE_MESSAGE_DELIVERED)):
        result = lz.find_bridge_crossing("tron_source_tx_hash")
    exit_ = result["bridge_exit"]
    assert exit_["to_address"] == OFT_REAL_RECIPIENT
    assert exit_["recipient_source"] == "oft_payload_decoded"
    assert exit_["amount_shared_decimals"] == OFT_AMOUNT_SD
    assert exit_["oapp_address"] == "0xUSDT0OAppAddressOnEthereum"
    assert exit_["to_address"] != exit_["oapp_address"]
    print("\ntest_oft_payload_decoding_prefers_real_recipient: OK")


def test_inflight_case():
    with patch("requests.get", new=_mock_get(FAKE_MESSAGE_INFLIGHT)):
        result = lz.find_bridge_crossing("tron_source_tx_inflight")
    assert result["found"] is True
    assert result["bridge_exit"]["tx_hash"] is None
    assert result["bridge_exit"]["status"] == "NOT DELIVERED YET"
    assert "верификацию DVN" in result["note"]
    print("\ntest_inflight_case: OK")
    lz.print_result(result)


def test_missing_payload_falls_back_to_oapp_address():
    """Без payload (или в неожиданном формате) to_address корректно
    деградирует к pathway.receiver.address, помечая источник как fallback,
    а не падает и не молча врёт про точность."""
    with patch("requests.get", new=_mock_get(FAKE_MESSAGE_INFLIGHT)):
        result = lz.find_bridge_crossing("tron_source_tx_inflight")
    exit_ = result["bridge_exit"]
    assert exit_["recipient_source"] == "pathway_receiver_fallback"
    assert exit_["to_address"] == exit_["oapp_address"]
    assert exit_["amount_shared_decimals"] is None
    print("\ntest_missing_payload_falls_back_to_oapp_address: OK")


def test_not_found_case():
    with patch("requests.get", new=_mock_get({"data": []})):
        result = lz.find_bridge_crossing("0xdeadbeef")
    assert result["found"] is False
    assert result["confidence"] == "UNRESOLVED"
    print("\ntest_not_found_case: OK")
    lz.print_result(result)


def test_404_case():
    with patch("requests.get", new=_mock_get({}, status_code=404)):
        result = lz.find_bridge_crossing("0xnothinghere")
    assert result["found"] is False
    print("\ntest_404_case: OK")


if __name__ == "__main__":
    test_delivered_case()
    test_oft_payload_decoding_prefers_real_recipient()
    test_inflight_case()
    test_missing_payload_falls_back_to_oapp_address()
    test_not_found_case()
    test_404_case()
    print("\nВСЕ ОФЛАЙН-ТЕСТЫ ПРОЙДЕНЫ")
