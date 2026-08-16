#!/usr/bin/env python3
"""
layerzero_tracer.py — MVP-трейсер кросс-чейн переводов через протокол LayerZero (V2).

ПОЧЕМУ LAYERZERO, А НЕ WORMHOLE
--------------------------------
Изначально MVP делался на Wormhole, но выяснилось (при повторной проверке
официальной документации Wormhole), что chain ID Tron в реестре протокола
Wormhole относится к продукту Queries (подтягивание on-chain данных для
дексов/цен), а НЕ к Token Bridge (Wrapped Token Transfers) — то есть реальных
переводов активов TRON <-> EVM через Wormhole сейчас нет. Актуальная таблица
поддерживаемых сетей Token Bridge (wormhole.com/docs, Wrapped Token Transfers
-> Supported Networks) Tron не содержит.
LayerZero, в отличие от этого, поддерживает Tron на уровне протокола с 2024
года (официальный анонс TRON DAO + LayerZero, есть и mainnet, и testnet
Endpoint), и именно через LayerZero работает USDT0 — основной канал
TRON <-> Ethereum для USDT в 2026 году. Поэтому MVP переделан на LayerZero.

ЧТО ДЕЛАЕТ
----------
По хэшу исходной транзакции находит связанное сообщение LayerZero (V2) через
публичный LayerZero Scan API и показывает точку входа (source) и точку
выхода (destination), включая статус доставки. Связь source -> destination
подтверждена криптографически через GUID и кворум DVN (Decentralized
Verifier Network), а не эвристикой — поэтому confidence = HIGH.

ПРИМЕР (перевод TRON -> Ethereum через LayerZero/USDT0):
    python3 layerzero_tracer.py --tx <хэш транзакции на TRON>

ИСТОЧНИК ДАННЫХ
----------------
Публичный LayerZero Scan API (без API-ключа):
  GET https://scan.layerzero-api.com/v1/messages/tx/{txHash}         (mainnet)
  GET https://scan-testnet.layerzero-api.com/v1/messages/tx/{txHash} (testnet)
Официальная документация: https://docs.layerzero.network/v2/tools/layerzeroscan/api

ОГРАНИЧЕНИЯ ЭТОГО MVP
-----------------------
- Покрывает только сообщения на протоколе LayerZero V2 (в т.ч. OFT-переводы,
  на которых построен USDT0). Wormhole, liquidity-pool мосты (Symbiosis,
  Allbridge, Meson) и intent-based мосты (ERC-7683) не покрыты — расписаны
  отдельно в архитектурном документе, но не реализованы в этом MVP.
- find_bridge_crossing() сама по себе сопоставляет один хоп за вызов
  (source -> destination) и не рекурсирует. Многоходовые цепочки (Legacy
  Mesh) собираются ВЫЗЫВАЮЩИМ кодом через повторный вызов на compose_tx_hash
  — см. docstring самой функции и bridge_tracer.trace_full_path().
- Endpoint ID (eid) — это внутренняя нумерация LayerZero, НЕ равна ни chain ID
  сети (EIP-155 для EVM), ни chain ID других протоколов вроде Wormhole.
  Реестр LAYERZERO_EID_INFO ниже — полный список mainnet V2 сетей LayerZero
  (снят напрямую с metadata.layerzero-api.com, см. комментарий у словаря),
  включая non-EVM сети (Solana, Sui, Aptos, TON и т.д.) — они размечены
  отдельно (chain_type != "evm"), для них evm_adapter принципиально не
  подходит, пост-bridge трейсинг для них не реализован (см. LIMITATIONS).

ЗАВИСИМОСТИ
------------
Только `requests` (pip install requests --break-system-packages, если нет).

КАК ПРОВЕРИТЬ РЕЗУЛЬТАТ САМОМУ (без чтения кода)
----------------------------------------------------
1. Взять реальный tx hash сообщения LayerZero (например, любой USDT0-перевод
   TRON -> Ethereum, или любую транзакцию со страницы layerzeroscan.com).
2. Запустить: python3 layerzero_tracer.py --tx <хэш>
3. Скрипт должен напечатать BRIDGE ENTRY / BRIDGE EXIT с сетями (по имени,
   не только eid), GUID и статус (DELIVERED / INFLIGHT / FAILED и т.д.).
4. Сверить вручную на https://layerzeroscan.com/tx/<хэш> — это тот же API,
   на котором работает сам официальный LayerZero Scan.
5. Проверить обработку ошибок: --tx 0xdeadbeef (несуществующий хэш) должен
   дать понятное сообщение "не найдено", а не трейсбек.
"""

import argparse
import sys
from typing import Any, Optional

import requests

# Без этого на Windows вывод кириллицы ломается в консолях с кодовой страницей
# по умолчанию (cp866/cp1251, не UTF-8) — проверено живым запуском в этой
# сессии. errors="replace", а не "strict", чтобы редкий несовместимый символ
# не уронил вывод результата целиком.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LAYERZERO_MAINNET = "https://scan.layerzero-api.com/v1"
LAYERZERO_TESTNET = "https://scan-testnet.layerzero-api.com/v1"

# Bridge Contract Registry — ПОЛНЫЙ список LayerZero V2 Endpoint ID (mainnet,
# version=2) -> метаданные сети. eid НЕ равен ни chain ID сети (EIP-155), ни
# chain ID других протоколов (например, Wormhole использует для Ethereum
# id=2, а LayerZero — 30101, это разные независимые нумерации, см. таблицу
# в брифинге). "evm_chain_id" — это отдельная, явная translation table
# eid -> EIP-155 chain_id, ТОЛЬКО для сетей с chain_type="evm"; для остальных
# (Tron, Solana, Sui, Aptos, TON, ...) она всегда None — это намеренно, а не
# пропуск: post-bridge walker должен по этому полю понимать, что evm_adapter
# для такой сети не подходит, и корректно останавливаться, а не падать.
#
# Источник данных: https://metadata.layerzero-api.com/v1/metadata/deployments
# (тот же бэкенд, на котором построена официальная страница
# docs.layerzero.network/v2/deployments/deployed-contracts). Снято 2026-08-15
# прямым запросом к API — НЕ по памяти (см. историю ошибки с Wormhole/Tron в
# докстринге модуля). Список сетей LayerZero пополняется, "status" отражает
# состояние на момент снятия (ACTIVE/DEPRECATED/PRIVATE) — при использовании
# в проде имеет смысл периодически освежать этот словарь тем же запросом.
LAYERZERO_EID_INFO: dict[int, dict[str, Any]] = {
    30101: {"name": "Ethereum", "chain_type": "evm", "evm_chain_id": 1, "status": "ACTIVE"},
    30102: {"name": "BNB Chain", "chain_type": "evm", "evm_chain_id": 56, "status": "ACTIVE"},
    30106: {"name": "Avalanche", "chain_type": "evm", "evm_chain_id": 43114, "status": "ACTIVE"},
    30108: {"name": "Aptos", "chain_type": "aptos", "evm_chain_id": None, "status": "ACTIVE"},
    30109: {"name": "Polygon", "chain_type": "evm", "evm_chain_id": 137, "status": "ACTIVE"},
    30110: {"name": "Arbitrum", "chain_type": "evm", "evm_chain_id": 42161, "status": "ACTIVE"},
    30111: {"name": "Optimism", "chain_type": "evm", "evm_chain_id": 10, "status": "ACTIVE"},
    30112: {"name": "Fantom", "chain_type": "evm", "evm_chain_id": 250, "status": "ACTIVE"},
    30114: {"name": "Swimmer", "chain_type": "evm", "evm_chain_id": None, "status": "DEPRECATED"},
    30115: {"name": "DFK", "chain_type": "evm", "evm_chain_id": 53935, "status": "ACTIVE"},
    30116: {"name": "Harmony", "chain_type": "evm", "evm_chain_id": 1666600000, "status": "ACTIVE"},
    30118: {"name": "Dexalot Subnet", "chain_type": "evm", "evm_chain_id": 432204, "status": "ACTIVE"},
    30125: {"name": "Celo Mainnet", "chain_type": "evm", "evm_chain_id": 42220, "status": "ACTIVE"},
    30126: {"name": "Moonbeam", "chain_type": "evm", "evm_chain_id": 1284, "status": "DEPRECATED"},
    30138: {"name": "Fuse Mainnet", "chain_type": "evm", "evm_chain_id": 122, "status": "ACTIVE"},
    30145: {"name": "Gnosis", "chain_type": "evm", "evm_chain_id": 100, "status": "ACTIVE"},
    30148: {"name": "Shrapnel Subnet", "chain_type": "evm", "evm_chain_id": 2044, "status": "ACTIVE"},
    30149: {"name": "DOS Chain", "chain_type": "evm", "evm_chain_id": 7979, "status": "DEPRECATED"},
    30150: {"name": "Kaia Mainnet Cypress", "chain_type": "evm", "evm_chain_id": 8217, "status": "ACTIVE"},
    30151: {"name": "Metis", "chain_type": "evm", "evm_chain_id": 1088, "status": "ACTIVE"},
    30152: {"name": "intain", "chain_type": "evm", "evm_chain_id": None, "status": "DEPRECATED"},
    30153: {"name": "Core Blockchain Mainnet", "chain_type": "evm", "evm_chain_id": 1116, "status": "ACTIVE"},
    30155: {"name": "OKXChain Mainnet", "chain_type": "evm", "evm_chain_id": 66, "status": "ACTIVE"},
    30158: {"name": "Polygon zkEVM", "chain_type": "evm", "evm_chain_id": 1101, "status": "DEPRECATED"},
    30159: {"name": "Canto", "chain_type": "evm", "evm_chain_id": 7700, "status": "DEPRECATED"},
    30161: {"name": "Sepolia", "chain_type": "evm", "evm_chain_id": 11155111, "status": "ACTIVE"},
    30165: {"name": "zkSync Era Mainnet", "chain_type": "evm", "evm_chain_id": 324, "status": "ACTIVE"},
    30167: {"name": "Moonriver", "chain_type": "evm", "evm_chain_id": 1285, "status": "DEPRECATED"},
    30168: {"name": "Solana", "chain_type": "solana", "evm_chain_id": None, "status": "ACTIVE"},
    30173: {"name": "Tenet", "chain_type": "evm", "evm_chain_id": 1559, "status": "DEPRECATED"},
    30175: {"name": "Arbitrum Nova", "chain_type": "evm", "evm_chain_id": 42170, "status": "ACTIVE"},
    30176: {"name": "Meter Mainnet", "chain_type": "evm", "evm_chain_id": 82, "status": "ACTIVE"},
    30177: {"name": "Kava", "chain_type": "evm", "evm_chain_id": 2222, "status": "ACTIVE"},
    30181: {"name": "Mantle", "chain_type": "evm", "evm_chain_id": 5000, "status": "ACTIVE"},
    30182: {"name": "hubble", "chain_type": "evm", "evm_chain_id": 1992, "status": "DEPRECATED"},
    30183: {"name": "Linea", "chain_type": "evm", "evm_chain_id": 59144, "status": "ACTIVE"},
    30184: {"name": "Base", "chain_type": "evm", "evm_chain_id": 8453, "status": "ACTIVE"},
    30195: {"name": "Zora", "chain_type": "evm", "evm_chain_id": 7777777, "status": "ACTIVE"},
    30196: {"name": "Viction", "chain_type": "evm", "evm_chain_id": 88, "status": "ACTIVE"},
    30197: {"name": "loot", "chain_type": "evm", "evm_chain_id": 5151706, "status": "DEPRECATED"},
    30198: {"name": "Merit Circle", "chain_type": "evm", "evm_chain_id": 4337, "status": "ACTIVE"},
    30199: {"name": "TelosEVM", "chain_type": "evm", "evm_chain_id": 40, "status": "ACTIVE"},
    30202: {"name": "opBNB Mainnet", "chain_type": "evm", "evm_chain_id": 204, "status": "ACTIVE"},
    30210: {"name": "Astar", "chain_type": "evm", "evm_chain_id": 592, "status": "ACTIVE"},
    30211: {"name": "Aurora Mainnet", "chain_type": "evm", "evm_chain_id": 1313161554, "status": "ACTIVE"},
    30212: {"name": "Conflux eSpace", "chain_type": "evm", "evm_chain_id": 1030, "status": "ACTIVE"},
    30213: {"name": "Orderly Mainnet", "chain_type": "evm", "evm_chain_id": 291, "status": "ACTIVE"},
    30214: {"name": "Scroll", "chain_type": "evm", "evm_chain_id": 534352, "status": "ACTIVE"},
    30215: {"name": "Horizen EON Mainnet", "chain_type": "evm", "evm_chain_id": 7332, "status": "DEPRECATED"},
    30216: {"name": "XPLA Mainnet", "chain_type": "evm", "evm_chain_id": 37, "status": "ACTIVE"},
    30217: {"name": "Manta", "chain_type": "evm", "evm_chain_id": 169, "status": "ACTIVE"},
    30218: {"name": "PGN (Public Goods Network)", "chain_type": "evm", "evm_chain_id": 424, "status": "DEPRECATED"},
    30230: {"name": "shimmer", "chain_type": "evm", "evm_chain_id": 148, "status": "ACTIVE"},
    30234: {"name": "Injective", "chain_type": "evm", "evm_chain_id": 2525, "status": "DEPRECATED"},
    30235: {"name": "Rari Chain", "chain_type": "evm", "evm_chain_id": 1380012617, "status": "DEPRECATED"},
    30236: {"name": "xai", "chain_type": "evm", "evm_chain_id": 660279, "status": "ACTIVE"},
    30237: {"name": "real", "chain_type": "evm", "evm_chain_id": 111188, "status": "DEPRECATED"},
    30238: {"name": "tiltyard", "chain_type": "evm", "evm_chain_id": 710420, "status": "DEPRECATED"},
    30243: {"name": "Blast", "chain_type": "evm", "evm_chain_id": 81457, "status": "ACTIVE"},
    30255: {"name": "Fraxtal", "chain_type": "evm", "evm_chain_id": 252, "status": "ACTIVE"},
    30257: {"name": "Astar zkEVM", "chain_type": "evm", "evm_chain_id": 3776, "status": "DEPRECATED"},
    30260: {"name": "Mode", "chain_type": "evm", "evm_chain_id": 34443, "status": "ACTIVE"},
    30263: {"name": "masa", "chain_type": "evm", "evm_chain_id": 13396, "status": "DEPRECATED"},
    30265: {"name": "homeverse", "chain_type": "evm", "evm_chain_id": 19011, "status": "DEPRECATED"},
    30266: {"name": "merlin", "chain_type": "evm", "evm_chain_id": 4200, "status": "ACTIVE"},
    30267: {"name": "degen", "chain_type": "evm", "evm_chain_id": 666666666, "status": "ACTIVE"},
    30273: {"name": "skale", "chain_type": "evm", "evm_chain_id": 2046399126, "status": "ACTIVE"},
    30274: {"name": "xlayer", "chain_type": "evm", "evm_chain_id": 196, "status": "ACTIVE"},
    30278: {"name": "sanko", "chain_type": "evm", "evm_chain_id": 1996, "status": "DEPRECATED"},
    30279: {"name": "bob", "chain_type": "evm", "evm_chain_id": 60808, "status": "ACTIVE"},
    30280: {"name": "sei", "chain_type": "evm", "evm_chain_id": 1329, "status": "ACTIVE"},
    30282: {"name": "EBI", "chain_type": "evm", "evm_chain_id": 98881, "status": "DEPRECATED"},
    30283: {"name": "cyber", "chain_type": "evm", "evm_chain_id": 7560, "status": "ACTIVE"},
    30284: {"name": "IOTA EVM", "chain_type": "evm", "evm_chain_id": 8822, "status": "ACTIVE"},
    30285: {"name": "joc", "chain_type": "evm", "evm_chain_id": 81, "status": "ACTIVE"},
    30290: {"name": "taiko", "chain_type": "evm", "evm_chain_id": 167000, "status": "ACTIVE"},
    30291: {"name": "xchain", "chain_type": "evm", "evm_chain_id": 94524, "status": "DEPRECATED"},
    30292: {"name": "etherlink", "chain_type": "evm", "evm_chain_id": 42793, "status": "ACTIVE"},
    30293: {"name": "bouncebit", "chain_type": "evm", "evm_chain_id": 6001, "status": "ACTIVE"},
    30294: {"name": "gravity", "chain_type": "evm", "evm_chain_id": 1625, "status": "ACTIVE"},
    30295: {"name": "flare", "chain_type": "evm", "evm_chain_id": 14, "status": "ACTIVE"},
    30301: {"name": "zklink", "chain_type": "evm", "evm_chain_id": 810180, "status": "DEPRECATED"},
    30302: {"name": "peaq", "chain_type": "evm", "evm_chain_id": 3338, "status": "ACTIVE"},
    30303: {"name": "zircuit", "chain_type": "evm", "evm_chain_id": 48900, "status": "ACTIVE"},
    30309: {"name": "lightlink", "chain_type": "evm", "evm_chain_id": 1890, "status": "ACTIVE"},
    30311: {"name": "lyra", "chain_type": "evm", "evm_chain_id": 957, "status": "ACTIVE"},
    30312: {"name": "ape", "chain_type": "evm", "evm_chain_id": 33139, "status": "ACTIVE"},
    30313: {"name": "reya", "chain_type": "evm", "evm_chain_id": 1729, "status": "ACTIVE"},
    30314: {"name": "bitlayer", "chain_type": "evm", "evm_chain_id": 200901, "status": "ACTIVE"},
    30315: {"name": "dm2verse", "chain_type": "evm", "evm_chain_id": 68770, "status": "PRIVATE"},
    30316: {"name": "hedera", "chain_type": "evm", "evm_chain_id": 295, "status": "ACTIVE"},
    30317: {"name": "bevm", "chain_type": "evm", "evm_chain_id": 11501, "status": "DEPRECATED"},
    30318: {"name": "plume", "chain_type": "evm", "evm_chain_id": 98865, "status": "DEPRECATED"},
    30319: {"name": "worldchain", "chain_type": "evm", "evm_chain_id": 480, "status": "ACTIVE"},
    30320: {"name": "unichain", "chain_type": "evm", "evm_chain_id": 130, "status": "ACTIVE"},
    30321: {"name": "lisk", "chain_type": "evm", "evm_chain_id": 1135, "status": "ACTIVE"},
    30322: {"name": "morph", "chain_type": "evm", "evm_chain_id": 2818, "status": "ACTIVE"},
    30323: {"name": "codex", "chain_type": "evm", "evm_chain_id": 81224, "status": "ACTIVE"},
    30324: {"name": "abstract", "chain_type": "evm", "evm_chain_id": 2741, "status": "ACTIVE"},
    30325: {"name": "movement", "chain_type": "aptos", "evm_chain_id": None, "status": "ACTIVE"},
    30326: {"name": "initia", "chain_type": "initia", "evm_chain_id": None, "status": "ACTIVE"},
    30327: {"name": "superposition", "chain_type": "evm", "evm_chain_id": 55244, "status": "ACTIVE"},
    30328: {"name": "edu", "chain_type": "evm", "evm_chain_id": 41923, "status": "ACTIVE"},
    30329: {"name": "hemi", "chain_type": "evm", "evm_chain_id": 43111, "status": "ACTIVE"},
    30330: {"name": "islander", "chain_type": "evm", "evm_chain_id": 1480, "status": "ACTIVE"},
    30331: {"name": "Corn", "chain_type": "evm", "evm_chain_id": 21000000, "status": "DEPRECATED"},
    30332: {"name": "sonic", "chain_type": "evm", "evm_chain_id": 146, "status": "ACTIVE"},
    30333: {"name": "rootstock", "chain_type": "evm", "evm_chain_id": 30, "status": "ACTIVE"},
    30334: {"name": "sophon", "chain_type": "evm", "evm_chain_id": 50104, "status": "ACTIVE"},
    30335: {"name": "swell", "chain_type": "evm", "evm_chain_id": 1923, "status": "DEPRECATED"},
    30336: {"name": "flow", "chain_type": "evm", "evm_chain_id": 747, "status": "ACTIVE"},
    30337: {"name": "bl4", "chain_type": "evm", "evm_chain_id": None, "status": "PRIVATE"},
    30338: {"name": "bl5", "chain_type": "evm", "evm_chain_id": None, "status": "PRIVATE"},
    30339: {"name": "ink", "chain_type": "evm", "evm_chain_id": 57073, "status": "ACTIVE"},
    30340: {"name": "soneium", "chain_type": "evm", "evm_chain_id": 1868, "status": "ACTIVE"},
    30341: {"name": "space", "chain_type": "evm", "evm_chain_id": 8227, "status": "ACTIVE"},
    30342: {"name": "glue", "chain_type": "evm", "evm_chain_id": 1300, "status": "ACTIVE"},
    30343: {"name": "TON", "chain_type": "ton", "evm_chain_id": None, "status": "ACTIVE"},
    30359: {"name": "cronosevm", "chain_type": "evm", "evm_chain_id": 25, "status": "ACTIVE"},
    30360: {"name": "cronoszkevm", "chain_type": "evm", "evm_chain_id": 388, "status": "ACTIVE"},
    30361: {"name": "goat", "chain_type": "evm", "evm_chain_id": 2345, "status": "ACTIVE"},
    30362: {"name": "bera", "chain_type": "evm", "evm_chain_id": 80094, "status": "ACTIVE"},
    30363: {"name": "bahamut", "chain_type": "evm", "evm_chain_id": 5165, "status": "DEPRECATED"},
    30364: {"name": "Data", "chain_type": "evm", "evm_chain_id": 1514, "status": "ACTIVE"},
    30365: {"name": "xdc", "chain_type": "evm", "evm_chain_id": 50, "status": "ACTIVE"},
    30366: {"name": "concrete", "chain_type": "evm", "evm_chain_id": 12739, "status": "DEPRECATED"},
    30367: {"name": "hyperliquid", "chain_type": "evm", "evm_chain_id": 999, "status": "ACTIVE"},
    30369: {"name": "nibiru", "chain_type": "evm", "evm_chain_id": 6900, "status": "ACTIVE"},
    30370: {"name": "plumephoenix", "chain_type": "evm", "evm_chain_id": 98866, "status": "ACTIVE"},
    30371: {"name": "gunz", "chain_type": "evm", "evm_chain_id": 43419, "status": "ACTIVE"},
    30372: {"name": "animechain", "chain_type": "evm", "evm_chain_id": 69000, "status": "ACTIVE"},
    30373: {"name": "lens", "chain_type": "evm", "evm_chain_id": 232, "status": "ACTIVE"},
    30374: {"name": "subtensorevm", "chain_type": "evm", "evm_chain_id": 964, "status": "ACTIVE"},
    30375: {"name": "katana", "chain_type": "evm", "evm_chain_id": 747474, "status": "ACTIVE"},
    30376: {"name": "botanix", "chain_type": "evm", "evm_chain_id": 3637, "status": "DEPRECATED"},
    30377: {"name": "tac", "chain_type": "evm", "evm_chain_id": 239, "status": "ACTIVE"},
    30378: {"name": "Sui", "chain_type": "sui", "evm_chain_id": None, "status": "ACTIVE"},
    30379: {"name": "silicon", "chain_type": "evm", "evm_chain_id": 2355, "status": "ACTIVE"},
    30380: {"name": "somnia", "chain_type": "evm", "evm_chain_id": 5031, "status": "ACTIVE"},
    30381: {"name": "camp", "chain_type": "evm", "evm_chain_id": 484, "status": "ACTIVE"},
    30382: {"name": "humanity", "chain_type": "evm", "evm_chain_id": 6985385, "status": "ACTIVE"},
    30383: {"name": "plasma", "chain_type": "evm", "evm_chain_id": 9745, "status": "ACTIVE"},
    30384: {"name": "apexfusionnexus", "chain_type": "evm", "evm_chain_id": 9069, "status": "ACTIVE"},
    30385: {"name": "dinari", "chain_type": "evm", "evm_chain_id": 202110, "status": "ACTIVE"},
    30386: {"name": "zkVerify", "chain_type": "evm", "evm_chain_id": 1408, "status": "ACTIVE"},
    30388: {"name": "0G", "chain_type": "evm", "evm_chain_id": 16661, "status": "ACTIVE"},
    30389: {"name": "gatelayer", "chain_type": "evm", "evm_chain_id": 10088, "status": "ACTIVE"},
    30390: {"name": "monad", "chain_type": "evm", "evm_chain_id": 143, "status": "ACTIVE"},
    30391: {"name": "ethereal", "chain_type": "evm", "evm_chain_id": 5064014, "status": "ACTIVE"},
    30392: {"name": "openledger", "chain_type": "evm", "evm_chain_id": 1612, "status": "ACTIVE"},
    30393: {"name": "doma", "chain_type": "evm", "evm_chain_id": 97477, "status": "ACTIVE"},
    30394: {"name": "injectiveevm", "chain_type": "evm", "evm_chain_id": 1776, "status": "ACTIVE"},
    30395: {"name": "nexera", "chain_type": "evm", "evm_chain_id": 7208, "status": "DEPRECATED"},
    30396: {"name": "stable", "chain_type": "evm", "evm_chain_id": 988, "status": "ACTIVE"},
    30397: {"name": "zama", "chain_type": "evm", "evm_chain_id": 261131, "status": "ACTIVE"},
    30398: {"name": "megaeth", "chain_type": "evm", "evm_chain_id": 4326, "status": "ACTIVE"},
    30399: {"name": "horizen", "chain_type": "evm", "evm_chain_id": 26514, "status": "ACTIVE"},
    30400: {"name": "converge", "chain_type": "evm", "evm_chain_id": 432, "status": "DEPRECATED"},
    30401: {"name": "rise", "chain_type": "evm", "evm_chain_id": 4153, "status": "ACTIVE"},
    30402: {"name": "redbelly", "chain_type": "evm", "evm_chain_id": 151, "status": "ACTIVE"},
    30403: {"name": "citrea", "chain_type": "evm", "evm_chain_id": 4114, "status": "ACTIVE"},
    30404: {"name": "moca", "chain_type": "evm", "evm_chain_id": 2288, "status": "ACTIVE"},
    30405: {"name": "sagaevm", "chain_type": "evm", "evm_chain_id": 5464, "status": "DEPRECATED"},
    30406: {"name": "kite", "chain_type": "evm", "evm_chain_id": 2366, "status": "ACTIVE"},
    30407: {"name": "pharos", "chain_type": "evm", "evm_chain_id": 1672, "status": "ACTIVE"},
    30408: {"name": "irys", "chain_type": "evm", "evm_chain_id": 3282, "status": "ACTIVE"},
    30409: {"name": "chiliz", "chain_type": "evm", "evm_chain_id": 88888, "status": "ACTIVE"},
    30410: {"name": "tempo", "chain_type": "evm", "evm_chain_id": 4217, "status": "ACTIVE"},
    30412: {"name": "gensyn", "chain_type": "evm", "evm_chain_id": 685689, "status": "ACTIVE"},
    30413: {"name": "ault", "chain_type": "evm", "evm_chain_id": 904, "status": "ACTIVE"},
    30414: {"name": "neox", "chain_type": "evm", "evm_chain_id": 47763, "status": "ACTIVE"},
    30415: {"name": "rayls", "chain_type": "evm", "evm_chain_id": 72957, "status": "ACTIVE"},
    30416: {"name": "robinhood", "chain_type": "evm", "evm_chain_id": 4663, "status": "ACTIVE"},
    30417: {"name": "arc", "chain_type": "evm", "evm_chain_id": 5042, "status": "ACTIVE"},
    30418: {"name": "Chain A", "chain_type": "evm", "evm_chain_id": None, "status": "ACTIVE"},
    30420: {"name": "Tron", "chain_type": "tron", "evm_chain_id": None, "status": "ACTIVE"},
    30423: {"name": "IOTA L1", "chain_type": "iotamove", "evm_chain_id": None, "status": "ACTIVE"},
    30464: {"name": "opn", "chain_type": "evm", "evm_chain_id": 222, "status": "ACTIVE"},
    30500: {"name": "starknet", "chain_type": "starknet", "evm_chain_id": None, "status": "ACTIVE"},
    30567: {"name": "canton", "chain_type": "canton", "evm_chain_id": None, "status": "ACTIVE"},
    30600: {"name": "stellar", "chain_type": "stellar", "evm_chain_id": None, "status": "ACTIVE"},
}

# Производные словари — держим их производными от LAYERZERO_EID_INFO, а не
# дублируем руками, чтобы не могли разойтись между собой.
LAYERZERO_EID_NAMES: dict[int, str] = {
    eid: info["name"] for eid, info in LAYERZERO_EID_INFO.items()
}

# Explicit translation table eid -> EIP-155 chain_id, ТОЛЬКО для EVM-сетей.
# Это единственное место в кодовой базе, где такое сопоставление должно
# строиться — post-bridge walker и trace_full_path берут chain_id отсюда,
# никогда не пересчитывают eid в chain_id по формуле (её не существует).
LAYERZERO_EID_TO_EVM_CHAIN_ID: dict[int, int] = {
    eid: info["evm_chain_id"]
    for eid, info in LAYERZERO_EID_INFO.items()
    if info["chain_type"] == "evm" and info["evm_chain_id"] is not None
}


def chain_name(eid: Optional[int]) -> str:
    if eid is None:
        return "неизвестная сеть"
    return LAYERZERO_EID_NAMES.get(eid, f"eid={eid} (нет в локальном реестре)")


def is_evm_chain(eid: Optional[int]) -> bool:
    """
    True, только если eid — EVM-совместимая сеть С известным EIP-155
    chain_id (то есть evm_adapter реально может по ней работать). Tron —
    пример сети, которая технически EVM-совместима на уровне байткода (TVM),
    но у LayerZero классифицируется отдельным chain_type="tron", НЕ "evm" —
    это решение самого LayerZero/его метаданных, здесь ему просто следуем.
    """
    return eid in LAYERZERO_EID_TO_EVM_CHAIN_ID


def fetch_message(tx_hash: str, testnet: bool = False, timeout: int = 15) -> Optional[dict[str, Any]]:
    """
    Запрашивает LayerZero Scan /v1/messages/tx/{tx} по хэшу транзакции.
    Возвращает первое найденное сообщение (обычно оно одно на хэш) или None,
    если хэш не относится к сообщению LayerZero.
    """
    base = LAYERZERO_TESTNET if testnet else LAYERZERO_MAINNET
    url = f"{base}/messages/tx/{tx_hash}"
    try:
        resp = requests.get(url, timeout=timeout, headers={"accept": "application/json"})
    except requests.exceptions.RequestException as e:
        print(f"[ошибка сети] не удалось обратиться к LayerZero Scan API: {e}", file=sys.stderr)
        return None

    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        print(f"[ошибка API] LayerZero Scan вернул HTTP {resp.status_code}: {resp.text[:300]}",
              file=sys.stderr)
        return None

    body = resp.json()
    data = body.get("data") if isinstance(body, dict) else body
    if not data:
        return None
    return data[0]


def _decode_oft_recipient(payload_hex: Optional[str]) -> Optional[dict[str, Any]]:
    """
    Декодирует реального получателя и сумму из сырого payload сообщения OFT
    (LayerZero Omnichain Fungible Token — на этом стандарте построен USDT0).

    ЗАЧЕМ ЭТО НУЖНО
    -----------------
    `pathway.receiver.address` в ответе LayerZero Scan API — это адрес
    OApp/adapter-контракта на целевой сети (общий для ВСЕХ пользователей
    этого моста), а НЕ адрес реального получателя перевода. Обнаружено
    живым сравнением в этой сессии: для tx
    0xcae6f9052cc83b91a4688e83d616ada07c390df64289ae1c88f6b967982ce3d1
    (TRON -> Ethereum, USDT0) `pathway.receiver.address` указывал на
    0x1f748c76de468e9d11bd340fa9d5cbadf315dfb0 — адрес OFT Adapter'а,
    ОДИНАКОВЫЙ для всех переводов USDT0 на Ethereum, — тогда как реальный
    ончейн-перевод USDT (проверено через Blockscout) ушёл на
    0x12945c1747213c2cdd0126fac3e333db7a875453. Если не поправить это,
    post-bridge walker (bridge_tracer.py) начинал бы обход не с адреса
    реального получателя, а с адреса самого моста — общего для тысяч
    несвязанных переводов, что делало бы трейс бессмысленным.

    ФОРМАТ PAYLOAD (стандартный OFT v2 send-пакет)
    -------------------------------------------------
    2 байта  — тип сообщения пакета (не используется здесь, просто пропускается)
    32 байта — получатель, left-padded до 32 байт (последние 20 байт — адрес EVM)
    8 байт   — amountSD, сумма в "shared decimals" OFT (НЕ обязательно равна
               локальным decimals токена на этой сети — OFT может использовать
               меньшую общую точность между сетями; здесь не пересчитывается,
               возвращается как есть, помеченная как raw/shared-decimals)

    Подтверждено эмпирически на том же tx: и адрес, и сумма, декодированные
    из payload, ТОЧНО совпали с реальным Transfer-событием USDT на
    Ethereum (адрес, и amountSD=51267464882 == реальное сырое значение
    перевода в 6-decimals единицах USDT). Это не гарантия для абсолютно
    любого LayerZero-сообщения (формат payload — забота конкретного OApp,
    не протокола LayerZero как такового), только для OFT-совместимых
    (USDT0 и большинство token-мостов на LayerZero V2 используют этот же
    codec) — поэтому декодирование обёрнуто в try/except и при несовпадении
    длины payload корректно возвращает None (см. вызывающий код: тогда
    используется pathway.receiver.address как менее точный fallback).

    ТИП СООБЩЕНИЯ (первые 2 байта) — обычный send vs composed
    ---------------------------------------------------------------
    Живой запрос в этой сессии нашёл реальный "composed" OFT-перевод (Legacy
    Mesh, USDT0 Transfer Hub): тип 0x0004 (SEND_AND_CALL), payload заметно
    длиннее 42 байт, а первые 32 байта "получателя" там оказались адресом
    Composer-контракта на destination-сети, а не реальным получателем —
    настоящий финальный получатель зашит глубже, в отдельном compose-блоке,
    формат которого здесь НЕ разбирается (вместо этого Legacy Mesh-переводы
    дочитываются через отдельное сообщение — см. "compose_tx_hash" в
    find_bridge_crossing() и докстринг bridge_tracer.py про Legacy Mesh).
    Обычный (не составной) OFT-перевод — тип 0x0002 (SEND), ровно 42 байта.
    Проверяем оба условия (тип И длина), а не только длину, — иначе при
    случайном совпадении длины на другом типе сообщения можно вернуть
    правдоподобно выглядящий, но неверный адрес.
    """
    if not payload_hex:
        return None
    try:
        raw = bytes.fromhex(payload_hex.removeprefix("0x"))
        if len(raw) != 42 or raw[:2] != b"\x00\x02":  # SEND (0x0002), не SEND_AND_CALL (0x0004)
            return None
        recipient = "0x" + raw[2:34][-20:].hex()
        amount_sd = int.from_bytes(raw[34:42], "big")
        return {"recipient": recipient, "amount_shared_decimals": amount_sd}
    except (ValueError, IndexError):
        return None


def find_messages_by_wallet(address_hex: str, testnet: bool = False, timeout: int = 15) -> list[dict[str, Any]]:
    """
    Запрашивает LayerZero Scan /v1/messages/wallet/{srcAddress} — список
    сообщений, где данный адрес выступает source.tx.from (реальным
    отправителем на исходной сети — это то же самое поле, которое
    find_bridge_crossing() уже предпочитает pathway.sender.address, см. его
    докстринг). Возвращает по убыванию времени (свежие первыми) — проверено
    живым запросом.

    ЗАЧЕМ ЭТО НУЖНО (обнаружено живым запуском в этой сессии)
    ------------------------------------------------------------
    Первая версия bridge_tracer.py искала депозит в мост эвристически: через
    TronGrid — среди исходящих TRC-20 Transfer-событий адреса, ищущих ПРЯМОЙ
    перевод на известный bridge-контракт. На реальных данных это дало
    ложноотрицательный результат: пользователь перевёл USDT не напрямую на
    OFT-контракт, а через промежуточный router-контракт (тот сначала получил
    USDT, затем сам перевёл их на OFT) — прямого Transfer от пользователя на
    известный контракт просто не существовало, хотя сообщение LayerZero было
    реально отправлено и доставлено. Этот эндпоинт устраняет саму
    необходимость угадывать промежуточные контракты: LayerZero Scan уже знает
    итоговый tx хэш независимо от того, сколько контрактов было между
    пользователем и OFT.

    Args:
        address_hex: адрес в hex-формате (для Tron — "голый" 20-байтный hex
            с префиксом "0x", БЕЗ версионного байта 0x41 — см. address.py в
            tron_adapter, normalize_tron_address/base58_to_hex).
    """
    base = LAYERZERO_TESTNET if testnet else LAYERZERO_MAINNET
    url = f"{base}/messages/wallet/{address_hex}"
    try:
        resp = requests.get(url, timeout=timeout, headers={"accept": "application/json"})
    except requests.exceptions.RequestException as e:
        print(f"[ошибка сети] не удалось обратиться к LayerZero Scan API: {e}", file=sys.stderr)
        return []

    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        print(f"[ошибка API] LayerZero Scan вернул HTTP {resp.status_code}: {resp.text[:300]}",
              file=sys.stderr)
        return []

    body = resp.json()
    data = body.get("data") if isinstance(body, dict) else body
    return data or []


def find_bridge_crossing(tx_hash: str, testnet: bool = False) -> dict[str, Any]:
    """
    Основная функция: по хэшу транзакции (на исходной сети) возвращает
    структурированный результат сопоставления bridge entry / bridge exit —
    ОДНОГО сообщения/хопа LayerZero. Многоходовые маршруты (Legacy Mesh —
    USDT0 Transfer Hub через Arbitrum как хаб, см. bridge_exit.compose_*
    ниже) собираются вызывающим кодом через повторный вызов этой функции на
    compose_tx_hash — сама она себя не вызывает рекурсивно, остаётся
    простым one-hop матчером (сигнатура позволяла рекурсию с самого начала,
    реализация — в bridge_tracer.trace_full_path).

    LEGACY MESH: КАК ОБНАРУЖИТЬ HOP 2 (подтверждено живым запросом)
    -----------------------------------------------------------------
    USDT0 Transfer Hub может отправлять "составное" (composed) OFT-сообщение
    вместо обычного — тогда после доставки Hop 1 на хаб (Arbitrum) LayerZero
    автоматически исполняет compose-вызов, который сам является ОТДЕЛЬНЫМ,
    самостоятельным LayerZero-сообщением (Hop 2: хаб -> реальная целевая
    сеть). Найдено и проверено на реальных данных: `destination.lzCompose`
    в сыром ответе API — `{"status": "N/A"}`, если хопа 2 нет (сообщение
    было простым SEND, хаб — финальная точка), или `{"status": "SUCCEEDED",
    "txs": [{"txHash": ...}, ...]}`, если Hop 2 состоялся — и это txHash
    сам по себе успешно резолвится через find_bridge_crossing(), возвращая
    полноценный отдельный bridge_entry/bridge_exit для хаб -> реальная сеть.
    Без этой проверки трейсер останавливался бы на хабе как на "выходе из
    моста", хотя на самом деле это середина пути.

    Проверено конкретно на переводах С TRON (не только гипотетически):
    отсканирован реестр сообщений OApp USDT0-на-Tron (/v1/messages/oapp/
    30420/{hex}, ~500 сообщений с Tron как source) — среди них нашлось 30
    реальных composed-переводов Tron -> Arbitrum -> дальше. Пример:
    tx 0x9574bb18862313c80b4d5476d75170ea567f76d038386d3af77c0cdc1d540050
    (Tron -> Arbitrum, compose_status=SUCCEEDED) -> compose-tx
    0x1155a198e1ab7270f43841976946a72810661dd85be617e229d18288a663c917
    сам резолвится как Hop 2 (Arbitrum -> xlayer, DELIVERED). Ещё один
    пример с Hop 2 на Polygon: tx 0x91fd56bd84cde56c308eda9653b519b7a6f2020
    532481886e9386244e1ba372b -> compose-tx 0x0a7870b9a23c1e5a827749e340e76
    99ba5784b83b126121f8ac23abb21c4c4db (Arbitrum -> Polygon, DELIVERED).
    """
    msg = fetch_message(tx_hash, testnet=testnet)
    if msg is None:
        return {
            "found": False,
            "confidence": "UNRESOLVED",
            "note": (
                "Транзакция не найдена в LayerZero Scan. Либо это не перевод "
                "через LayerZero (проверьте другой мост/протокол), либо хэш "
                "некорректен, либо это очень свежая транзакция, ещё не "
                "проиндексированная."
            ),
        }

    pathway = msg.get("pathway") or {}
    source = msg.get("source") or {}
    destination = msg.get("destination") or {}
    source_tx = source.get("tx") or {}
    dest_tx = destination.get("tx") or {}
    top_status = (msg.get("status") or {}).get("name")

    delivered = bool(dest_tx.get("txHash"))

    oapp_address = (pathway.get("receiver") or {}).get("address")
    oft_decoded = _decode_oft_recipient(source_tx.get("payload"))
    if oft_decoded is not None:
        real_recipient = oft_decoded["recipient"]
        recipient_source = "oft_payload_decoded"
    else:
        # Fallback: адрес OApp/adapter-контракта, ОБЩИЙ для всех переводов
        # через этот мост — не настоящий получатель (см. _decode_oft_recipient),
        # но лучше, чем None, если payload не в ожидаемом OFT-формате.
        real_recipient = oapp_address
        recipient_source = "pathway_receiver_fallback"

    result = {
        "found": True,
        "confidence": "HIGH",  # криптографическое сопоставление через GUID + кворум DVN
        "guid": msg.get("guid"),
        "message_status": top_status,
        "bridge_entry": {
            "chain": chain_name(pathway.get("srcEid")),
            "eid": pathway.get("srcEid"),
            "tx_hash": source_tx.get("txHash"),
            "from_address": source_tx.get("from") or (pathway.get("sender") or {}).get("address"),
            "timestamp": source_tx.get("blockTimestamp"),
            "source_status": source.get("status"),
        },
        "bridge_exit": {
            "chain": chain_name(pathway.get("dstEid")),
            "eid": pathway.get("dstEid"),
            "tx_hash": dest_tx.get("txHash"),
            # Реальный получатель, декодированный из OFT-payload, а не
            # адрес bridge-контракта (см. _decode_oft_recipient) — важно
            # для любого пост-bridge трейсинга, который берёт этот адрес
            # как отправную точку следующего сегмента.
            "to_address": real_recipient,
            "oapp_address": oapp_address,
            "recipient_source": recipient_source,
            "amount_shared_decimals": oft_decoded["amount_shared_decimals"] if oft_decoded else None,
            # Legacy Mesh Hop 2 (см. докстринг функции выше): compose_status
            # "N/A" (или отсутствует) — обычное сообщение, эта bridge_exit
            # финальна. Другой статус ("SUCCEEDED" и т.п.) с непустым
            # compose_tx_hash — на destination-сети автоматически исполнился
            # compose-вызов, который сам является следующим LayerZero-
            # сообщением; вызывающий код должен продолжить
            # find_bridge_crossing(compose_tx_hash), а не считать эту
            # bridge_exit финальной точкой выхода из моста.
            "compose_status": (destination.get("lzCompose") or {}).get("status"),
            "compose_tx_hash": (
                ((destination.get("lzCompose") or {}).get("txs") or [{}])[0].get("txHash")
                if (destination.get("lzCompose") or {}).get("txs") else None
            ),
            "timestamp": dest_tx.get("blockTimestamp"),
            # Человекочитаемый статус всегда отражает наличие destination tx hash,
            # а не сырой API-статус (например, "WAITING") — так однозначнее для
            # комплаенс-офицера, который не обязан знать внутренние статусы LayerZero.
            "status": "DELIVERED" if delivered else "NOT DELIVERED YET",
            "raw_status": destination.get("status"),
        },
        # note: три исхода, не два — наличие dest tx_hash (`delivered`) само
        # по себе НЕ значит "финализировано". По официальному определению
        # LayerZero Scan (docs.layerzero.network/v2/tools/layerzeroscan,
        # проверено в этой сессии): CONFIRMING = "Destination transaction
        # submitted, waiting for finality" — tx на целевой сети уже
        # существует (destination.tx.txHash непуст, delivered=True), но
        # LayerZero ещё не считает сообщение доставленным окончательно.
        # Раньше note затиралось в None при первом же непустом tx_hash,
        # независимо от top_status — теряя единственное предупреждение о
        # том, что дальнейший пост-bridge обход может опираться на ещё не
        # финализированную транзакцию (риск реорга, небольшой, но
        # ненулевой). Обнаружено живым запуском в этой сессии
        # (message_status="CONFIRMING", tx_hash уже непуст).
        "note": (
            "Сообщение отправлено и проходит верификацию DVN (Decentralized "
            "Verifier Network), но ещё не исполнено (lzReceive) на целевой "
            "сети. Это НЕ означает, что перевод завис: типичное время "
            "доставки для LayerZero V2 — от секунд до нескольких минут в "
            "зависимости от требуемого числа подтверждений на исходной сети "
            "и от того, сколько DVN настроено в конфигурации пути."
        ) if not delivered else (
            None if top_status == "DELIVERED" else (
                f"Транзакция на целевой сети уже отправлена (destination "
                f"tx_hash есть), но LayerZero ещё не считает сообщение "
                f"финализированным (status={top_status!r}, ожидается "
                "'DELIVERED') — по официальному определению LayerZero Scan "
                "это 'waiting for finality': до финализации теоретически "
                "возможен реорг транзакции на целевой сети (риск обычно "
                "небольшой и зависит от конкретной сети, но ненулевой). "
                "Любые дальнейшие хопы пост-bridge обхода, найденные после "
                "этого перехода, построены поверх ещё не финализированных "
                "данных."
            )
        ),
    }
    return result


def print_result(result: dict[str, Any]) -> None:
    if not result["found"]:
        print("=" * 60)
        print("РЕЗУЛЬТАТ: транзакция не сопоставлена")
        print("=" * 60)
        print(result["note"])
        print(f"\nConfidence: {result['confidence']}")
        return

    entry = result["bridge_entry"]
    exit_ = result["bridge_exit"]

    print("=" * 60)
    print("BRIDGE ENTRY (точка входа в мост)")
    print("=" * 60)
    print(f"  Сеть:          {entry['chain']}")
    print(f"  Tx hash:       {entry['tx_hash']}")
    print(f"  Отправитель:   {entry['from_address']}")
    print(f"  Время:         {entry['timestamp']}")
    print(f"  Статус source: {entry['source_status']}")

    print()
    print("=" * 60)
    print("BRIDGE EXIT (точка выхода из моста)")
    print("=" * 60)
    print(f"  Сеть:          {exit_['chain']}")
    print(f"  Tx hash:       {exit_['tx_hash'] or '(ещё не доставлено)'}")
    print(f"  Получатель:    {exit_['to_address']}"
          f"{'  (декодировано из OFT payload)' if exit_['recipient_source'] == 'oft_payload_decoded' else '  (fallback: адрес OApp-контракта, НЕ точный получатель)'}")
    print(f"  OApp-контракт: {exit_['oapp_address']}")
    print(f"  Время:         {exit_['timestamp'] or '—'}")
    print(f"  Статус:        {exit_['status']} (raw: {exit_['raw_status']})")

    print()
    print(f"GUID:              {result['guid']}")
    print(f"Message status:    {result['message_status']}")
    print(f"Confidence:        {result['confidence']}  (криптографическое сопоставление через GUID + DVN)")
    if result["note"]:
        print(f"\nПримечание: {result['note']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MVP-трейсер переводов через мост LayerZero (TRON <-> Ethereum и др.)"
    )
    parser.add_argument("--tx", required=True, help="Хэш транзакции на исходной сети")
    parser.add_argument("--testnet", action="store_true", help="Использовать testnet LayerZero Scan")
    args = parser.parse_args()

    result = find_bridge_crossing(args.tx, testnet=args.testnet)
    print_result(result)


if __name__ == "__main__":
    main()
