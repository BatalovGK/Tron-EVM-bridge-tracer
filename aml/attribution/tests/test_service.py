# attribution/tests/test_service.py
"""
Тесты attribution.service.check_address на РЕАЛЬНОМ Postgres (проверяем, что
evidence_log и label_cache реально пишутся с провенансом), с замоканными
GoPlus/OpenSanctions (сеть до них недоступна из песочницы).

Требует POSTGRES_DSN, как и common/tests/test_evidence_store.py.
"""

import uuid
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from common.db import init_schema, get_pool, close_pool
from common.evidence_store import upsert_label, get_evidence
from attribution.service import check_address


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    await close_pool()
    await init_schema()
    pool = await get_pool()
    await pool.execute("TRUNCATE label_cache, seed_registry, evidence_log, verdicts")
    yield
    await close_pool()


@pytest.mark.asyncio
async def test_ofac_sanctioned_address_gives_high_risk():
    await upsert_label("0xsanctioned", source="ofac_sdn", label_type="sanctioned", chain_id=None)

    clean_goplus = {"is_risky": False, "risk_flags": [], "raw": {"code": 1}}
    with patch("attribution.service.check_address_goplus", new=AsyncMock(return_value=clean_goplus)):
        result = await check_address("0xSanctioned", chain_id=1)

    assert result["ofac_sanctioned"] is True
    assert result["overall_risk"] == "high"


@pytest.mark.asyncio
async def test_opensanctions_bulk_label_gives_high_risk():
    """OpenSanctions теперь читается из label_cache (bulk-паттерн, см.
    attribution/opensanctions.py::refresh_opensanctions_bulk), а не через
    живой /match — метка должна попадать в overall_risk так же, как OFAC."""
    await upsert_label("0xfromopensanctions", source="opensanctions_bulk", label_type="sanctioned", chain_id=None)

    clean_goplus = {"is_risky": False, "risk_flags": [], "raw": {"code": 1}}
    with patch("attribution.service.check_address_goplus", new=AsyncMock(return_value=clean_goplus)):
        result = await check_address("0xFromOpenSanctions", chain_id=1)

    assert result["opensanctions_checked"] is True
    assert result["opensanctions_risky"] is True
    assert result["overall_risk"] == "high"


@pytest.mark.asyncio
async def test_goplus_risky_without_ofac_gives_medium_risk():
    risky_goplus = {"is_risky": True, "risk_flags": ["phishing"], "raw": {"code": 1}}

    with patch("attribution.service.check_address_goplus", new=AsyncMock(return_value=risky_goplus)):
        result = await check_address("0xclean_but_phishy", chain_id=1)

    assert result["ofac_sanctioned"] is False
    assert result["goplus_risky"] is True
    assert result["overall_risk"] == "medium"


@pytest.mark.asyncio
async def test_all_clean_gives_low_risk():
    clean_goplus = {"is_risky": False, "risk_flags": [], "raw": {"code": 1}}

    with patch("attribution.service.check_address_goplus", new=AsyncMock(return_value=clean_goplus)):
        result = await check_address("0xtotallyclean", chain_id=1)

    assert result["overall_risk"] == "low"
    assert result["opensanctions_checked"] is True
    assert result["opensanctions_risky"] is False


@pytest.mark.asyncio
async def test_goplus_failure_does_not_crash_check():
    """Если GoPlus недоступен (сеть/лимиты) — проверка должна продолжиться
    по остальным источникам, а не упасть целиком."""
    with patch("attribution.service.check_address_goplus", new=AsyncMock(side_effect=Exception("network error"))):
        result = await check_address("0xabc", chain_id=1)

    assert result["goplus_risky"] is False
    assert result["overall_risk"] == "low"


@pytest.mark.asyncio
async def test_evidence_log_written_with_provenance():
    """Проверяем, что каждый источник реально пишется в evidence_log с
    правильным source/subagent — это провенанс, требуемый разделом 6 архитектуры."""
    inv_id = uuid.uuid4()
    clean_goplus = {"is_risky": False, "risk_flags": [], "raw": {"code": 1}}

    with patch("attribution.service.check_address_goplus", new=AsyncMock(return_value=clean_goplus)):
        await check_address("0xabc", chain_id=1, investigation_id=inv_id)

    evidence = await get_evidence(inv_id)
    sources = {e["source"] for e in evidence}
    assert "ofac_sdn" in sources
    assert "goplus" in sources
    assert "opensanctions_bulk" in sources
    assert all(e["subagent"] == "attribution" for e in evidence)
