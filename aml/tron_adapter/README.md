# Tron Adapter for TronGrid API

Клиент к TronGrid API (Tron mainnet) — по духу аналогичен `evm_adapter`
(кэш, rate limiting, провенанс), но под особенности Tron: одна сеть (нет
`chain_id`/`chains.yaml`), ключ опционален (нет модели "кредитов", есть
только QPS-троттлинг), три несовместимых формата адресов (см. `address.py`).

## Возможности

- **Конвертация адресов** (`address.py`): base58 ("T...") <-> Tron hex
  (0x41 + 20 байт) <-> "голый" EVM-style hex без версионного байта (именно
  в этом формате LayerZero Scan API отдаёт Tron-адреса — расхождение
  форматов подтверждено живым сравнением в этой сессии, см. докстринг
  модуля). Все публичные функции адаптера принимают адрес в любом из
  форматов и сами приводят к base58, который ожидает TronGrid.
- **Rate limiting** — тот же token bucket, что в `evm_adapter`, но БЕЗ
  подстройки под заголовки ответа: TronGrid не отдаёт `x-ratelimit-*`
  (проверено живым запросом) и официальная документация прямо просит не
  зашивать конкретные цифры лимита в код — `rate_limit` здесь консервативное
  локальное значение по умолчанию, а не воспроизведение официальной цифры.
- **Ключ опционален** — `TRONGRID_API_KEY` не обязателен (в отличие от
  `BLOCKSCOUT_API_KEY`), без него запросы просто медленнее из-за более
  жёсткого публичного лимита.
- **Неблокирующий кэш** на `aiosqlite`, короткий TTL (60с) для списков
  переводов — это динамические данные, в отличие от `evm_adapter`, где
  TTL для похожих ручек длиннее (там это менее срочно для AML-разбора).
- Единая точка сборки метаданных (`_meta`: `fetched_at`, `source`, `cached`).

## Установка и требования

```bash
pip install aiohttp aiosqlite pytest pytest-asyncio
export TRONGRID_API_KEY="ваш_ключ"   # опционально
```

## Использование в коде

```python
import asyncio
from tron_adapter import get_trc20_transfers, close_client

async def main():
    # Адрес можно передать и в base58, и в hex-формате (в т.ч. как отдаёт
    # LayerZero Scan API для Tron) — адаптер сам нормализует.
    transfers = await get_trc20_transfers(
        address="TFG4wBaDQ8sHWWP1ACeSGnoNR6RRzevLPt",
        only_from=True,
        limit=50,
    )
    for tx in transfers["data"]:
        if tx["type"] == "Transfer":
            print(tx["from"], "->", tx["to"], tx["value"], tx["token_info"]["symbol"])

    await close_client()

asyncio.run(main())
```

## Тестирование

```bash
pytest tron_adapter/tests/test_adapter.py -v
```
