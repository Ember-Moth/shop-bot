"""钱包仓储：按币种记账的余额、充值单与流水。"""

from __future__ import annotations

import aiosqlite

from ...models import BalanceTransaction, Topup
from ...money import normalize_currency
from ..mappers import row_to_balance_tx, row_to_topup
from .base import Repository


class WalletRepository(Repository):
    async def get_balance(self, user_id: int, currency: str) -> int:
        row = await self._db.fetch_one(
            "SELECT balance_cents FROM wallet_balances WHERE user_id = ? AND currency = ?",
            (user_id, normalize_currency(currency)),
        )
        return row["balance_cents"] if row else 0

    async def get_balances(self, user_id: int) -> dict[str, int]:
        rows = await self._db.fetch_all(
            "SELECT currency, balance_cents FROM wallet_balances WHERE user_id = ?", (user_id,)
        )
        return {row["currency"]: row["balance_cents"] for row in rows}

    async def change_wallet(
        self,
        conn: aiosqlite.Connection,
        user_id: int,
        currency: str,
        amount: int,
        kind: str,
        *,
        order_id: int | None = None,
        topup_id: int | None = None,
        note: str = "",
    ) -> int | None:
        """调用方持有事务；余额和币种明确的流水原子写入。"""
        currency = normalize_currency(currency)
        async with conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)) as cur:
            if await cur.fetchone() is None:
                return None
        await conn.execute(
            "INSERT OR IGNORE INTO wallet_balances (user_id, currency) VALUES (?, ?)", (user_id, currency)
        )
        async with conn.execute(
            """UPDATE wallet_balances SET balance_cents = balance_cents + ?
            WHERE user_id = ? AND currency = ? AND balance_cents + ? >= 0 RETURNING balance_cents""",
            (amount, user_id, currency, amount),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        balance = row["balance_cents"]
        if currency == "CNY":
            # 兼容旧 User.balance_cents 只读接口；业务收付一律使用 wallet_balances。
            await conn.execute("UPDATE users SET balance_cents = ? WHERE id = ?", (balance, user_id))
        await conn.execute(
            """INSERT INTO balance_transactions
            (user_id, currency, amount_cents, balance_after, kind, order_id, topup_id, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, currency, amount, balance, kind, order_id, topup_id, note),
        )
        return balance

    async def create_topup(self, user_id: int, amount_cents: int, currency: str = "CNY") -> Topup:
        currency = normalize_currency(currency)
        if amount_cents <= 0:
            raise ValueError("topup amount must be positive")
        async with self._db.transaction() as conn:
            async with conn.execute(
                "INSERT INTO balance_topups (user_id, amount_cents, currency) VALUES (?, ?, ?) RETURNING *",
                (user_id, amount_cents, currency),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return row_to_topup(row)

    async def get_topup(self, topup_id: int) -> Topup | None:
        row = await self._db.fetch_one("SELECT * FROM balance_topups WHERE id = ?", (topup_id,))
        return row_to_topup(row) if row else None

    async def complete_topup(self, topup_id: int, trade_no: str) -> Topup | None:
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM balance_topups WHERE id = ?", (topup_id,)) as cur:
                topup = await cur.fetchone()
            if topup is None:
                return None
            receipt = await self._db.payments.check_receipt(conn, trade_no, topup_id=topup_id)
            if receipt is not None or topup["trade_no"] == trade_no:
                return row_to_topup(topup)
            if topup["status"] not in ("pending", "paid"):
                return None
            balance = await self.change_wallet(
                conn,
                topup["user_id"],
                topup["currency"],
                topup["amount_cents"],
                "topup",
                topup_id=topup_id,
                note=trade_no,
            )
            if balance is None:
                raise ValueError("topup owner missing")
            await conn.execute(
                """INSERT INTO payment_receipts (trade_no, topup_id, amount_cents, currency, disposition)
                VALUES (?, ?, ?, ?, 'topup')""",
                (trade_no, topup_id, topup["amount_cents"], topup["currency"]),
            )
            async with conn.execute(
                """UPDATE balance_topups SET status = 'paid', trade_no = COALESCE(trade_no, ?),
                updated_at = datetime('now') WHERE id = ? RETURNING *""",
                (trade_no, topup_id),
            ) as cur:
                paid = await cur.fetchone()
        assert paid is not None
        return row_to_topup(paid)

    async def adjust_balance(self, user_id: int, delta_cents: int, note: str, currency: str = "CNY") -> int | None:
        async with self._db.transaction() as conn:
            return await self.change_wallet(conn, user_id, currency, delta_cents, "adjust", note=note)

    async def list_balance_transactions(self, user_id: int, limit: int = 5) -> list[BalanceTransaction]:
        rows = await self._db.fetch_all(
            "SELECT * FROM balance_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [row_to_balance_tx(r) for r in rows]
