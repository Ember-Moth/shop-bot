"""交付仓储：交付原子落账、历史收敛、撤销旧交付与买家通知进度。"""

from __future__ import annotations

import aiosqlite

from ...models import Order, OrderStatus, PurchaseState
from ..mappers import row_to_order
from .base import Repository


class DeliveryRepository(Repository):
    async def finalize_delivery(
        self,
        order_id: int,
        purchase_id: int,
        *,
        from_purchase_state: PurchaseState,
        upstream_ref: str | None,
        payload: str | None,
        delivery_esims: str | None = None,
    ) -> Order | None:
        """同一事务内交付订单并落采购终态（审计 P2：消除两阶段提交中断窗口）。

        幂等：订单已是 delivered（上次中断只完成订单侧）时仅补齐采购终态。
        """
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            async with conn.execute(
                "SELECT * FROM purchases WHERE id = ? AND order_id = ?", (purchase_id, order_id)
            ) as cur:
                purchase = await cur.fetchone()
            if purchase is None or purchase["state"] != from_purchase_state:
                return None
            if purchase["state"] in (
                PurchaseState.SUBMISSION_UNKNOWN,
                PurchaseState.REJECTED,
                PurchaseState.REFUND_PENDING,
                PurchaseState.REFUNDED,
            ):
                return None
            expected_ref = purchase["upstream_request_id"] or f"STUB-{order_id:06d}"
            if upstream_ref != expected_ref:
                return None
            if purchase["upstream_request_id"] is not None:
                async with conn.execute(
                    "SELECT id FROM purchases WHERE upstream_request_id = ? AND order_id != ?",
                    (upstream_ref, order_id),
                ) as cur:
                    if await cur.fetchone() is not None:
                        return None
            if row["status"] == "delivered":
                # 只收敛同一份已验证交付，不能用旧货品完成新绑定的采购。
                if row["upstream_ref"] != upstream_ref:
                    return None
                await conn.execute(
                    """UPDATE purchases SET state = ?, updated_at = datetime('now')
                    WHERE id = ? AND state = ?""",
                    (PurchaseState.FULFILLED, purchase_id, from_purchase_state),
                )
                return row_to_order(row)
            if row["status"] != "paid":
                return None
            async with conn.execute(
                """UPDATE orders SET status = ?, upstream_ref = ?,
                payload = ?, delivery_esims = ?, updated_at = datetime('now'),
                notification_pending = 1, notification_cursor = 0, notification_retry_at = NULL,
                notification_plan_version = 0
                WHERE id = ? AND status = 'paid' RETURNING *""",
                (OrderStatus.DELIVERED, upstream_ref, payload, delivery_esims, order_id),
            ) as cur:
                updated = await cur.fetchone()
            await conn.execute(
                "INSERT INTO order_events (order_id, from_status, to_status) VALUES (?, ?, ?)",
                (order_id, "paid", "delivered"),
            )
            await conn.execute(
                """UPDATE purchases SET state = ?, updated_at = datetime('now')
                WHERE id = ? AND state = ?""",
                (PurchaseState.FULFILLED, purchase_id, from_purchase_state),
            )
        assert updated is not None
        return row_to_order(updated)

    async def invalidate_delivery(self, conn: aiosqlite.Connection, order: aiosqlite.Row, note: str) -> Order:
        """调用方持有事务：仅撤销旧交付资料，金额、支付交易号与付款事实保持不变。"""
        async with conn.execute(
            """UPDATE orders SET status = 'paid', upstream_ref = NULL, payload = NULL,
            delivery_esims = NULL, notification_cursor = 0, notification_retry_at = NULL, notification_plan_version = 0,
            notification_pending = 0, notified_at = NULL, updated_at = datetime('now')
            WHERE id = ? RETURNING *""",
            (order["id"],),
        ) as cur:
            updated = await cur.fetchone()
        await conn.execute(
            "INSERT INTO order_events (order_id, from_status, to_status, note) VALUES (?, ?, 'paid', ?)",
            (order["id"], order["status"], note),
        )
        assert updated is not None
        return row_to_order(updated)

    async def reconcile_delivery(self, order_id: int) -> Order | None:
        """真实采购的历史恢复：同引用补齐终态；引用改变则重新查询交付，不能信任旧 payload。"""
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
                order = await cur.fetchone()
            if order is None:
                return None
            if order["status"] != OrderStatus.DELIVERED:
                return row_to_order(order)
            async with conn.execute("SELECT * FROM purchases WHERE order_id = ?", (order_id,)) as cur:
                purchase = await cur.fetchone()
            if purchase is None or purchase["state"] in (PurchaseState.SUBMISSION_UNKNOWN, PurchaseState.REJECTED):
                return row_to_order(order)
            upstream_ref = purchase["upstream_request_id"]
            async with conn.execute(
                "SELECT id FROM purchases WHERE upstream_request_id = ? AND order_id != ?",
                (upstream_ref, order_id),
            ) as cur:
                conflict = await cur.fetchone()
            if not upstream_ref or conflict is not None:
                await conn.execute(
                    """UPDATE purchases SET state = 'submission_unknown',
                    last_error = 'delivery reference missing or shared; manual reconciliation required',
                    updated_at = datetime('now') WHERE id = ?""",
                    (purchase["id"],),
                )
                return row_to_order(order)
            if upstream_ref != order["upstream_ref"]:
                await conn.execute(
                    """UPDATE purchases SET state = 'upstream_pending', last_error = NULL,
                    updated_at = datetime('now') WHERE id = ?""",
                    (purchase["id"],),
                )
                return await self.invalidate_delivery(
                    conn,
                    order,
                    f"delivery reference changed from {order['upstream_ref']} to {upstream_ref}; revalidation required",
                )
            if purchase["state"] != PurchaseState.FULFILLED:
                await conn.execute(
                    """UPDATE purchases SET state = 'fulfilled', last_error = NULL,
                    updated_at = datetime('now') WHERE id = ?""",
                    (purchase["id"],),
                )
            return row_to_order(order)

    async def queue_redelivery(self, order_id: int) -> None:
        # 已在发送时保留 cursor；已完成时归零。短事务不等待网络持有的订单锁。
        async with self._db.transaction() as conn:
            await conn.execute(
                """UPDATE orders SET notification_pending = 1, notification_cursor = 0
                WHERE id = ? AND status = 'delivered' AND notification_pending = 0""",
                (order_id,),
            )

    async def mark_notified(self, order_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """UPDATE orders SET notified_at = datetime('now'), notification_pending = 0,
                notification_retry_at = NULL
                WHERE id = ? AND status IN ('delivered', 'refunded')""",
                (order_id,),
            )

    async def request_notification(self, order_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE orders SET notification_pending = 1, notification_cursor = 0"
                " WHERE id = ? AND status = 'delivered'",
                (order_id,),
            )

    async def prepare_notification(self, order_id: int, version: int) -> Order | None:
        """调用方持有订单锁；新方案一次性重置未完成进度，不触碰已完成通知或货品。"""
        async with self._db.transaction() as conn:
            async with conn.execute(
                """UPDATE orders SET notification_cursor = CASE WHEN notification_plan_version = ?
                    THEN notification_cursor ELSE 0 END, notification_plan_version = ?
                WHERE id = ? AND status = 'delivered' AND notification_pending = 1 RETURNING *""",
                (version, version, order_id),
            ) as cur:
                row = await cur.fetchone()
        return row_to_order(row) if row else None

    async def advance_notification(self, order_id: int, expected_cursor: int) -> bool:
        async with self._db.transaction() as conn:
            async with conn.execute(
                """UPDATE orders SET notification_cursor = notification_cursor + 1
                WHERE id = ? AND status = 'delivered' AND notification_pending = 1
                AND notification_cursor = ? RETURNING id""",
                (order_id, expected_cursor),
            ) as cur:
                return await cur.fetchone() is not None

    async def defer_notification(self, order_id: int, retry_at: float) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE orders SET notification_retry_at = ? WHERE id = ? AND notification_pending = 1",
                (retry_at, order_id),
            )
