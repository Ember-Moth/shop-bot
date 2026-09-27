"""付款提示仓储：记录待付款消息的位置，供付款后更新。"""

from __future__ import annotations

from typing import Any

from .base import Repository


class PaymentPromptRepository(Repository):
    async def record(
        self,
        chat_id: int,
        message_id: int,
        *,
        order_id: int | None = None,
        topup_id: int | None = None,
        trade_ref: int | None = None,
    ) -> None:
        """同一条消息重复记录（例如先选渠道、再重新获取收款信息）只更新它当前显示的交易。"""
        async with self._db.transaction() as conn:
            await conn.execute(
                """INSERT INTO payment_prompts (chat_id, message_id, order_id, topup_id, trade_ref)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET trade_ref = excluded.trade_ref""",
                (chat_id, message_id, order_id, topup_id, trade_ref),
            )

    async def for_trade(self, trade_ref: int) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM payment_prompts WHERE trade_ref = ? AND state = 'open' ORDER BY id", (trade_ref,)
        )
        return [dict(row) for row in rows]

    async def clear_trade(self, prompt_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("UPDATE payment_prompts SET trade_ref = NULL WHERE id = ?", (prompt_id,))

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
