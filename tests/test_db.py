import sqlite3
from pathlib import Path

import pytest

from penta.services.db import PentaDB


class TestAppendAndQuery:
    async def test_append_returns_rowid(self, memory_db: PentaDB):
        row_id = await memory_db.append_message("User", "hello")
        assert row_id == 1

    async def test_append_increments(self, memory_db: PentaDB):
        id1 = await memory_db.append_message("User", "first")
        id2 = await memory_db.append_message("Claude", "second")
        assert id2 > id1

    async def test_get_messages_empty(self, memory_db: PentaDB):
        assert await memory_db.get_messages() == []

    async def test_get_messages_returns_ordered(self, memory_db: PentaDB):
        await memory_db.append_message("User", "one")
        await memory_db.append_message("Claude", "two")
        await memory_db.append_message("Codex", "three")
        msgs = await memory_db.get_messages()
        assert len(msgs) == 3
        assert msgs[0][1] == "User"
        assert msgs[0][2] == "one"
        assert msgs[2][1] == "Codex"
        assert msgs[2][2] == "three"

    async def test_get_messages_respects_limit(self, memory_db: PentaDB):
        for i in range(10):
            await memory_db.append_message("User", f"msg-{i}")
        msgs = await memory_db.get_messages(limit=3)
        assert len(msgs) == 3
        # Should be the 3 most recent, oldest first
        assert msgs[0][2] == "msg-7"
        assert msgs[2][2] == "msg-9"

    async def test_timestamp_is_iso(self, memory_db: PentaDB):
        await memory_db.append_message("User", "hi")
        msgs = await memory_db.get_messages()
        ts = msgs[0][3]
        assert "T" in ts  # ISO format
        assert "+" in ts or "Z" in ts or ts.endswith("+00:00")


class TestCompact:
    async def test_compact_trims(self, memory_db: PentaDB):
        for i in range(20):
            await memory_db.append_message("User", f"msg-{i}")
        await memory_db.compact(max_messages=5)
        msgs = await memory_db.get_messages()
        assert len(msgs) == 5
        assert msgs[0][2] == "msg-15"
        assert msgs[4][2] == "msg-19"

    async def test_compact_noop_when_under_limit(self, memory_db: PentaDB):
        for i in range(3):
            await memory_db.append_message("User", f"msg-{i}")
        await memory_db.compact(max_messages=10)
        assert len(await memory_db.get_messages()) == 3


class TestSessions:
    async def test_save_and_load(self, memory_db: PentaDB):
        await memory_db.save_session("Claude", "abc-123")
        assert await memory_db.load_session("Claude") == "abc-123"

    async def test_load_missing_returns_none(self, memory_db: PentaDB):
        assert await memory_db.load_session("Codex") is None

    async def test_save_overwrites(self, memory_db: PentaDB):
        await memory_db.save_session("Claude", "old")
        await memory_db.save_session("Claude", "new")
        assert await memory_db.load_session("Claude") == "new"


# -- File-based tests (MCP external-change detection) -------------------------


@pytest.fixture
async def file_db(tmp_path: Path):
    db = PentaDB(tmp_path / "test-project", storage_root=tmp_path)
    await db.connect()
    yield db
    await db.close()


class TestExternalChanges:
    async def test_no_changes_returns_empty(self, file_db: PentaDB):
        assert await file_db.check_external_changes() == []

    async def test_own_writes_not_detected(self, file_db: PentaDB):
        await file_db.append_message("User", "own write")
        assert await file_db.check_external_changes() == []

    async def test_external_write_detected(self, file_db: PentaDB):
        # Simulate an external write via a second connection
        await file_db.append_message("User", "setup")  # Establish baseline
        assert file_db._db_path is not None
        ext_conn = sqlite3.connect(file_db._db_path)
        ext_conn.execute("PRAGMA journal_mode=WAL")
        ext_conn.execute(
            "INSERT INTO messages (conversation_id, sender, text, timestamp) VALUES (?, ?, ?, ?)",
            (file_db.conversation_id, "MCP-Agent", "external msg", "2026-01-01T00:00:00+00:00"),
        )
        ext_conn.commit()
        ext_conn.close()

        rows = await file_db.check_external_changes()
        assert len(rows) == 1
        assert rows[0][1] == "MCP-Agent"
        assert rows[0][2] == "external msg"

    async def test_external_changes_only_returns_new(self, file_db: PentaDB):
        await file_db.append_message("User", "setup")
        assert file_db._db_path is not None
        ext_conn = sqlite3.connect(file_db._db_path)
        ext_conn.execute("PRAGMA journal_mode=WAL")
        ext_conn.execute(
            "INSERT INTO messages (conversation_id, sender, text, timestamp) VALUES (?, ?, ?, ?)",
            (file_db.conversation_id, "Agent", "ext-1", "2026-01-01T00:00:00+00:00"),
        )
        ext_conn.commit()
        ext_conn.close()

        rows1 = await file_db.check_external_changes()
        assert len(rows1) == 1

        # Second check with no new writes
        rows2 = await file_db.check_external_changes()
        assert len(rows2) == 0


class TestDbPath:
    def test_path_is_deterministic(self, tmp_path: Path):
        db1 = PentaDB(tmp_path / "myproject", storage_root=tmp_path)
        db2 = PentaDB(tmp_path / "myproject", storage_root=tmp_path)
        assert db1._db_path == db2._db_path

    def test_different_dirs_get_different_paths(self, tmp_path: Path):
        db1 = PentaDB(tmp_path / "project-a", storage_root=tmp_path)
        db2 = PentaDB(tmp_path / "project-b", storage_root=tmp_path)
        assert db1._db_path != db2._db_path
