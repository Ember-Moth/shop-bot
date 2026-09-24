"""任务仓储：持久化任务领取/回写、业务广播与钱包通知投递记录。"""

from __future__ import annotations

import time
from typing import Any

from ...models import Order
from ..mappers import row_to_order
from ..work_queue import WorkItem
from .base import Repository


class WorkRepository(Repository):
    async def claim_work(self, kind: str) -> WorkItem | None:
        async with self._db.connection() as conn:
            async with conn.execute(
                """UPDATE work_items SET claimed = 1 WHERE id = (
                    SELECT id FROM work_items WHERE kind = ? AND claimed = 0 AND due_at <= ?
                    ORDER BY due_at, id LIMIT 1
                ) RETURNING id, kind, entity_id, attempts, revision""",
                (kind, time.time()),
            ) as cur:
                row = await cur.fetchone()
        return WorkItem(**dict(row)) if row else None

    async def finish_work(self, item: WorkItem, *, done: bool = False, delay: float = 5) -> None:
        """版本条件避免抹掉处理期间新产生的重绑/补发任务。"""
        async with self._db.transaction() as conn:
            if done:
                await conn.execute("DELETE FROM work_items WHERE id = ? AND revision = ?", (item.id, item.revision))
            else:
                await conn.execute(
                    """UPDATE work_items SET due_at = MAX(due_at, ?), attempts = attempts + 1
                    WHERE id = ? AND revision = ?""",
                    (time.time() + delay, item.id, item.revision),
                )
            await conn.execute("UPDATE work_items SET claimed = 0 WHERE id = ?", (item.id,))

    async def list_recovery_orders(self) -> list[Order]:
        """兼容管理/测试入口；运行时调度只领取有限数量的 ID。"""
        rows = await self._db.fetch_all("""
            SELECT * FROM orders WHERE id IN (
                SELECT entity_id FROM work_items WHERE kind IN ('purchase', 'delivery')
            ) ORDER BY id
        """)
        return [row_to_order(row) for row in rows]

    async def configure_business_notifications(self, routes: dict[str, list[int]]) -> None:
        """启动时应用路由；撤销的收件人停止收到积压消息，重新启用不回放旧事件。"""
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM business_routes")
            await conn.executemany(
                "INSERT INTO business_routes (event, chat_id) VALUES (?, ?)",
                [(event, chat_id) for event, chat_ids in routes.items() for chat_id in dict.fromkeys(chat_ids)],
            )
            await conn.execute("""UPDATE business_deliveries SET state = 'skipped'
                WHERE state = 'pending' AND NOT EXISTS (
                    SELECT 1 FROM business_routes r
                    WHERE r.event = business_deliveries.event AND r.chat_id = business_deliveries.chat_id
                )""")
            await conn.execute("""DELETE FROM work_items WHERE kind = 'business' AND EXISTS (
                SELECT 1 FROM business_deliveries d WHERE d.id = work_items.entity_id AND d.state != 'pending'
            )""")

    async def get_business_delivery(self, delivery_id: int) -> dict[str, Any] | None:
        row = await self._db.fetch_one("SELECT * FROM business_deliveries WHERE id = ?", (delivery_id,))
        return dict(row) if row else None

    async def mark_business_sent(self, delivery_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """UPDATE business_deliveries SET state = 'sent', sent_at = datetime('now')
                WHERE id = ? AND state = 'pending'""",
                (delivery_id,),
            )

    async def get_wallet_notification(self, transaction_id: int) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            """SELECT t.*, u.telegram_id FROM balance_transactions t
            JOIN users u ON u.id = t.user_id WHERE t.id = ?""",
            (transaction_id,),
        )
        return dict(row) if row else None
