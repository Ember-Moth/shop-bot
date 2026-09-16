from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from weakref import WeakValueDictionary

import aiosqlite
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from .models import Order, OrderStatus, Product, Purchase, PurchaseState, User

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
    active INTEGER NOT NULL DEFAULT 1,
    sku TEXT,
    upstream_plan_id TEXT
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
    payload TEXT,
    notified_at TEXT,
    notification_pending INTEGER NOT NULL DEFAULT 0,
    input_iccid TEXT,
    input_msisdn TEXT,
    input_days INTEGER,
    input_sku TEXT,
    input_request_type TEXT,
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

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    state TEXT NOT NULL DEFAULT 'ready',
    request_type TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    upstream_request_id TEXT,
    upstream_order_no TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    kyc_documents TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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
    """单连接数据库。所有读写通过锁保护的连接上下文，事务不能跨请求共享。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._order_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        try:
            await self._conn.executescript(SCHEMA)
            async with self.transaction() as conn:
                await self._migrate(conn)
        except BaseException:
            await self.close()
            raise

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        async with conn.execute("PRAGMA table_info(orders)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        for column in ("trade_no", "payload", "notified_at"):
            if column not in columns:
                await conn.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT")
        if "notification_pending" not in columns:
            # 旧版没有通知结果证据。历史已发货订单只允许主动补发，避免升级时群发旧货品。
            await conn.execute("ALTER TABLE orders ADD COLUMN notification_pending INTEGER NOT NULL DEFAULT 0")
        for column in ("input_iccid", "input_msisdn", "input_sku", "input_request_type"):
            if column not in columns:
                await conn.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT")
        if "input_days" not in columns:
            await conn.execute("ALTER TABLE orders ADD COLUMN input_days INTEGER")
        async with conn.execute("PRAGMA table_info(products)") as cur:
            product_columns = {row["name"] for row in await cur.fetchall()}
        for column in ("sku", "upstream_plan_id", "request_type"):
            if column not in product_columns:
                await conn.execute(f"ALTER TABLE products ADD COLUMN {column} TEXT")
        # 部分唯一索引：手工商品 sku 为 NULL，不参与唯一约束
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku ON products(sku) WHERE sku IS NOT NULL"
        )
        # 旧实现读取最早一行；它包含后续 set_state/set_data 更新的完整会话。
        # 后插入的重复行可能只包含 state 或 data，不能简单保留最新行。
        await conn.execute("DROP INDEX IF EXISTS idx_fsm_state_unique")
        await conn.execute("""
            DELETE FROM fsm_state WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM fsm_state
                GROUP BY bot_id, chat_id, user_id, thread_id, business_connection_id, destiny
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX idx_fsm_state_unique ON fsm_state (
                bot_id, chat_id, user_id, thread_id IS NULL, IFNULL(thread_id, 0),
                business_connection_id IS NULL, IFNULL(business_connection_id, ''), destiny
            )
        """)
        # 恢复上一版已提交事件、但还未写 orders.payload 时中断的订单。
        await conn.execute("""
            UPDATE orders SET payload = (
                SELECT note FROM order_events WHERE order_id = orders.id
                AND to_status = 'delivered' AND note IS NOT NULL ORDER BY id DESC LIMIT 1
            ) WHERE status = 'delivered' AND payload IS NULL
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_trade_no ON orders(trade_no)")
        async with conn.execute("PRAGMA table_info(purchases)") as cur:
            purchase_columns = {row["name"] for row in await cur.fetchall()}
        if "kyc_documents" not in purchase_columns:
            await conn.execute("ALTER TABLE purchases ADD COLUMN kyc_documents TEXT")
        # 上游请求 ID 全局唯一：一份货品只能归属一个本店订单（防重复交付）
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_purchases_upstream"
            " ON purchases(upstream_request_id) WHERE upstream_request_id IS NOT NULL"
        )

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """持有连接期间不得调用其他 Database 方法；业务代码使用 DAO。"""
        async with self._lock:
            if self._conn is None:
                raise RuntimeError("Database.connect() must be called first")
            yield self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.connection() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                yield conn
                await conn.commit()
            except BaseException:
                # 包括任务取消；先清理事务，再允许下一个协程访问连接。
                await conn.rollback()
                raise

    @asynccontextmanager
    async def order_operation(self, order_id: int) -> AsyncIterator[None]:
        """单进程内串行处理同一订单的履约和通知，不占用数据库连接。"""
        lock = self._order_locks.setdefault(order_id, asyncio.Lock())
        async with lock:
            yield

    async def _one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.connection() as conn, conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.connection() as conn, conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def upsert_user(self, telegram_id: int, username: str | None) -> User:
        async with self.transaction() as conn:
            async with conn.execute(
                """INSERT INTO users (telegram_id, username) VALUES (?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username RETURNING *""",
                (telegram_id, username),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return _row_to_user(row)

    async def get_user(self, user_id: int) -> User | None:
        row = await self._one("SELECT * FROM users WHERE id = ?", (user_id,))
        return _row_to_user(row) if row else None

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        row = await self._one("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        return _row_to_user(row) if row else None

    async def list_products(self) -> list[Product]:
        return [_row_to_product(r) for r in await self._all("SELECT * FROM products WHERE active = 1 ORDER BY id")]

    async def get_product(self, product_id: int) -> Product | None:
        row = await self._one("SELECT * FROM products WHERE id = ?", (product_id,))
        return _row_to_product(row) if row else None

    async def seed_products(self, products: list[Product]) -> None:
        async with self.transaction() as conn:
            await conn.executemany(
                """INSERT OR IGNORE INTO products (id, name, description, price_cents, currency,
                sku, upstream_plan_id, request_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (p.id, p.name, p.description, p.price_cents, p.currency, p.sku, p.upstream_plan_id, p.request_type)
                    for p in products
                ],
            )

    async def upsert_product_from_upstream(
        self, *, sku: str, name: str, description: str, upstream_plan_id: str, request_type: str | None
    ) -> bool:
        """按 SKU 同步上游商品；只更新名称/描述/上游 ID，不动本店价格与上架状态。

        返回 True 表示新建。新商品 0 价且下架（目录可见不等于可售，开发方案 7.3），
        需管理员定价并上架后才对用户可见。
        """
        async with self.transaction() as conn:
            async with conn.execute("SELECT id FROM products WHERE sku = ?", (sku,)) as cur:
                row = await cur.fetchone()
            if row is None:
                await conn.execute(
                    """INSERT INTO products
                        (name, description, price_cents, currency, active, sku, upstream_plan_id, request_type)
                    VALUES (?, ?, 0, 'CNY', 0, ?, ?, ?)""",
                    (name, description, sku, upstream_plan_id, request_type),
                )
                return True
            # request_type 保留人工配置（COALESCE）：管理员指定的业务类型不被目录同步覆盖
            await conn.execute(
                """UPDATE products SET name = ?, description = ?, upstream_plan_id = ?,
                request_type = COALESCE(request_type, ?)
                WHERE id = ?""",
                (name, description, upstream_plan_id, request_type, row["id"]),
            )
            return False

    # ---- purchases ----

    async def ensure_purchase(
        self, order_id: int, *, request_type: str, sku: str, quantity: int
    ) -> Purchase:
        """按订单建立采购任务（幂等，order_id 唯一）。已存在时原样返回。"""
        async with self.transaction() as conn:
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
        return _row_to_purchase(row)

    async def get_purchase_by_order(self, order_id: int) -> Purchase | None:
        row = await self._one("SELECT * FROM purchases WHERE order_id = ?", (order_id,))
        return _row_to_purchase(row) if row else None

    async def get_purchase_by_upstream_request_id(self, upstream_request_id: str) -> Purchase | None:
        row = await self._one(
            "SELECT * FROM purchases WHERE upstream_request_id = ?", (upstream_request_id,)
        )
        return _row_to_purchase(row) if row else None

    async def list_purchases_by_states(self, states: tuple[PurchaseState, ...]) -> list[Purchase]:
        # placeholders 只由 len(states) 生成，无外部输入参与拼接
        placeholders = ",".join("?" for _ in states)
        rows = await self._all(
            f"SELECT * FROM purchases WHERE state IN ({placeholders}) ORDER BY id",  # noqa: S608
            tuple(state.value for state in states),
        )
        return [_row_to_purchase(r) for r in rows]

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
        async with self.transaction() as conn:
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
                    to_state, upstream_request_id, upstream_order_no, last_error, attempts,
                    set_kyc_documents, purchase_id,
                ),
            ) as cur:
                updated = await cur.fetchone()
        assert updated is not None
        return _row_to_purchase(updated)

    async def add_order_note(self, order_id: int, note: str) -> None:
        """向 order_events 写一条人工操作审计记录（状态不变）。"""
        async with self.transaction() as conn:
            async with conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return
            await conn.execute(
                "INSERT INTO order_events (order_id, from_status, to_status, note) VALUES (?, ?, ?, ?)",
                (order_id, row["status"], row["status"], note),
            )

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
    ) -> Order:
        """创建订单并固定本次采购输入快照（SKU/业务类型/数量/天数/ICCID/号码）。

        快照在下单时锁定，之后商品目录变更不影响已创建订单的采购。
        """
        async with self.transaction() as conn:
            async with conn.execute(
                """INSERT INTO orders (user_id, product_id, quantity, amount_cents, currency,
                input_iccid, input_msisdn, input_days, input_sku, input_request_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
                (user_id, product_id, quantity, amount_cents, currency, iccid, msisdn, days, sku, request_type),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return _row_to_order(row)

    async def get_order(self, order_id: int) -> Order | None:
        row = await self._one("SELECT * FROM orders WHERE id = ?", (order_id,))
        return _row_to_order(row) if row else None

    async def list_orders(self, status: OrderStatus | None = None, limit: int = 20) -> list[Order]:
        if status is None:
            rows = await self._all("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = await self._all("SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
        return [_row_to_order(r) for r in rows]

    async def list_orders_for_user(self, user_id: int, limit: int = 20) -> list[Order]:
        rows = await self._all("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return [_row_to_order(r) for r in rows]

    async def list_recovery_orders(self) -> list[Order]:
        rows = await self._all("""SELECT * FROM orders WHERE status = 'paid'
            OR (status = 'delivered' AND notification_pending = 1) ORDER BY id""")
        return [_row_to_order(r) for r in rows]

    async def mark_notified(self, order_id: int) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE orders SET notified_at = datetime('now'), notification_pending = 0
                WHERE id = ? AND status = 'delivered'""",
                (order_id,),
            )

    async def request_notification(self, order_id: int) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE orders SET notification_pending = 1 WHERE id = ? AND status = 'delivered'", (order_id,)
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
        async with self.transaction() as conn:
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
        return _row_to_order(updated)


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
        sku=row["sku"],
        upstream_plan_id=row["upstream_plan_id"],
        request_type=row["request_type"],
    )


def _row_to_purchase(row: aiosqlite.Row) -> Purchase:
    return Purchase(
        id=row["id"],
        order_id=row["order_id"],
        state=PurchaseState(row["state"]),
        request_type=row["request_type"],
        sku=row["sku"],
        quantity=row["quantity"],
        upstream_request_id=row["upstream_request_id"],
        upstream_order_no=row["upstream_order_no"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        kyc_documents=row["kyc_documents"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
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
        payload=row["payload"],
        notified_at=row["notified_at"],
        notification_pending=bool(row["notification_pending"]),
        input_iccid=row["input_iccid"],
        input_msisdn=row["input_msisdn"],
        input_days=row["input_days"],
        input_sku=row["input_sku"],
        input_request_type=row["input_request_type"],
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

        async with self._db.transaction() as conn:
            # 先尝试 INSERT，已存在则忽略；再 UPDATE，保证并发安全
            await conn.execute(
                """
                INSERT OR IGNORE INTO fsm_state
                    (bot_id, chat_id, user_id, thread_id, business_connection_id, destiny, state, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
                """,
                (*self._key_tuple(key), state_str),
            )
            await conn.execute(
                """
                UPDATE fsm_state SET state = ?, updated_at = datetime('now')
                WHERE bot_id = ? AND chat_id = ? AND user_id = ?
                  AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
                """,
                (state_str, *self._key_tuple(key)),
            )

    async def get_state(self, key: StorageKey) -> str | None:
        async with (
            self._db.connection() as conn,
            conn.execute(
                """
            SELECT state FROM fsm_state
            WHERE bot_id = ? AND chat_id = ? AND user_id = ?
              AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
            """,
                self._key_tuple(key),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        data_json = json.dumps(dict(data))

        async with self._db.transaction() as conn:
            # 先尝试 INSERT，已存在则忽略；再 UPDATE，保证并发安全
            await conn.execute(
                """
                INSERT OR IGNORE INTO fsm_state
                    (bot_id, chat_id, user_id, thread_id, business_connection_id, destiny, state, data)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (*self._key_tuple(key), data_json),
            )
            await conn.execute(
                """
                UPDATE fsm_state SET data = ?, updated_at = datetime('now')
                WHERE bot_id = ? AND chat_id = ? AND user_id = ?
                  AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
                """,
                (data_json, *self._key_tuple(key)),
            )

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with (
            self._db.connection() as conn,
            conn.execute(
                """
            SELECT data FROM fsm_state
            WHERE bot_id = ? AND chat_id = ? AND user_id = ?
              AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?
            """,
                self._key_tuple(key),
            ) as cur,
        ):
            row = await cur.fetchone()
        return json.loads(row["data"]) if row else {}

    async def close(self) -> None:
        # 存储不持有连接，由 Database 统一管理
        pass
