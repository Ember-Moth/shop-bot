from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..config import get_settings
from ..db import Database
from ..models import OrderStatus
from ..services import orders
from ..services.orders import OrderError
from ..services.upstream import UpstreamClient

router = Router()
router.message.filter(F.from_user.id.in_(get_settings().admin_ids))
router.callback_query.filter(F.from_user.id.in_(get_settings().admin_ids))


def _fmt(o) -> str:
    return f"#{o.id} · user_db_id={o.user_id} · x{o.quantity} · {o.amount_text} · {o.status}"


@router.message(Command("orders"))
async def cmd_orders(message: Message, db: Database) -> None:
    text = message.text
    assert text is not None  # 只有文本消息会进入命令处理器
    parts = text.split(maxsplit=1)
    if len(parts) == 2:
        try:
            status = OrderStatus(parts[1].strip())
        except ValueError:
            valid = ", ".join(s.value for s in OrderStatus)
            await message.answer(f"未知状态。可用：{valid}")
            return
        orders_ = await db.list_orders(status)
        title = f"状态为 {status.value} 的订单"
    else:
        orders_ = await db.list_orders()
        title = "最近订单"
    if not orders_:
        await message.answer("没有订单。")
        return
    await message.answer(f"📦 {title}\n\n" + "\n".join(_fmt(o) for o in orders_))


@router.message(Command("paid"))
async def cmd_paid(message: Message, db: Database, upstream: UpstreamClient, bot: Bot) -> None:
    order_id = _parse_order_id(message)
    if order_id is None:
        await message.answer("用法：/paid <订单号>")
        return
    # 允许重试 delivery_failed 的订单
    order = await db.get_order(order_id)
    if order is not None and order.status == "delivery_failed":
        await db.transition_order(order_id, OrderStatus.PENDING_PAYMENT, from_status=OrderStatus.DELIVERY_FAILED)
    try:
        order, result = await orders.mark_paid(db, upstream, order_id)
    except OrderError as exc:
        await message.answer(f"❌ {exc}")
        return
    await message.answer(
        f"✅ 订单 #{order.id} 已发货（上游单号：{result.upstream_ref or '无'}）"
    )
    owner = await db.get_user(order.user_id)
    if owner is not None:
        text = f"🎉 你的订单 #{order.id} 已发货！"
        if result.payload:
            text += f"\n\n{result.payload}"
        await bot.send_message(owner.telegram_id, text)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db: Database) -> None:
    order_id = _parse_order_id(message)
    if order_id is None:
        await message.answer("用法：/cancel <订单号>")
        return
    try:
        order = await orders.cancel_order(db, order_id)
    except OrderError as exc:
        await message.answer(f"❌ {exc}")
        return
    await message.answer(f"🚫 订单 #{order.id} 已取消")


def _parse_order_id(message: Message) -> int | None:
    text = message.text
    if text is None:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[1].strip())
    except ValueError:
        return None
