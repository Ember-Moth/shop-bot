"""GMPay 交易表：每次向 epusdt 下单一行，保存商户订单号、epusdt 交易号和链上收款信息。"""

import aiosqlite


async def migrate_gmpay(conn: aiosqlite.Connection) -> None:
    for sql in (
        """CREATE TABLE IF NOT EXISTS gmpay_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_order_no TEXT NOT NULL UNIQUE,
            order_id INTEGER REFERENCES orders(id),
            topup_id INTEGER REFERENCES balance_topups(id),
            amount_cents INTEGER NOT NULL,
            currency TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'creating',
            trade_id TEXT UNIQUE,
            token TEXT,
            network TEXT,
            receive_address TEXT,
            actual_amount TEXT,
            payment_url TEXT,
            expires_at REAL,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK ((order_id IS NULL) != (topup_id IS NULL))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gmpay_order ON gmpay_trades(order_id) WHERE order_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_gmpay_topup ON gmpay_trades(topup_id) WHERE topup_id IS NOT NULL",
    ):
        await conn.execute(sql)
