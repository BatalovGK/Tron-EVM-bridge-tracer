# aml/ — сетевые адаптеры, переиспользуемые bridge_tracer.py

Эта директория раньше содержала более широкий прототип AML-платформы
(Attribution & Labeling Engine, Flow & Hop Tracer, agent tool-calling поверх
Ollama, PostgreSQL-хранилище evidence log/label cache/seed registry). Для
сдачи тестового задания BitOK (Cross-Chain Bridge Tracer, см.
`../README.md`) она сужена до того, что реально использует `bridge_tracer.py`
— два чистых сетевых адаптера и общая точка чтения секретов. Ничего не
потеряно безвозвратно: полная платформа (`attribution/`, `flow_tracer/`,
`agent/`, `common/db.py`, `common/evidence_store.py` и их тесты) продолжает
жить в отдельном, не связанном с этим репозиторием проекте.

## Структура

```
common/
  secrets.py         — единая точка чтения API-ключей (env / Docker secrets / .env)
evm_adapter/          — клиент к Blockscout Pro API (rate limiting, async-кэш, credits guard)
  tests/              — офлайн-тесты на моках
tron_adapter/         — клиент к TronGrid API + утилиты адресов Tron (base58/hex)
  tests/              — офлайн-тесты на моках
requirements.txt
```

Ключи (`BLOCKSCOUT_API_KEY`, `TRONGRID_API_KEY`) и их формат — см. `.env.example`
в корне репозитория, отдельного `.env.example` здесь больше нет (совпадал бы
с корневым один в один после сужения scope).

## Откуда берутся API-ключи

Единая точка — `common/secrets.py`. Ключ ищется в таком порядке:
1. `<ИМЯ>_FILE` — путь к файлу (Docker secrets).
2. `<ИМЯ>` — обычная переменная окружения (например, из `.env` через
   `python-dotenv`, который `common/secrets.py` подхватывает автоматически
   при импорте).

## Установка

```bash
pip install -r requirements.txt --break-system-packages
```

## Тесты (полностью офлайн, без внешней инфраструктуры)

```bash
pytest evm_adapter/tests/ tron_adapter/tests/ -v
```

Оба адаптера покрыты тестами на моках (rate limiting, дисковый кэш, credits
guard, маскирование ключей в логах, нормализация адресов) — реальная сеть и
БД не нужны. Актуальное число тестов и живая проверка на реальных API — см.
`../README.md`.
