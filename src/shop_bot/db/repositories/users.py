"""用户仓储：Telegram 用户档案。"""

from __future__ import annotations

from ...models import User
from ..mappers import row_to_user
from .base import Repository


class UserRepository(Repository):
    async def upsert_user(self, telegram_id: int, username: str | None, display_name: str | None = None) -> User:
        async with self._db.transaction() as conn:
            async with conn.execute(
                """INSERT INTO users (telegram_id, username, display_name) VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username,
                display_name = excluded.display_name RETURNING *""",
                (telegram_id, username, display_name),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return row_to_user(row)

    async def get_user(self, user_id: int) -> User | None:
        row = await self._db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
        return row_to_user(row) if row else None

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        row = await self._db.fetch_one("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        return row_to_user(row) if row else None

    async def search_users(self, query: str) -> list[User]:
        """按 TG ID / 用户名 / 昵称模糊检索（管理员用，上限 20）。"""
        like = f"%{query}%"
        rows = await self._db.fetch_all(
            """SELECT * FROM users
            WHERE CAST(telegram_id AS TEXT) = ? OR username LIKE ? OR display_name LIKE ?
            ORDER BY id LIMIT 20""",
            (query, like, like),
        )
        return [row_to_user(r) for r in rows]

    async def refresh_user_profile(self, telegram_id: int, username: str | None, display_name: str | None) -> None:
        """已注册用户的资料刷新（改名/改昵称）；未注册则忽略（由 /start 建档）。"""
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE users SET username = ?, display_name = ? WHERE telegram_id = ?",
                (username, display_name, telegram_id),
            )
