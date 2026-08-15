# Crypto AML Platform — прототип (evm_adapter + agent tool-calling)

## Структура

```
common/            — общие утилиты
  secrets.py         — единая точка чтения API-ключей (env / Docker secrets / .env)
  db.py              — пул подключений PostgreSQL (asyncpg), накатка схемы
  evidence_store.py  — типизированные функции: label_cache, seed_registry, evidence_log, verdicts
  sql/schema.sql     — DDL для всех таблиц хранилища
  tests/             — тесты на РЕАЛЬНОМ Postgres (не моки — так были найдены реальные баги)
evm_adapter/        — клиент к Blockscout Pro API (rate limiting, async-кэш, credits guard)
attribution/        — Attribution & Labeling Engine (субагент 3): OFAC/GoPlus/OpenSanctions
  ofac.py            — периодическое обновление локального кэша OFAC SDN (не live-вызов)
  goplus.py          — живой запрос к GoPlus Malicious Address API
  opensanctions.py   — bulk-обновление кэша (targets.nested.json, без ключа) + опциональный живой /match-резерв
  service.py         — check_address(): агрегирует все три источника + пишет evidence
  tests/             — OFAC/OpenSanctions bulk на офлайн-фикстурах; GoPlus и живой OpenSanctions /match на моках; service на реальном Postgres
flow_tracer/         — Flow & Hop Tracer (субагент 2): обход исходящих переводов + резолв свопов
  hop_tracer.py      — BFS-обход графа переводов, poison/taint-разметка для incident_response
  swap_resolver.py   — определяет своп через известный DEX-контракт, резолвит token_in/out через TheGraph
  thegraph.py        — клиент к TheGraph decentralized network (gateway), НЕ hosted-сервис (депрекейтнут в 2024)
  config/dex_subgraphs.yaml  — chain_id -> subgraph_id (ЧАСТЬ ЗАПОЛНИТЬ ВРУЧНУЮ, см. комментарии в файле)
  config/dex_contracts.yaml  — известные DEX router-адреса -> protocol_key (ЧАСТЬ ЗАПОЛНИТЬ ВРУЧНУЮ)
  tests/             — на моках (evm_adapter/TheGraph); реальная проверка — scripts/manual_check_flow_tracer.py
  ОГРАНИЧЕНИЯ v1 (согласовано явно): мосты (bridges) не детектируются отдельно — вернёмся после
  остальных сетевых адаптеров (BTC/TRON); только Uniswap v2/v3-совместимые схемы подграфов.
agent/               — обвязка tool-calling: тулзы для Ollama + цикл вызова (Execution Layer)
  tools_evm.py       — обёртки над evm_adapter с докстрингами для автосхемы ollama
  tools_attribution.py — обёртка над attribution.check_address как тулза check_sanctions_tool
  tools_flow.py      — обёртка над flow_tracer.trace_flow как тулза trace_flow_tool
  orchestrator.py    — цикл: чат с Qwen3-Coder -> tool_calls -> вызов тулзы -> ответ модели
  tests/             — тесты цикла на моках (без реальной Ollama)
scripts/
  manual_check_live_ollama.py  — ручная проверка ПРОТИВ ВАШЕЙ реальной Ollama
  manual_check_attribution.py  — ручная проверка ПРОТИВ РЕАЛЬНЫХ OFAC/GoPlus/OpenSanctions
.env.example         — какие ключи нужны и где их взять
docker-compose.yml   — пример подключения рядом с ollama + open-webui + postgres
requirements.txt
```

## Откуда берутся API-ключи

Единая точка — `common/secrets.py`. Ключ ищется в таком порядке:
1. `<ИМЯ>_FILE` — путь к файлу (Docker secrets).
2. `<ИМЯ>` — обычная переменная окружения (например, из `.env` через `env_file` в
   docker-compose, или через `export` в терминале).

Скопируйте `.env.example` в `.env`, впишите реальные значения, добавьте `.env`
в `.gitignore`. В docker-compose он подключается через `env_file: [.env]` —
ничего в коде дополнительно менять не нужно.

## Установка

```bash
pip install -r requirements.txt --break-system-packages
```

## Хранилище (PostgreSQL)

Схема (`common/sql/schema.sql`) покрывает 4 таблицы из раздела 4-5 архитектуры:
`label_cache` (внешние метки: OFAC/OpenSanctions/GoPlus, с провенансом),
`seed_registry` (свои находки — посев меток), `evidence_log` (сырые данные,
собранные Execution Layer по каждому расследованию), `verdicts` (финальные
вердикты).

Для локального запуска без Docker:
```bash
# Поднять Postgres любым способом (локально или в контейнере) и создать БД,
# затем в .env:
POSTGRES_DSN=postgresql://user:password@localhost:5432/aml_platform
```

Схема накатывается автоматически при первом вызове `get_pool()` /
`init_schema()` — отдельно применять .sql-файл руками не нужно.

### Тесты хранилища — на РЕАЛЬНОМ Postgres, не на моках

Важно: в отличие от `evm_adapter`/`agent`, тесты `common/tests/` идут против
настоящей БД, потому что именно так были найдены реальные баги при разработке
(NULL в UNIQUE-ограничении не дедуплицируется как обычное значение; REAL
теряет точность при сравнении с Python float; регистр адреса рассинхронизировался
между полями). Моки на dict/JSON эти проблемы не ловят.

```bash
# Тестовая БД (можно ту же, что и рабочая — тесты чистят таблицы через TRUNCATE,
# но лучше отдельную "aml_platform_test", чтобы не тереть рабочие данные)
export POSTGRES_DSN="postgresql://user:password@localhost:5432/aml_platform_test"
PYTHONPATH=. pytest common/tests/test_evidence_store.py -v
```

Должно быть 10 пройденных тестов.

## Attribution & Labeling Engine (OFAC / GoPlus / OpenSanctions)

Важное отступление от архитектуры, обнаруженное при подготовке: документ
описывал OpenSanctions как "бесплатно для некоммерческого использования" —
это верно про лицензию на ДАННЫЕ, но живой hosted `/match` API требует
регистрации и работает по pay-as-you-go (как и произошло с Blockscout).
Поэтому OpenSanctions переведён на bulk-паттерн (как OFAC): периодически
скачивается весь `targets.nested.json` (бесплатно, без ключа) и пишется в
`label_cache`, а проверка одного адреса — обычный SELECT. Живой платный
`/match` (`OPENSANCTIONS_API_KEY`) оставлен в коде как опциональный резерв
для будущего сценария "поиск по имени контрагента", но в `check_address()`
больше не вызывается. OFAC и OpenSanctions bulk обновляются периодическим
вызовом `refresh_ofac_sdn()` / `refresh_opensanctions_bulk()` (не на каждый
запрос), GoPlus — живой запрос без ключа (ключ только повышает лимиты).

### Тесты — что на моках, что на реальных внешних API

- `attribution/tests/test_ofac.py` — офлайн, на фрагменте XML, повторяющем
  подтверждённую структуру реального файла. Полный файл (~80 МБ) не
  скачивается в тестах.
- `attribution/tests/test_opensanctions.py` — bulk-парсер офлайн, на
  синтетических фикстурах JSON Lines (структура собрана по документации, не
  по реальному файлу — см. предупреждение в docstring модуля); живой
  `/match`-резерв — на моках aiohttp.
- `attribution/tests/test_goplus.py` — на моках aiohttp (сеть до этих API
  недоступна из песочницы, где писался код).
- `attribution/tests/test_service.py` — на РЕАЛЬНОМ Postgres (evidence_log/
  label_cache), с замоканными GoPlus/OpenSanctions.

```bash
PYTHONPATH=. pytest attribution/tests/ -v
```
Должно быть 23 пройденных теста.

### Обязательная ручная проверка на реальных API

Ни OFAC, ни OpenSanctions bulk, ни GoPlus не были проверены против настоящих
серверов (сеть ограничена в среде, где готовился код) — только на моках/
фикстурах. Прежде чем полагаться на Attribution Engine всерьёз, прогоните:

```bash
export POSTGRES_DSN="postgresql://user:pass@host:5432/aml_platform"
PYTHONPATH=. python scripts/manual_check_attribution.py
```

Скрипт скачивает настоящий OFAC SDN файл и настоящий OpenSanctions
`targets.nested.json`, проверяет, что 4 ЗАВЕДОМО реальных sanctioned-адреса
(SUEX OTC, официальный пресс-релиз OFAC 2021) находятся в обоих источниках —
если нет в OFAC, значит структура XML изменилась и парсер
(`attribution/ofac.py`) нужно поправить; если нет в OpenSanctions bulk —
скорее всего структура вложенности `CryptoWallet` внутри `targets.nested.json`
угадана неверно (`attribution/opensanctions.py::_iter_nested_entities`) и
парсер нужно доработать по реальному файлу. Затем проверяет обычный адрес
(Vitalik Buterin) — не должен помечаться как high-risk.

**Windows: если скрипт падает с `SSLCertVerificationError: self-signed
certificate in certificate chain`** — это почти всегда антивирус или
корпоративный прокси, делающий HTTPS-инспекцию (у него свой корневой
сертификат, которому Windows доверяет, а "чистый" Python — нет). Решение —
не отключать проверку сертификатов, а подключить Python к системному
хранилищу сертификатов Windows:
```powershell
pip install truststore --break-system-packages
```
Скрипт уже подхватывает `truststore`, если он установлен (см. начало
`scripts/manual_check_attribution.py`) — достаточно просто поставить пакет
и перезапустить, менять код не нужно.

## Тесты (без реальной Ollama, всё на моках)

```bash
PYTHONPATH=. pytest evm_adapter/tests/ agent/tests/ -v
```

Должно быть 14 пройденных тестов: 8 на evm_adapter (rate limiting, кэш, credits
guard, маскирование ключа, TTL) и 6 на agent (успешный tool call, ошибка внутри
тулзы не роняет цикл, неизвестная тулза, неверные аргументы, защита от
бесконечного цикла, ответ без тулз).

## Ручная проверка против вашей реальной Ollama

```bash
export BLOCKSCOUT_API_KEY="proapi_ваш_ключ"
export OLLAMA_HOST="http://ollama:11434"   # или http://localhost:11434, если не в докере
export ORCHESTRATOR_MODEL="qwen3-coder"    # имя модели как в `ollama list`

PYTHONPATH=. python scripts/manual_check_live_ollama.py
```

Что должно появиться в консоли, если всё работает:
1. `--- Модель запрашивает тулзу: get_address_info_tool(...) ---` — модель реально
   решила вызвать инструмент, а не придумала ответ сама.
2. Строка с реальным JSON-результатом от Blockscout Pro API.
3. Финальный текстовый ответ модели с балансом тестового адреса.

Если строка про вызов тулзы не появилась — модель ответила без tool calling.
Проверьте:
- версию Ollama: `ollama --version` (нужна 0.3.0+);
- что модель реально поддерживает tool calling (не все Qwen-сборки одинаковы);
- что `OLLAMA_HOST` указывает на правильный адрес (внутри docker-сети — имя
  сервиса, не `localhost`).

## Что здесь НЕ реализовано (сознательно, следующие шаги)

- Полноценный Оркестратор (Qwen 3.6) со стратегией по `mode` — сейчас тулзы
  напрямую подключены к Qwen3-Coder для проверки механики вызовов.
- Валидатор циклов как отдельная чистая функция — сейчас есть только грубый
  `max_turns` как предохранитель от зависания.
- Attribution Engine не проверен на реальных внешних API (см. раздел выше) —
  только на моках/фрагментах, нужен ручной прогон `manual_check_attribution.py`.
- Behavior & Clustering Engine (субагент 4) — сознательное упрощение на
  первую версию: планируется через SQL-эвристики к `evidence_log`, БЕЗ
  Neo4j/GraphSense (открытый вопрос №2 архитектуры остаётся открытым).
- Flow & Hop Tracer (субагент 2) — разрешение свопов/мостов через TheGraph.
- Rolling Dump, эскалация по off-ramp/санкциям.
- BTC- и TRON-адаптеры (по тому же паттерну, что и evm_adapter).
- Persistent путь для `cache.db` — сейчас файл создаётся рядом со скриптом;
  для докера стоит явно указать `Cache(db_path="/app/cache_data/cache.db")`
  и примонтировать том (см. `docker-compose.yml`).
