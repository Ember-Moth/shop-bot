"""模型层：基础表结构与旧库就地迁移。

``work_queue`` 与 ``business_notifications`` 各自维护任务表、视图和触发器；
三段迁移在 ``Database.connect()`` 的同一个事务内执行。
"""

from __future__ import annotations

import aiosqlite

from ..logging_config import get_logger

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

CREATE TABLE IF NOT EXISTS daily_report_log (
    report_date TEXT PRIMARY KEY,  -- 报表覆盖的本地日期（YYYY-MM-DD），防止同日重复发送
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
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


async def migrate(conn: aiosqlite.Connection) -> None:
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
