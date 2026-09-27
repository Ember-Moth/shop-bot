"""付款提示：记录发给买家的待付款消息，付款或关单时同事务入队，后台把它更新为最终状态。

只更新本 Bot 自己发出的消息（chat_id/message_id 来自发送结果），不接受外部输入的目标。
"""

import aiosqlite


async def migrate_payment_prompts(conn: aiosqlite.Connection) -> None:
    for sql in (
        """CREATE TABLE IF NOT EXISTS payment_prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            order_id INTEGER REFERENCES orders(id),
            topup_id INTEGER REFERENCES balance_topups(id),
            state TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(chat_id, message_id),
            CHECK ((order_id IS NULL) != (topup_id IS NULL))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_prompts_order ON payment_prompts(order_id) WHERE state = 'open'",
        "CREATE INDEX IF NOT EXISTS idx_prompts_topup ON payment_prompts(topup_id) WHERE state = 'open'",
        # 付款、取消与更新任务同事务提交；重复回调不会改变状态，也就不会重复入队。
        """CREATE TRIGGER IF NOT EXISTS prompt_order_settled AFTER UPDATE OF status ON orders
        WHEN OLD.status = 'pending_payment' AND NEW.status != 'pending_payment' BEGIN
            INSERT OR IGNORE INTO work_items (kind, entity_id)
            SELECT 'prompt', id FROM payment_prompts WHERE order_id = NEW.id AND state = 'open';
        END""",
        """CREATE TRIGGER IF NOT EXISTS prompt_topup_settled AFTER UPDATE OF status ON balance_topups
        WHEN OLD.status = 'pending' AND NEW.status != 'pending' BEGIN
            INSERT OR IGNORE INTO work_items (kind, entity_id)
            SELECT 'prompt', id FROM payment_prompts WHERE topup_id = NEW.id AND state = 'open';
        END""",
        # 付款先于提示落库（并发）时，记录提示的同一事务立即入队。
        """CREATE TRIGGER IF NOT EXISTS prompt_recorded_after_settlement AFTER INSERT ON payment_prompts
        WHEN EXISTS (SELECT 1 FROM orders WHERE id = NEW.order_id AND status != 'pending_payment')
            OR EXISTS (SELECT 1 FROM balance_topups WHERE id = NEW.topup_id AND status != 'pending') BEGIN
            INSERT OR IGNORE INTO work_items (kind, entity_id) VALUES ('prompt', NEW.id);
        END""",
        # 启动修复：已结算但仍打开的提示补入队列。
        """INSERT OR IGNORE INTO work_items (kind, entity_id)
            SELECT 'prompt', p.id FROM payment_prompts p WHERE p.state = 'open' AND (
                EXISTS (SELECT 1 FROM orders WHERE id = p.order_id AND status != 'pending_payment')
                OR EXISTS (SELECT 1 FROM balance_topups WHERE id = p.topup_id AND status != 'pending')
            )""",
    ):
        await conn.execute(sql)
