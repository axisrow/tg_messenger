"""Storage — a small SQLite persistence layer for services above the client.

stdlib ``sqlite3`` only; every call hops to a worker thread via ``asyncio.to_thread``
and is serialised by an ``asyncio.Lock`` (one connection, ``check_same_thread=False``)
so concurrent ``gather`` callers neither race nor deadlock. WAL mode, foreign keys on.

Consumers (moderator #16, suggester #17, heartbeat #19) register their own tables as
**migrations** (versioned by ``PRAGMA user_version``, applied in order inside one
transaction — a failing batch rolls back and the version does not advance). A ``kv``
table with JSON values covers small odds and ends. The TTL read cache does NOT live
here — that stays in-memory (#8); ``client.py`` does not depend on this module.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from tg_messenger.core.names import sanitize_profile_name

# the kv table is always present; consumer migrations start applying on top of it
_KV_MIGRATION = "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"

DEFAULT_DB_DIR = Path.home() / ".tg_messenger"


def default_db_path(profile: str = "default") -> Path:
    """``~/.tg_messenger/<safe-profile>.db`` — one DB file per account profile (#11)."""
    return DEFAULT_DB_DIR / f"{sanitize_profile_name(profile)}.db"


class Storage:
    """Async wrapper over a single SQLite connection (thread-offloaded, lock-serialised)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._migrations: list[str] = []

    def register_migrations(self, statements: list[str]) -> None:
        """Append a consumer's schema migrations; applied in order on ``connect()``."""
        self._migrations.extend(statements)

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._open)
        await self._apply_pending_migrations()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    async def __aenter__(self) -> "Storage":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Storage is not connected — call connect() first")
        return self._conn

    async def _apply_pending_migrations(self) -> None:
        """Apply kv + every registered migration past ``user_version``, in one transaction.

        A failure rolls the whole batch back and leaves ``user_version`` unchanged, so a
        broken migration never half-applies.
        """
        async with self._lock:
            await asyncio.to_thread(self._migrate_sync)

    def _migrate_sync(self) -> None:
        conn = self._require_conn()
        # kv always exists and is NOT versioned (idempotent CREATE IF NOT EXISTS);
        # user_version counts only consumer-registered migrations (1..N).
        conn.execute(_KV_MIGRATION)
        conn.commit()
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        target = len(self._migrations)
        if current >= target:
            return
        try:
            conn.execute("BEGIN")
            for i in range(current, target):
                conn.execute(self._migrations[i])
            # user_version can't be parameterised — target is our own int, not user input
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    async def user_version(self) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: self._require_conn().execute("PRAGMA user_version").fetchone()[0]
            )

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self._lock:
            await asyncio.to_thread(self._execute_sync, sql, params)

    def _execute_sync(self, sql: str, params: tuple) -> None:
        conn = self._require_conn()
        conn.execute(sql, params)
        conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()):
        async with self._lock:
            return await asyncio.to_thread(
                lambda: self._require_conn().execute(sql, params).fetchone()
            )

    async def fetchall(self, sql: str, params: tuple = ()) -> list:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: self._require_conn().execute(sql, params).fetchall()
            )

    async def set_value(self, key: str, value) -> None:
        """Store a JSON-serialisable value under ``key`` (upsert)."""
        payload = json.dumps(value)
        await self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, payload),
        )

    async def get_value(self, key: str):
        """Return the stored value for ``key`` (JSON-decoded), or None if absent."""
        row = await self.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row[0]) if row is not None else None
