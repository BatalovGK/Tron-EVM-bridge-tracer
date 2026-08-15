-- common/sql/schema.sql
-- Схема PostgreSQL для AML/форензик-платформы (раздел 4 и 5 архитектуры).
-- Пять смысловых блоков:
--   1. label_cache   — кэш ВНЕШНИХ меток (OFAC/OpenSanctions/GoPlus) с провенансом.
--   2. seed_registry — СВОИ находки (посев меток), отдельно от заимствованных.
--   3. evidence_log  — сырые данные, собранные Execution Layer в ходе расследования.
--   4. verdicts      — финальные вердикты по расследованиям.
--   5. flow_edges    — структурированные рёбра графа переводов (Flow&Hop Tracer,
--                      субагент 2), отдельно от сырых JSONB-блобов evidence_log —
--                      чтобы Behavior & Clustering Engine (субагент 4) мог обходить
--                      граф движения средств обычными SQL join'ами, не парся JSONB
--                      на каждый запрос. Это НЕ замена Neo4j/GraphSense (открытый
--                      вопрос архитектуры №2 по-прежнему открыт) — просто минимально
--                      достаточная структура для SQL-эвристик первой версии.

CREATE TABLE IF NOT EXISTS label_cache (
    id              BIGSERIAL PRIMARY KEY,
    address         TEXT NOT NULL,
    chain_id        INTEGER,                    -- NULL = источник не привязан к конкретной сети (напр. OFAC SDN)
    source          TEXT NOT NULL,               -- 'ofac_sdn' | 'opensanctions' | 'goplus' | 'chainabuse' | ...
    label_type      TEXT NOT NULL,               -- 'sanctioned' | 'mixer' | 'phishing' | 'exchange' | ...
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    raw_data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Обычный UNIQUE(address, chain_id, source, label_type) не сработает как надо:
-- в Postgres NULL никогда не считается равным другому NULL, поэтому строки с
-- chain_id IS NULL (сети-агностичные источники вроде OFAC) не дедуплицировались
-- бы через ON CONFLICT. COALESCE(chain_id, -1) в индексе решает это явно.
CREATE UNIQUE INDEX IF NOT EXISTS uq_label_cache
    ON label_cache (address, (COALESCE(chain_id, -1)), source, label_type);

CREATE INDEX IF NOT EXISTS idx_label_cache_address ON label_cache (address);

CREATE TABLE IF NOT EXISTS seed_registry (
    id              BIGSERIAL PRIMARY KEY,
    address         TEXT NOT NULL,
    chain_id        INTEGER NOT NULL,
    tag             TEXT NOT NULL,               -- произвольная метка аналитика
    seed_source     TEXT NOT NULL,               -- откуда взят посев: 'darknet_market_x' | 'court_doc_2026_123' | ...
    derived_from    TEXT,                        -- адрес-родитель, если это находка от кластеризации, иначе NULL
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    seeded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_by    TEXT,                        -- кто/что подтвердило при кластеризации (аналитик или auto)
    confirmed_at    TIMESTAMPTZ,
    UNIQUE (address, chain_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_seed_registry_address ON seed_registry (address, chain_id);

CREATE TABLE IF NOT EXISTS evidence_log (
    id               BIGSERIAL PRIMARY KEY,
    investigation_id UUID NOT NULL,
    mode             TEXT NOT NULL,              -- 'aml_check' | 'incident_response' | 'wallet_labeling'
    subagent         TEXT NOT NULL,              -- 'network_adapter' | 'flow_hop_tracer' | 'attribution' | 'behavior_clustering'
    chain_id         INTEGER,
    action           TEXT NOT NULL,              -- имя вызванной функции/эндпоинта
    payload          JSONB NOT NULL,             -- сырые данные ответа
    source           TEXT NOT NULL,              -- 'blockscout_pro_api' | 'ofac_sdn' | ...
    cached           BOOLEAN NOT NULL DEFAULT false,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_evidence_log_investigation ON evidence_log (investigation_id);

CREATE TABLE IF NOT EXISTS flow_edges (
    id               BIGSERIAL PRIMARY KEY,
    investigation_id UUID NOT NULL,
    chain_id         INTEGER NOT NULL,
    hop_number       INTEGER NOT NULL,           -- 0 = исходный адрес -> первый хоп
    parent_address   TEXT NOT NULL,
    child_address    TEXT NOT NULL,
    tx_hash          TEXT NOT NULL,
    token            TEXT,                       -- адрес токена или 'native'
    -- TEXT, не DOUBLE PRECISION: суммы в wei/minimal units легко превышают
    -- точность double (2^53) уже на суммах порядка 1 ETH и выше — храним сырую
    -- строку без конвертации, чтобы не терять точность (форензик-контекст).
    amount           TEXT,
    edge_kind        TEXT NOT NULL DEFAULT 'transfer',  -- 'transfer' | 'swap' | 'swap_unresolved'
    tainted          BOOLEAN NOT NULL DEFAULT false,     -- poison-метод (только incident_response)
    terminal_reason  TEXT,                       -- NULL пока не терминальный узел, иначе
                                                  -- 'sanctioned' | 'known_exchange' | 'max_hops' | 'max_branch'
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_flow_edges_investigation ON flow_edges (investigation_id);
CREATE INDEX IF NOT EXISTS idx_flow_edges_parent ON flow_edges (parent_address, chain_id);
CREATE INDEX IF NOT EXISTS idx_flow_edges_child ON flow_edges (child_address, chain_id);

CREATE TABLE IF NOT EXISTS verdicts (
    id               BIGSERIAL PRIMARY KEY,
    investigation_id UUID NOT NULL UNIQUE,
    mode             TEXT NOT NULL,
    risk_score       DOUBLE PRECISION NOT NULL,
    narrative        TEXT NOT NULL,
    escalated        BOOLEAN NOT NULL DEFAULT false,
    human_reviewed   BOOLEAN NOT NULL DEFAULT false,
    decided_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
