# common/db.py
"""
Единая точка подключения к PostgreSQL для всей платформы (evidence log,
Seed Registry, label cache, вердикты). Аналогично common/secrets.py — здесь
собирается DSN и синглтон пула соединений, чтобы не плодить свою логику
подключения в каждом модуле.
"""

import json
import os
from pathlib import Path
from typing import Optional

import asyncpg

from common.secrets import get_secret

SCHEMA_PATH = Path(__file__).parent / "sql" / "schema.sql"

_pool: Optional[asyncpg.Pool] = None


def _build_dsn() -> str:
    """
    DSN собирается в таком порядке приоритета:
    1. POSTGRES_DSN целиком (например, "postgresql://user:pass@host:5432/db").
    2. По кусочкам: POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_HOST /
       POSTGRES_PORT / POSTGRES_DB, с разумными дефолтами под docker-compose
       (host по умолчанию "postgres" — имя сервиса, а не localhost).
    """
    dsn = get_secret("POSTGRES_DSN", required=False)
    if dsn:
        return dsn

    user = get_secret("POSTGRES_USER", required=False) or "aml_platform"
    password = get_secret("POSTGRES_PASSWORD", required=True)
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "aml_platform")

    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


async def get_pool() -> asyncpg.Pool:
    """Возвращает синглтон пула соединений, создавая его при первом вызове.
    Настраивает codec для jsonb/json, чтобы asyncpg отдавал уже распарсенные
    dict/list, а не сырую JSON-строку. Кодировщик использует default=str,
    потому что payload'ы нередко содержат вложенные данные из самой БД
    (например, строки label_cache с datetime в fetched_at), которые иначе
    не сериализуются стандартным json.dumps."""
    global _pool
    if _pool is None:
        def _encode(obj):
            return json.dumps(obj, default=str)

        async def _init_connection(conn):
            await conn.set_type_codec(
                "jsonb", encoder=_encode, decoder=json.loads, schema="pg_catalog"
            )
            await conn.set_type_codec(
                "json", encoder=_encode, decoder=json.loads, schema="pg_catalog"
            )

        _pool = await asyncpg.create_pool(
            dsn=_build_dsn(), min_size=1, max_size=10, init=_init_connection
        )
    return _pool


async def init_schema():
    """Накатывает schema.sql (идемпотентно — все CREATE TABLE IF NOT EXISTS)."""
    pool = await get_pool()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def close_pool():
    """Закрывает пул соединений. Вызывать при завершении работы приложения."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
