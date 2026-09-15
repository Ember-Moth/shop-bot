from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aiosqlite
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from .models import Order, OrderStatus, Product, User

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment',
    upstream_ref TEXT,
    trade_no TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS fsm_state (
    bot_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    thread_id INTEGER,
    business_connection_id TEXT,
    destiny TEXT NOT NULL DEFAULT 'default',
    state TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (bot_id, chat_id, user_id, thread_id, business_connection_id, destiny)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called first")
        return self._conn

    # ---- users ----

    async def upsert_user(self, telegram_id: int, username: str | None) -> User:
        await self.conn.execute(
            """
            INSERT INTO users (telegram_id, username) VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
            """,
            (telegram_id, username),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            assert row is not None  # 刚 upsert 过，必然存在
            return _row_to_user(row)

    async def get_user(self, user_id: int) -> User | None:
        async with self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_user(row) if row else None

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        async with self.conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_user(row) if row else None

    # ---- products ----

    async def list_products(self) -> list[Product]:
        async with self.conn.execute(
            "SELECT * FROM products WHERE active = 1 ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_product(r) for r in rows]

    async def get_product(self, product_id: int) -> Product | None:
        async with self.conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_product(row) if row else None

    async def seed_products(self, products: list[Product]) -> None:
        """插入演示商品，供首次启动时调用。"""
        for p in products:
            await self.conn.execute(
                """
                INSERT OR IGNORE INTO products (id, name, description, price_cents, currency)
                VALUES (?, ?, ?, ?, ?)
                """,
                (p.id, p.name, p.description, p.price_cents, p.currency),
            )
        await self.conn.commit()

    # ---- orders ----

    async def create_order(
        self, user_id: int, product_id: int, quantity: int, amount_cents: int, currency: str
    ) -> Order:
        cur = await self.conn.execute(
            """
            INSERT INTO orders (user_id, product_id, quantity, amount_cents, currency)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, product_id, quantity, amount_cents, currency),
        )
        await self.conn.commit()
        order_id = cur.lastrowid
        assert order_id is not None
        order = await self.get_order(order_id)
        assert order is not None  # 刚插入，必然存在
        return order

    async def get_order(self, order_id: int) -> Order | None:
        async with self.conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_order(row) if row else None

    async def list_orders(
        self, status: OrderStatus | None = None, limit: int = 20
    ) -> list[Order]:
        if status is None:
            query = "SELECT * FROM orders ORDER BY id DESC LIMIT ?"
            params: tuple = (limit,)
        else:
            query = "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ?"
            params = (status.value, limit)
        async with self.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_order(r) for r in rows]

    async def list_orders_for_user(self, user_id: int, limit: int = 20) -> list[Order]:
        async with self.conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_order(r) for r in rows]

    async def transition_order(
        self,
        order_id: int,
        to_status: OrderStatus,
        *,
        from_status: OrderStatus | None = None,
        upstream_ref: str | None = None,
        trade_no: str | None = None,
        note: str | None = None,
    ) -> Order | None:
        """应用状态转换；订单不存在或当前状态与 from_status 不匹配时返回 None。

        「读状态 → 条件 UPDATE → 写审计日志 → commit」四步，条件 UPDATE 保证并发转换
        只有一个成功，显式 commit 保证状态变更和审计日志同时落盘。
        """
        async with self.conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        current = OrderStatus(row["status"])
        if from_status is not None and current != from_status:
            return None

        cursor = await self.conn.execute(
            """
            UPDATE orders SET status = ?, upstream_ref = COALESCE(?, upstream_ref),
                trade_no = COALESCE(?, trade_no), updated_at = datetime('now')
            WHERE id = ? AND status = ?
            """,
            (to_status.value, upstream_ref, trade_no, order_id, current.value),
        )
        if cursor.rowcount != 1:
            return None
        await self.conn.execute(
            "INSERT INTO order_events (order_id, from_status, to_status, note) VALUES (?, ?, ?, ?)",
            (order_id, current.value, to_status.value, note),
        )
        await self.conn.commit()  # 保证状态变更和审计日志同时落盘
        return await self.get_order(order_id)


def _row_to_user(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        created_at=row["created_at"],
    )


def _row_to_product(row: aiosqlite.Row) -> Product:
    return Product(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        price_cents=row["price_cents"],
        currency=row["currency"],
        active=bool(row["active"]),
    )


def _row_to_order(row: aiosqlite.Row) -> Order:
    return Order(
        id=row["id"],
        user_id=row["user_id"],
        product_id=row["product_id"],
        quantity=row["quantity"],
        amount_cents=row["amount_cents"],
        currency=row["currency"],
        status=OrderStatus(row["status"]),
        upstream_ref=row["upstream_ref"],
        trade_no=row["trade_no"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class FSMStorage(BaseStorage):
    """SQLite 持久化 FSM 存储，bot 重启后对话状态不丢。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _key_tuple(self, key: StorageKey) -> tuple:
        return (
            key.bot_id,
            key.chat_id,
            key.user_id,
            key.thread_id,
            key.business_connection_id,
            key.destiny,
        )

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        state_str = None
        if state is not None:
            state_str = state if isinstance(state, str) else state.state

        # 先尝试 INSERT，已存在则忽略；再 UPDATE，保证并发安全
        await self._db.conn.execute(
            """
            INSERT OR IGNORE INTO fsm_state
                (bot_id, chat_id, user_id, thread_id, business_connection_id, destiny, state, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
            """,
            (*self._key_tuple(key), state_str),
        )
        await self._db.conn.execute(
            """
            UPDATE fsm_state SET state = ?, updated_at = datetime('now')
            WHERE bot_id = ? AND chat_id = ? AND user_id = ?
              AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
            """,
            (state_str, *self._key_tuple(key)),
        )
        await self._db.conn.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._db.conn.execute(
            """
            SELECT state FROM fsm_state
            WHERE bot_id = ? AND chat_id = ? AND user_id = ?
              AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
            """,
            self._key_tuple(key),
        ) as cur:
            row = await cur.fetchone()
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        data_json = json.dumps(dict(data))

        # 先尝试 INSERT，已存在则忽略；再 UPDATE，保证并发安全
        await self._db.conn.execute(
            """
            INSERT OR IGNORE INTO fsm_state
                (bot_id, chat_id, user_id, thread_id, business_connection_id, destiny, state, data)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (*self._key_tuple(key), data_json),
        )
        await self._db.conn.execute(
            """
            UPDATE fsm_state SET data = ?, updated_at = datetime('now')
            WHERE bot_id = ? AND chat_id = ? AND user_id = ?
              AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
            """,
            (data_json, *self._key_tuple(key)),
        )
        await self._db.conn.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._db.conn.execute(
            """
            SELECT data FROM fsm_state
            WHERE bot_id = ? AND chat_id = ? AND user_id = ?
              AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
            """,
            self._key_tuple(key),
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row["data"]) if row else {}

    async def close(self) -> None:
        # 存储不持有连接，由 Database 统一管理
        pass
