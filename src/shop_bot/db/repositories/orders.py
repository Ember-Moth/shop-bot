"""订单仓储：创建（含采购输入快照）、查询、状态转换与审计事件。"""

from __future__ import annotations

from ...models import Order, OrderStatus, Product
from ..mappers import row_to_order
from .base import Repository


class OrderRepository(Repository):
    async def create_order(
        self,
        user_id: int,
        product_id: int,
        quantity: int,
        amount_cents: int,
        currency: str,
        *,
        iccid: str | None = None,
        msisdn: str | None = None,
        days: int | None = None,
        sku: str | None = None,
        request_type: str | None = None,
        plan_id: str | None = None,
        expected_product: Product | None = None,
    ) -> Order:
        """创建订单并固定本次采购输入快照（SKU/业务类型/套餐/数量/天数/ICCID/号码）。

        快照在下单时锁定，之后商品目录变更不影响已创建订单的采购与交付核验。
        """
        async with self._db.transaction() as conn:
            if expected_product is not None:
                async with conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)) as cur:
                    current = await cur.fetchone()
                if (
                    current is None
                    or not current["active"]
                    or current["price_cents"] <= 0
                    or any(
                        current[key] != getattr(expected_product, key)
                        for key in ("price_cents", "currency", "sku", "request_type", "upstream_plan_id")
                    )
                ):
                    raise ValueError("商品已下架或报价已变化，请重新下单")
            async with conn.execute(
                """INSERT INTO orders (user_id, product_id, quantity, amount_cents, currency,
                input_iccid, input_msisdn, input_days, input_sku, input_request_type, input_plan_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
                (
                    user_id,
                    product_id,
                    quantity,
                    amount_cents,
                    currency,
                    iccid,
                    msisdn,
                    days,
                    sku,
                    request_type,
                    plan_id,
                ),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return row_to_order(row)

    async def get_order(self, order_id: int) -> Order | None:
        row = await self._db.fetch_one("SELECT * FROM orders WHERE id = ?", (order_id,))
        return row_to_order(row) if row else None

    async def list_orders(self, status: OrderStatus | None = None, limit: int = 20) -> list[Order]:
        if status is None:
            rows = await self._db.fetch_all("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        return [row_to_order(r) for r in rows]

    async def list_orders_for_user(self, user_id: int, limit: int = 20) -> list[Order]:
        rows = await self._db.fetch_all(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )
        return [row_to_order(r) for r in rows]

    async def add_order_note(self, order_id: int, note: str) -> None:
        """向 order_events 写一条人工操作审计记录（状态不变）。"""
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return
            await conn.execute(
                "INSERT INTO order_events (order_id, from_status, to_status, note) VALUES (?, ?, ?, ?)",
                (order_id, row["status"], row["status"], note),
            )

    async def transition_order(
        self,
        order_id: int,
        to_status: OrderStatus,
        *,
        from_status: OrderStatus | None = None,
        upstream_ref: str | None = None,
        trade_no: str | None = None,
        note: str | None = None,
        payload: str | None = None,
    ) -> Order | None:
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
                row = await cur.fetchone()
            if row is None or (from_status is not None and row["status"] != from_status):
                return None
            if trade_no is not None:
                if not trade_no.strip() or (row["trade_no"] and row["trade_no"] != trade_no):
                    raise ValueError("payment transaction does not match order")
                async with conn.execute(
                    "SELECT id FROM orders WHERE trade_no = ? AND id != ?", (trade_no, order_id)
                ) as cur:
                    if await cur.fetchone() is not None:
                        raise ValueError("payment transaction belongs to another order")
            async with conn.execute(
                """UPDATE orders SET status = ?, upstream_ref = COALESCE(?, upstream_ref),
                trade_no = COALESCE(?, trade_no), payload = COALESCE(?, payload), updated_at = datetime('now'),
                notification_pending = CASE WHEN ? = 'delivered' AND status != 'delivered'
                    THEN 1 ELSE notification_pending END
                WHERE id = ? RETURNING *""",
                (to_status, upstream_ref, trade_no, payload, to_status, order_id),
            ) as cur:
                updated = await cur.fetchone()
            if row["status"] != to_status:
                await conn.execute(
                    "INSERT INTO order_events (order_id, from_status, to_status, note) VALUES (?, ?, ?, ?)",
                    (order_id, row["status"], to_status, note),
                )
        assert updated is not None
        return row_to_order(updated)
