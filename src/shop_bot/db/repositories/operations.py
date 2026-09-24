"""运维仓储：停滞检查、管理员告警去重与每日报表登记。"""

from __future__ import annotations

from typing import Any

from .base import Repository


class OperationsRepository(Repository):
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
            "business_notifications": (
                """SELECT entity_id AS id, COUNT(*) OVER() AS total FROM work_items
                WHERE kind = 'business' AND created_at <= datetime('now', ?) ORDER BY id LIMIT 5""",
                (f"-{notification_seconds} seconds",),
                "业务广播持续未送达（通知任务编号）",
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
            rows = await self._db.fetch_all(sql, params)
            if rows:
                ids = ", ".join(f"#{r['id']}" for r in rows)
                issues[key] = f"{summary}：{rows[0]['total']} 单（{ids}）"
        return issues

    async def daily_summary(self, start_utc: str, end_utc: str) -> dict[str, Any]:
        """按 [start_utc, end_utc) 窗口汇总昨日流水；created_at 均为 UTC 存储。"""
        orders_rows = await self._db.fetch_all(
            """SELECT status, COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents, currency
            FROM orders WHERE created_at >= ? AND created_at < ? GROUP BY status, currency""",
            (start_utc, end_utc),
        )
        receipts_rows = await self._db.fetch_all(
            """SELECT currency,
                COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents,
                SUM(CASE WHEN order_id IS NOT NULL THEN 1 ELSE 0 END) AS order_n,
                COALESCE(SUM(CASE WHEN order_id IS NOT NULL THEN amount_cents ELSE 0 END), 0) AS order_cents
            FROM payment_receipts WHERE created_at >= ? AND created_at < ? GROUP BY currency""",
            (start_utc, end_utc),
        )
        tx_rows = await self._db.fetch_all(
            """SELECT kind, currency, COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents
            FROM balance_transactions WHERE created_at >= ? AND created_at < ? GROUP BY kind, currency""",
            (start_utc, end_utc),
        )
        wallet_rows = await self._db.fetch_all(
            "SELECT currency, COALESCE(SUM(balance_cents), 0) AS cents FROM wallet_balances GROUP BY currency"
        )
        return {
            "orders": [dict(r) for r in orders_rows],
            "receipts": [dict(r) for r in receipts_rows],
            "transactions": [dict(r) for r in tx_rows],
            "wallet_total": [dict(r) for r in wallet_rows],
        }

    async def report_sent(self, report_date: str) -> bool:
        row = await self._db.fetch_one("SELECT 1 FROM daily_report_log WHERE report_date = ?", (report_date,))
        return row is not None

    async def mark_report_sent(self, report_date: str) -> bool:
        """登记某日报表已送达；当天已登记过（重启重入）返回 False。"""
        async with self._db.transaction() as conn:
            cur = await conn.execute("INSERT OR IGNORE INTO daily_report_log (report_date) VALUES (?)", (report_date,))
            return cur.rowcount > 0

    async def set_alert(self, key: str, summary: str | None) -> None:
        async with self._db.transaction() as conn:
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
        rows = await self._db.fetch_all(
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
        async with self._db.transaction() as conn:
            await conn.execute(
                """INSERT INTO alert_deliveries (alert_key, admin_id, revision, last_sent) VALUES (?, ?, ?, ?)
                ON CONFLICT(alert_key, admin_id) DO UPDATE SET revision = excluded.revision,
                last_sent = excluded.last_sent""",
                (key, admin_id, revision, now),
            )
