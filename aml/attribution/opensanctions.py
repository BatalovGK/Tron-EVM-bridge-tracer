# attribution/opensanctions.py
"""
OpenSanctions — с этой версии два независимых пути (см. "Следующий шаг" в
summary_for_new_dialog.md — переделка на bulk принята явно, не тихо):

1. **Bulk (основной, refresh_opensanctions_bulk)** — тот же паттерн, что и
   OFAC (attribution/ofac.py): периодически скачать весь снапшот, распарсить,
   записать в label_cache. Бесплатно и без ключа.
2. **Live /match (check_address_opensanctions, опциональный резерв)** —
   оставлен как было, но теперь НЕ для проверки адресов по bulk-логике, а
   для будущего сценария "проверка по имени контрагента" (fuzzy-матчинг по
   названию компании/физлица, для которого bulk не подходит). Требует
   OPENSANCTIONS_API_KEY, платный (0.10 EUR/запрос сверх 50 бесплатных/мес,
   подтверждено www.opensanctions.org/docs/api/ на июль 2026) — поэтому
   по-прежнему опционален и тихо пропускается без ключа.

Почему bulk, а не live-запрос на каждый адрес: тот же аргумент, что и для
OFAC — при bulk CSV или постоянном поллинге Watcher'а (раздел 5.2 архитектуры)
живой платный запрос на каждый адрес не масштабируется, а весь список можно
скачивать раз в несколько часов бесплатно.

Формат файла: **targets.nested.json**, НЕ targets.simple.csv — CSV теряет
множественные значения (несколько кошельков на одну сущность), это отдельно
подтверждено официальной документацией
(https://www.opensanctions.org/docs/bulk/json/, июль 2026):
"The targets.nested.json format ... combines related entities into a nested
object structure ... one line per target, with adjacent entities (e.g.
addresses, sanctions) nested inside the properties section".

Почему nested, а не плоский entities.ftm.json: CryptoWallet — это
reference-сущность (сама по себе не "target"/не под санкциями), она
привязана к владельцу через свойство `owner`. targets.nested.json уже
группирует целевую (санкционную) сущность вместе со связанными сущностями
(в т.ч. CryptoWallet) в одном JSON-объекте на строку — не нужен отдельный
проход "найти владельца по ссылке".

URL-паттерны (подтверждены https://www.opensanctions.org/docs/bulk/updates/,
июль 2026 — ЗАМЕТЬ: порядок "latest"/"<dataset>" в этих двух URL разный,
это не опечатка, так в самой документации):
    Скачивание файла снапшота:
        https://data.opensanctions.org/datasets/latest/<dataset>/<format>
    Метаданные (версия, SHA1, для проверки "не скачали ли уже актуальное"):
        https://data.opensanctions.org/datasets/<dataset>/latest/index.json

Рекомендованная частота (та же документация): полный файл — не чаще раз в
6 часов; index.json дёшев, его можно проверять раз в 30 минут, чтобы не
тянуть занозово весь файл, если ничего не изменилось.

ВАЖНО, изменение вступает в силу 2026-08-17 (подтверждено changelog
https://www.opensanctions.org/changelog/45/, объявлено 2026-06-22): оба
URL выше начнут отвечать HTTP 307 редиректом на канонический путь
`.../datasets/<dataset>/...`. aiohttp следует редиректам по умолчанию,
поэтому дополнительных изменений в коде не нужно, НО важно не выставлять
`allow_redirects=False` при рефакторинге, иначе после 17.08.2026 скачивание
начнёт молча падать/возвращать 307 вместо данных.

Датасет по умолчанию — `default` (агрегированная коллекция); OpenSanctions
явно советует не использовать датасет `all` (внутренний артефакт,
обновляется реже и содержит тестовые сущности) — подтверждено changelog
https://www.opensanctions.org/changelog/18/.

НЕ ПРОВЕРЕНО НА РЕАЛЬНОМ ФАЙЛЕ (сеть до data.opensanctions.org недоступна
из среды разработки — то же ограничение, что и у OFAC-парсера). Структура
вложенности собрана из документации
(docs/bulk/json/, docs/entities/), а не эмпирически по реальному снапшоту.
Перед первым продовым запуском нужен ручной прогон (см.
scripts/manual_check_attribution.py) с sanity-check на заведомо известном
sanctioned-кошельке — можно использовать те же адреса, что и в
attribution/ofac.py::KNOWN_SANCTIONED_ADDRESSES_FOR_SANITY_CHECK (SUEX OTC
входит и в датасет OpenSanctions `default`, не только в чистый OFAC XML).
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

import aiohttp

from common.evidence_store import upsert_label
from common.secrets import get_secret

logger = logging.getLogger(__name__)

DEFAULT_DATASET = "default"
BULK_DOWNLOAD_URL_TEMPLATE = "https://data.opensanctions.org/datasets/latest/{dataset}/targets.nested.json"
BULK_INDEX_URL_TEMPLATE = "https://data.opensanctions.org/datasets/{dataset}/latest/index.json"

OPENSANCTIONS_MATCH_URL = "https://api.opensanctions.org/match/default"

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


async def fetch_bulk_index(dataset: str = DEFAULT_DATASET) -> Dict[str, Any]:
    """
    Скачивает лёгкие метаданные снапшота (версия/SHA1/last_export), чтобы
    решить, нужно ли вообще тянуть тяжёлый targets.nested.json заново.
    Дёшево — можно дёргать чаще, чем сам bulk-файл (см. docstring модуля).
    """
    timeout = aiohttp.ClientTimeout(total=30)
    url = BULK_INDEX_URL_TEMPLATE.format(dataset=dataset)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            return await response.json()


async def download_targets_nested(dataset: str = DEFAULT_DATASET) -> bytes:
    """Скачивает targets.nested.json целиком (JSON Lines, потенциально крупный файл)."""
    timeout = aiohttp.ClientTimeout(total=300)
    url = BULK_DOWNLOAD_URL_TEMPLATE.format(dataset=dataset)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            return await response.read()


def _iter_nested_entities(node: Any):
    """
    Рекурсивно обходит один JSON-объект targets.nested.json (одна строка =
    одна целевая сущность + вложенные связанные сущности внутри свойств) и
    отдаёт все встреченные объекты вида {"id":..., "schema":..., "properties":...}.

    Формат подтверждён docs/entities/ (id/schema/properties на верхнем уровне
    любой сущности) и docs/bulk/json/ (вложенные сущности живут прямо внутри
    значений properties родительской сущности) — но точная глубина вложенности
    CryptoWallet под конкретной sanctioned-сущностью НЕ проверена на реальном
    файле, поэтому обход намеренно рекурсивный по всей структуре, а не
    привязан к жёсткому пути properties.something.
    """
    if isinstance(node, dict):
        if "schema" in node and "properties" in node:
            yield node
        for value in node.values():
            yield from _iter_nested_entities(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_nested_entities(item)


def parse_targets_nested(data_bytes: bytes) -> List[Dict[str, Any]]:
    """
    Парсит targets.nested.json (JSON Lines) и возвращает список найденных
    криптокошельков вида:
        {"address": "0x...", "entity_id": ..., "target_caption": ...,
         "target_topics": [...], "dataset": "..."}

    v1 ограничен EVM-адресами (0x + 40 hex) — по аналогии с ofac.py, чтобы
    не выдавать за проверенную логику разбор BTC/TRON форматов адресов,
    которые здесь не тестировались.
    """
    results: List[Dict[str, Any]] = []

    for line in data_bytes.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            target = json.loads(line)
        except ValueError:
            logger.warning("Не удалось распарсить строку targets.nested.json как JSON — пропущена")
            continue

        target_caption = target.get("caption")
        target_props = target.get("properties")
        target_topics = target_props.get("topics", []) if isinstance(target_props, dict) else []
        target_id = target.get("id")
        datasets = target.get("datasets", [])

        for entity in _iter_nested_entities(target):
            if entity.get("schema") != "CryptoWallet":
                continue
            public_keys = entity.get("properties", {}).get("publicKey", [])
            for address in public_keys:
                if not isinstance(address, str):
                    continue
                if _EVM_ADDRESS_RE.match(address):
                    results.append({
                        "address": address.lower(),
                        "entity_id": entity.get("id"),
                        "target_id": target_id,
                        "target_caption": target_caption,
                        "target_topics": target_topics,
                        "datasets": datasets,
                    })

    return results


async def refresh_opensanctions_bulk(dataset: str = DEFAULT_DATASET) -> int:
    """
    Полный цикл обновления bulk-данных OpenSanctions: скачать
    targets.nested.json -> распарсить CryptoWallet-сущности -> записать в
    label_cache. Возвращает число записанных адресов.

    Вызывать периодически (не чаще раз в 6 часов, см. docstring модуля), а
    не при каждой проверке одного адреса. Перед вызовом имеет смысл сверить
    fetch_bulk_index() с сохранённым SHA1/версией предыдущего скачивания,
    чтобы не тянуть файл повторно без необходимости — эта логика "нужно ли
    обновлять" оставлена вызывающему коду (см. scripts/), здесь — только
    сам цикл скачать/распарсить/записать.
    """
    data_bytes = await download_targets_nested(dataset)
    entries = parse_targets_nested(data_bytes)

    for entry in entries:
        topics = entry["target_topics"] or []
        label_type = "sanctioned" if "sanction" in topics else (",".join(topics) or "listed")
        await upsert_label(
            address=entry["address"],
            source="opensanctions_bulk",
            label_type=label_type,
            chain_id=None,
            confidence=1.0,
            raw_data={
                "entity_id": entry["entity_id"],
                "target_id": entry["target_id"],
                "target_caption": entry["target_caption"],
                "target_topics": topics,
                "datasets": entry["datasets"],
            },
        )

    logger.info(f"OpenSanctions bulk: обновлено {len(entries)} EVM-адресов в label_cache (dataset={dataset})")
    return len(entries)


async def check_address_opensanctions(address: str) -> Optional[Dict[str, Any]]:
    """
    Живой запрос к OpenSanctions Matching API (schema=CryptoWallet).

    ОПЦИОНАЛЬНЫЙ РЕЗЕРВ, не основной путь проверки адресов — см. docstring
    модуля: для bulk-скрининга адресов используйте refresh_opensanctions_bulk()
    + локальный SELECT из label_cache (по аналогии с OFAC в service.py).
    Этот live-запрос сохранён на будущее для сценария fuzzy-поиска по имени
    контрагента, где bulk не подходит.

    Возвращает None, если OPENSANCTIONS_API_KEY не настроен (источник
    пропускается, а не считается ошибкой).
    """
    api_key = get_secret("OPENSANCTIONS_API_KEY", required=False)
    if not api_key:
        logger.info("OPENSANCTIONS_API_KEY не задан — источник OpenSanctions пропущен")
        return None

    query = {
        "queries": {
            "q": {
                "schema": "CryptoWallet",
                "properties": {"publicKey": [address]},
            }
        }
    }

    headers = {"Authorization": f"ApiKey {api_key}"}
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(OPENSANCTIONS_MATCH_URL, json=query) as response:
            response.raise_for_status()
            data = await response.json()

    results = data.get("responses", {}).get("q", {}).get("results", [])
    matches = [
        {
            "caption": r.get("caption"),
            "score": r.get("score"),
            "topics": r.get("properties", {}).get("topics", []),
        }
        for r in results
    ]
    # Порог 0.7 — разумный дефолт для скрининга без false positive на слабых совпадениях;
    # при необходимости вынести в параметр/конфиг позже.
    is_risky = any((m["score"] or 0) >= 0.7 for m in matches)

    return {"raw": data, "matches": matches, "is_risky": is_risky}
