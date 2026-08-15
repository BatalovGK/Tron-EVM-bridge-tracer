# Cross-Chain Bridge Tracer

MVP-трейсер полного пути транзакции через кросс-чейн мост: TRON → LayerZero
(USDT0) → произвольная EVM-сеть. Тестовое задание на позицию "blockchain
analyst" (BitOK).

Единая точка входа — `bridge_tracer.trace_full_path()`:

```
TronGrid + Bridge Contract Registry (депозит на Tron, ОСНОВНОЙ путь,
    bridge_registry.py), fallback на LayerZero Scan (messages/wallet, если
    TronGrid ничего не нашёл в реестре)
    -> LayerZero Scan (сопоставление входа/выхода моста, включая Legacy Mesh
       с промежуточным хабом Arbitrum, до MAX_BRIDGE_HOPS сообщений подряд)
    -> Blockscout Pro (пост-bridge обход исходящих переводов на EVM-сети)
    -> плоский вердикт (final_status / final_chain / final_address / hops / ...)
```

Почему LayerZero, а не Wormhole/другой мост, разбор всех архитектурных
решений и ограничений (включая два разворота логики детекции депозита за
один день — оба по живым данным) — см.
`BitOK_bridge_tracer_architecture.docx`, раздел 9. Здесь — только
практическое "как запустить и проверить".

## Установка

Требуется Python 3.10+ (разработано и проверено на 3.14).

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env
# впишите в .env реальный BLOCKSCOUT_API_KEY (обязателен для пост-bridge
# обхода на EVM). TRONGRID_API_KEY опционален — TronGrid работает и без
# ключа (публичный тир, просто медленнее), но раз TronGrid теперь основной
# путь детекции депозита (см. .env.example), ключ рекомендуется.
```

## Как запустить

```bash
python3 bridge_tracer.py --start <Tron-адрес отправителя> [--max-hops N]
# или, если уже есть хэш депозитной транзакции на Tron:
python3 bridge_tracer.py --start <tx_hash> --start-type tx_hash
```

Одиночный сопоставитель одного хопа моста (без пост-bridge обхода) можно
проверить отдельно:

```bash
python3 layerzero_tracer.py --tx <хэш транзакции LayerZero>
```

## Как проверить

### 1. Офлайн-тесты (без сети, ~5 секунд)

```bash
pytest -v --ignore=aml/common/tests --ignore=aml/attribution/tests/test_service.py
```

110 тестов должны пройти. Два указанных исключения — тесты `aml/common/`
и `attribution/test_service.py` — идут против настоящего PostgreSQL
(`POSTGRES_DSN`), к трейсеру не относятся и в этом задании не поднимаются;
остальные тесты выполняются полностью офлайн (все внешние API замоканы на
синтетических ответах, повторяющих реальные схемы, проверенные живыми
запросами — см. докстринги модулей и раздел 9.6 архитектурного документа).

### 2. Живая проверка на реальных данных

Единственный надёжный способ убедиться, что интеграция с живыми API
работает — прогнать на реальном Tron-адресе или tx hash, связанном с
переводом USDT0 TRON → EVM:

```bash
python3 bridge_tracer.py --start <реальный Tron-адрес>
```

Результат можно сверить вручную на [layerzeroscan.com](https://layerzeroscan.com)
(тот же API, на котором работает сам официальный LayerZero Scan) и на
Blockscout соответствующей целевой сети.

## Структура

```
bridge_tracer.py           — оркестратор, trace_full_path()
bridge_registry.py         — generic Bridge Contract Registry (адрес -> сеть/
                              протокол/тип/источник+дата), официальные записи
                              из USDT0 Deployments API + эмпирически
                              подтверждённые вручную (pool/router-контракты)
layerzero_tracer.py        — сопоставитель одного сообщения LayerZero
                              (source ↔ destination), декодирование
                              реального получателя из OFT-payload,
                              обнаружение Legacy Mesh Hop 2
usdt0_deployments.py       — живой (с диск-кэшем на 24ч) клиент к
                              официальному USDT0 Deployments API
known_contracts.py         — статический реестр меток (биржи/DEX/мосты)
                              для стоп-условий пост-bridge обхода
test_bridge_tracer.py      — офлайн-тесты trace_full_path() (21 сценарий)
test_layerzero_tracer.py   — офлайн-тесты find_bridge_crossing()
aml/                       — переиспользуемая часть основной AML-платформы
  evm_adapter/                клиент к Blockscout Pro (rate limiting, кэш)
  tron_adapter/                клиент к TronGrid (используется bridge_tracer.py
                                как основной путь детекции депозита — сверка
                                исходящих TRC-20-переводов с bridge_registry.py
                                — плюс утилита base58_to_hex для fallback-пути)
  common/                      secrets.py, db.py, evidence_store.py —
                                общие для всей платформы, не используются
                                bridge_tracer.py напрямую
  attribution/, flow_tracer/, agent/ — остальные подсистемы AML-платформы,
                                вне scope этого задания (см. aml/README.md)
BitOK_bridge_tracer_architecture.docx — архитектурный документ (полная
                              история решения, разделы 1–8 — общий подход
                              к разным типам мостов, раздел 9 — реализация)
briefing_for_claude_code.md — журнал разработки для передачи между сессиями
```

## Известные ограничения (сознательные, MVP)

- **Обнаружение депозита** — двухуровневое: TronGrid сверяется с
  `bridge_registry.py` (официальные OFT-адреса из USDT0 Deployments API +
  небольшой эмпирический список вручную подтверждённых pool/router-
  контрактов); если совпадения нет — fallback на LayerZero Scan
  messages/wallet, который сам ограничен реестром известных Tron-OApp того
  же USDT0 Deployments API (сейчас один — USDT0 OFT на Tron). Депозит через
  ДРУГОЙ протокол/мост LayerZero трейсер не найдёт ни одним из путей.
- **Пост-bridge обход на EVM** — линейный "один хоп = самый ранний подходящий
  по времени исходящий ERC-20-перевод", НЕ amount-aware taint-трейсинг.
  Ограничен одной страницей Blockscout (до 50 переводов) на хоп.
- **Второй мост или DEX-своп на пути не резолвится** — трейсер корректно
  останавливается на нём (`RESTED_AT_CONTRACT`), а не идёт дальше.
- **`aml/evm_adapter/config/chains.yaml` не покрывает все сети**, куда может
  прийти USDT0 (сконфигурированы: Ethereum, BNB Chain, Polygon, Arbitrum,
  Optimism, Base). На неконфигурированной сети `_walk_evm()` падает с понятной
  `ValueError`, не тихо врёт.
- **Не-EVM целевые сети** (Solana, Sui, Aptos, TON и др.) — трейс корректно
  останавливается на точке выхода из моста (`RESTED_AT_ADDRESS`), дальнейший
  обход вне scope MVP (нужен отдельный network adapter под конкретную сеть).
- `aml/tron_adapter.get_transaction_info()` использует, похоже, устаревший
  эндпоинт TronGrid (стабильно отдаёт 404) — не используется bridge_tracer.py,
  зафиксировано на будущее в `briefing_for_claude_code.md`.

## Что не реализовано (сознательно, следующий шаг)

Тонкие обёртки поверх `trace_full_path()` (agent-тулза для локального
оркестратора, MCP-сервер) — не часть этого тестового задания, см.
`briefing_for_claude_code.md`.
