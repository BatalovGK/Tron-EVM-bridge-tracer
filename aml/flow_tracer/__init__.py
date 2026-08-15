# flow_tracer/__init__.py
"""Flow & Hop Tracer (субагент 2, раздел 2 архитектуры) — EVM: обход исходящих
переводов от адреса + резолв свопов через известные DEX (TheGraph)."""

from flow_tracer.hop_tracer import trace_flow, TraceResult, HopNode
from flow_tracer.swap_resolver import resolve_swap, identify_dex_protocol
from flow_tracer.thegraph import TheGraphClient, TheGraphQueryError

__all__ = [
    "trace_flow",
    "TraceResult",
    "HopNode",
    "resolve_swap",
    "identify_dex_protocol",
    "TheGraphClient",
    "TheGraphQueryError",
]
