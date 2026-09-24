"""SQLite 持久化任务：状态变更与入队同事务，后台只读取到期的任务 ID。

触发器覆盖所有收款、退款、重绑及交付入口，避免某个入口漏入队。
仅支持单进程部署；启动时释放上个进程的领取标记，采购状态机决定能否重试。
"""

from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True)
class WorkItem:
    id: int
    kind: str
    entity_id: int
    attempts: int
    revision: int


async def migrate_work_queue(conn: aiosqlite.Connection) -> None:
    # 不用 executescript：它会隐式提交外层迁移事务。
    statements = [
        """CREATE TABLE IF NOT EXISTS work_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            due_at REAL NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0,
            claimed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(kind, entity_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_work_due ON work_items(kind, claimed, due_at, id)",
        "CREATE INDEX IF NOT EXISTS idx_topups_trade_no ON balance_topups(trade_no)",
        """CREATE VIEW IF NOT EXISTS purchase_work_needed AS
            SELECT o.id AS entity_id FROM orders o LEFT JOIN purchases p ON p.order_id = o.id
            WHERE (o.status = 'paid' AND (
                p.id IS NULL OR p.state IN ('ready', 'submitting', 'upstream_pending',
                    'refund_pending', 'rejected', 'fulfilled')
                OR (p.state IN ('awaiting_kyc', 'kyc_submitted') AND p.upstream_request_id IS NOT NULL)
            )) OR (o.status = 'delivered' AND p.state NOT IN ('rejected', 'submission_unknown')
                AND (p.state != 'fulfilled' OR (p.upstream_request_id IS NOT NULL
                    AND p.upstream_request_id IS NOT o.upstream_ref)))""",
        """CREATE VIEW IF NOT EXISTS delivery_work_needed AS
            SELECT o.id AS entity_id, COALESCE(o.notification_retry_at, 0) AS due_at
            FROM orders o LEFT JOIN purchases p ON p.order_id = o.id
            WHERE o.notification_pending = 1 AND (o.status = 'refunded' OR (
                o.status = 'delivered' AND (p.id IS NULL OR (p.state = 'fulfilled'
                    AND o.upstream_ref = COALESCE(p.upstream_request_id, printf('STUB-%06d', o.id))))
            ))""",
    ]
    for sql in statements:
        await conn.execute(sql)

    # paid 的订单快照是采购唯一来源；只在旧快照缺失时读取商品。
    purchase_insert = """
        INSERT OR IGNORE INTO purchases (order_id, request_type, sku, quantity)
        SELECT o.id, COALESCE(NULLIF(o.input_request_type, ''), NULLIF(p.request_type, ''), 'unknown'),
            COALESCE(NULLIF(o.input_sku, ''), NULLIF(p.sku, ''), 'UNMAPPED-PRODUCT-' || o.product_id), o.quantity
        FROM orders o LEFT JOIN products p ON p.id = o.product_id
        WHERE o.status = 'paid'
    """
    # 同一个事务中建立采购记录、调度任务。外部网络调用从不持有这个事务。
    for event in ("INSERT", "UPDATE OF status"):
        suffix = "insert" if event == "INSERT" else "update"
        await conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS paid_purchase_{suffix} AFTER {event} ON orders
            WHEN NEW.status = 'paid' BEGIN
                {purchase_insert} AND o.id = NEW.id;
            END
        """)

    for table, event, suffix, entity in (
        ("orders", "INSERT", "insert", "NEW.id"),
        ("orders", "UPDATE OF status, upstream_ref, notification_pending, notification_retry_at", "update", "NEW.id"),
        ("purchases", "INSERT", "insert", "NEW.order_id"),
        ("purchases", "UPDATE OF state, upstream_request_id", "update", "NEW.order_id"),
    ):
        body = ""
        for kind, view, due in (
            ("purchase", "purchase_work_needed", "0"),
            ("delivery", "delivery_work_needed", "due_at"),
        ):
            body += f"""
                DELETE FROM work_items WHERE kind = '{kind}' AND entity_id = {entity}
                    AND NOT EXISTS (SELECT 1 FROM {view} WHERE entity_id = {entity});
                INSERT INTO work_items (kind, entity_id, due_at)
                    SELECT '{kind}', entity_id, {due} FROM {view} WHERE entity_id = {entity}
                    ON CONFLICT(kind, entity_id) DO UPDATE SET
                        due_at = excluded.due_at, attempts = 0, revision = work_items.revision + 1;
            """  # noqa: S608 - identifiers are fixed internal constants
        guard = ""
        if suffix == "update":
            fields = (
                ("state", "upstream_request_id")
                if table == "purchases"
                else ("status", "upstream_ref", "notification_pending", "notification_retry_at")
            )
            guard = "WHEN " + " OR ".join(f"NEW.{field} IS NOT OLD.{field}" for field in fields)
        await conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS work_{table}_{suffix} AFTER {event} ON {table}
            {guard} BEGIN {body} END
        """)
    await conn.execute("""
        CREATE TRIGGER IF NOT EXISTS wallet_notification AFTER INSERT ON balance_transactions
        WHEN NEW.kind IN ('topup', 'adjust', 'payment_credit') BEGIN
            INSERT INTO work_items (kind, entity_id) VALUES ('wallet', NEW.id);
        END
    """)
    # 启动时做历史一致性修复；平时扫描不再遍历已交付历史订单。
    await conn.execute(purchase_insert)
    for kind, view, due in (
        ("purchase", "purchase_work_needed", "0"),
        ("delivery", "delivery_work_needed", "due_at"),
    ):
        await conn.execute(f"""
            INSERT OR IGNORE INTO work_items (kind, entity_id, due_at)
            SELECT '{kind}', entity_id, {due} FROM {view}
        """)  # noqa: S608 - identifiers are fixed internal constants
    # 历史钱包流水没有通知证据，不能在升级时群发；仅恢复已有任务。
    await conn.execute("UPDATE work_items SET claimed = 0")
