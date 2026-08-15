# attribution/service.py
"""
Attribution & Labeling Engine (субагент 3, раздел 2 архитектуры) для EVM.

Собирает три источника воедино:
- OFAC SDN — локальный SELECT к label_cache (список обновляется отдельно,
  через attribution.ofac.refresh_ofac_sdn(), не на каждый запрос).
- GoPlus — живой запрос (свободный тир, агрегирует SlowMist/BlockSec/OFAC/Chainabuse).
- OpenSanctions — С ЭТОЙ ВЕРСИИ тоже локальный SELECT к label_cache (см.
  "Следующий шаг" в summary_for_new_dialog.md: переделано с живого /match на
  bulk-паттерн, тот же принцип, что и у OFAC — список обновляется отдельно
  через attribution.opensanctions.refresh_opensanctions_bulk()). Живой
  /match (attribution.opensanctions.check_address_opensanctions) сохранён в
  модуле как опциональный резерв для будущего fuzzy-поиска по имени
  контрагента, но здесь, в проверке адреса, больше не вызывается — иначе
  каждая проверка адреса стоила бы 0.10 EUR сверх бесплатной квоты.

Каждый сырой ответ пишется в evidence_log с провенансом (раздел 6 архитектуры:
"провенанс каждой метки — источник + дата запроса"). GoPlus дополнительно
пишется в label_cache через upsert_label, чтобы повторные проверки того же
адреса могли использовать это как часть общей картины меток (в отличие от
OFAC/OpenSanctions, здесь это не замена bulk-обновлению, а просто накопление
истории живых ответов).
"""

import logging
import uuid
from typing import Any, Dict, Optional

from common.evidence_store import get_labels, upsert_label, write_evidence
from attribution.goplus import check_address_goplus

logger = logging.getLogger(__name__)


async def check_address(
    address: str,
    chain_id: int,
    investigation_id: Optional[uuid.UUID] = None,
    mode: str = "aml_check",
) -> Dict[str, Any]:
    """
    Полная проверка адреса по всем доступным источникам Attribution Engine.

    Args:
        address: EVM-адрес.
        chain_id: ID сети.
        investigation_id: UUID расследования для evidence_log; если не задан,
            генерируется новый (для точечных проверок вне полного графа).
        mode: режим расследования (для provenance в evidence_log).

    Returns:
        {
            "address": ..., "chain_id": ...,
            "ofac_sanctioned": bool, "ofac_labels": [...],
            "goplus_risky": bool, "goplus_flags": [...],
            "opensanctions_checked": bool, "opensanctions_risky": bool, "opensanctions_matches": [...],
            "overall_risk": "high" | "medium" | "low",
        }
    """
    inv_id = investigation_id or uuid.uuid4()
    address = address.lower()

    # 1. OFAC — локальный кэш, не живой вызов
    ofac_labels = [l for l in await get_labels(address, chain_id=chain_id) if l["source"] == "ofac_sdn"]
    ofac_sanctioned = len(ofac_labels) > 0
    await write_evidence(
        investigation_id=inv_id, mode=mode, subagent="attribution",
        action="check_ofac_sdn_cached", payload={"sanctioned": ofac_sanctioned, "labels": ofac_labels},
        source="ofac_sdn", chain_id=chain_id, cached=True,
    )

    # 2. GoPlus — живой вызов
    goplus_risky = False
    goplus_flags: list = []
    try:
        goplus_result = await check_address_goplus(address, chain_id)
        goplus_risky = goplus_result["is_risky"]
        goplus_flags = goplus_result["risk_flags"]
        await write_evidence(
            investigation_id=inv_id, mode=mode, subagent="attribution",
            action="check_goplus", payload=goplus_result["raw"],
            source="goplus", chain_id=chain_id, cached=False,
        )
        if goplus_risky:
            await upsert_label(
                address=address, chain_id=chain_id, source="goplus",
                label_type=",".join(goplus_flags), confidence=0.8, raw_data=goplus_result["raw"],
            )
    except Exception as e:
        logger.error(f"GoPlus check failed for {address} на chain_id={chain_id}: {e}")
        await write_evidence(
            investigation_id=inv_id, mode=mode, subagent="attribution",
            action="check_goplus_failed", payload={"error": str(e)},
            source="goplus", chain_id=chain_id, cached=False,
        )

    # 3. OpenSanctions — локальный кэш (bulk, см. docstring модуля), не живой вызов
    opensanctions_labels = [l for l in await get_labels(address, chain_id=chain_id) if l["source"] == "opensanctions_bulk"]
    opensanctions_checked = True
    opensanctions_risky = len(opensanctions_labels) > 0
    opensanctions_matches = opensanctions_labels
    await write_evidence(
        investigation_id=inv_id, mode=mode, subagent="attribution",
        action="check_opensanctions_bulk_cached", payload={"risky": opensanctions_risky, "labels": opensanctions_labels},
        source="opensanctions_bulk", chain_id=chain_id, cached=True,
    )

    # Скоринг: детерминированный, без LLM (соответствует "детерминированный
    # скоринг" из раздела 3 архитектуры для итогового Вердикта — здесь это
    # локальный вклад одного субагента, а не финальный вердикт).
    if ofac_sanctioned or opensanctions_risky:
        overall_risk = "high"
    elif goplus_risky:
        overall_risk = "medium"
    else:
        overall_risk = "low"

    return {
        "address": address,
        "chain_id": chain_id,
        "ofac_sanctioned": ofac_sanctioned,
        "ofac_labels": ofac_labels,
        "goplus_risky": goplus_risky,
        "goplus_flags": goplus_flags,
        "opensanctions_checked": opensanctions_checked,
        "opensanctions_risky": opensanctions_risky,
        "opensanctions_matches": opensanctions_matches,
        "overall_risk": overall_risk,
    }
