"""Tests for the SQLite session store."""

import pytest

from ohbot.store import Store


@pytest.fixture
async def store(tmp_path):
    async with Store(tmp_path / "state" / "bot.sqlite3") as opened:
        yield opened


async def test_session_round_trip(store):
    assert await store.get_session(42) is None
    await store.set_session(42, "conv-1", "local")
    session = await store.get_session(42)
    assert session is not None
    assert session.conversation_id == "conv-1"
    assert session.backend == "local"


async def test_setting_a_session_twice_updates_in_place(store):
    await store.set_session(42, "conv-1", "local")
    await store.set_session(42, "conv-2", "local")
    session = await store.get_session(42)
    assert session is not None and session.conversation_id == "conv-2"


async def test_clearing_a_session_forgets_the_conversation(store):
    await store.set_session(42, "conv-1", "local")
    await store.clear_session(42)
    assert await store.get_session(42) is None


async def test_updates_are_deduplicated(store):
    assert await store.mark_update_seen(1001) is True
    assert await store.mark_update_seen(1001) is False
    assert await store.mark_update_seen(1002) is True


async def test_sessions_survive_reopening(tmp_path):
    path = tmp_path / "bot.sqlite3"
    async with Store(path) as store:
        await store.set_session(7, "conv-xyz", "cloud")
    async with Store(path) as reopened:
        session = await reopened.get_session(7)
        assert session is not None and session.conversation_id == "conv-xyz"


async def test_kv_get_set(store):
    assert await store.get_kv("offset") is None
    await store.set_kv("offset", "123")
    await store.set_kv("offset", "456")
    assert await store.get_kv("offset") == "456"


async def test_unwritable_directory_reports_an_actionable_error(tmp_path):
    # A root-owned Docker volume surfaces as PermissionError; the message should
    # tell the operator what to do rather than dumping a bare traceback.
    import os

    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")

    blocked = tmp_path / "readonly"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match="cannot create the database directory"):
            async with Store(blocked / "nested" / "bot.sqlite3"):
                pass
    finally:
        blocked.chmod(0o700)
