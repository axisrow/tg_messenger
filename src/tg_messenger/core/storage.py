"""Storage — a small SQLite persistence layer for services above the client.

stdlib ``sqlite3`` only; every call hops to the default executor
and is serialised by an ``asyncio.Lock`` (one connection, ``check_same_thread=False``)
held until the worker finishes, even on cancellation. A cancelled write may commit;
cancellation is not a rollback. WAL mode, foreign keys on.

Consumers (moderator #16, suggester #17, heartbeat #19) register their own tables as
**migrations** (tracked by stable statement ids and applied inside one transaction —
a failing batch rolls back and migration metadata does not advance). A ``kv`` table
with JSON values covers small odds and ends. The TTL read cache does NOT live here —
that stays in-memory (#8); ``client.py`` does not depend on this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from pathlib import Path

from tg_messenger.core.names import sanitize_profile_name
from tg_messenger.core.paths import tg_home

logger = logging.getLogger(__name__)

# the kv table is always present; consumer migrations start applying on top of it
_KV_MIGRATION = "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_SCHEMA_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS _tg_messenger_migrations "
    "(id TEXT PRIMARY KEY, statement TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)

def default_db_dir() -> Path:
    """``<tg_home>`` — resolved lazily so ``TG_HOME``/legacy state is honored at runtime."""
    return tg_home()


def default_db_path(profile: str = "default") -> Path:
    """``<tg_home>/<safe-profile>.db`` — one DB file per account profile (#11).

    The root is ``~/.tg/`` (or ``$TG_HOME``, or the legacy ``~/.tg_messenger/``
    when it exists and ``~/.tg/`` does not) — see :func:`tg_home`.
    """
    return default_db_dir() / f"{sanitize_profile_name(profile)}.db"


def __getattr__(name: str):
    # Back-compat: the old module-level ``DEFAULT_DB_DIR`` constant is now lazy.
    if name == "DEFAULT_DB_DIR":
        return default_db_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        async with self._lock:
            if self._conn is not None:
                return
            try:
                await self._run_locked(self._connect_sync)
            except BaseException:
                # __aexit__ is not called if __aenter__ fails or is cancelled.
                await self._run_locked(self._close_sync)
                raise

    def _connect_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        self._migrate_sync()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            conn.close()
            raise
        return conn

    async def close(self) -> None:
        await self._run(self._close_sync)

    def _close_sync(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def _run(self, operation, *args):
        async with self._lock:
            return await self._run_locked(operation, *args)

    async def _run_locked(self, operation, *args):
        """Defer cancellation until the worker ends; caller owns the lock.

        Keep an executor Future, not a Task: asyncio.run's task cancellation
        sweep must not release our lock while the thread still uses SQLite.
        """
        future = asyncio.get_running_loop().run_in_executor(None, operation, *args)
        cancelled = None
        while True:
            try:
                result = await asyncio.shield(future)
                break
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                if cancelled is None:
                    raise
                logger.exception("storage operation failed during cancellation")
                raise cancelled from None
        if cancelled is not None:
            raise cancelled
        return result

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
        """Apply kv + every unapplied registered migration in one transaction.

        A failure rolls the whole batch back and leaves migration metadata unchanged, so
        a broken migration never half-applies.
        """
        await self._run(self._migrate_sync)

    def _migrate_sync(self) -> None:
        conn = self._require_conn()
        # kv always exists and is NOT versioned (idempotent CREATE IF NOT EXISTS);
        # user_version mirrors the applied migration count for easy inspection.
        conn.execute(_KV_MIGRATION)
        conn.execute(_SCHEMA_MIGRATIONS)
        conn.commit()
        applied = {
            row[0]
            for row in conn.execute("SELECT id FROM _tg_messenger_migrations").fetchall()
        }
        pending: list[tuple[str, str]] = []
        seen = set(applied)
        for statement in self._migrations:
            migration_id = self._migration_id(statement)
            if migration_id in seen:
                continue
            seen.add(migration_id)
            pending.append((migration_id, statement))
        if not pending:
            return
        try:
            conn.execute("BEGIN")
            for migration_id, statement in pending:
                conn.execute(statement)
                conn.execute(
                    "INSERT INTO _tg_messenger_migrations (id, statement) VALUES (?, ?)",
                    (migration_id, statement),
                )
            # user_version can't be parameterised — target is our own int, not user input
            conn.execute(f"PRAGMA user_version = {len(seen)}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _migration_id(statement: str) -> str:
        return hashlib.sha256(statement.encode("utf-8")).hexdigest()

    async def user_version(self) -> int:
        return (await self.fetchone("PRAGMA user_version"))[0]

    async def execute(self, sql: str, params: tuple = ()) -> int:
        return await self._run(self._execute_sync, sql, params)

    def _execute_sync(self, sql: str, params: tuple) -> int:
        conn = self._require_conn()
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount

    async def fetchone(self, sql: str, params: tuple = ()):
        return await self._run(lambda: self._require_conn().execute(sql, params).fetchone())

    async def fetchall(self, sql: str, params: tuple = ()) -> list:
        return await self._run(lambda: self._require_conn().execute(sql, params).fetchall())

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
