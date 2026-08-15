# scripts/manual_check_flow_tracer.py
"""
Ручная проверка Flow & Hop Tracer против РЕАЛЬНЫХ Blockscout Pro API и
TheGraph gateway — сеть до них недоступна из песочницы, где писался этот код,
поэтому это нужно прогнать самостоятельно, как и с attribution/ модулями.

В отличие от attribution/ (которая полностью проверена на реальных серверах в
предыдущей сессии), здесь предположения о структуре ответов Blockscout
("from"/"to" как {"hash": "0x..."}, значение в "value") — ВЗЯТЫ ИЗ
ДОКУМЕНТАЦИИ, не проверены живьём. Это первое, что должен показать этот скрипт.

Использование:
    export BLOCKSCOUT_API_KEY="proapi_..."
    export THEGRAPH_API_KEY="..."        # из Subgraph Studio, нужен для шага 3
    PYTHONPATH=. python scripts/manual_check_flow_tracer.py \\
        --address 0xАДРЕС_С_ИЗВЕСТНЫМИ_ИСХОДЯЩИМИ_ПЕРЕВОДАМИ \\
        --chain-id 1 \\
        --swap-tx 0xХЭШ_ИЗВЕСТНОГО_UNISWAP_СВОПА  # опционально, для шага 3

Что делает:
1. Печатает сырой ответ get_address_transactions() для --address — сверьте
   глазами, что "from"/"to" реально приходят как {"hash": "0x..."}, а не как
   голая строка или другое поле (напр. "from_address_hash") — если структура
   отличается, _extract_address() в hop_tracer.py нужно поправить ДО того,
   как доверять результатам трейса.
2. Прогоняет trace_flow(..., max_hops=1) на --address и печатает результат —
   проверьте вручную по блок-эксплореру, что найденные хопы соответствуют
   реальным исходящим переводам.
3. Если передан --swap-tx (реальный tx_hash свопа через Uniswap v3 на указанной
   сети, для которой subgraph_id уже заполнен в dex_subgraphs.yaml, НЕ
   плейсхолдер) — резолвит его через resolve_swap() и печатает token_in/out.
   Без этого шага TheGraph-часть трейсера остаётся ПОЛНОСТЬЮ непроверенной.
"""

import argparse
import asyncio
import json
import logging

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from evm_adapter.adapter import get_address_transactions, close_client
from flow_tracer import trace_flow, resolve_swap

logging.basicConfig(level=logging.INFO)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--chain-id", type=int, default=1)
    parser.add_argument("--swap-tx", default=None)
    args = parser.parse_args()

    print("\n=== Шаг 1: сырой ответ get_address_transactions ===")
    raw = await get_address_transactions(chain_id=args.chain_id, address=args.address, limit=3)
    print(json.dumps(raw, indent=2, ensure_ascii=False)[:3000])
    print(
        "\nПРОВЕРЬТЕ ГЛАЗАМИ: поля 'from'/'to' в items — это {'hash': '0x...'}? "
        "Если нет, поправьте _extract_address() в flow_tracer/hop_tracer.py."
    )

    print("\n=== Шаг 2: trace_flow(max_hops=1) на реальном адресе ===")
    result = await trace_flow(
        args.address, chain_id=args.chain_id, mode="incident_response",
        max_hops=1, write_to_seed_registry=False,  # не засоряем seed_registry ручной проверкой
    )
    print(f"Посещено адресов: {result.visited_addresses}")
    print(f"Записано рёбер: {result.edges_written}")
    print(f"Терминальные узлы: {result.terminal_nodes}")
    print("СВЕРЬТЕ с блок-эксплорером (Blockscout/Etherscan) вручную, что это реальные исходящие переводы.")

    if args.swap_tx:
        print(f"\n=== Шаг 3: резолв свопа {args.swap_tx} через TheGraph ===")
        swaps = await resolve_swap(chain_id=args.chain_id, tx_hash=args.swap_tx)
        if swaps is None:
            print(
                "None — либо это не известный DEX-контракт (см. dex_contracts.yaml), "
                "либо subgraph_id для этой сети/протокола не заполнен (см. dex_subgraphs.yaml)."
            )
        else:
            print(json.dumps(swaps, indent=2, ensure_ascii=False))
            print("СВЕРЬТЕ token_in/token_out/amount с реальным свопом (напр. по Etherscan decoded input).")
    else:
        print(
            "\n=== Шаг 3 пропущен (--swap-tx не передан) ===\n"
            "TheGraph-часть трейсера (swap_resolver.py) остаётся НЕ проверенной на "
            "реальном API — прогоните с реальным tx_hash известного Uniswap-свопа "
            "на сети, где subgraph_id уже заполнен, прежде чем доверять этой части."
        )

    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
