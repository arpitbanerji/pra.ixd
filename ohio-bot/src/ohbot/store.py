"""SQLite-backed state: chat -> conversation mapping and update de-duplication.

State lives on a mounted volume so restarting the container does not lose the
conversation a chat is attached to, and so a Telegram update that has already
been handled is never executed twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    chat_id         INTEGER PRIMARY KEY,
    conversation_id TEXT    NOT NULL,
    backend         TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_updates (
    update_id INTEGER PRIMARY KEY,
    seen_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Session:
    chat_id: int
    conversation_id: str
    backend: str


class Store:
    """Async SQLite store. Use as an async context manager."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "Store":
        path = Path(self._path)
        if path.parent and str(path.parent) not in ("", "."):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                raise RuntimeError(
                    f"cannot create the database directory {path.parent!s}: {exc}. "
                    "If you are running in Docker this is usually a volume owned by "
                    "root; run `docker compose down -v` to recreate it, or set DB_PATH "
                    "to a writable location such as /tmp/ohbot.sqlite3."
                ) from exc
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store is not open; use 'async with Store(...)'")
        return self._db

    # ---- sessions --------------------------------------------------------
    async def get_session(self, chat_id: int) -> Session | None:
        async with self._conn.execute(
            "SELECT chat_id, conversation_id, backend FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return Session(
            chat_id=row["chat_id"],
            conversation_id=row["conversation_id"],
            backend=row["backend"],
        )

    async def set_session(self, chat_id: int, conversation_id: str, backend: str) -> Session:
        now = _now()
        await self._conn.execute(
            """
            INSERT INTO sessions (chat_id, conversation_id, backend, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                backend         = excluded.backend,
                updated_at      = excluded.updated_at
            """,
            (chat_id, conversation_id, backend, now, now),
        )
        await self._conn.commit()
        return Session(chat_id=chat_id, conversation_id=conversation_id, backend=backend)

    async def clear_session(self, chat_id: int) -> None:
        await self._conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
        await self._conn.commit()

    # ---- update de-duplication ------------------------------------------
    async def mark_update_seen(self, update_id: int) -> bool:
        """Return True the first time an update is seen, False on a replay."""
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO seen_updates (update_id, seen_at) VALUES (?, ?)",
            (update_id, _now()),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def prune_seen_updates(self, keep_days: int = 3) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cursor = await self._conn.execute(
            "DELETE FROM seen_updates WHERE seen_at < ?", (cutoff,)
        )
        await self._conn.commit()
        return cursor.rowcount

    # ---- generic key/value ----------------------------------------------
    async def get_kv(self, key: str) -> str | None:
        async with self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_kv(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()
