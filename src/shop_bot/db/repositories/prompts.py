"""付款提示仓储：记录待付款消息的位置，供付款后更新。"""

from __future__ import annotations

from typing import Any

from .base import Repository


class PaymentPromptRepository(Repository):
    async def record(
        self, chat_id: int, message_id: int, *, order_id: int | None = None, topup_id: int | None = None
    ) -> None:
        """同一条消息重复记录（例如先选渠道再打开收银台）保持第一条。"""
        async with self._db.transaction() as conn:
            await conn.execute(
                """INSERT OR IGNORE INTO payment_prompts (chat_id, message_id, order_id, topup_id)
                VALUES (?, ?, ?, ?)""",
                (chat_id, message_id, order_id, topup_id),
            )

    async def get_open(self, prompt_id: int) -> dict[str, Any] | None:
        row = await self._db.fetch_one("SELECT * FROM payment_prompts WHERE id = ? AND state = 'open'", (prompt_id,))
        return dict(row) if row else None

    async def close(self, prompt_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("UPDATE payment_prompts SET state = 'closed' WHERE id = ?", (prompt_id,))

    async def forget(self, chat_id: int, message_id: int) -> None:
        """消息已被买家切换成其他页面（如订单列表），付款后不再覆盖它。"""
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE payment_prompts SET state = 'closed' WHERE chat_id = ? AND message_id = ? AND state = 'open'",
                (chat_id, message_id),
            )
