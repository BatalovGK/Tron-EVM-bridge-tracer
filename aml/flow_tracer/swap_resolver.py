# flow_tracer/swap_resolver.py
"""
Определяет, была ли конкретная транзакция свопом через известный DEX, и если
да — достаёт token_in/amount_in/token_out/amount_out через TheGraph (раздел 4
архитектуры: "TheGraph... для разрешения свопов").

Зачем это вообще нужно для Flow&Hop Tracer: без резолва свопа трейс упирается
в контракт роутера/пула как в тупик — Blockscout покажет "перевод на адрес
роутера", но не покажет, в какой токен и кому в итоге ушла стоимость. Для
несвопового перевода (обычный transfer) резолвер вообще не вызывается — это
не бесплатно (расходует квоту TheGraph), поэтому вызывается только когда
контрагент транзакции — известный DEX-контракт из dex_contracts.yaml.

Ограничения v1 (согласовано явно, не молча):
- Только Uniswap v2/v3-совместимые схемы подграфов (protocol_key: "uniswap_v2",
  "uniswap_v3"). Другие протоколы (Curve, Balancer и т.д.) добавляются позже
  через новый QUERY_TEMPLATES-ключ + конфиг.
- Один hop — один своп. Мультихоп-свопы внутри одной tx (напр. USDC->WETH->DAI
  через несколько пулов в одном вызове) вернутся как несколько Swap-записей
  подряд — резолвер отдаёт их все, порядок сборки в цепочку — на hop_tracer.
- Если subgraph_id для сети+протокола не заполнен в конфиге (плейсхолдер
  "ЗАПОЛНИТЬ_ВРУЧНУЮ") — резолвер логирует предупреждение и возвращает None
  (не швырят исключение наверх, не останавливают весь трейс), считая своп
  нерезолвленным. Hop_tracer должен фиксировать такие случаи в evidence как
  "swap_unresolved", а не тихо пропускать.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from evm_adapter.adapter import get_transaction_summary
from flow_tracer.thegraph import TheGraphClient, TheGraphQueryError

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent / "config"
_PLACEHOLDER_PREFIX = "ЗАПОЛНИТЬ_ВРУЧНУЮ"


def _load_yaml(filename: str) -> Dict[int, Dict[str, str]]:
    path = CONFIG_DIR / filename
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {int(k): v for k, v in raw.items()}


try:
    _DEX_SUBGRAPHS = _load_yaml("dex_subgraphs.yaml")
    _DEX_CONTRACTS = _load_yaml("dex_contracts.yaml")
except Exception as e:  # конфиги опциональны на этапе первичной сборки/тестов
    logger.warning(f"Не удалось загрузить конфиги flow_tracer: {e}")
    _DEX_SUBGRAPHS = {}
    _DEX_CONTRACTS = {}


QUERY_TEMPLATES = {
    "uniswap_v3": """
        query($txHash: Bytes!) {
          swaps(where: { transaction: $txHash }) {
            id
            sender
            recipient
            origin
            amount0
            amount1
            amountUSD
            pool {
              id
              token0 { id symbol decimals }
              token1 { id symbol decimals }
            }
          }
        }
    """,
    "uniswap_v2": """
        query($txHash: Bytes!) {
          swaps(where: { transaction: $txHash }) {
            id
            sender
            to
            amount0In
            amount1In
            amount0Out
            amount1Out
            pair {
              id
              token0 { id symbol decimals }
              token1 { id symbol decimals }
            }
          }
        }
    """,
}


def identify_dex_protocol(chain_id: int, contract_address: str) -> Optional[str]:
    """Возвращает protocol_key, если contract_address — известный DEX-контракт
    на этой сети (см. dex_contracts.yaml), иначе None."""
    contracts = _DEX_CONTRACTS.get(chain_id, {})
    return contracts.get(contract_address.lower())


def _get_subgraph_id(chain_id: int, protocol_key: str) -> Optional[str]:
    subgraph_id = _DEX_SUBGRAPHS.get(chain_id, {}).get(protocol_key)
    if not subgraph_id or subgraph_id.startswith(_PLACEHOLDER_PREFIX):
        logger.warning(
            f"subgraph_id для chain_id={chain_id}, protocol={protocol_key} не заполнен "
            f"в dex_subgraphs.yaml — своп не будет резолвлен через TheGraph."
        )
        return None
    return subgraph_id


def _normalize_v3_swap(raw: Dict[str, Any]) -> Dict[str, Any]:
    amount0 = float(raw["amount0"])
    amount1 = float(raw["amount1"])
    token0 = raw["pool"]["token0"]
    token1 = raw["pool"]["token1"]
    if amount0 > 0:
        token_in, amount_in = token0, amount0
        token_out, amount_out = token1, -amount1
    else:
        token_in, amount_in = token1, amount1
        token_out, amount_out = token0, -amount0
    return {
        "protocol": "uniswap_v3",
        "pool": raw["pool"]["id"],
        "sender": raw["sender"],
        "recipient": raw["recipient"],
        "token_in": token_in,
        "amount_in": amount_in,
        "token_out": token_out,
        "amount_out": amount_out,
        "amount_usd": raw.get("amountUSD"),
    }


def _normalize_v2_swap(raw: Dict[str, Any]) -> Dict[str, Any]:
    amount0_in = float(raw["amount0In"])
    amount1_in = float(raw["amount1In"])
    amount0_out = float(raw["amount0Out"])
    amount1_out = float(raw["amount1Out"])
    token0 = raw["pair"]["token0"]
    token1 = raw["pair"]["token1"]
    if amount0_in > 0:
        token_in, amount_in = token0, amount0_in
        token_out, amount_out = token1, amount1_out
    else:
        token_in, amount_in = token1, amount1_in
        token_out, amount_out = token0, amount0_out
    return {
        "protocol": "uniswap_v2",
        "pool": raw["pair"]["id"],
        "sender": raw["sender"],
        "recipient": raw["to"],
        "token_in": token_in,
        "amount_in": amount_in,
        "token_out": token_out,
        "amount_out": amount_out,
        "amount_usd": None,
    }


_NORMALIZERS = {
    "uniswap_v3": _normalize_v3_swap,
    "uniswap_v2": _normalize_v2_swap,
}


async def resolve_swap(
    chain_id: int,
    tx_hash: str,
    client: Optional[TheGraphClient] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    Если tx_hash — своп через известный DEX-контракт, возвращает список
    резолвленных свопов (обычно один, может быть несколько при мультихопе
    внутри одной tx). Если контрагент не распознан как DEX или subgraph_id не
    заполнен — возвращает None (не ошибка, см. докстринг модуля).
    """
    summary = await get_transaction_summary(chain_id=chain_id, tx_hash=tx_hash)
    to_address = summary.get("to", {}).get("hash") if isinstance(summary.get("to"), dict) else summary.get("to")
    if not to_address:
        return None

    protocol_key = identify_dex_protocol(chain_id, to_address)
    if protocol_key is None:
        return None  # обычный перевод, не своп через известный DEX

    subgraph_id = _get_subgraph_id(chain_id, protocol_key)
    if subgraph_id is None:
        return None  # известный DEX, но нечем резолвить — см. предупреждение в логе

    query = QUERY_TEMPLATES.get(protocol_key)
    normalizer = _NORMALIZERS.get(protocol_key)
    if query is None or normalizer is None:
        logger.warning(f"Нет GraphQL-шаблона для protocol_key={protocol_key}")
        return None

    cl = client or TheGraphClient()
    try:
        data = await cl.query(subgraph_id, query, variables={"txHash": tx_hash.lower()})
    except TheGraphQueryError as e:
        logger.error(f"Ошибка резолва свопа {tx_hash} на chain_id={chain_id}: {e}")
        return None

    raw_swaps = data.get("swaps", [])
    if not raw_swaps:
        return None

    return [normalizer(s) for s in raw_swaps]
