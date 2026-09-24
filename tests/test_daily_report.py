"""每日流水报表：窗口换算、汇总查询、格式化、送达幂等与失败重试。"""

from datetime import datetime, timedelta, timezone

from shop_bot.config import Settings
from shop_bot.services.daily_report import (
    format_daily_report,
    seconds_until_next_midnight,
    send_daily_report,
    yesterday_window,
)


def test_yesterday_window_converts_local_midnight_to_utc():
    """本地（UTC+8）昨天 0 点应换算为前一日 16:00 UTC；库里 created_at 是 UTC。"""
    now = datetime(2026, 9, 19, 15, 30, tzinfo=timezone(timedelta(hours=8)))
    label, start, end = yesterday_window(now)
    assert label == "2026-09-18"
    assert start == "2026-09-17 16:00:00"
    assert end == "2026-09-18 16:00:00"


def test_seconds_until_next_midnight():
    now = datetime(2026, 9, 19, 23, 45, tzinfo=timezone(timedelta(hours=8)))
    assert seconds_until_next_midnight(now) == 15 * 60
    now = datetime(2026, 9, 19, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert seconds_until_next_midnight(now) == 24 * 3600


async def test_daily_summary_groups_by_kind_currency_and_purpose(db, user):
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO payment_receipts (trade_no, order_id, amount_cents, currency, disposition, created_at)"
            " VALUES ('R1', 1, 700, 'USD', 'order', '2026-09-18 02:00:00')"
        )
        await conn.execute(
            """INSERT INTO payment_receipts
            (trade_no, order_id, topup_id, amount_cents, currency, disposition, created_at)
            VALUES ('R2', NULL, 1, 2000, 'USD', 'topup', '2026-09-18 09:00:00')"""
        )
        await conn.execute(
            "INSERT INTO payment_receipts (trade_no, order_id, amount_cents, currency, disposition, created_at)"
            " VALUES ('R3', NULL, 0, 'USD', 'topup', '2026-09-19 01:00:00')"  # 窗口外
        )
        await conn.execute(
            "INSERT INTO balance_transactions (user_id, amount_cents, balance_after, kind, currency, created_at)"
            " VALUES (?, 700, 700, 'refund', 'USD', '2026-09-18 05:00:00')",
            (user.id,),
        )
        await conn.execute(
            "INSERT INTO balance_transactions (user_id, amount_cents, balance_after, kind, currency, created_at)"
            " VALUES (?, -630, 70, 'purchase', 'USD', '2026-09-18 06:00:00')",
            (user.id,),
        )
    summary = await db.operations.daily_summary("2026-09-17 16:00:00", "2026-09-18 16:00:00")
    receipts = {r["currency"]: r for r in summary["receipts"]}
    assert receipts["USD"]["n"] == 2 and receipts["USD"]["cents"] == 2700
    assert receipts["USD"]["order_cents"] == 700 and receipts["USD"]["order_n"] == 1
    txs = {(t["kind"], t["currency"]): t for t in summary["transactions"]}
    assert txs[("refund", "USD")]["n"] == 1 and txs[("refund", "USD")]["cents"] == 700
    assert txs[("purchase", "USD")]["cents"] == -630
    assert len(summary["receipts"]) == 1  # R3 在窗口外不进分组


def test_format_daily_report_renders_sections():
    summary = {
        "orders": [
            {"status": "delivered", "n": 3, "cents": 2100, "currency": "USD"},
            {"status": "refunded", "n": 1, "cents": 700, "currency": "USD"},
        ],
        "receipts": [
            {"currency": "USD", "n": 3, "cents": 2700, "order_n": 1, "order_cents": 700},
        ],
        "transactions": [{"kind": "refund", "currency": "USD", "n": 1, "cents": 700}],
        "wallet_total": [{"currency": "USD", "cents": 123456}],
    }
    text = format_daily_report(summary, "2026-09-18")
    assert "📊 每日流水 · 2026-09-18" in text
    assert "已交付 3" in text and "已退款 1" in text
    assert "7.00 USD 商品款（1 笔）" in text
    assert "20.00 USD 充值（2 笔）" in text
    assert "退款回余额 7.00 USD（1 笔）" in text
    assert "1234.56 USD" in text


async def test_send_daily_report_marks_only_after_delivery(db, user, bot, monkeypatch):
    """送达成功才登记；登记后当天重入不再发送；全部失败可重试。"""
    settings = Settings(bot_token=bot.token, admin_ids=[42])
    monkeypatch.setattr(
        "shop_bot.services.daily_report.yesterday_window",
        lambda: ("2026-09-18", "2026-09-17 16:00:00", "2026-09-18 16:00:00"),
    )
    # 第一次全部失败：不登记，可重试
    bot.session.fail_send = True
    assert not await send_daily_report(db, bot, settings)
    assert not await db.operations.report_sent("2026-09-18")
    # 重试成功：登记
    bot.session.fail_send = False
    assert await send_daily_report(db, bot, settings)
    assert await db.operations.report_sent("2026-09-18")
    sent_count = len(bot.session.sent)
    # 当天再次调用（重启重入）：直接返回，不再发送
    assert await send_daily_report(db, bot, settings)
    assert len(bot.session.sent) == sent_count
