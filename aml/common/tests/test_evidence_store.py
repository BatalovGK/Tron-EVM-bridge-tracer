# common/tests/test_evidence_store.py
"""
Тесты работают против РЕАЛЬНОГО PostgreSQL, а не моков — именно так были
найдены и исправлены настоящие баги (NULL в UNIQUE-ограничении, потеря
точности REAL vs DOUBLE PRECISION, несогласованный регистр адресов).
Моки на dict/JSON здесь бы эти проблемы не поймали.

Требует переменную окружения POSTGRES_DSN, указывающую на тестовую БД
(НЕ на продакшн — тесты чистят таблицы между собой через TRUNCATE).

Пример запуска:
    export POSTGRES_DSN="postgresql://postgres:pass@localhost/aml_platform_test"
    pytest common/tests/test_evidence_store.py -v
"""

import uuid
import pytest
import pytest_asyncio

from common.db import init_schema, get_pool, close_pool
from common.evidence_store import (
    upsert_label, get_labels,
    upsert_seed, get_seeds,
    write_evidence, get_evidence,
    write_flow_edge, get_flow_edges,
    write_verdict, get_verdict,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    """Пересоздаёт пул и накатывает схему перед каждым тестом, чистит таблицы.
    Пул пересоздаётся на каждый тест (а не один на модуль), потому что
    pytest-asyncio в strict-режиме даёт каждому тесту свой event loop, а
    asyncpg-пул, созданный в одном loop, нельзя использовать в другом."""
    await close_pool()
    await init_schema()
    pool = await get_pool()
    await pool.execute("TRUNCATE label_cache, seed_registry, evidence_log, flow_edges, verdicts")
    yield
    await close_pool()


@pytest.mark.asyncio
async def test_upsert_label_deduplicates_with_null_chain_id():
    """Регресс-тест на баг: NULL chain_id (сети-агностичные источники вроде
    OFAC) не дедуплицировался бы через обычный UNIQUE-constraint."""
    await upsert_label("0xDEADBEEF", source="ofac_sdn", label_type="sanctioned", raw_data={"entry": "v1"})
    await upsert_label("0xDEADBEEF", source="ofac_sdn", label_type="sanctioned", raw_data={"entry": "v2"})

    labels = await get_labels("0xDEADBEEF")
    assert len(labels) == 1, "Повторный upsert с chain_id=NULL не должен создавать дубликат"
    assert labels[0]["raw_data"]["entry"] == "v2"


@pytest.mark.asyncio
async def test_get_labels_includes_chain_agnostic_and_chain_specific():
    await upsert_label("0xabc", source="ofac_sdn", label_type="sanctioned")
    await upsert_label("0xabc", source="goplus", label_type="mixer", chain_id=1)
    await upsert_label("0xabc", source="goplus", label_type="mixer", chain_id=56)

    labels_chain_1 = await get_labels("0xabc", chain_id=1)
    sources = {l["source"] for l in labels_chain_1}
    assert sources == {"ofac_sdn", "goplus"}
    assert len(labels_chain_1) == 2  # ofac (chain-agnostic) + goplus для chain_id=1, но НЕ chain_id=56


@pytest.mark.asyncio
async def test_address_case_insensitive():
    """Адреса должны нормализовываться в нижний регистр при записи/чтении."""
    await upsert_label("0xAbCdEf", source="goplus", label_type="phishing", chain_id=1)
    labels = await get_labels("0xabcdef", chain_id=1)
    assert len(labels) == 1


@pytest.mark.asyncio
async def test_seed_registry_derived_from_case_normalized():
    """Регресс-тест: derived_from раньше не приводился к нижнему регистру,
    что рассинхронизировало бы ссылки между посевами."""
    await upsert_seed("0xSEED1", chain_id=1, tag="root", seed_source="manual")
    await upsert_seed("0xSEED2", chain_id=1, tag="child", seed_source="cluster_auto", derived_from="0xSEED1")

    seeds = await get_seeds("0xseed2", chain_id=1)
    assert seeds[0]["derived_from"] == "0xseed1"


@pytest.mark.asyncio
async def test_seed_upsert_updates_confidence_not_duplicates():
    await upsert_seed("0xabc", chain_id=1, tag="darknet_vendor", seed_source="manual", confidence=0.5)
    await upsert_seed("0xabc", chain_id=1, tag="darknet_vendor", seed_source="manual", confidence=0.9)

    seeds = await get_seeds("0xabc", chain_id=1)
    assert len(seeds) == 1
    assert seeds[0]["confidence"] == 0.9


@pytest.mark.asyncio
async def test_evidence_log_preserves_order_and_payload():
    inv_id = uuid.uuid4()
    await write_evidence(
        investigation_id=inv_id, mode="incident_response", subagent="network_adapter",
        action="get_address_info", payload={"hash": "0x1", "balance": "100"},
        source="blockscout_pro_api", chain_id=1,
    )
    await write_evidence(
        investigation_id=inv_id, mode="incident_response", subagent="flow_hop_tracer",
        action="get_internal_transactions", payload={"items": [{"tx": "0xabc"}]},
        source="blockscout_pro_api", chain_id=1,
    )

    evidence = await get_evidence(inv_id)
    assert [e["subagent"] for e in evidence] == ["network_adapter", "flow_hop_tracer"]
    assert evidence[1]["payload"]["items"][0]["tx"] == "0xabc"


@pytest.mark.asyncio
async def test_evidence_log_isolated_by_investigation_id():
    inv1, inv2 = uuid.uuid4(), uuid.uuid4()
    await write_evidence(
        investigation_id=inv1, mode="aml_check", subagent="network_adapter",
        action="a", payload={}, source="s",
    )
    await write_evidence(
        investigation_id=inv2, mode="aml_check", subagent="network_adapter",
        action="b", payload={}, source="s",
    )

    assert len(await get_evidence(inv1)) == 1
    assert len(await get_evidence(inv2)) == 1


@pytest.mark.asyncio
async def test_flow_edge_preserves_hop_order_and_case():
    inv_id = uuid.uuid4()
    await write_flow_edge(
        investigation_id=inv_id, chain_id=1, hop_number=1,
        parent_address="0xPARENT", child_address="0xCHILD1", tx_hash="0xTX1",
        edge_kind="transfer", tainted=True,
    )
    await write_flow_edge(
        investigation_id=inv_id, chain_id=1, hop_number=2,
        parent_address="0xCHILD1", child_address="0xCHILD2", tx_hash="0xTX2",
        edge_kind="swap", token="0xTOKEN", amount=12.5, tainted=True,
    )

    edges = await get_flow_edges(inv_id)
    assert [e["hop_number"] for e in edges] == [1, 2]
    assert edges[0]["parent_address"] == "0xparent"
    assert edges[1]["token"] == "0xtoken"
    assert edges[1]["amount"] == 12.5
    assert all(e["tainted"] for e in edges)


@pytest.mark.asyncio
async def test_flow_edge_isolated_by_investigation_id():
    inv1, inv2 = uuid.uuid4(), uuid.uuid4()
    await write_flow_edge(
        investigation_id=inv1, chain_id=1, hop_number=1,
        parent_address="0xa", child_address="0xb", tx_hash="0xtx1",
    )
    await write_flow_edge(
        investigation_id=inv2, chain_id=1, hop_number=1,
        parent_address="0xc", child_address="0xd", tx_hash="0xtx2",
    )

    assert len(await get_flow_edges(inv1)) == 1
    assert len(await get_flow_edges(inv2)) == 1


@pytest.mark.asyncio
async def test_flow_edge_terminal_reason_nullable_by_default():
    inv_id = uuid.uuid4()
    await write_flow_edge(
        investigation_id=inv_id, chain_id=1, hop_number=1,
        parent_address="0xa", child_address="0xb", tx_hash="0xtx1",
    )
    edges = await get_flow_edges(inv_id)
    assert edges[0]["terminal_reason"] is None
    assert edges[0]["edge_kind"] == "transfer"


@pytest.mark.asyncio
async def test_verdict_risk_score_precision_preserved():
    """Регресс-тест: колонка была REAL (float32) и теряла точность на
    значениях вроде 0.15, приходящих из Python float (float64)."""
    inv_id = uuid.uuid4()
    await write_verdict(inv_id, mode="aml_check", risk_score=0.15, narrative="test")

    verdict = await get_verdict(inv_id)
    assert verdict["risk_score"] == 0.15


@pytest.mark.asyncio
async def test_verdict_upsert_updates_not_duplicates():
    inv_id = uuid.uuid4()
    await write_verdict(inv_id, mode="aml_check", risk_score=0.1, narrative="первичный")
    await write_verdict(inv_id, mode="aml_check", risk_score=0.95, narrative="обновлённый", escalated=True)

    verdict = await get_verdict(inv_id)
    assert verdict["risk_score"] == 0.95
    assert verdict["escalated"] is True
    assert verdict["narrative"] == "обновлённый"


@pytest.mark.asyncio
async def test_get_verdict_returns_none_when_missing():
    assert await get_verdict(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_evidence_payload_with_nested_datetime_from_db_row():
    """Регресс-тест: payload иногда содержит данные, уже прочитанные из БД
    (например, результат get_labels() с datetime в fetched_at) — раньше это
    падало с 'Object of type datetime is not JSON serializable'."""
    await upsert_label("0xabc", source="ofac_sdn", label_type="sanctioned")
    labels = await get_labels("0xabc")
    assert isinstance(labels[0]["fetched_at"], object)  # это datetime, не строка

    inv_id = uuid.uuid4()
    await write_evidence(
        investigation_id=inv_id, mode="aml_check", subagent="attribution",
        action="check_ofac_sdn_cached", payload={"sanctioned": True, "labels": labels},
        source="ofac_sdn",
    )
    evidence = await get_evidence(inv_id)
    assert evidence[0]["payload"]["sanctioned"] is True
    assert isinstance(evidence[0]["payload"]["labels"][0]["fetched_at"], str)
