"""收款仓储：外部交易凭据、EPay/余额结算、人工确认与退款。"""

from __future__ import annotations

import aiosqlite

from ...models import Order, PurchaseState
from ..mappers import row_to_order
from .base import Repository


class PaymentRepository(Repository):
    async def check_receipt(
        self,
        conn: aiosqlite.Connection,
        trade_no: str,
        *,
        order_id: int | None = None,
        topup_id: int | None = None,
    ) -> aiosqlite.Row | None:
        """调用方持有事务：核验外部交易号未被其他订单/充值单占用，返回既有凭据（无则 None）。"""
        if not trade_no or trade_no != trade_no.strip():
            raise ValueError("invalid payment transaction")
        async with conn.execute("SELECT * FROM payment_receipts WHERE trade_no = ?", (trade_no,)) as cur:
            receipt = await cur.fetchone()
        if receipt is not None and (receipt["order_id"] != order_id or receipt["topup_id"] != topup_id):
            raise ValueError("payment transaction belongs to another order")
        # 尚未进入 payment_receipts 的历史支付也参与全局唯一性核验。
        for table, target in (("orders", order_id), ("balance_topups", topup_id)):
            clause = " AND payment_method IS NOT 'balance'" if table == "orders" else ""
            async with conn.execute(
                f"SELECT id FROM {table} WHERE trade_no = ?{clause}",  # noqa: S608
                (trade_no,),
            ) as cur:
                if any(row["id"] != target for row in await cur.fetchall()):
                    raise ValueError("payment transaction belongs to another order")
        return receipt

    async def reserve_epay(self, order_id: int, user_id: int, currency: str) -> Order | None:
        """先锁定在线渠道，再生成签名链接；余额扣款不能跨过这个持久化选择。"""
        async with self._db.transaction() as conn:
            async with conn.execute(
                """UPDATE orders SET payment_method = 'epay' WHERE id = ? AND user_id = ?
                AND currency = ? AND amount_cents > 0 AND status = 'pending_payment'
                AND (payment_method IS NULL OR payment_method = 'epay') RETURNING *""",
                (order_id, user_id, currency),
            ) as cur:
                row = await cur.fetchone()
        return row_to_order(row) if row else None

    async def record_epay_payment(self, order_id: int, trade_no: str) -> tuple[Order, str]:
        """调用方已验签并核对订单/金额/币种；重复收款同币种补偿入钱包。"""
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
                order = await cur.fetchone()
            if order is None:
                raise ValueError("order not found")
            receipt = await self.check_receipt(conn, trade_no, order_id=order_id)
            if receipt is not None:
                return row_to_order(order), receipt["disposition"]
            # /paid 可能先于真实回调确认了同一笔收款；首次补齐交易号不能再次送余额。
            is_original = order["payment_method"] != "balance" and (
                order["trade_no"] == trade_no
                or (
                    order["trade_no"] is None
                    and order["status"]
                    in (
                        "paid",
                        "delivered",
                        "delivery_failed",
                        "refunded",
                    )
                )
            )
            disposition = "order" if order["status"] == "pending_payment" or is_original else "wallet_credit"
            if disposition == "wallet_credit":
                balance = await self._db.wallet.change_wallet(
                    conn,
                    order["user_id"],
                    order["currency"],
                    order["amount_cents"],
                    "payment_credit",
                    order_id=order_id,
                    note=f"additional/closed-order payment: {trade_no}",
                )
                if balance is None:
                    raise ValueError("payment owner missing")
            elif order["status"] == "pending_payment":
                await conn.execute(
                    """UPDATE orders SET status = 'paid', trade_no = ?, payment_method = 'epay',
                    updated_at = datetime('now') WHERE id = ?""",
                    (trade_no, order_id),
                )
                await conn.execute(
                    "INSERT INTO order_events (order_id, from_status, to_status, note) VALUES (?, ?, 'paid', ?)",
                    (order_id, order["status"], "verified epay payment"),
                )
            elif order["trade_no"] is None:
                await conn.execute(
                    "UPDATE orders SET trade_no = ?, payment_method = 'epay' WHERE id = ?", (trade_no, order_id)
                )
            await conn.execute(
                """INSERT INTO payment_receipts (trade_no, order_id, amount_cents, currency, disposition)
                VALUES (?, ?, ?, ?, ?)""",
                (trade_no, order_id, order["amount_cents"], order["currency"], disposition),
            )
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
                updated = await cur.fetchone()
        assert updated is not None
        return row_to_order(updated), disposition

    async def confirm_order_payment(
        self,
        order_id: int,
        trade_no: str | None,
        *,
        retry_failed: bool = False,
    ) -> Order:
        """管理员确认：短事务核对当前状态，触发器原子保存采购及调度意图。"""
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                raise ValueError(f"order {order_id} not found")
            if row["status"] == "cancelled":
                raise ValueError(f"order {order_id} is cancelled")
            if trade_no is not None:
                if row["trade_no"] and row["trade_no"] != trade_no:
                    raise ValueError("payment transaction does not match order")
                await self.check_receipt(conn, trade_no, order_id=order_id)
            status = row["status"]
            if status == "pending_payment" or (status == "delivery_failed" and retry_failed):
                status = "paid"
            if status == row["status"] and (trade_no is None or trade_no == row["trade_no"]):
                return row_to_order(row)
            async with conn.execute(
                """UPDATE orders SET status = ?, trade_no = COALESCE(?, trade_no),
                updated_at = datetime('now') WHERE id = ? RETURNING *""",
                (status, trade_no, order_id),
            ) as cur:
                updated = await cur.fetchone()
            if status != row["status"]:
                await conn.execute(
                    "INSERT INTO order_events (order_id, from_status, to_status, note) VALUES (?, ?, ?, ?)",
                    (order_id, row["status"], status, "manual payment confirmation"),
                )
        assert updated is not None
        return row_to_order(updated)

    async def pay_order_with_balance(
        self, order_id: int, user_id: int, amount_cents: int
    ) -> tuple[Order | None, str | None]:
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM orders WHERE id = ? AND status = 'pending_payment'", (order_id,)
            ) as cur:
                order = await cur.fetchone()
            if order is None:
                return None, "order not payable"
            if order["user_id"] != user_id or order["amount_cents"] != amount_cents or amount_cents <= 0:
                return None, "order mismatch"
            if order["payment_method"] == "epay":
                return None, "online payment selected"
            balance = await self._db.wallet.change_wallet(
                conn,
                user_id,
                order["currency"],
                -amount_cents,
                "purchase",
                order_id=order_id,
                note=f"order #{order_id}",
            )
            if balance is None:
                return None, "insufficient"
            async with conn.execute(
                """UPDATE orders SET status = 'paid', payment_method = 'balance',
                trade_no = ?, updated_at = datetime('now') WHERE id = ? RETURNING *""",
                (f"BAL{order_id}", order_id),
            ) as cur:
                paid = await cur.fetchone()
            await conn.execute(
                "INSERT INTO order_events (order_id, from_status, to_status, note)"
                " VALUES (?, 'pending_payment', 'paid', 'balance payment')",
                (order_id,),
            )
        assert paid is not None
        return row_to_order(paid), None

    async def refund_order_to_balance(self, order_id: int, note: str) -> tuple[Order | None, str | None]:
        """人工退款与履约/KYC 共用订单锁；正在上游处理的订单不得直接退款。"""
        async with self._db.order_operation(order_id), self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM purchases WHERE order_id = ?", (order_id,)) as cur:
                purchase = await cur.fetchone()
            if purchase is not None and purchase["state"] not in (
                PurchaseState.READY,
                PurchaseState.REJECTED,
                PurchaseState.REFUND_PENDING,
                PurchaseState.SUBMISSION_UNKNOWN,
                PurchaseState.REFUNDED,
            ):
                return None, "upstream purchase active; reconcile or cancel upstream first"
            return await self._refund_in_transaction(conn, order_id, note)

    async def reject_and_refund(
        self,
        order_id: int,
        purchase_id: int,
        from_state: PurchaseState,
        note: str,
    ) -> tuple[Order | None, str | None]:
        """fulfill 已持有订单锁；先保存退款意图，再原子写余额、账本和两侧终态。"""
        # 先持久化明确拒绝的证据；退款事务中断后，恢复循环仍知道应退而非重购。
        async with self._db.transaction() as conn:
            async with conn.execute(
                """UPDATE purchases SET state = 'refund_pending', last_error = ?, updated_at = datetime('now')
                WHERE id = ? AND order_id = ? AND state = ? AND EXISTS (
                    SELECT 1 FROM orders WHERE id = ? AND status = 'paid'
                ) RETURNING id""",
                (note, purchase_id, order_id, from_state, order_id),
            ) as cur:
                if await cur.fetchone() is None:
                    return None, "purchase state changed"
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT id FROM purchases WHERE id = ? AND order_id = ? AND state = ?",
                (purchase_id, order_id, PurchaseState.REFUND_PENDING),
            ) as cur:
                if await cur.fetchone() is None:
                    return None, "purchase state changed"
            return await self._refund_in_transaction(conn, order_id, note)

    async def _refund_in_transaction(
        self,
        conn: aiosqlite.Connection,
        order_id: int,
        note: str,
    ) -> tuple[Order | None, str | None]:
        async with conn.execute("SELECT * FROM orders WHERE id = ? AND status = 'paid'", (order_id,)) as cur:
            order = await cur.fetchone()
        if order is None:
            return None, "not refundable"
        balance = await self._db.wallet.change_wallet(
            conn,
            order["user_id"],
            order["currency"],
            order["amount_cents"],
            "refund",
            order_id=order_id,
            note=note,
        )
        if balance is None:
            raise ValueError("refund owner missing")
        async with conn.execute(
            """UPDATE orders SET status = 'refunded', notification_pending = 1, notified_at = NULL,
            updated_at = datetime('now') WHERE id = ? RETURNING *""",
            (order_id,),
        ) as cur:
            refunded = await cur.fetchone()
        await conn.execute(
            "UPDATE purchases SET state = 'refunded', last_error = ?, updated_at = datetime('now') WHERE order_id = ?",
            (note, order_id),
        )
        await conn.execute(
            "INSERT INTO order_events (order_id, from_status, to_status, note) VALUES (?, 'paid', 'refunded', ?)",
            (order_id, note),
        )
        assert refunded is not None
        return row_to_order(refunded), None
