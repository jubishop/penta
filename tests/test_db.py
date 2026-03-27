import sqlite3
from pathlib import Path

import pytest

from penta.services.db import PentaDB


@pytest.fixture
def db(tmp_path: Path) -> PentaDB:
    return PentaDB(tmp_path / "test-project")


class TestAppendAndQuery:
    def test_append_returns_rowid(self, db: PentaDB):
        row_id = db.append_message("User", "hello")
        assert row_id == 1

    def test_append_increments(self, db: PentaDB):
        id1 = db.append_message("User", "first")
        id2 = db.append_message("Claude", "second")
        assert id2 > id1

    def test_get_messages_empty(self, db: PentaDB):
        assert db.get_messages() == []

    def test_get_messages_returns_ordered(self, db: PentaDB):
        db.append_message("User", "one")
        db.append_message("Claude", "two")
        db.append_message("Codex", "three")
        msgs = db.get_messages()
        assert len(msgs) == 3
        assert msgs[0][1] == "User"
        assert msgs[0][2] == "one"
        assert msgs[2][1] == "Codex"
        assert msgs[2][2] == "three"

    def test_get_messages_respects_limit(self, db: PentaDB):
        for i in range(10):
            db.append_message("User", f"msg-{i}")
        msgs = db.get_messages(limit=3)
        assert len(msgs) == 3
        # Should be the 3 most recent, oldest first
        assert msgs[0][2] == "msg-7"
        assert msgs[2][2] == "msg-9"

    def test_timestamp_is_iso(self, db: PentaDB):
        db.append_message("User", "hi")
        msgs = db.get_messages()
        ts = msgs[0][3]
        assert "T" in ts  # ISO format
        assert "+" in ts or "Z" in ts or ts.endswith("+00:00")


class TestCompact:
    def test_compact_trims(self, db: PentaDB):
        for i in range(20):
            db.append_message("User", f"msg-{i}")
        db.compact(max_messages=5)
        msgs = db.get_messages()
        assert len(msgs) == 5
        assert msgs[0][2] == "msg-15"
        assert msgs[4][2] == "msg-19"

    def test_compact_noop_when_under_limit(self, db: PentaDB):
        for i in range(3):
            db.append_message("User", f"msg-{i}")
        db.compact(max_messages=10)
        assert len(db.get_messages()) == 3


class TestSessions:
    def test_save_and_load(self, db: PentaDB):
        db.save_session("Claude", "abc-123")
        assert db.load_session("Claude") == "abc-123"

    def test_load_missing_returns_none(self, db: PentaDB):
        assert db.load_session("Codex") is None

    def test_save_overwrites(self, db: PentaDB):
        db.save_session("Claude", "old")
        db.save_session("Claude", "new")
        assert db.load_session("Claude") == "new"


class TestExternalChanges:
    def test_no_changes_returns_empty(self, db: PentaDB):
        assert db.check_external_changes() == []

    def test_own_writes_not_detected(self, db: PentaDB):
        db.append_message("User", "own write")
        assert db.check_external_changes() == []

    def test_external_write_detected(self, db: PentaDB):
        # Simulate an external write via a second connection
        db.append_message("User", "setup")  # Establish baseline
        ext_conn = sqlite3.connect(db._db_path)
        ext_conn.execute("PRAGMA journal_mode=WAL")
        ext_conn.execute(
            "INSERT INTO messages (sender, text, timestamp) VALUES (?, ?, ?)",
            ("MCP-Agent", "external msg", "2026-01-01T00:00:00+00:00"),
        )
        ext_conn.commit()
        ext_conn.close()

        rows = db.check_external_changes()
        assert len(rows) == 1
        assert rows[0][1] == "MCP-Agent"
        assert rows[0][2] == "external msg"

    def test_external_changes_only_returns_new(self, db: PentaDB):
        db.append_message("User", "setup")
        ext_conn = sqlite3.connect(db._db_path)
        ext_conn.execute("PRAGMA journal_mode=WAL")
        ext_conn.execute(
            "INSERT INTO messages (sender, text, timestamp) VALUES (?, ?, ?)",
            ("Agent", "ext-1", "2026-01-01T00:00:00+00:00"),
        )
        ext_conn.commit()
        ext_conn.close()

        rows1 = db.check_external_changes()
        assert len(rows1) == 1

        # Second check with no new writes
        rows2 = db.check_external_changes()
        assert len(rows2) == 0


class TestDbPath:
    def test_path_is_deterministic(self, tmp_path: Path):
        db1 = PentaDB(tmp_path / "myproject")
        db2 = PentaDB(tmp_path / "myproject")
        assert db1._db_path == db2._db_path
        db1.close()
        db2.close()

    def test_different_dirs_get_different_paths(self, tmp_path: Path):
        db1 = PentaDB(tmp_path / "project-a")
        db2 = PentaDB(tmp_path / "project-b")
        assert db1._db_path != db2._db_path
        db1.close()
        db2.close()
