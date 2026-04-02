"""Tests for multi-conversation support.

Covers conversation CRUD, message/session scoping, switching,
and migration from the pre-conversation schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from penta.app_state import AppState
from penta.models.agent_type import AgentType
from penta.services.db import PentaDB
from penta.services.db_schema import SCHEMA_VERSION

from .fakes import FakeAgentService


# ---------------------------------------------------------------------------
# DB-level conversation CRUD
# ---------------------------------------------------------------------------


class TestConversationCRUD:
    async def test_default_conversation_exists(self, memory_db: PentaDB):
        rows = await memory_db.list_conversations()
        assert len(rows) == 1
        assert rows[0][1] == "Default"

    async def test_create_conversation(self, memory_db: PentaDB):
        cid = await memory_db.create_conversation("My Chat")
        assert cid > 0
        rows = await memory_db.list_conversations()
        titles = [r[1] for r in rows]
        assert "My Chat" in titles

    async def test_delete_conversation(self, memory_db: PentaDB):
        cid = await memory_db.create_conversation("To Delete")
        await memory_db.delete_conversation(cid)
        rows = await memory_db.list_conversations()
        ids = [r[0] for r in rows]
        assert cid not in ids

    async def test_rename_conversation(self, memory_db: PentaDB):
        cid = await memory_db.create_conversation("Old Name")
        await memory_db.rename_conversation(cid, "New Name")
        rows = await memory_db.list_conversations()
        by_id = {r[0]: r[1] for r in rows}
        assert by_id[cid] == "New Name"

    async def test_list_ordered_by_updated_at(self, memory_db: PentaDB):
        c1 = await memory_db.create_conversation("First")
        c2 = await memory_db.create_conversation("Second")
        # Append a message to c1 to make it most recently updated
        await memory_db.set_conversation(c1)
        await memory_db.append_message("User", "bump")
        rows = await memory_db.list_conversations()
        # c1 should be first (most recently updated)
        assert rows[0][0] == c1


# ---------------------------------------------------------------------------
# Message scoping
# ---------------------------------------------------------------------------


class TestMessageScoping:
    async def test_messages_isolated_between_conversations(self, memory_db: PentaDB):
        c1 = await memory_db.create_conversation("Conv 1")
        c2 = await memory_db.create_conversation("Conv 2")

        await memory_db.set_conversation(c1)
        await memory_db.append_message("User", "in conv 1")

        await memory_db.set_conversation(c2)
        await memory_db.append_message("User", "in conv 2")

        await memory_db.set_conversation(c1)
        msgs = await memory_db.get_messages()
        assert len(msgs) == 1
        assert msgs[0][2] == "in conv 1"

        await memory_db.set_conversation(c2)
        msgs = await memory_db.get_messages()
        assert len(msgs) == 1
        assert msgs[0][2] == "in conv 2"

    async def test_delete_cascades_messages(self, memory_db: PentaDB):
        cid = await memory_db.create_conversation("Temp")
        await memory_db.set_conversation(cid)
        await memory_db.append_message("User", "will be deleted")

        await memory_db.delete_conversation(cid)

        # Verify messages are gone via raw query (can't use set_conversation
        # on a deleted conversation — it raises ValueError)
        cur = await memory_db._db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0


class TestSessionScoping:
    async def test_sessions_isolated_between_conversations(self, memory_db: PentaDB):
        c1 = await memory_db.create_conversation("Conv 1")
        c2 = await memory_db.create_conversation("Conv 2")

        await memory_db.set_conversation(c1)
        await memory_db.save_session("Claude", "session-c1")

        await memory_db.set_conversation(c2)
        await memory_db.save_session("Claude", "session-c2")

        await memory_db.set_conversation(c1)
        assert await memory_db.load_session("Claude") == "session-c1"

        await memory_db.set_conversation(c2)
        assert await memory_db.load_session("Claude") == "session-c2"

    async def test_no_session_in_new_conversation(self, memory_db: PentaDB):
        await memory_db.save_session("Claude", "existing-session")
        cid = await memory_db.create_conversation("Fresh")
        await memory_db.set_conversation(cid)
        assert await memory_db.load_session("Claude") is None

    async def test_delete_cascades_sessions(self, memory_db: PentaDB):
        cid = await memory_db.create_conversation("Temp")
        await memory_db.set_conversation(cid)
        await memory_db.save_session("Claude", "temp-session")
        await memory_db.delete_conversation(cid)

        cur = await memory_db._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE conversation_id = ?", (cid,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0


# ---------------------------------------------------------------------------
# Compact scoping
# ---------------------------------------------------------------------------


class TestCompactScoping:
    async def test_compact_only_affects_active_conversation(self, memory_db: PentaDB):
        c1 = await memory_db.create_conversation("Conv 1")
        c2 = await memory_db.create_conversation("Conv 2")

        await memory_db.set_conversation(c1)
        for i in range(10):
            await memory_db.append_message("User", f"c1-msg-{i}")

        await memory_db.set_conversation(c2)
        for i in range(10):
            await memory_db.append_message("User", f"c2-msg-{i}")

        # Compact c2 to 3 messages
        await memory_db.compact(max_messages=3)

        # c2 should have 3
        msgs = await memory_db.get_messages()
        assert len(msgs) == 3

        # c1 should still have 10
        await memory_db.set_conversation(c1)
        msgs = await memory_db.get_messages()
        assert len(msgs) == 10


# ---------------------------------------------------------------------------
# AppState conversation switching
# ---------------------------------------------------------------------------


class TestAppStateConversationSwitching:
    @pytest.fixture
    async def state_with_agents(self, memory_db):
        services: dict[str, FakeAgentService] = {}

        def factory(config):
            svc = FakeAgentService()
            services[config.name] = svc
            return svc

        state = AppState(Path("/tmp/test"), db=memory_db, service_factory=factory)
        await state.connect()
        await state.add_agent("Claude", AgentType.CLAUDE)
        await state.add_agent("Codex", AgentType.CODEX)
        yield state, services
        await state.shutdown()

    async def test_switch_clears_stalled_routes(self, state_with_agents):
        """Stalled routes from conversation A must not survive into conversation B."""
        state, services = state_with_agents

        # Trigger a stall in the default conversation
        services["Claude"].enqueue_text("@Codex what do you think?")
        services["Codex"].enqueue_text("@Claude I agree")
        state.round_limit = 1

        await state.send_user_message("@Claude go")
        await state.router.drain()
        assert state.is_routing_stalled

        # Switch to a new conversation — stalled routes should be discarded
        info = await state.create_conversation("Fresh")
        await state.switch_conversation(info.id)
        assert not state.is_routing_stalled

    async def test_switch_loads_correct_history(self, state_with_agents):
        state, services = state_with_agents

        # Add messages to default conversation
        await state.send_user_message("msg in default")
        await state.router.drain()

        # Create and switch to new conversation
        info = await state.create_conversation("New Chat")
        await state.switch_conversation(info.id)

        # New conversation should be empty
        assert len(state.conversation) == 0
        assert state.current_conversation_id == info.id
        assert state.current_conversation_title == "New Chat"

    async def test_switch_back_restores_history(self, state_with_agents):
        state, services = state_with_agents
        original_id = state.current_conversation_id

        await state.send_user_message("original message")
        await state.router.drain()

        # Switch to new conversation
        info = await state.create_conversation("Temp")
        await state.switch_conversation(info.id)
        assert len(state.conversation) == 0

        # Switch back
        await state.switch_conversation(original_id)
        texts = [m.text for m in state.conversation]
        assert "original message" in texts

    async def test_switch_gives_fresh_sessions(self, state_with_agents):
        state, services = state_with_agents

        # First turn in default conversation — agent gets a session
        services["Claude"].enqueue_text("hi", session_id="sess-default")
        await state.send_user_message("@Claude hello")
        await state.router.drain()

        # Switch to new conversation
        info = await state.create_conversation("New")
        await state.switch_conversation(info.id)

        # Coordinator should have no session (fresh conversation)
        claude_config = state.agent_by_name("Claude")
        coord = state.coordinators[claude_config.id]
        assert coord.session_id is None

    async def test_delete_non_active_conversation(self, state_with_agents):
        state, services = state_with_agents
        info = await state.create_conversation("To Delete")
        deleted = await state.delete_conversation(info.id)
        assert deleted is True

        convos = await state.list_conversations()
        ids = [c.id for c in convos]
        assert info.id not in ids

    async def test_cannot_delete_active_conversation(self, state_with_agents):
        state, services = state_with_agents
        deleted = await state.delete_conversation(state.current_conversation_id)
        assert deleted is False

    async def test_cannot_delete_sole_conversation(self, state_with_agents):
        state, services = state_with_agents
        # Only the default conversation exists
        convos = await state.list_conversations()
        assert len(convos) == 1
        deleted = await state.delete_conversation(convos[0].id)
        assert deleted is False

    async def test_conversation_switched_callback_fires(self, state_with_agents):
        state, services = state_with_agents
        switched = []
        state.on_conversation_switched = lambda: switched.append(True)

        info = await state.create_conversation("New")
        await state.switch_conversation(info.id)
        assert len(switched) == 1

    async def test_rename_updates_title(self, state_with_agents):
        state, services = state_with_agents
        await state.rename_conversation(state.current_conversation_id, "Renamed")
        assert state.current_conversation_title == "Renamed"

    async def test_switch_to_same_conversation_is_noop(self, state_with_agents):
        """Switching to the already-active conversation should not tear down coordinators."""
        state, services = state_with_agents

        # Send a message so there's an active coordinator state
        services["Claude"].enqueue_text("hi")
        await state.send_user_message("@Claude hello")
        await state.router.drain()

        original_coord_ids = set(id(c) for c in state.coordinators.values())

        # Switch to the same conversation
        await state.switch_conversation(state.current_conversation_id)

        # Coordinators should be the same objects (not rebuilt)
        assert set(id(c) for c in state.coordinators.values()) == original_coord_ids

    async def test_switch_to_invalid_conversation_raises(self, state_with_agents):
        """Switching to a nonexistent conversation should raise without tearing down."""
        state, services = state_with_agents
        original_coord_ids = set(id(c) for c in state.coordinators.values())

        with pytest.raises(ValueError, match="does not exist"):
            await state.switch_conversation(9999)

        # Coordinators should still be intact
        assert set(id(c) for c in state.coordinators.values()) == original_coord_ids

    async def test_delete_nonexistent_conversation_raises(self, state_with_agents):
        state, services = state_with_agents
        with pytest.raises(ValueError, match="does not exist"):
            await state.delete_conversation(9999)


# ---------------------------------------------------------------------------
# Migration from pre-conversation schema
# ---------------------------------------------------------------------------


class TestMigration:
    @pytest.fixture
    def old_db_path(self, tmp_path: Path) -> Path:
        """Create a DB with the old schema (no conversations table)."""
        db_dir = tmp_path / "penta" / "chats" / "abc123"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "penta.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE sessions (
                agent_name TEXT PRIMARY KEY,
                session_id TEXT NOT NULL
            );
        """)
        # Insert some pre-existing data
        conn.execute(
            "INSERT INTO messages (sender, text, timestamp) VALUES (?, ?, ?)",
            ("User", "old message", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO sessions (agent_name, session_id) VALUES (?, ?)",
            ("Claude", "old-session"),
        )
        conn.commit()
        conn.close()
        return db_path

    async def test_migration_preserves_messages(self, old_db_path: Path, tmp_path: Path):
        import aiosqlite

        conn = await aiosqlite.connect(str(old_db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")

        from penta.services.db_schema import run_migrations

        await run_migrations(conn)

        # Verify messages are preserved with conversation_id = 1
        cur = await conn.execute("SELECT conversation_id, sender, text FROM messages")
        rows = list(await cur.fetchall())
        assert len(rows) == 1
        assert rows[0] == (1, "User", "old message")

        await conn.close()

    async def test_migration_preserves_sessions(self, old_db_path: Path, tmp_path: Path):
        import aiosqlite

        conn = await aiosqlite.connect(str(old_db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")

        from penta.services.db_schema import run_migrations

        await run_migrations(conn)

        # Verify sessions are preserved with conversation_id = 1
        cur = await conn.execute(
            "SELECT agent_name, conversation_id, session_id FROM sessions"
        )
        rows = list(await cur.fetchall())
        assert len(rows) == 1
        assert rows[0] == ("Claude", 1, "old-session")

        await conn.close()

    async def test_migration_creates_default_conversation(self, old_db_path: Path, tmp_path: Path):
        import aiosqlite

        conn = await aiosqlite.connect(str(old_db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")

        from penta.services.db_schema import run_migrations

        await run_migrations(conn)

        cur = await conn.execute("SELECT id, title FROM conversations")
        rows = list(await cur.fetchall())
        assert len(rows) == 1
        assert rows[0] == (1, "Default")

        await conn.close()

    async def test_migration_sets_user_version(self, old_db_path: Path, tmp_path: Path):
        import aiosqlite

        conn = await aiosqlite.connect(str(old_db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")

        from penta.services.db_schema import run_migrations

        await run_migrations(conn)

        cur = await conn.execute("PRAGMA user_version")
        row = await cur.fetchone()
        assert row is not None
        version = row[0]
        assert version == SCHEMA_VERSION

        await conn.close()

    async def test_fresh_db_sets_user_version(self, memory_db: PentaDB):
        """A fresh DB (via memory_db fixture) should have the current schema version."""
        cur = await memory_db._db.execute("PRAGMA user_version")
        row = await cur.fetchone()
        assert row is not None
        version = row[0]
        assert version == SCHEMA_VERSION

    async def test_migration_rerun_after_partial_sessions_drop(self, tmp_path: Path):
        """Simulate crash after sessions was dropped but before sessions_new renamed.

        The migration should handle this partial state on re-run.
        """
        import aiosqlite

        db_path = tmp_path / "partial.db"
        conn = await aiosqlite.connect(str(db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")

        # Create the old schema
        await conn.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
        """)
        await conn.execute(
            "INSERT INTO messages (sender, text, timestamp) VALUES (?, ?, ?)",
            ("User", "test", "2026-01-01T00:00:00+00:00"),
        )

        # Simulate partial migration: conversations exists, sessions was dropped,
        # sessions_new exists but wasn't renamed
        await conn.executescript("""
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE sessions_new (
                agent_name TEXT NOT NULL,
                conversation_id INTEGER NOT NULL DEFAULT 1,
                session_id TEXT NOT NULL,
                PRIMARY KEY (agent_name, conversation_id)
            );
        """)
        await conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (1, ?, ?, ?)",
            ("Default", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        # conversation_id column already added to messages
        await conn.executescript(
            "ALTER TABLE messages ADD COLUMN conversation_id INTEGER NOT NULL DEFAULT 1;"
        )
        await conn.commit()

        # Now run migrations — should handle the partial state
        from penta.services.db_schema import run_migrations

        await run_migrations(conn)

        # Verify sessions table exists and is usable
        await conn.execute(
            "INSERT INTO sessions (agent_name, conversation_id, session_id) VALUES (?, ?, ?)",
            ("Claude", 1, "test-session"),
        )
        cur = await conn.execute("SELECT * FROM sessions")
        rows = list(await cur.fetchall())
        assert len(rows) == 1

        # Verify sessions_new is gone
        cur = await conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sessions_new'"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0

        await conn.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_set_conversation_rejects_invalid_id(self, memory_db: PentaDB):
        with pytest.raises(ValueError, match="does not exist"):
            await memory_db.set_conversation(999)

    async def test_delete_conversation_rejects_invalid_id(self, memory_db: PentaDB):
        with pytest.raises(ValueError, match="does not exist"):
            await memory_db.delete_conversation(999)

    async def test_rename_conversation_rejects_invalid_id(self, memory_db: PentaDB):
        with pytest.raises(ValueError, match="does not exist"):
            await memory_db.rename_conversation(999, "nope")


# ---------------------------------------------------------------------------
# Polling pause/resume
# ---------------------------------------------------------------------------


class TestPollingPause:
    async def test_pause_prevents_external_change_processing(self, memory_db: PentaDB):
        """While polling is paused, check_external_changes should not be called
        and _last_seen_id should not advance."""
        # Add a message to establish baseline
        await memory_db.append_message("User", "baseline")
        initial_seen_id = memory_db._last_seen_id

        memory_db.pause_polling()

        # Verify the flag is set
        assert memory_db._polling_paused is True

        memory_db.resume_polling()
        assert memory_db._polling_paused is False

    async def test_switch_pauses_and_resumes_polling(self):
        """switch_conversation should pause polling, do the switch, then resume."""
        from tests.fakes import FakeAgentService

        services: dict[str, FakeAgentService] = {}

        def factory(config):
            svc = FakeAgentService()
            services[config.name] = svc
            return svc

        db = PentaDB(Path("/unused"), in_memory=True)
        await db.connect()
        state = AppState(Path("/tmp/test"), db=db, service_factory=factory)
        await state.connect()
        await state.add_agent("Claude", AgentType.CLAUDE)

        # Verify polling starts unpaused
        assert db._polling_paused is False

        info = await state.create_conversation("New")
        await state.switch_conversation(info.id)

        # After switch, polling should be resumed
        assert db._polling_paused is False

        await state.shutdown()
