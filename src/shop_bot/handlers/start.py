from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from ..config import get_settings
from ..db import Database
from ..keyboards import main_menu
from ..logging_config import get_logger
from ..services import orders
from ..services.epay import EPayClient
from ..services.orders import OrderError
from ..services.upstream import UpstreamClient

router = Router()
logger = get_logger(__name__)


async def _safe_edit(callback: CallbackQuery, text: str, **kwargs) -> None:
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        return
    try:
        await msg.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    from_user = message.from_user
    assert from_user is not None  # aiogram 在私聊场景下保证非空
    await db.upsert_user(from_user.id, from_user.username)
    await message.answer(
        "你好！我是商店机器人 🤖\n从下方菜单浏览商品并下单。",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery) -> None:
    await _safe_edit(callback, "主菜单", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "myorders")
async def cb_my_orders(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer("你还没有下过单", show_alert=True)
        return
    orders = await db.list_orders_for_user(user.id)
    if not orders:
        await _safe_edit(
            callback, "你还没有订单。\n去商品目录看看吧！", reply_markup=main_menu()
        )
        await callback.answer()
        return
    lines = [f"#{o.id} · 数量 x{o.quantity} · {o.amount_text} · {o.status}" for o in orders]
    await _safe_edit(
        callback, "📦 我的订单\n\n" + "\n".join(lines), reply_markup=main_menu()
    )
    await callback.answer()


@router.message(Command("query"))
async def cmd_query(message: Message, db: Database, epay: EPayClient | None, upstream: UpstreamClient) -> None:
    """用户主动查询订单支付状态（回调可能延迟或丢失时兜底）。"""
    text = message.text
    if text is None:
        await message.answer("消息内容为空")
        return
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("用法：/query <订单号>")
        return
    try:
        order_id = int(parts[1].strip())
    except ValueError:
        await message.answer("订单号必须是数字")
        return

    order = await db.get_order(order_id)
    if order is None:
        await message.answer("订单不存在")
        return

    # 只能查自己的订单（管理员除外）
    from_user = message.from_user
    assert from_user is not None
    user = await db.get_user_by_telegram_id(from_user.id)
    if user is None or (order.user_id != user.id and from_user.id not in get_settings().admin_ids):
        await message.answer("只能查询自己的订单")
        return

    if order.status != "pending_payment":
        await message.answer(f"订单 #{order.id} 当前状态：{order.status}")
        return

    if epay is None:
        await message.answer(f"订单 #{order.id} 待支付，请稍后再试或联系管理员")
        return

    try:
        result = await epay.query_order(str(order.id))
    except Exception:
        logger.exception("query order failed", extra={"order_id": order.id})
        await message.answer("查询失败，请稍后再试或联系管理员")
        return

    if not result.paid:
        await message.answer(f"订单 #{order.id} 尚未支付")
        return

    # 网关确认已支付，触发履约（回调丢失时的兜底）
    try:
        order, delivery = await orders.mark_paid(db, upstream, order.id, trade_no=result.trade_no)
    except OrderError as exc:
        await message.answer(f"订单 #{order.id} 已支付，但发货失败：{exc}")
        return
    text = f"🎉 你的订单 #{order.id} 已发货！"
    if delivery.payload:
        text += f"\n\n{delivery.payload}"
    await message.answer(text)
