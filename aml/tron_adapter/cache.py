# tron_adapter/cache.py
"""SQLite-кэш для tron_adapter — тот же подход, что evm_adapter/cache.py
(aiosqlite, гибкий TTL), но без измерения chain_id в ключе: Tron mainnet —
одна сеть, а не набор конфигурируемых chain_id, как у evm_adapter."""

import aiosqlite
import json
import time
import asyncio
from typing import Optional, Dict, Any
from hashlib import md5


class Cache:
    def __init__(self, db_path: str = "tron_cache.db"):
        self.db_path = db_path
        self._db_initialized = False
        self._init_lock = asyncio.Lock()

    async def _init_db(self):
        if self._db_initialized:
            return
        async with self._init_lock:
            if self._db_initialized:
                return
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        ttl INTEGER NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                await db.commit()
            self._db_initialized = True

    def _get_cache_key(self, endpoint: str, params: Dict[str, Any]) -> str:
        param_str = json.dumps(params, sort_keys=True)
        combined = f"{endpoint}:{param_str}"
        return md5(combined.encode()).hexdigest()

    async def get(self, endpoint: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        await self._init_db()
        key = self._get_cache_key(endpoint, params)

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value, ttl, created_at FROM cache WHERE key = ?", (key,)) as cursor:
                result = await cursor.fetchone()

            if not result:
                return None

            value, ttl, created_at = result
            if ttl != 0 and (time.time() - created_at) > ttl:
                await db.execute("DELETE FROM cache WHERE key = ?", (key,))
                await db.commit()
                return None

        return json.loads(value)

    async def set(self, endpoint: str, params: Dict[str, Any], value: Dict[str, Any], ttl: int):
        await self._init_db()
        key = self._get_cache_key(endpoint, params)
        val_copy = value.copy()
        if "_meta" in val_copy:
            del val_copy["_meta"]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO cache (key, value, ttl, created_at)
                VALUES (?, ?, ?, ?)
            """, (key, json.dumps(val_copy), ttl, time.time()))
            await db.commit()
