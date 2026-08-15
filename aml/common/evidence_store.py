# common/evidence_store.py
"""
Типизированные функции для работы с пятью таблицами из common/sql/schema.sql:
label_cache (внешние метки), seed_registry (свои находки), evidence_log (сырые
данные расследования), flow_edges (структурированные рёбра графа переводов,
Flow&Hop Tracer), verdicts (финальные вердикты).

Это единственное место в проекте, где должен встречаться сырой SQL к этим
таблицам — остальной код (субагенты, Оркестратор, Валидатор) работает только
через эти функции.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from common.db import get_pool


# --- label_cache: внешние метки (OFAC/OpenSanctions/GoPlus и т.д.) ---

async def upsert_label(
    address: str,
    source: str,
    label_type: str,
    chain_id: Optional[int] = None,
    confidence: float = 1.0,
    raw_data: Optional[Dict[str, Any]] = None,
) -> None:
    """Записывает/обновляет метку из внешнего источника. Провенанс (source,
    fetched_at) обновляется автоматически при каждом upsert."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO label_cache (address, chain_id, source, label_type, confidence, raw_data, fetched_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, now())
        ON CONFLICT (address, (COALESCE(chain_id, -1)), source, label_type)
        DO UPDATE SET confidence = EXCLUDED.confidence,
                      raw_data = EXCLUDED.raw_data,
                      fetched_at = now()
        """,
        address.lower(), chain_id, source, label_type, confidence,
        raw_data or {},
    )


async def get_labels(address: str, chain_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Возвращает все известные внешние метки по адресу (опционально — только
    для конкретной сети + сети-агностичные источники вроде OFAC)."""
    pool = await get_pool()
    if chain_id is not None:
        rows = await pool.fetch(
            "SELECT * FROM label_cache WHERE address = $1 AND (chain_id = $2 OR chain_id IS NULL)",
            address.lower(), chain_id,
        )
    else:
        rows = await pool.fetch("SELECT * FROM label_cache WHERE address = $1", address.lower())
    return [dict(r) for r in rows]


# --- seed_registry: свои находки (посев меток) ---

async def upsert_seed(
    address: str,
    chain_id: int,
    tag: str,
    seed_source: str,
    derived_from: Optional[str] = None,
    confidence: float = 1.0,
) -> None:
    """Добавляет/обновляет собственную метку в Seed Registry (раздел 5.1)."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO seed_registry (address, chain_id, tag, seed_source, derived_from, confidence, seeded_at)
        VALUES ($1, $2, $3, $4, $5, $6, now())
        ON CONFLICT (address, chain_id, tag)
        DO UPDATE SET confidence = EXCLUDED.confidence,
                      derived_from = EXCLUDED.derived_from
        """,
        address.lower(), chain_id, tag, seed_source,
        derived_from.lower() if derived_from else None, confidence,
    )


async def get_seeds(address: str, chain_id: int) -> List[Dict[str, Any]]:
    """Возвращает все собственные метки по адресу в конкретной сети."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM seed_registry WHERE address = $1 AND chain_id = $2",
        address.lower(), chain_id,
    )
    return [dict(r) for r in rows]


# --- evidence_log: сырые данные, собранные Execution Layer ---

async def write_evidence(
    investigation_id: uuid.UUID,
    mode: str,
    subagent: str,
    action: str,
    payload: Dict[str, Any],
    source: str,
    chain_id: Optional[int] = None,
    cached: bool = False,
) -> None:
    """Пишет один кусок сырых данных в evidence log с полным провенансом."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO evidence_log
            (investigation_id, mode, subagent, chain_id, action, payload, source, cached, fetched_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, now())
        """,
        investigation_id, mode, subagent, chain_id, action,
        payload, source, cached,
    )


async def get_evidence(investigation_id: uuid.UUID) -> List[Dict[str, Any]]:
    """Возвращает весь evidence log по конкретному расследованию, по порядку сбора."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM evidence_log WHERE investigation_id = $1 ORDER BY id ASC",
        investigation_id,
    )
    return [dict(r) for r in rows]


# --- flow_edges: структурированные рёбра графа переводов (Flow&Hop Tracer) ---

async def write_flow_edge(
    investigation_id: uuid.UUID,
    chain_id: int,
    hop_number: int,
    parent_address: str,
    child_address: str,
    tx_hash: str,
    edge_kind: str = "transfer",
    token: Optional[str] = None,
    amount: Optional[str] = None,
    tainted: bool = False,
    terminal_reason: Optional[str] = None,
) -> None:
    """Пишет одно ребро графа переводов, найденное Flow&Hop Tracer'ом. amount —
    строка (сырое значение в минимальной единице, напр. wei), не float: суммы
    легко превышают точность double уже на величинах порядка 1 ETH."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO flow_edges
            (investigation_id, chain_id, hop_number, parent_address, child_address,
             tx_hash, token, amount, edge_kind, tainted, terminal_reason, fetched_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
        """,
        investigation_id, chain_id, hop_number, parent_address.lower(), child_address.lower(),
        tx_hash.lower(), token.lower() if token else None, amount, edge_kind, tainted, terminal_reason,
    )


async def get_flow_edges(investigation_id: uuid.UUID) -> List[Dict[str, Any]]:
    """Возвращает все рёбра графа переводов по расследованию, по порядку сбора
    (соответствует порядку обхода hop_number)."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM flow_edges WHERE investigation_id = $1 ORDER BY hop_number ASC, id ASC",
        investigation_id,
    )
    return [dict(r) for r in rows]


# --- verdicts: финальный вердикт по расследованию ---

async def write_verdict(
    investigation_id: uuid.UUID,
    mode: str,
    risk_score: float,
    narrative: str,
    escalated: bool = False,
) -> None:
    """Записывает финальный вердикт (детерминированный скоринг + LLM-нарратив)."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO verdicts (investigation_id, mode, risk_score, narrative, escalated, decided_at)
        VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (investigation_id)
        DO UPDATE SET risk_score = EXCLUDED.risk_score,
                      narrative = EXCLUDED.narrative,
                      escalated = EXCLUDED.escalated,
                      decided_at = now()
        """,
        investigation_id, mode, risk_score, narrative, escalated,
    )


async def get_verdict(investigation_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM verdicts WHERE investigation_id = $1", investigation_id)
    return dict(row) if row else None
