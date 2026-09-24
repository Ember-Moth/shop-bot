"""aiogram FSM 状态的 SQLite 持久化存储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.storage.base import BaseStorage, StorageKey

from .core import Database


class FSMStorage(BaseStorage):
    """SQLite 持久化 FSM 存储，bot 重启后对话状态不丢。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _key_tuple(self, key: StorageKey) -> tuple:
        return (
            key.bot_id,
            key.chat_id,
            key.user_id,
            key.thread_id,
            key.business_connection_id,
            key.destiny,
        )

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        state_str = None
        if state is not None:
            state_str = state if isinstance(state, str) else state.state

        async with self._db.transaction() as conn:
            # 先尝试 INSERT，已存在则忽略；再 UPDATE，保证并发安全
            await conn.execute(
                """
                INSERT OR IGNORE INTO fsm_state
                    (bot_id, chat_id, user_id, thread_id, business_connection_id, destiny, state, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
                """,
                (*self._key_tuple(key), state_str),
            )
            await conn.execute(
                """
                UPDATE fsm_state SET state = ?, updated_at = datetime('now')
                WHERE bot_id = ? AND chat_id = ? AND user_id = ?
                  AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
                """,
                (state_str, *self._key_tuple(key)),
            )

    async def get_state(self, key: StorageKey) -> str | None:
        async with (
            self._db.connection() as conn,
            conn.execute(
                """
            SELECT state FROM fsm_state
            WHERE bot_id = ? AND chat_id = ? AND user_id = ?
              AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
            """,
                self._key_tuple(key),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        data_json = json.dumps(dict(data))

        async with self._db.transaction() as conn:
            # 先尝试 INSERT，已存在则忽略；再 UPDATE，保证并发安全
            await conn.execute(
                """
                INSERT OR IGNORE INTO fsm_state
                    (bot_id, chat_id, user_id, thread_id, business_connection_id, destiny, state, data)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (*self._key_tuple(key), data_json),
            )
            await conn.execute(
                """
                UPDATE fsm_state SET data = ?, updated_at = datetime('now')
                WHERE bot_id = ? AND chat_id = ? AND user_id = ?
                  AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
                """,
                (data_json, *self._key_tuple(key)),
            )

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with (
            self._db.connection() as conn,
            conn.execute(
                """
            SELECT data FROM fsm_state
            WHERE bot_id = ? AND chat_id = ? AND user_id = ?
              AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
            """,
                self._key_tuple(key),
            ) as cur,
        ):
            row = await cur.fetchone()
        return json.loads(row["data"]) if row else {}

    async def close(self) -> None:
        # 存储不持有连接，由 Database 统一管理
        pass
