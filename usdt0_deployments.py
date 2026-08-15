#!/usr/bin/env python3
"""
usdt0_deployments.py — живой, кэшируемый клиент к официальному USDT0
Deployments API: https://docs.usdt0.to/api/deployments — структурированный
JSON-эндпоинт (не статическая HTML-страница), отдающий актуальные адреса
OFT/адаптер-контрактов USDT0 (и других токенов на том же протоколе — XAUT0,
USAT0 и т.д.) для всех поддерживаемых сетей одним запросом.

ЗАЧЕМ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ
--------------------------
Раньше bridge_tracer.py держал единственный Tron-адрес USDT0 OFT
захардкоженным (найден один раз живым запросом в начале сессии). Пользователь
нашёл этот официальный API с теми же данными — переходим на живой запрос
вместо статического словаря, с диск-кэшем (адреса меняются редко, незачем
бить по API на каждый вызов trace_full_path).

СТРУКТУРА ОТВЕТА (проверено живым запросом, 2026-08-15)
-----------------------------------------------------------
{
  "usdt0": {
    "native": [
      {"name": "Ethereum", "chainId": 1, "lzEid": "30101", "isSource": true,
       "contracts": [{"name": "OFT Adapter", "address": "0x...", "explorer": "..."}, ...]},
      ...
    ],
    "legacyMesh": [
      {"name": "Tron", "lzEid": "30420",
       "contracts": [{"name": "OFT", "address": "TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt", "explorer": "..."}]},
      {"name": "Arbitrum", "chainId": 42161, "lzEid": "30110",
       "contracts": [{"name": "OFT", "address": "0x7765..."}, {"name": "Composer", "address": "0x759B..."}]},
      ...
    ]
  },
  "xaut0": {...}, "usat0": {...}  # другие токены семейства, здесь не используются
}

Tron числится ТОЛЬКО в "legacyMesh" (не в "native") — это Legacy Mesh
сеть: депозиты идут двухходово через Arbitrum как хаб (Hop 1: source ->
Arbitrum через liquidity-pool-конвертацию, Hop 2: Arbitrum -> реальная
целевая сеть через OFT mint/burn, ЕСЛИ они не совпадают) — см. подробности
и живое подтверждение механизма в докстринге bridge_tracer.py.
"""
import json
import os
import time
from typing import Any, Optional

import requests

DEPLOYMENTS_URL = "https://docs.usdt0.to/api/deployments"
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".usdt0_deployments_cache.json")
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 часа — "адреса меняются редко" (формулировка задачи)


def _load_cache() -> Optional[dict[str, Any]]:
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - cached.get("_fetched_at", 0) > CACHE_TTL_SECONDS:
            return None
        return cached.get("data")
    except (OSError, ValueError):
        return None


def _save_cache(data: dict[str, Any]) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"_fetched_at": time.time(), "data": data}, f)
    except OSError:
        pass  # диск-кэш — best-effort ускорение, не критично для корректности


def fetch_deployments(timeout: int = 15, force_refresh: bool = False) -> dict[str, Any]:
    """
    Возвращает сырой JSON официального USDT0 Deployments API целиком (все
    токены, все сети) — из диск-кэша, если он не старше CACHE_TTL_SECONDS,
    иначе живым запросом (и обновляет кэш). Сетевые и HTTP-ошибки НЕ
    проглатываются — пробрасываются вызывающему коду как есть (тот же
    принцип, что и у остальных Layer 1 адаптеров в этом проекте).
    """
    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            return cached
    resp = requests.get(DEPLOYMENTS_URL, timeout=timeout, headers={"accept": "application/json"})
    resp.raise_for_status()
    data = resp.json()
    _save_cache(data)
    return data


def get_tron_oft_contracts(token: str = "usdt0", **kwargs: Any) -> dict[str, str]:
    """
    Возвращает {base58-адрес: название контракта} для Tron из секции
    legacyMesh указанного токена (по умолчанию usdt0 — единственный, который
    использует bridge_tracer.py). Обычно один элемент ("OFT"), но структура
    ответа допускает несколько контрактов на сеть — учитываем все.

    kwargs пробрасываются в fetch_deployments (timeout, force_refresh).
    """
    data = fetch_deployments(**kwargs)
    result: dict[str, str] = {}
    for entry in data.get(token, {}).get("legacyMesh", []):
        if entry.get("name") != "Tron":
            continue
        for c in entry.get("contracts", []):
            addr = c.get("address")
            if addr:
                result[addr] = f"{c.get('name', 'OFT')} ({token.upper()})"
    return result
