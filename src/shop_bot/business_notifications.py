"""业务广播的事务内快照与收件人任务；不回放启用前的历史事件。"""

import aiosqlite


async def migrate_business_notifications(conn: aiosqlite.Connection) -> None:
    for sql in (
        """CREATE TABLE IF NOT EXISTS business_routes (
            event TEXT NOT NULL, chat_id INTEGER NOT NULL, PRIMARY KEY(event, chat_id)
        )""",
        """CREATE TABLE IF NOT EXISTS business_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, entity_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')), sent_at TEXT,
            UNIQUE(event, entity_id, chat_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_business_pending ON business_deliveries(id) WHERE state = 'pending'",
        """CREATE TRIGGER IF NOT EXISTS business_delivery_work AFTER INSERT ON business_deliveries
        BEGIN INSERT INTO work_items (kind, entity_id) VALUES ('business', NEW.id); END""",
    ):
        await conn.execute(sql)
    order_payload = """json_object('order_id', NEW.id, 'product',
        COALESCE((SELECT name FROM products WHERE id = NEW.product_id), '商品'),
        'quantity', NEW.quantity, 'amount_cents', NEW.amount_cents, 'currency', NEW.currency,
        'payment_method', CASE WHEN NEW.trade_no IS NULL THEN 'manual' ELSE NEW.payment_method END)"""
    credited_payload = """json_object('topup_id', NEW.topup_id, 'amount_cents', NEW.amount_cents,
        'currency', NEW.currency)"""
    for event, table, operation, condition, payload in (
        (
            "order_paid",
            "orders",
            "UPDATE OF status",
            "OLD.status = 'pending_payment' AND NEW.status = 'paid'",
            order_payload,
        ),
        ("topup_paid", "balance_transactions", "INSERT", "NEW.kind = 'topup'", credited_payload),
    ):
        # 标识符与 SQL 表达式均来自上面的内部常量；收件人来自已验证的配置表。
        await conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS business_{event} AFTER {operation} ON {table}
            WHEN {condition} BEGIN
                INSERT OR IGNORE INTO business_deliveries (event, entity_id, chat_id, payload)
                SELECT '{event}', NEW.id, chat_id, {payload} FROM business_routes WHERE event = '{event}';
            END
        """)  # noqa: S608 - identifiers and expressions are fixed internal constants
