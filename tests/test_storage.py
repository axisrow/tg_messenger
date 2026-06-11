"""Storage — stdlib sqlite3 behind asyncio.to_thread, WAL, versioned migrations, kv helpers.

Everything runs on tmp paths (no network, no real ~/.tg_messenger). Concurrency is
exercised with asyncio.gather, never a real sleep. The cache does NOT live here —
it stays in-memory (#8); storage is the persistence layer for moderator rules,
suggester style profiles, heartbeat schedules, action logs (#16/#17/#19).
"""

from __future__ import annotations

import asyncio

import pytest

from tg_messenger.core.storage import Storage, default_db_path

# --- цикл 67: connect/close + kv ---

async def test_connect_creates_file(tmp_path):
    db = tmp_path / "test.db"
    storage = Storage(db)
    await storage.connect()
    try:
        assert db.exists()
    finally:
        await storage.close()


async def test_kv_roundtrip_str(tmp_path):
    async with Storage(tmp_path / "t.db") as storage:
        await storage.set_value("greeting", "hello")
        assert await storage.get_value("greeting") == "hello"


async def test_kv_roundtrip_dict_and_list(tmp_path):
    async with Storage(tmp_path / "t.db") as storage:
        await storage.set_value("cfg", {"a": 1, "b": [2, 3]})
        await storage.set_value("items", [1, "two", 3.0])
        assert await storage.get_value("cfg") == {"a": 1, "b": [2, 3]}
        assert await storage.get_value("items") == [1, "two", 3.0]


async def test_get_missing_returns_none(tmp_path):
    async with Storage(tmp_path / "t.db") as storage:
        assert await storage.get_value("nope") is None


async def test_set_value_overwrites(tmp_path):
    async with Storage(tmp_path / "t.db") as storage:
        await storage.set_value("k", "first")
        await storage.set_value("k", "second")
        assert await storage.get_value("k") == "second"


async def test_context_manager_closes(tmp_path):
    storage = Storage(tmp_path / "t.db")
    async with storage:
        await storage.set_value("k", "v")
    # after the context, a fresh connect still reads persisted data
    async with Storage(tmp_path / "t.db") as again:
        assert await again.get_value("k") == "v"


# --- цикл 68: миграции (PRAGMA user_version) ---

async def test_migrations_raise_user_version(tmp_path):
    storage = Storage(tmp_path / "m.db")
    storage.register_migrations([
        "CREATE TABLE a (id INTEGER PRIMARY KEY)",
        "CREATE TABLE b (id INTEGER PRIMARY KEY)",
    ])
    await storage.connect()
    try:
        assert await storage.user_version() == 2
    finally:
        await storage.close()


async def test_migrations_not_reapplied(tmp_path):
    db = tmp_path / "m.db"
    migs = ["CREATE TABLE a (id INTEGER PRIMARY KEY)"]
    async with Storage(db) as s:
        s.register_migrations(migs)
        await s._apply_pending_migrations()  # idempotent re-run path
    # reconnecting applies nothing new — no "table already exists" error
    s2 = Storage(db)
    s2.register_migrations(migs)
    await s2.connect()
    try:
        assert await s2.user_version() == 1
    finally:
        await s2.close()


async def test_two_consumers_migrations_in_order(tmp_path):
    storage = Storage(tmp_path / "m.db")
    storage.register_migrations(["CREATE TABLE a (id INTEGER PRIMARY KEY)"])
    storage.register_migrations(["CREATE TABLE b (id INTEGER PRIMARY KEY)"])
    await storage.connect()
    try:
        # both tables exist, version reflects the total
        await storage.execute("INSERT INTO a (id) VALUES (1)")
        await storage.execute("INSERT INTO b (id) VALUES (1)")
        assert await storage.user_version() == 2
    finally:
        await storage.close()


async def test_failing_migration_rolls_back(tmp_path):
    storage = Storage(tmp_path / "m.db")
    storage.register_migrations([
        "CREATE TABLE a (id INTEGER PRIMARY KEY)",
        "THIS IS NOT VALID SQL",
    ])
    with pytest.raises(Exception):
        await storage.connect()
    await storage.close()
    # version did not advance past the good migration's failure boundary
    s2 = Storage(tmp_path / "m.db")
    await s2.connect()
    try:
        # the whole batch rolled back → version is 0 (nothing committed)
        assert await s2.user_version() == 0
        assert await s2.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'a'"
        ) == []
    finally:
        await s2.close()


# --- цикл 69: конкурентность ---

async def test_concurrent_set_get_no_loss(tmp_path):
    async with Storage(tmp_path / "c.db") as storage:
        async def writer(i):
            await storage.set_value(f"k{i}", i)

        await asyncio.gather(*(writer(i) for i in range(20)))
        results = await asyncio.gather(*(storage.get_value(f"k{i}") for i in range(20)))
        assert results == list(range(20))


async def test_execute_fetchone_fetchall(tmp_path):
    async with Storage(tmp_path / "q.db") as storage:
        storage.register_migrations(["CREATE TABLE t (id INTEGER, name TEXT)"])
        await storage._apply_pending_migrations()
        await storage.execute("INSERT INTO t (id, name) VALUES (?, ?)", (1, "a"))
        await storage.execute("INSERT INTO t (id, name) VALUES (?, ?)", (2, "b"))
        row = await storage.fetchone("SELECT name FROM t WHERE id = ?", (1,))
        assert row[0] == "a"
        rows = await storage.fetchall("SELECT id FROM t ORDER BY id")
        assert [r[0] for r in rows] == [1, 2]


# --- цикл 70: default path per-profile ---

def test_default_db_path_per_profile():
    assert default_db_path("default").name == "default.db"
    assert default_db_path("work").name == "work.db"
    assert default_db_path("work").parent.name == ".tg_messenger"
