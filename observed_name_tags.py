#!/usr/bin/env python3
"""
observed_name_tags.py — персистентный write-through кэш "самоуказанных"
имён адресов, обнаруженных живыми запросами во время трейсинга: TronGrid
(`account_name`, hex-encoded on-chain имя аккаунта) и Blockscout Pro
(`name`, только для верифицированных контрактов — имя из исходника).

ПОЧЕМУ ЭТО НЕ ИСПОЛЬЗУЕТСЯ ДЛЯ СТОП-УСЛОВИЙ (сознательное решение)
---------------------------------------------------------------------
Это НЕ то же самое, что known_contracts.py/bridge_registry.py — те два
реестра курируются вручную (сверка по публичным меткам) или официальным
API (USDT0 Deployments) и служат основанием стоп-условия
(RESTED_AT_EXCHANGE/RESTED_AT_CONTRACT). Имена здесь — САМОУКАЗАННЫЕ
владельцем контракта/аккаунта, НИКЕМ не модерируются: TronGrid
`account_name` — произвольное on-chain имя, которое задаёт сам аккаунт;
Blockscout `name` (только verified-контракты) — имя класса из
Solidity-исходника, тоже выбирает разработчик. Злоумышленник может
назвать свой контракт как угодно — использовать это как основание "это
точно биржа/мост" небезопасно для комплаенс-инструмента. Проверено живым
запросом в этой сессии: на реальном примере (TWPziSAroSacAjDuL52ByQzU86s9mP2gPr)
TronGrid отдал account_name="OftBridge" — информативно и в данном случае
достоверно (подтверждено независимо через transaction receipt в
bridge_registry.py), но полагаться на это ПРОГРАММНО как единственное
основание для авто-классификации типа контракта решено не делать —
явный выбор пользователя, см. историю сессии.

Живёт здесь ОТДЕЛЬНО от bridge_registry.py/known_contracts.py именно
поэтому: чтобы структура файла сама несла это разграничение (запись здесь
— просто заметка "вот как адрес сам себя назвал", не классификация),
а не полагаться на то, что вызывающий код не перепутает поля.

ИСТОЧНИКИ, ПРОВЕРЕННЫЕ ЖИВЫМ ЗАПРОСОМ (не по памяти)
---------------------------------------------------------
- TronGrid (`tron_adapter.get_account_info`, поле `data[0].account_name`,
  hex-encoded) — есть, содержательное для проверенных примеров.
- Blockscout Pro (`evm_adapter.get_address_info`, поле `name`) — есть,
  НО заполняется только для is_verified=True контрактов; `public_tags`
  проверен и оказался ПУСТЫМ `[]` даже у заведомо известных адресов
  (Binance 14) — Blockscout Pro НЕ отдаёт готовую бизнес-категоризацию
  через этот эндпоинт, вопреки наличию поля в схеме.
- Tronscan API (`apilist.tronscanapi.com/api/account`, ОТДЕЛЬНЫЙ от
  TronGrid публичный API) — своё поле `name`, но проверено: значение
  ИДЕНТИЧНО TronGrid `account_name` (тот же on-chain источник данных, не
  независимая метка). `addressTagLogo`/`blueTagUrl` (официальная
  "синяя галочка" Tronscan) — пустые для проверенных примеров. Поскольку
  информация не даёт ничего сверх TronGrid, отдельный клиент для
  Tronscan API здесь НЕ реализован (не оправдано ради дублирующих данных
  для MVP) — если понадобится именно `blueTagUrl`/официальные теги в
  будущем, это отдельная, самостоятельная интеграция.

ФОРМАТ ЗАПИСИ
--------------
{"<chain_key>:<address_lower>": {"name": str, "source": str, "observed_at": ISO-дата}}
chain_key: "tron" | "evm:<chain_id>" (EIP-155).
source: SOURCE_TRONGRID | SOURCE_BLOCKSCOUT (см. константы ниже).

ПЕРСИСТЕНТНОСТЬ
-----------------
Простой JSON-файл на диске рядом со скриптом (тот же принцип, что диск-кэш
usdt0_deployments.py) — не Postgres, MVP без внешних сервисов, кроме трёх
исходных сетевых адаптеров.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".observed_name_tags_cache.json")

SOURCE_TRONGRID = "trongrid_tag"
SOURCE_BLOCKSCOUT = "blockscout_name_tag"


def _load() -> dict[str, Any]:
    if not os.path.exists(_CACHE_PATH):
        return {}
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass  # диск-кэш — best-effort, не критично для корректности


def _key(chain_key: str, address: str) -> str:
    return f"{chain_key}:{address.lower()}"


def get_cached(chain_key: str, address: str) -> Optional[dict[str, Any]]:
    """Читает запись из персистентного кэша БЕЗ обращения к сети."""
    return _load().get(_key(chain_key, address))


def write_through(chain_key: str, address: str, name: str, source: str) -> dict[str, Any]:
    """Записывает обнаруженное имя в кэш (с провенансом и датой) и
    возвращает саму запись."""
    entry = {
        "name": name,
        "source": source,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    data = _load()
    data[_key(chain_key, address)] = entry
    _save(data)
    return entry
