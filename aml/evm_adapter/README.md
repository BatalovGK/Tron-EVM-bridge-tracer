# EVM Adapter for Blockscout Pro API

Модуль предназначен для унифицированного взаимодействия с EVM-совместимыми блокчейнами
через интерфейс Blockscout Pro API.

## Возможности

- Полная изоляция от специфики сетей (переключение только через `chain_id`).
- Автоматическая валидация поддерживаемых сетей через `chains.yaml`.
- **Честный time-based rate limiting** на алгоритме token bucket (`asyncio.Lock` +
  `time.monotonic()`), настраиваемый лимит запросов в секунду (`rate_limit`,
  по умолчанию 5 — под бесплатный тир).
- **Проактивный guard по дневным кредитам**: `CreditsExhaustedError` выбрасывается
  до сетевого запроса, если `credits_remaining <= 0`.
- **Маскирование API-ключа** в логах и текстах исключений (`_mask_text`).
- **Неблокирующий кэш** на `aiosqlite` — не тормозит event loop при bulk-обработке
  сотен адресов.
- Строгое разделение TTL: `0` (вечно) для неизменяемых данных (транзакции, блоки),
  900 секунд для динамических списков (переводы, список транзакций адреса).
- Единая точка сборки метаданных (`_meta`: `chain_id`, `fetched_at`, `source`,
  `cached`) для провенанса каждого ответа.

## Установка и требования

```bash
pip install aiohttp aiosqlite pyyaml pytest pytest-asyncio
export BLOCKSCOUT_API_KEY="proapi_your_secret_key"
```

## Использование в коде

```python
import asyncio
from evm_adapter import get_address_info, get_token_transfers, CreditsExhaustedError

async def main():
    info = await get_address_info(chain_id=1, address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    print(info)

    try:
        transfers = await get_token_transfers(
            chain_id=56,
            address="0x...",
            token_standard="ERC-20",
            limit=50
        )
    except CreditsExhaustedError:
        print("Дневные кредиты Blockscout Pro API исчерпаны, дождитесь сброса лимита")

asyncio.run(main())
```

Настройка лимита запросов в секунду под платный тариф:

```python
from evm_adapter import BlockscoutClient

client = BlockscoutClient(rate_limit=30)  # для тарифа Pro (30 RPS)
```

## Тестирование

```bash
pytest evm_adapter/tests/test_adapter.py -v
```

Тесты покрывают: базовые сценарии адаптера (cache hit/miss, валидация chain_id),
вечный TTL для блоков, проактивный guard по кредитам, соблюдение time-based
rate limit, маскирование ключа в текстах ошибок.
