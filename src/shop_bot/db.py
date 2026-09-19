from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from weakref import WeakValueDictionary

import aiosqlite
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from .logging_config import get_logger
from .models import (
    BalanceTransaction,
    Order,
    OrderStatus,
    Product,
    Purchase,
    PurchaseState,
    Topup,
    TopupState,
    User,
)
from .money import REQUEST_TYPES, normalize_currency
from .work_queue import WorkItem, migrate_work_queue

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    balance_cents INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS balance_topups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    trade_no TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS balance_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    kind TEXT NOT NULL,
    order_id INTEGER,
    topup_id INTEGER,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_balance_tx_user ON balance_transactions(user_id, id);

CREATE TABLE IF NOT EXISTS wallet_balances (
    user_id INTEGER NOT NULL REFERENCES users(id),
    currency TEXT NOT NULL,
    balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    PRIMARY KEY (user_id, currency)
);

CREATE TABLE IF NOT EXISTS payment_receipts (
    trade_no TEXT PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id),
    topup_id INTEGER REFERENCES balance_topups(id),
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    disposition TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS product_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    actor_id INTEGER,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS operator_alerts (
    key TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    alert_key TEXT NOT NULL,
    admin_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    last_sent REAL NOT NULL,
    PRIMARY KEY (alert_key, admin_id)
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
    input_plan_id TEXT,
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
                await migrate_work_queue(conn)
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
        for column in ("input_iccid", "input_msisdn", "input_sku", "input_request_type", "input_plan_id"):
            if column not in columns:
                await conn.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT")
        if "input_days" not in columns:
            await conn.execute("ALTER TABLE orders ADD COLUMN input_days INTEGER")
        if "delivery_esims" not in columns:
            await conn.execute("ALTER TABLE orders ADD COLUMN delivery_esims TEXT")
        if "notification_cursor" not in columns:
            await conn.execute("ALTER TABLE orders ADD COLUMN notification_cursor INTEGER NOT NULL DEFAULT 0")
        if "notification_retry_at" not in columns:
            await conn.execute("ALTER TABLE orders ADD COLUMN notification_retry_at REAL")
        if "notification_plan_version" not in columns:
            # 旧版存在两种不同步骤排列，无法只凭 cursor 判断。保持完成标记；
            # 待通知订单在经过采购/归属核验后，由 prepare_notification 重置未知进度。
            await conn.execute("ALTER TABLE orders ADD COLUMN notification_plan_version INTEGER NOT NULL DEFAULT 0")
        if "payment_method" not in columns:
            await conn.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT")
            # 旧待付单可能已生成可用的 EPay 链接，升级后不允许再切换到余额。
            await conn.execute("UPDATE orders SET payment_method = 'epay' WHERE currency = 'CNY'")
            await conn.execute("""
                UPDATE orders SET payment_method = 'balance'
                WHERE trade_no = 'BAL' || id AND EXISTS (
                    SELECT 1 FROM balance_transactions WHERE order_id = orders.id AND kind = 'purchase'
                )
            """)
        async with conn.execute("PRAGMA table_info(users)") as cur:
            user_columns = {row["name"] for row in await cur.fetchall()}
        if "balance_cents" not in user_columns:
            await conn.execute("ALTER TABLE users ADD COLUMN balance_cents INTEGER NOT NULL DEFAULT 0")
        if "display_name" not in user_columns:
            await conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        async with conn.execute("PRAGMA table_info(balance_transactions)") as cur:
            tx_columns = {row["name"] for row in await cur.fetchall()}
        if "currency" not in tx_columns:
            await conn.execute("ALTER TABLE balance_transactions ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'")
        async with conn.execute("PRAGMA table_info(balance_topups)") as cur:
            topup_columns = {row["name"] for row in await cur.fetchall()}
        if "currency" not in topup_columns:
            await conn.execute("ALTER TABLE balance_topups ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'")
        # 原 users.balance_cents 仅表示人民币；迁移不能把它重新解释为美元。
        await conn.execute("""
            INSERT OR IGNORE INTO wallet_balances (user_id, currency, balance_cents)
            SELECT id, 'CNY', balance_cents FROM users
        """)
        async with conn.execute("PRAGMA table_info(products)") as cur:
            product_columns = {row["name"] for row in await cur.fetchall()}
        for column in ("sku", "upstream_plan_id", "request_type"):
            if column not in product_columns:
                await conn.execute(f"ALTER TABLE products ADD COLUMN {column} TEXT")
        # 部分唯一索引：手工商品 sku 为 NULL，不参与唯一约束
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku ON products(sku) WHERE sku IS NOT NULL")
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
        # 恢复扫描（list_recovery_orders）与运维监控按 status 过滤 paid/delivered 订单；
        # 覆盖索引含 notification_pending/updated_at，避免回表与对 delivered 历史的全扫
        await conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_orders_status_notify
            ON orders(status, notification_pending, updated_at, id)"""
        )
        # 运维监控按 state 过滤 purchases 并按 updated_at 判断停滞
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_purchases_state_updated ON purchases(state, updated_at)")
        async with conn.execute("PRAGMA table_info(purchases)") as cur:
            purchase_columns = {row["name"] for row in await cur.fetchall()}
        if "kyc_documents" not in purchase_columns:
            await conn.execute("ALTER TABLE purchases ADD COLUMN kyc_documents TEXT")
        await conn.execute("""
            UPDATE purchases SET state = 'refunded'
            WHERE order_id IN (SELECT id FROM orders WHERE status = 'refunded')
        """)
        # 上游请求 ID 全局唯一：一份货品只能归属一个本店订单（防重复交付）。
        # 旧版本允许重复绑定：存在冲突时跳过索引、冻结冲突记录履约与通知并告警，
        # 由管理员人工核对后手动清空多余记录的 upstream_request_id
        # （不能擅自删除订单关联）。 fulfilled 记录同样冻结——重复货品归属
        # 本就存疑，通知前必须人工确认归属（审计第四轮 P1）。
        # 新绑定的唯一性由 bind_upstream_request 事务内复核保证，不依赖该索引。
        async with conn.execute(
            """SELECT upstream_request_id FROM purchases WHERE upstream_request_id IS NOT NULL
            GROUP BY upstream_request_id HAVING COUNT(*) > 1"""
        ) as cur:
            duplicates = [row["upstream_request_id"] for row in await cur.fetchall()]
        if duplicates:
            await conn.execute(
                """UPDATE purchases SET state = 'submission_unknown',
                last_error = 'duplicate upstream request id; frozen for manual reconciliation'
                WHERE upstream_request_id IN (
                    SELECT upstream_request_id FROM purchases WHERE upstream_request_id IS NOT NULL
                    GROUP BY upstream_request_id HAVING COUNT(*) > 1
                ) AND order_id NOT IN (SELECT id FROM orders WHERE status = 'refunded')"""
            )
            logger.error(
                "duplicate upstream request ids found in purchases; unique index skipped, "
                "conflicting purchases frozen as submission_unknown for manual reconciliation: %s",
                duplicates,
            )
        else:
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

    async def upsert_user(self, telegram_id: int, username: str | None, display_name: str | None = None) -> User:
        async with self.transaction() as conn:
            async with conn.execute(
                """INSERT INTO users (telegram_id, username, display_name) VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username,
                display_name = excluded.display_name RETURNING *""",
                (telegram_id, username, display_name),
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

    async def search_users(self, query: str) -> list[User]:
        """按 TG ID / 用户名 / 昵称模糊检索（管理员用，上限 20）。"""
        like = f"%{query}%"
        rows = await self._all(
            """SELECT * FROM users
            WHERE CAST(telegram_id AS TEXT) = ? OR username LIKE ? OR display_name LIKE ?
            ORDER BY id LIMIT 20""",
            (query, like, like),
        )
        return [_row_to_user(r) for r in rows]

    async def refresh_user_profile(self, telegram_id: int, username: str | None, display_name: str | None) -> None:
        """已注册用户的资料刷新（改名/改昵称）；未注册则忽略（由 /start 建档）。"""
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE users SET username = ?, display_name = ? WHERE telegram_id = ?",
                (username, display_name, telegram_id),
            )

    async def list_products(self) -> list[Product]:
        return [_row_to_product(r) for r in await self._all("SELECT * FROM products WHERE active = 1 ORDER BY id")]

    async def list_products_page(self, page: int, page_size: int) -> tuple[list[Product], int, int]:
        if page < 0 or not 1 <= page_size <= 20:
            raise ValueError("invalid catalog page")
        async with self.connection() as conn:
            async with conn.execute("SELECT COUNT(*) FROM products WHERE active = 1") as cur:
                row = await cur.fetchone()
            assert row is not None
            page_count = max(1, (row[0] + page_size - 1) // page_size)
            current_page = min(page, page_count - 1)
            async with conn.execute(
                "SELECT * FROM products WHERE active = 1 ORDER BY id LIMIT ? OFFSET ?",
                (page_size, current_page * page_size),
            ) as cur:
                products = [_row_to_product(row) for row in await cur.fetchall()]
        return products, current_page, page_count

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
                    VALUES (?, ?, 0, 'USD', 0, ?, ?, ?)""",
                    (name, description, sku, upstream_plan_id, request_type),
                )
                return True
            # 名称/描述只在新建时写入：人工改名（/rename）不被目录同步覆盖；
            # request_type 保留人工配置（COALESCE）：管理员指定的业务类型不被目录同步覆盖
            await conn.execute(
                """UPDATE products SET upstream_plan_id = ?,
                request_type = COALESCE(request_type, ?)
                WHERE id = ?""",
                (upstream_plan_id, request_type, row["id"]),
            )
            return False

    # ---- purchases ----

    async def ensure_purchase(self, order_id: int, *, request_type: str, sku: str, quantity: int) -> Purchase:
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
        row = await self._one("SELECT * FROM purchases WHERE upstream_request_id = ?", (upstream_request_id,))
        return _row_to_purchase(row) if row else None

    async def get_purchase_conflict(self, order_id: int, upstream_request_id: str) -> Purchase | None:
        row = await self._one(
            "SELECT * FROM purchases WHERE upstream_request_id = ? AND order_id != ?",
            (upstream_request_id, order_id),
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
        return _row_to_purchase(updated)

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
        async with self.transaction() as conn:
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
                return _row_to_order(row)
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
        return _row_to_order(updated)

    async def _invalidate_delivery(self, conn: aiosqlite.Connection, order: aiosqlite.Row, note: str) -> Order:
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
        return _row_to_order(updated)

    async def reconcile_delivery(self, order_id: int) -> Order | None:
        """真实采购的历史恢复：同引用补齐终态；引用改变则重新查询交付，不能信任旧 payload。"""
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
                order = await cur.fetchone()
            if order is None:
                return None
            if order["status"] != OrderStatus.DELIVERED:
                return _row_to_order(order)
            async with conn.execute("SELECT * FROM purchases WHERE order_id = ?", (order_id,)) as cur:
                purchase = await cur.fetchone()
            if purchase is None or purchase["state"] in (PurchaseState.SUBMISSION_UNKNOWN, PurchaseState.REJECTED):
                return _row_to_order(order)
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
                return _row_to_order(order)
            if upstream_ref != order["upstream_ref"]:
                await conn.execute(
                    """UPDATE purchases SET state = 'upstream_pending', last_error = NULL,
                    updated_at = datetime('now') WHERE id = ?""",
                    (purchase["id"],),
                )
                return await self._invalidate_delivery(
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
            return _row_to_order(order)

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
        async with self.transaction() as conn:
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
            await self._invalidate_delivery(
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
        return _row_to_purchase(updated), None

    async def list_all_products(self) -> list[Product]:
        return [_row_to_product(r) for r in await self._all("SELECT * FROM products ORDER BY id")]

    async def configure_product(
        self,
        product_id: int,
        *,
        actor_id: int | None = None,
        price_cents: int | None = None,
        currency: str | None = None,
        name: str | None = None,
        description: str | None = None,
        active: bool | None = None,
        require_upstream: bool = False,
    ) -> Product | None:
        if price_cents is not None and not 0 < price_cents <= 999999999:
            raise ValueError("价格需大于零且不超过 9999999.99")
        if currency is not None:
            currency = normalize_currency(currency)
        if name is not None:
            name = name.strip()
            if not 0 < len(name) <= 100:
                raise ValueError("名称需为 1–100 个字符")
            if any(ord(c) < 0x20 or c == "\x7f" for c in name):
                # 名称会进入买家可见的目录按钮与详情，换行等控制字符会破坏排版
                raise ValueError("名称不能包含换行等控制字符")
        if description is not None:
            description = description.strip()
            if len(description) > 500:
                raise ValueError("描述最长 500 个字符")
            if any(ord(c) < 0x20 or c == "\x7f" for c in description):
                # 描述显示在目录正文与详情页，控制字符会破坏排版
                raise ValueError("描述不能包含换行等控制字符")
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            before = {key: row[key] for key in ("name", "description", "price_cents", "currency", "active")}
            after = {
                "name": name if name is not None else row["name"],
                "description": description if description is not None else row["description"],
                "price_cents": price_cents if price_cents is not None else row["price_cents"],
                "currency": currency if currency is not None else row["currency"],
                "active": int(active) if active is not None else row["active"],
            }
            if active:
                if after["price_cents"] <= 0:
                    raise ValueError("请先用 /price 设置有效售价")
                normalize_currency(after["currency"])
                if require_upstream and (
                    not row["sku"] or not row["upstream_plan_id"] or row["request_type"] not in REQUEST_TYPES
                ):
                    raise ValueError("真实商品缺少 SKU、上游套餐或明确业务类型，暂不能上架")
            async with conn.execute(
                """UPDATE products SET name = ?, description = ?, price_cents = ?, currency = ?, active = ?
                WHERE id = ? RETURNING *""",
                (
                    after["name"],
                    after["description"],
                    after["price_cents"],
                    after["currency"],
                    after["active"],
                    product_id,
                ),
            ) as cur:
                updated = await cur.fetchone()
            if before != after:
                await conn.execute(
                    "INSERT INTO product_events (product_id, actor_id, before_json, after_json) VALUES (?, ?, ?, ?)",
                    (product_id, actor_id, json.dumps(before), json.dumps(after)),
                )
        return _row_to_product(updated) if updated else None

    async def set_product_currency(
        self,
        product_id: int,
        currency: str,
        *,
        actor_id: int | None = None,
    ) -> Product | None:
        return await self.configure_product(product_id, currency=currency, actor_id=actor_id)

    async def ping(self) -> None:
        await self._one("SELECT 1 FROM users LIMIT 1")

    async def operational_issues(self, stale_seconds: int, notification_seconds: int) -> dict[str, str]:
        queries = {
            "manual_purchases": (
                """SELECT o.id, COUNT(*) OVER() AS total FROM orders o JOIN purchases p ON p.order_id = o.id
                WHERE o.status IN ('paid', 'delivered') AND p.state = 'submission_unknown' ORDER BY o.id LIMIT 5""",
                (),
                "采购结果不明，需 /purchases 核对",
            ),
            "stalled_orders": (
                """SELECT o.id, COUNT(*) OVER() AS total FROM orders o LEFT JOIN purchases p ON p.order_id = o.id
                WHERE o.status = 'paid' AND (p.id IS NULL OR p.state IN (
                    'ready', 'submitting', 'upstream_pending', 'refund_pending', 'rejected'
                )) AND COALESCE(p.updated_at, o.updated_at) <= datetime('now', ?)
                ORDER BY o.id LIMIT 5""",
                (f"-{stale_seconds} seconds",),
                "付款后履约/退款长时间未完成",
            ),
            "wallet_notifications": (
                """SELECT entity_id AS id, COUNT(*) OVER() AS total FROM work_items
                WHERE kind = 'wallet' AND created_at <= datetime('now', ?) ORDER BY id LIMIT 5""",
                (f"-{notification_seconds} seconds",),
                "钱包流水通知持续未送达",
            ),
            "pending_notifications": (
                """SELECT id, COUNT(*) OVER() AS total FROM orders
                WHERE notification_pending = 1 AND status IN ('delivered', 'refunded')
                AND updated_at <= datetime('now', ?) ORDER BY id LIMIT 5""",
                (f"-{notification_seconds} seconds",),
                "买家通知持续未送达",
            ),
        }
        issues = {}
        for key, (sql, params, summary) in queries.items():
            rows = await self._all(sql, params)
            if rows:
                ids = ", ".join(f"#{r['id']}" for r in rows)
                issues[key] = f"{summary}：{rows[0]['total']} 单（{ids}）"
        return issues

    async def set_alert(self, key: str, summary: str | None) -> None:
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM operator_alerts WHERE key = ?", (key,)) as cur:
                old = await cur.fetchone()
            if summary is None:
                if old is not None and old["active"]:
                    await conn.execute(
                        """UPDATE operator_alerts SET active = 0, revision = revision + 1,
                        updated_at = datetime('now') WHERE key = ?""",
                        (key,),
                    )
            elif old is None:
                await conn.execute("INSERT INTO operator_alerts (key, summary) VALUES (?, ?)", (key, summary))
            elif not old["active"] or old["summary"] != summary:
                await conn.execute(
                    """UPDATE operator_alerts SET summary = ?, active = 1, revision = revision + 1,
                    updated_at = datetime('now') WHERE key = ?""",
                    (summary, key),
                )

    async def pending_alerts(self, admin_id: int, now: float, cooldown: float) -> list[dict[str, Any]]:
        rows = await self._all(
            """SELECT a.* FROM operator_alerts a LEFT JOIN alert_deliveries d
            ON d.alert_key = a.key AND d.admin_id = ?
            WHERE (a.active = 1 OR d.revision IS NOT NULL)
            AND (d.revision IS NULL OR a.revision > d.revision OR (
                a.active = 1 AND a.key != 'stalled_orders' AND d.last_sent <= ?
            ))
            ORDER BY COALESCE(d.last_sent, 0), a.key LIMIT 10""",
            (admin_id, now - cooldown),
        )
        return [dict(row) for row in rows]

    async def mark_alert_sent(self, key: str, admin_id: int, revision: int, now: float) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO alert_deliveries (alert_key, admin_id, revision, last_sent) VALUES (?, ?, ?, ?)
                ON CONFLICT(alert_key, admin_id) DO UPDATE SET revision = excluded.revision,
                last_sent = excluded.last_sent""",
                (key, admin_id, revision, now),
            )

    async def get_balance(self, user_id: int, currency: str) -> int:
        row = await self._one(
            "SELECT balance_cents FROM wallet_balances WHERE user_id = ? AND currency = ?",
            (user_id, normalize_currency(currency)),
        )
        return row["balance_cents"] if row else 0

    async def get_balances(self, user_id: int) -> dict[str, int]:
        rows = await self._all("SELECT currency, balance_cents FROM wallet_balances WHERE user_id = ?", (user_id,))
        return {row["currency"]: row["balance_cents"] for row in rows}

    async def _change_wallet(
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

    async def _check_receipt(
        self,
        conn: aiosqlite.Connection,
        trade_no: str,
        *,
        order_id: int | None = None,
        topup_id: int | None = None,
    ) -> aiosqlite.Row | None:
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
        async with self.transaction() as conn:
            async with conn.execute(
                """UPDATE orders SET payment_method = 'epay' WHERE id = ? AND user_id = ?
                AND currency = ? AND amount_cents > 0 AND status = 'pending_payment'
                AND (payment_method IS NULL OR payment_method = 'epay') RETURNING *""",
                (order_id, user_id, currency),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_order(row) if row else None

    async def record_epay_payment(self, order_id: int, trade_no: str) -> tuple[Order, str]:
        """调用方已验签并核对订单/金额/币种；重复收款同币种补偿入钱包。"""
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
                order = await cur.fetchone()
            if order is None:
                raise ValueError("order not found")
            receipt = await self._check_receipt(conn, trade_no, order_id=order_id)
            if receipt is not None:
                return _row_to_order(order), receipt["disposition"]
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
                balance = await self._change_wallet(
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
        return _row_to_order(updated), disposition

    async def confirm_order_payment(
        self,
        order_id: int,
        trade_no: str | None,
        *,
        retry_failed: bool = False,
    ) -> Order:
        """管理员确认：短事务核对当前状态，触发器原子保存采购及调度意图。"""
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                raise ValueError(f"order {order_id} not found")
            if row["status"] == "cancelled":
                raise ValueError(f"order {order_id} is cancelled")
            if trade_no is not None:
                if row["trade_no"] and row["trade_no"] != trade_no:
                    raise ValueError("payment transaction does not match order")
                await self._check_receipt(conn, trade_no, order_id=order_id)
            status = row["status"]
            if status == "pending_payment" or (status == "delivery_failed" and retry_failed):
                status = "paid"
            if status == row["status"] and (trade_no is None or trade_no == row["trade_no"]):
                return _row_to_order(row)
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
        return _row_to_order(updated)

    async def create_topup(self, user_id: int, amount_cents: int, currency: str = "CNY") -> Topup:
        currency = normalize_currency(currency)
        if amount_cents <= 0:
            raise ValueError("topup amount must be positive")
        async with self.transaction() as conn:
            async with conn.execute(
                "INSERT INTO balance_topups (user_id, amount_cents, currency) VALUES (?, ?, ?) RETURNING *",
                (user_id, amount_cents, currency),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return _row_to_topup(row)

    async def get_topup(self, topup_id: int) -> Topup | None:
        row = await self._one("SELECT * FROM balance_topups WHERE id = ?", (topup_id,))
        return _row_to_topup(row) if row else None

    async def complete_topup(self, topup_id: int, trade_no: str) -> Topup | None:
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM balance_topups WHERE id = ?", (topup_id,)) as cur:
                topup = await cur.fetchone()
            if topup is None:
                return None
            receipt = await self._check_receipt(conn, trade_no, topup_id=topup_id)
            if receipt is not None or topup["trade_no"] == trade_no:
                return _row_to_topup(topup)
            if topup["status"] not in ("pending", "paid"):
                return None
            balance = await self._change_wallet(
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
        return _row_to_topup(paid)

    async def pay_order_with_balance(
        self, order_id: int, user_id: int, amount_cents: int
    ) -> tuple[Order | None, str | None]:
        async with self.transaction() as conn:
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
            balance = await self._change_wallet(
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
        return _row_to_order(paid), None

    async def adjust_balance(self, user_id: int, delta_cents: int, note: str, currency: str = "CNY") -> int | None:
        async with self.transaction() as conn:
            return await self._change_wallet(conn, user_id, currency, delta_cents, "adjust", note=note)

    async def refund_order_to_balance(self, order_id: int, note: str) -> tuple[Order | None, str | None]:
        """人工退款与履约/KYC 共用订单锁；正在上游处理的订单不得直接退款。"""
        async with self.order_operation(order_id), self.transaction() as conn:
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
        async with self.transaction() as conn:
            async with conn.execute(
                """UPDATE purchases SET state = 'refund_pending', last_error = ?, updated_at = datetime('now')
                WHERE id = ? AND order_id = ? AND state = ? AND EXISTS (
                    SELECT 1 FROM orders WHERE id = ? AND status = 'paid'
                ) RETURNING id""",
                (note, purchase_id, order_id, from_state, order_id),
            ) as cur:
                if await cur.fetchone() is None:
                    return None, "purchase state changed"
        async with self.transaction() as conn:
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
        balance = await self._change_wallet(
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
        return _row_to_order(refunded), None

    async def list_balance_transactions(self, user_id: int, limit: int = 5) -> list[BalanceTransaction]:
        rows = await self._all(
            "SELECT * FROM balance_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [_row_to_balance_tx(r) for r in rows]

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
        plan_id: str | None = None,
        expected_product: Product | None = None,
    ) -> Order:
        """创建订单并固定本次采购输入快照（SKU/业务类型/套餐/数量/天数/ICCID/号码）。

        快照在下单时锁定，之后商品目录变更不影响已创建订单的采购与交付核验。
        """
        async with self.transaction() as conn:
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
        """兼容管理/测试入口；运行时调度只领取有限数量的 ID。"""
        rows = await self._all("""
            SELECT * FROM orders WHERE id IN (
                SELECT entity_id FROM work_items WHERE kind IN ('purchase', 'delivery')
            ) ORDER BY id
        """)
        return [_row_to_order(row) for row in rows]

    async def claim_work(self, kind: str) -> WorkItem | None:
        async with self.connection() as conn:
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
        async with self.transaction() as conn:
            if done:
                await conn.execute("DELETE FROM work_items WHERE id = ? AND revision = ?", (item.id, item.revision))
            else:
                await conn.execute(
                    """UPDATE work_items SET due_at = MAX(due_at, ?), attempts = attempts + 1
                    WHERE id = ? AND revision = ?""",
                    (time.time() + delay, item.id, item.revision),
                )
            await conn.execute("UPDATE work_items SET claimed = 0 WHERE id = ?", (item.id,))

    async def get_wallet_notification(self, transaction_id: int) -> dict[str, Any] | None:
        row = await self._one(
            """SELECT t.*, u.telegram_id FROM balance_transactions t
            JOIN users u ON u.id = t.user_id WHERE t.id = ?""",
            (transaction_id,),
        )
        return dict(row) if row else None

    async def queue_redelivery(self, order_id: int) -> None:
        # 已在发送时保留 cursor；已完成时归零。短事务不等待网络持有的订单锁。
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE orders SET notification_pending = 1, notification_cursor = 0
                WHERE id = ? AND status = 'delivered' AND notification_pending = 0""",
                (order_id,),
            )

    async def mark_notified(self, order_id: int) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE orders SET notified_at = datetime('now'), notification_pending = 0,
                notification_retry_at = NULL
                WHERE id = ? AND status IN ('delivered', 'refunded')""",
                (order_id,),
            )

    async def request_notification(self, order_id: int) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE orders SET notification_pending = 1, notification_cursor = 0"
                " WHERE id = ? AND status = 'delivered'",
                (order_id,),
            )

    async def prepare_notification(self, order_id: int, version: int) -> Order | None:
        """调用方持有订单锁；新方案一次性重置未完成进度，不触碰已完成通知或货品。"""
        async with self.transaction() as conn:
            async with conn.execute(
                """UPDATE orders SET notification_cursor = CASE WHEN notification_plan_version = ?
                    THEN notification_cursor ELSE 0 END, notification_plan_version = ?
                WHERE id = ? AND status = 'delivered' AND notification_pending = 1 RETURNING *""",
                (version, version, order_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_order(row) if row else None

    async def advance_notification(self, order_id: int, expected_cursor: int) -> bool:
        async with self.transaction() as conn:
            async with conn.execute(
                """UPDATE orders SET notification_cursor = notification_cursor + 1
                WHERE id = ? AND status = 'delivered' AND notification_pending = 1
                AND notification_cursor = ? RETURNING id""",
                (order_id, expected_cursor),
            ) as cur:
                return await cur.fetchone() is not None

    async def defer_notification(self, order_id: int, retry_at: float) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE orders SET notification_retry_at = ? WHERE id = ? AND notification_pending = 1",
                (retry_at, order_id),
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
    keys = row.keys()
    return User(
        id=row["id"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        display_name=row["display_name"] if "display_name" in keys else None,
        balance_cents=row["balance_cents"],
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


def _row_to_topup(row: aiosqlite.Row) -> Topup:
    return Topup(
        id=row["id"],
        user_id=row["user_id"],
        currency=row["currency"],
        amount_cents=row["amount_cents"],
        status=TopupState(row["status"]),
        trade_no=row["trade_no"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_balance_tx(row: aiosqlite.Row) -> BalanceTransaction:
    return BalanceTransaction(
        id=row["id"],
        user_id=row["user_id"],
        currency=row["currency"],
        amount_cents=row["amount_cents"],
        balance_after=row["balance_after"],
        kind=row["kind"],
        order_id=row["order_id"],
        topup_id=row["topup_id"],
        note=row["note"],
        created_at=row["created_at"],
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
        input_plan_id=row["input_plan_id"],
        payment_method=row["payment_method"],
        delivery_esims=row["delivery_esims"],
        notification_cursor=row["notification_cursor"],
        notification_retry_at=row["notification_retry_at"],
        notification_plan_version=row["notification_plan_version"],
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
