"""每日 0 点向管理员私信昨日流水的定时任务。

窗口按服务器本地时区计算「昨天」，再换算成 UTC 与库内 created_at 比较；
送达成功才写入 daily_report_log（发送失败不标记，可重试），
进程重启后若发现昨日报表未发也会补发。长睡眠按 30 秒分片并打心跳，
避免被 worker 监督误判为失联。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot

from ..config import Settings
from ..db import Database
from ..logging_config import get_logger
from ..models import OrderStatus
from .balance import format_cents

if TYPE_CHECKING:
    from .operations import RuntimeState

logger = get_logger(__name__)

HEARTBEAT_SLICE = 30.0  # 分片睡眠上限，期间持续上报心跳
SEND_RETRY_DELAYS = (0, 900, 1800, 3600)  # 发送失败的重试间隔（秒），用尽后等明天

_ORDER_STATUS_LABELS = {
    OrderStatus.PENDING_PAYMENT: "待支付",
    OrderStatus.PAID: "已付款",
    OrderStatus.DELIVERED: "已交付",
    OrderStatus.DELIVERY_FAILED: "交付失败",
    OrderStatus.CANCELLED: "已取消",
    OrderStatus.REFUNDED: "已退款",
}

_TX_KIND_LABELS = {
    "topup": "充值入账",
    "purchase": "余额消费",
    "refund": "退款回余额",
    "adjust": "人工调账",
    "payment_credit": "额外收款补偿",
}


def yesterday_window(now: datetime | None = None) -> tuple[str, str, str]:
    """返回 (本地日期标签, 窗口起点 UTC, 窗口终点 UTC)。now 仅测试注入用。

    库内 created_at 以 UTC 存储；本地 0 点需经 astimezone(utc) 换算后再比较。
    """
    local_now = now if now is not None else datetime.now().astimezone()
    start_local = (local_now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (
        start_local.strftime("%Y-%m-%d"),
        start_local.astimezone(UTC).strftime(fmt),
        end_local.astimezone(UTC).strftime(fmt),
    )


def seconds_until_next_midnight(now: datetime | None = None) -> float:
    local_now = now if now is not None else datetime.now().astimezone()
    next_midnight = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (next_midnight - local_now).total_seconds())


def format_daily_report(summary: dict, date_label: str) -> str:
    lines = [f"📊 每日流水 · {date_label}", "━━━━━━━━━━━━━━━━━━", ""]

    lines.append("订单（按创建日）：")
    orders_by_status: dict[str, int] = {}
    for row in summary["orders"]:
        status = str(row["status"])
        label = _ORDER_STATUS_LABELS.get(OrderStatus(status), status)
        orders_by_status[label] = orders_by_status.get(label, 0) + row["n"]
    if orders_by_status:
        lines.append("  " + " · ".join(f"{k} {v}" for k, v in sorted(orders_by_status.items())))
    else:
        lines.append("  无")

    lines.append("")
    lines.append("外部实收（EPay 到账）：")
    if summary["receipts"]:
        for row in summary["receipts"]:
            topup_cents = row["cents"] - row["order_cents"]
            parts = [f"{format_cents(row['order_cents'])} {row['currency']} 商品款（{row['order_n']} 笔）"]
            if topup_cents:
                parts.append(f"{format_cents(topup_cents)} {row['currency']} 充值（{row['n'] - row['order_n']} 笔）")
            lines.append("  " + "，".join(parts))
    else:
        lines.append("  无")

    lines.append("")
    lines.append("钱包变动（净额）：")
    if summary["transactions"]:
        for row in summary["transactions"]:
            label = _TX_KIND_LABELS.get(row["kind"], row["kind"])
            lines.append(f"  {label} {format_cents(row['cents'])} {row['currency']}（{row['n']} 笔）")
    else:
        lines.append("  无")

    lines.append("")
    wallet = sorted(summary["wallet_total"], key=lambda r: r["currency"])
    parts = [f"{format_cents(r['cents'])} {r['currency']}" for r in wallet]
    lines.append("当前钱包总余额：" + ("，".join(parts) if parts else "无"))
    return "\n".join(lines)


async def send_daily_report(db: Database, bot: Bot, settings: Settings) -> bool:
    """生成并发送昨日报表；至少送达一位管理员才登记为已发送。"""
    date_label, start_utc, end_utc = yesterday_window()
    if await db.report_sent(date_label):
        return True  # 当天已送达（重启重入）
    summary = await db.daily_summary(start_utc, end_utc)
    text = format_daily_report(summary, date_label)
    delivered = False
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, request_timeout=10)
            delivered = True
        except Exception as exc:
            logger.warning("daily report delivery failed", extra={"error": type(exc).__name__})
    if delivered:
        await db.mark_report_sent(date_label)
    return delivered


async def _attempt_yesterday_report(db: Database, bot: Bot, settings: Settings) -> None:
    for delay in SEND_RETRY_DELAYS:
        await asyncio.sleep(delay)
        try:
            if await send_daily_report(db, bot, settings):
                return
        except Exception as exc:
            logger.warning("daily report failed", extra={"error": type(exc).__name__})
    logger.warning("daily report gave up until tomorrow")


async def daily_report_loop(db: Database, bot: Bot, settings: Settings, runtime: RuntimeState | None = None) -> None:
    while True:
        # 启动/重启后：若昨天的报表尚未送达（如 0 点时服务不可用），先补发再进入常规调度
        if not await db.report_sent(yesterday_window()[0]):
            await _attempt_yesterday_report(db, bot, settings)
        remaining = seconds_until_next_midnight()
        while remaining > 0:  # 分片睡眠：长眠期间保持心跳，供 worker 监督判定存活
            if runtime is not None:
                runtime.beat("report")
            slice_ = min(remaining, HEARTBEAT_SLICE)
            await asyncio.sleep(slice_)
            remaining -= slice_
        await _attempt_yesterday_report(db, bot, settings)
        await asyncio.sleep(60)  # 跨过 0 点再进入下一轮，避免同日二次触发
