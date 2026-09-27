"""GMPay 交易仓储。

状态：creating（已登记、正在请求 epusdt）→ pending（已拿到收款信息）→ paid / expired；
请求失败或结果不明记为 failed，该商户订单号不再复用，下次付款换下一个序号。
"""

from __future__ import annotations

from typing import Any

from .base import Repository


class GMPayTradeRepository(Repository):
    async def begin(
        self, *, order_id: int | None = None, topup_id: int | None = None, amount_cents: int, currency: str
    ) -> tuple[int, str]:
        """登记一次下单尝试，返回 (记录 ID, 商户订单号)。订单 20 为 20-1、20-2，充值单 5 为 T5-1。"""
        if (order_id is None) == (topup_id is None):
            raise ValueError("exactly one of order_id and topup_id is required")
        prefix = str(order_id) if order_id is not None else f"T{topup_id}"
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT COUNT(*) AS n FROM gmpay_trades WHERE order_id IS ? AND topup_id IS ?", (order_id, topup_id)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            order_no = f"{prefix}-{row['n'] + 1}"
            async with conn.execute(
                """INSERT INTO gmpay_trades (merchant_order_no, order_id, topup_id, amount_cents, currency)
                VALUES (?, ?, ?, ?, ?) RETURNING id""",
                (order_no, order_id, topup_id, amount_cents, currency),
            ) as cur:
                created = await cur.fetchone()
        assert created is not None
        return created["id"], order_no

    async def created(
        self,
        trade_ref: int,
        *,
        trade_id: str,
        token: str,
        network: str,
        receive_address: str,
        actual_amount: str,
        payment_url: str,
        expires_at: float,
        check_at: float,
    ) -> None:
        """保存收款信息，并在同一事务里安排到期核对（兼作回调丢失时的兜底查询）。"""
        async with self._db.transaction() as conn:
            await conn.execute(
                """UPDATE gmpay_trades SET state = 'pending', trade_id = ?, token = ?, network = ?,
                receive_address = ?, actual_amount = ?, payment_url = ?, expires_at = ?, error = NULL
                WHERE id = ? AND state = 'creating'""",
                (trade_id, token, network, receive_address, actual_amount, payment_url, expires_at, trade_ref),
            )
            await conn.execute(
                """INSERT INTO work_items (kind, entity_id, due_at) VALUES ('gmpay', ?, ?)
                ON CONFLICT(kind, entity_id) DO UPDATE SET due_at = excluded.due_at, attempts = 0,
                revision = work_items.revision + 1""",
                (trade_ref, check_at),
            )

    async def fail(self, trade_ref: int, error: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE gmpay_trades SET state = 'failed', error = ? WHERE id = ? AND state = 'creating'",
                (error[:200], trade_ref),
            )

    async def get(self, trade_ref: int) -> dict[str, Any] | None:
        row = await self._db.fetch_one("SELECT * FROM gmpay_trades WHERE id = ?", (trade_ref,))
        return dict(row) if row else None

    async def by_order_no(self, order_no: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one("SELECT * FROM gmpay_trades WHERE merchant_order_no = ?", (order_no,))
        return dict(row) if row else None

    async def active(
        self, *, order_id: int | None = None, topup_id: int | None = None, after: float
    ) -> dict[str, Any] | None:
        """最近一笔仍在有效期内（截止晚于 after）的待付交易。"""
        row = await self._db.fetch_one(
            """SELECT * FROM gmpay_trades WHERE order_id IS ? AND topup_id IS ?
            AND state = 'pending' AND expires_at > ? ORDER BY id DESC LIMIT 1""",
            (order_id, topup_id, after),
        )
        return dict(row) if row else None

    async def has_active_for_order(self, order_id: int, *, now: float, grace: float) -> bool:
        """订单或其补差价充值单是否还有买家可能正在付款的收款信息。

        截止后再留 grace 秒给最后一刻的转账和回调；正在请求 epusdt 的尝试五分钟内也算。
        """
        row = await self._db.fetch_one(
            """SELECT 1 FROM gmpay_trades t
            WHERE (t.order_id = ? OR t.topup_id IN (
                SELECT id FROM balance_topups WHERE order_id = ? AND status = 'pending'
            )) AND (
                (t.state = 'pending' AND t.expires_at > ?)
                OR (t.state = 'creating' AND t.created_at > datetime(?, 'unixepoch'))
            ) LIMIT 1""",
            (order_id, order_id, now - grace, now - 300),
        )
        return row is not None

    async def checkable(self, order_id: int) -> list[dict[str, Any]]:
        """/query 核对用：已拿到 epusdt 交易号、尚未确认付款的最近几次尝试。"""
        rows = await self._db.fetch_all(
            """SELECT * FROM gmpay_trades WHERE order_id = ? AND trade_id IS NOT NULL
            AND state IN ('pending', 'expired') ORDER BY id DESC LIMIT 5""",
            (order_id,),
        )
        return [dict(row) for row in rows]

    async def attach_trade_id(self, trade_ref: int, trade_id: str) -> None:
        """下单结果不明的尝试后来收到回调时，补记 epusdt 交易号。"""
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE gmpay_trades SET trade_id = ? WHERE id = ? AND trade_id IS NULL", (trade_id, trade_ref)
            )

    async def mark_paid(self, trade_ref: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("UPDATE gmpay_trades SET state = 'paid' WHERE id = ? AND state != 'paid'", (trade_ref,))

    async def mark_expired(self, trade_ref: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE gmpay_trades SET state = 'expired' WHERE id = ? AND state = 'pending'", (trade_ref,)
            )
