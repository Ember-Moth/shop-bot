"""采购仓储：采购记录、状态机转换与人工绑定上游单。"""

from __future__ import annotations

from ...models import OrderStatus, Purchase, PurchaseState
from ..mappers import row_to_purchase
from .base import Repository


class PurchaseRepository(Repository):
    async def ensure_purchase(self, order_id: int, *, request_type: str, sku: str, quantity: int) -> Purchase:
        """按订单建立采购任务（幂等，order_id 唯一）。已存在时原样返回。"""
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM purchases WHERE order_id = ?", (order_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                await conn.execute(
                    "INSERT INTO purchases (order_id, request_type, sku, quantity) VALUES (?, ?, ?, ?)",
                    (order_id, request_type, sku, quantity),
                )
                async with conn.execute("SELECT * FROM purchases WHERE order_id = ?", (order_id,)) as cur:
                    row = await cur.fetchone()
        assert row is not None
        return row_to_purchase(row)

    async def get_purchase_by_order(self, order_id: int) -> Purchase | None:
        row = await self._db.fetch_one("SELECT * FROM purchases WHERE order_id = ?", (order_id,))
        return row_to_purchase(row) if row else None

    async def get_purchase_by_upstream_request_id(self, upstream_request_id: str) -> Purchase | None:
        row = await self._db.fetch_one("SELECT * FROM purchases WHERE upstream_request_id = ?", (upstream_request_id,))
        return row_to_purchase(row) if row else None

    async def get_purchase_conflict(self, order_id: int, upstream_request_id: str) -> Purchase | None:
        row = await self._db.fetch_one(
            "SELECT * FROM purchases WHERE upstream_request_id = ? AND order_id != ?",
            (upstream_request_id, order_id),
        )
        return row_to_purchase(row) if row else None

    async def list_purchases_by_states(self, states: tuple[PurchaseState, ...]) -> list[Purchase]:
        # placeholders 只由 len(states) 生成，无外部输入参与拼接
        placeholders = ",".join("?" for _ in states)
        rows = await self._db.fetch_all(
            f"SELECT * FROM purchases WHERE state IN ({placeholders}) ORDER BY id",  # noqa: S608
            tuple(state.value for state in states),
        )
        return [row_to_purchase(r) for r in rows]

    async def transition_purchase(
        self,
        purchase_id: int,
        to_state: PurchaseState,
        *,
        from_state: PurchaseState | None = None,
        upstream_request_id: str | None = None,
        upstream_order_no: str | None = None,
        last_error: str | None = None,
        set_kyc_documents: str | None = None,
        bump_attempt: bool = False,
    ) -> Purchase | None:
        """采购状态机转换。条件 UPDATE 保证并发下只有一个协程推进成功。

        set_kyc_documents 非 None 时显式覆盖暂存证件（建单前 KYC 流程用）。
        """
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)) as cur:
                row = await cur.fetchone()
            if row is None or (from_state is not None and row["state"] != from_state):
                return None
            attempts = row["attempts"] + (1 if bump_attempt else 0)
            async with conn.execute(
                """UPDATE purchases SET state = ?,
                upstream_request_id = COALESCE(?, upstream_request_id),
                upstream_order_no = COALESCE(?, upstream_order_no),
                last_error = ?, attempts = ?, updated_at = datetime('now'),
                kyc_documents = COALESCE(?, kyc_documents)
                WHERE id = ? RETURNING *""",
                (
                    to_state,
                    upstream_request_id,
                    upstream_order_no,
                    last_error,
                    attempts,
                    set_kyc_documents,
                    purchase_id,
                ),
            ) as cur:
                updated = await cur.fetchone()
        assert updated is not None
        return row_to_purchase(updated)

    async def bind_upstream_request(
        self,
        purchase_id: int,
        *,
        from_state: PurchaseState,
        upstream_request_id: str,
        upstream_order_no: str | None,
    ) -> tuple[Purchase | None, int | None]:
        """人工绑定上游单，事务内复核唯一性并撤销旧交付，等待重新核验货品。

        返回 (绑定后的采购, None) 或 (None, 冲突订单 ID)。
        """
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)) as cur:
                row = await cur.fetchone()
            if row is None or row["state"] != from_state.value:
                return None, None
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (row["order_id"],)) as cur:
                order = await cur.fetchone()
            if order is None or order["status"] not in (OrderStatus.PAID, OrderStatus.DELIVERED):
                return None, None
            async with conn.execute(
                "SELECT order_id FROM purchases WHERE upstream_request_id = ? AND order_id != ?",
                (upstream_request_id, row["order_id"]),
            ) as cur:
                conflict = await cur.fetchone()
            if conflict is not None:
                return None, conflict["order_id"]
            await self._db.deliveries.invalidate_delivery(
                conn,
                order,
                f"admin bound upstream request {upstream_request_id}; "
                f"previous delivery reference {order['upstream_ref']}",
            )
            async with conn.execute(
                """UPDATE purchases SET state = ?, upstream_request_id = ?,
                upstream_order_no = ?, last_error = NULL,
                updated_at = datetime('now') WHERE id = ? AND state = ? RETURNING *""",
                (
                    PurchaseState.UPSTREAM_PENDING,
                    upstream_request_id,
                    upstream_order_no,
                    purchase_id,
                    from_state.value,
                ),
            ) as cur:
                updated = await cur.fetchone()
        if updated is None:
            return None, None
        return row_to_purchase(updated), None
