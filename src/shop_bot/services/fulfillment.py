"""私信交付与恢复。数据库记录是恢复依据，Telegram 通知失败不撤销付款。"""

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .operations import RuntimeState

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile

from ..db import Database
from ..logging_config import get_logger
from ..models import Order, OrderStatus, PurchaseState
from ..telegram_text import text_units
from ..work_queue import WorkItem
from .esim_media import MAX_ESIMS_PER_ARCHIVE, EsimMedia, delivery_esims, esim_zip, qr_png
from .notification_transport import NotificationThrottle
from .purchasing import Purchaser, split_payload_chunks

logger = get_logger(__name__)
RECOVERY_INTERVAL = 5
# 更改步骤顺序、数量或载荷含义时必须递增，不能复用旧方案的 cursor。
NOTIFICATION_PLAN_VERSION = 2


@dataclass(frozen=True)
class NotificationStep:
    text: str
    photo: EsimMedia | None = None
    photo_index: int = 0
    archive: tuple[EsimMedia, ...] = ()
    archive_start: int = 0


def notification_steps(order: Order, esims: list[EsimMedia]) -> list[NotificationStep]:
    if len(esims) >= 2:
        archive_steps = []
        part_count = (len(esims) + MAX_ESIMS_PER_ARCHIVE - 1) // MAX_ESIMS_PER_ARCHIVE
        for start in range(0, len(esims), MAX_ESIMS_PER_ARCHIVE):
            batch = tuple(esims[start : start + MAX_ESIMS_PER_ARCHIVE])
            part = f"（第 {start // MAX_ESIMS_PER_ARCHIVE + 1}/{part_count} 包）" if part_count > 1 else ""
            archive_steps.append(
                NotificationStep(
                    f"🎉 订单 #{order.id} · 共 {len(esims)} 张 eSIM{part}\n"
                    f"本包包含第 {start + 1}–{start + len(batch)} 张的二维码、ICCID 和完整 LPA。\n解压后按编号安装。",
                    archive=batch,
                    archive_start=start,
                )
            )
        return archive_steps
    steps = [NotificationStep(f"🎉 你的订单 #{order.id} 已发货！")]
    if not esims:
        steps.extend(NotificationStep(text) for text in split_payload_chunks(order.payload or ""))
        return steps
    for index, esim in enumerate(esims):
        label = f"eSIM {index + 1}/{len(esims)}"
        heading = f"{label}\n\nICCID: {esim.iccid[:80]}"
        caption = f"{heading}\nLPA: {esim.lpa}\n\n扫码或按 LPA 安装码安装"
        if text_units(caption) <= 1024:
            steps.append(NotificationStep(caption, esim, index))
        else:
            steps.append(NotificationStep(f"{heading}\n\n完整 LPA 安装码见下一条消息。", esim, index))
            # LPA 已限制为 2000 UTF-8 字节，单独发送可完整保留且不会超过消息上限。
            steps.append(NotificationStep(f"订单 #{order.id} · {label}\n完整 LPA 安装码：\n{esim.lpa}"))
    return steps


async def notify_owner(
    db: Database,
    bot: Bot,
    order_id: int,
    *,
    resend: bool = False,
    runtime: RuntimeState | None = None,
    throttle: NotificationThrottle | None = None,
) -> bool:
    async with db.order_operation(order_id):
        order = await db.get_order(order_id)
        if order is None or order.status != OrderStatus.DELIVERED:
            return False
        # 自动通知和手动补发均只使用当前采购已经核验、落库的货品。
        # 重绑后的 upstream_pending、冻结记录和旧引用的 payload 都不能发送。
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is not None:
            expected_ref = purchase.upstream_request_id or f"STUB-{order_id:06d}"
            if purchase.state != PurchaseState.FULFILLED or order.upstream_ref != expected_ref:
                logger.warning(
                    "delivery notification blocked: unverified purchase or reference", extra={"order_id": order_id}
                )
                return False
            if purchase.upstream_request_id and await db.get_purchase_conflict(order_id, purchase.upstream_request_id):
                logger.warning("delivery notification blocked: shared upstream reference", extra={"order_id": order_id})
                return False
        if not order.notification_pending and not resend:
            return True
        if order.notification_retry_at and time.time() < order.notification_retry_at:
            return False
        if resend:
            await db.request_notification(order.id)
        owner = await db.get_user(order.user_id)
        if owner is None:
            return False
        try:
            # 收件人只从持久化订单取，不能使用命令所在群聊或查询者身份。
            # 单张 eSIM 发图文，多张按 ZIP 文件交付；每个 ZIP 单独记录发送进度。
            # 非 eSIM（兑换券/激活等）：头部 + 文本货品分条。每步各存进度，断点续发不重购。
            esims = (
                delivery_esims(order.delivery_esims, order.payload, order.quantity)
                if (
                    order.delivery_esims is not None
                    or (purchase and purchase.request_type == "esim")
                    or order.input_request_type == "esim"
                    or (purchase is None and order.input_request_type is None)
                )
                else []
            )
            steps = notification_steps(order, esims)
            prepared = await db.prepare_notification(order.id, NOTIFICATION_PLAN_VERSION)
            if prepared is None:
                return False
            cursor = prepared.notification_cursor
            total = len(steps)
            if not 0 <= cursor <= total:
                logger.warning("invalid notification progress", extra={"order_id": order.id})
                return False
            loop = asyncio.get_running_loop()

            def archive_progress() -> None:
                if runtime is not None:
                    loop.call_soon_threadsafe(runtime.beat, "recovery")

            for step in range(cursor, total):
                if runtime is not None:
                    runtime.beat("recovery")
                item = steps[step]
                if item.archive:
                    data = await asyncio.to_thread(
                        esim_zip, order.id, item.archive, item.archive_start, archive_progress
                    )
                    first_index = item.archive_start + 1
                    last_index = item.archive_start + len(item.archive)
                    filename = (
                        f"esims-{order.id}.zip"
                        if total == 1
                        else (f"esims-{order.id}-{first_index:04d}-{last_index:04d}.zip")
                    )
                    if throttle is not None:
                        await throttle.wait(owner.telegram_id)
                    await bot.send_document(
                        owner.telegram_id,
                        BufferedInputFile(data, filename=filename),
                        caption=item.text,
                        parse_mode=None,
                        request_timeout=120,
                    )
                    del data
                elif item.photo is None:
                    if throttle is not None:
                        await throttle.wait(owner.telegram_id)
                    await bot.send_message(owner.telegram_id, item.text, parse_mode=None, request_timeout=20)
                else:
                    png = await asyncio.to_thread(qr_png, item.photo.lpa)
                    if throttle is not None:
                        await throttle.wait(owner.telegram_id)
                    await bot.send_photo(
                        owner.telegram_id,
                        BufferedInputFile(png, filename=f"esim-{order.id}-{item.photo_index + 1}.png"),
                        caption=item.text,
                        parse_mode=None,
                        request_timeout=20,
                    )
                if not await db.advance_notification(order.id, step):
                    return False
        except TelegramRetryAfter as exc:
            await db.defer_notification(order.id, time.time() + exc.retry_after)
            logger.warning("delivery notification rate limited", extra={"order_id": order.id})
            return False
        except Exception as exc:
            logger.warning("delivery notification failed", extra={"order_id": order.id, "error": type(exc).__name__})
            return False
        await db.mark_notified(order.id)
        return True


async def notify_refund(db: Database, bot: Bot, order_id: int, *, throttle: NotificationThrottle | None = None) -> bool:
    """退款与通知分开恢复；成功发送后清除待通知标记。"""
    async with db.order_operation(order_id):
        order = await db.get_order(order_id)
        if order is None or order.status != OrderStatus.REFUNDED:
            return False
        if not order.notification_pending:
            return True
        if order.notification_retry_at and time.time() < order.notification_retry_at:
            return False
        owner = await db.get_user(order.user_id)
        if owner is None:
            return False
        balance = await db.get_balance(order.user_id, order.currency)
        try:
            if throttle is not None:
                await throttle.wait(owner.telegram_id)
            await bot.send_message(
                owner.telegram_id,
                f"❌ 订单 #{order.id} 无法完成交付，{order.amount_text} 已退回余额\n"
                f"当前余额：{balance / 100:.2f} {order.currency}",
                parse_mode=None,
                request_timeout=20,
            )
        except TelegramRetryAfter as exc:
            await db.defer_notification(order.id, time.time() + exc.retry_after)
            return False
        except Exception as exc:
            logger.warning("refund notification failed", extra={"order_id": order_id, "error": type(exc).__name__})
            return False
        await db.mark_notified(order_id)
        return True


async def notify_wallet(db: Database, bot: Bot, transaction_id: int, throttle: NotificationThrottle | None) -> bool:
    tx = await db.get_wallet_notification(transaction_id)
    if tx is None:
        return True
    amount = tx["amount_cents"] / 100
    currency = tx["currency"]
    if tx["kind"] == "topup":
        heading = f"💰 充值到账 {amount:.2f} {currency}"
    elif tx["kind"] == "adjust":
        heading = f"💳 余额调整 {amount:+.2f} {currency}"
        if tx["note"]:
            heading += f"（{tx['note']}）"
    else:
        heading = f"💰 订单 #{tx['order_id']} 的额外/关单收款 {amount:.2f} {currency} 已存入余额"
    if throttle is not None:
        await throttle.wait(tx["telegram_id"])
    await bot.send_message(
        tx["telegram_id"],
        f"{heading}\n入账后余额：{tx['balance_after'] / 100:.2f} {currency}",
        parse_mode=None,
        request_timeout=20,
    )
    return True


async def process_work(
    db: Database,
    purchaser: Purchaser,
    bot: Bot | None,
    item: WorkItem,
    runtime: RuntimeState | None = None,
    *,
    throttle: NotificationThrottle | None = None,
) -> bool:
    """业务幂等保护仍在采购/通知状态机；队列只负责执行时机和失败退避。"""
    done = False
    delay = min(300, RECOVERY_INTERVAL * 2 ** min(item.attempts, 6))
    failed = False
    try:
        # 被取消的建单保留 submitting，下轮转人工；文件发送保留已确认的 cursor。
        async with asyncio.timeout(300):
            if item.kind == "purchase":
                await purchaser.fulfill(db, item.entity_id)
                delay = min(delay, 60)
            elif bot is not None and item.kind == "delivery":
                order = await db.get_order(item.entity_id)
                if order is None:
                    done = True
                elif order.status == OrderStatus.REFUNDED:
                    done = await notify_refund(db, bot, item.entity_id, throttle=throttle)
                else:
                    done = await notify_owner(db, bot, item.entity_id, runtime=runtime, throttle=throttle)
                failed = not done
            elif bot is not None and item.kind == "wallet":
                done = await notify_wallet(db, bot, item.entity_id, throttle)
    except TelegramRetryAfter as exc:
        delay = max(delay, exc.retry_after)
        failed = True
    except asyncio.CancelledError:
        await db.finish_work(item, delay=0)
        raise
    except Exception as exc:
        failed = True
        logger.warning("work failed", extra={"order_id": item.entity_id, "error": type(exc).__name__})
    await db.finish_work(item, done=done, delay=delay)
    return failed


async def _drain_work(
    db: Database,
    purchaser: Purchaser,
    bot: Bot | None,
    kind: str,
    runtime: RuntimeState | None,
) -> int:
    failures = 0
    # 有限批量，避免持续新订单使一次恢复永不返回。
    for _ in range(50):
        item = await db.claim_work(kind)
        if item is None:
            break
        failures += await process_work(db, purchaser, bot, item, runtime)
    return failures


async def recover_once(
    db: Database,
    purchaser: Purchaser,
    bot: Bot | None,
    runtime: RuntimeState | None = None,
) -> int:
    """有限恢复批次，供维护和测试使用；常驻服务的三个通道完全独立。"""
    results = await asyncio.gather(*(_drain_work(db, purchaser, bot, "purchase", runtime) for _ in range(3)))
    if bot is not None:
        results += await asyncio.gather(
            *(_drain_work(db, purchaser, bot, kind, runtime) for kind in ("delivery", "delivery", "wallet"))
        )
    return sum(results)


async def _work_loop(
    db: Database,
    purchaser: Purchaser,
    bot: Bot,
    kind: str,
    throttle: NotificationThrottle,
    *,
    runtime: RuntimeState | None,
) -> None:
    while True:
        item = await db.claim_work(kind)
        if item is None:
            await asyncio.sleep(1)
            continue
        await process_work(db, purchaser, bot, item, runtime, throttle=throttle)


async def recovery_loop(db: Database, purchaser: Purchaser, bot: Bot, runtime: RuntimeState | None = None) -> None:
    throttle = NotificationThrottle()
    # 固定工作协程，不为整个积压队列一次性创建 Task。异常会传到主进程监督器。
    async with asyncio.TaskGroup() as group:
        for kind in ("purchase", "purchase", "purchase", "delivery", "delivery", "wallet"):
            group.create_task(_work_loop(db, purchaser, bot, kind, throttle, runtime=runtime))
        while True:
            if runtime is not None:
                runtime.beat("recovery")
            await asyncio.sleep(1)
