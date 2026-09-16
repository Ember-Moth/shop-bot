from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from .. import keyboards
from ..config import get_settings
from ..db import Database
from ..keyboards import (
    MENU_BUY,
    MENU_HELP,
    MENU_HISTORY,
    MENU_KYC,
    MENU_ORDERS,
    MENU_USAGE,
    main_menu,
    main_menu_reply,
)
from ..logging_config import get_logger
from ..models import OrderStatus
from ..services import orders
from ..services.epay import EPayClient, EPayError
from ..services.fulfillment import notify_owner
from ..services.orders import OrderError
from ..services.purchasing import Purchaser

router = Router()
logger = get_logger(__name__)

HELP_TEXT = (
    "❓ **使用帮助**\n\n"
    "下单：点「🛒 购买商品」选商品 → 按提示回复数量 / ICCID / 手机号 → 确认并支付\n"
    "查询：/query <订单号> 查支付状态；📦 我的订单 查看全部订单\n"
    "用量：/usage <订单号>（已交付 eSIM 的流量与有效期）\n"
    "证件：/kyc <订单号>（需要身份核验的订单，私聊提交材料）\n\n"
    "如有其他问题请联系管理员。"
)


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
async def cmd_start(
    message: Message, db: Database, state: FSMContext
) -> None:
    # 清理残留的未完成流程，避免旧状态影响新会话
    await state.clear()
    from_user = message.from_user
    assert from_user is not None  # aiogram 在私聊场景下保证非空
    await db.upsert_user(from_user.id, from_user.username)
    await message.answer("👇 请选择功能 👇", reply_markup=main_menu())
    # 常驻回复键盘：发送一次即驻留，用户点底部按钮即可触发功能
    await message.answer("点击下方按钮快速使用", reply_markup=main_menu_reply())


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery) -> None:
    await _safe_edit(callback, "👇 请选择功能 👇", reply_markup=main_menu())
    await callback.answer()


async def _send_catalog(message: Message, db: Database) -> None:
    products = await db.list_products()
    if not products:
        await message.answer("暂时没有商品")
        return
    await message.answer("🛍 商品目录", reply_markup=keyboards.catalog(products))


async def _render_my_orders(message: Message, db: Database, title: str) -> None:
    from_user = message.from_user
    assert from_user is not None
    user = await db.get_user_by_telegram_id(from_user.id)
    if user is None:
        await message.answer("你还没有下过单")
        return
    user_orders = await db.list_orders_for_user(user.id)
    if not user_orders:
        await message.answer("你还没有订单。\n点「🛒 购买商品」开始第一单！", reply_markup=main_menu())
        return
    lines = [
        f"#{o.id} · 数量 x{o.quantity} · {o.amount_text} · {o.status}" for o in user_orders
    ]
    await message.answer(f"{title}\n\n" + "\n".join(lines), reply_markup=main_menu())


@router.callback_query(F.data == "myorders")
async def cb_my_orders(callback: CallbackQuery, db: Database) -> None:
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer()
        return
    user = await db.get_user_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer("你还没有下过单", show_alert=True)
        return
    user_orders = await db.list_orders_for_user(user.id)
    if not user_orders:
        await _safe_edit(
            callback, "你还没有订单。\n点「🛒 购买商品」开始第一单！", reply_markup=main_menu()
        )
        await callback.answer()
        return
    lines = [f"#{o.id} · 数量 x{o.quantity} · {o.amount_text} · {o.status}" for o in user_orders]
    await _safe_edit(callback, "📦 我的订单\n\n" + "\n".join(lines), reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "usage_hint")
async def cb_usage_hint(callback: CallbackQuery) -> None:
    await _safe_edit(
        callback,
        "📶 用量 / 有效期查询\n\n请发送：/usage <订单号>\n（查询已交付 eSIM 的流量与有效期）",
        reply_markup=main_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery) -> None:
    await _safe_edit(callback, HELP_TEXT, reply_markup=main_menu(), parse_mode="Markdown")
    await callback.answer()


# ---- 回复键盘路由：点底部按钮即触发，无需打命令（仅空闲状态生效）----


@router.message(F.text == MENU_BUY, StateFilter(None))
async def menu_buy(message: Message, db: Database) -> None:
    await _send_catalog(message, db)


@router.message(F.text == MENU_ORDERS, StateFilter(None))
async def menu_orders(message: Message, db: Database) -> None:
    await _render_my_orders(message, db, "📦 我的订单")


@router.message(F.text == MENU_HISTORY, StateFilter(None))
async def menu_history(message: Message, db: Database) -> None:
    await _render_my_orders(message, db, "🧾 交易记录")


@router.message(F.text == MENU_USAGE, StateFilter(None))
async def menu_usage(message: Message) -> None:
    await message.answer(
        "📶 用量 / 有效期查询\n\n请发送：/usage <订单号>\n（查询已交付 eSIM 的流量与有效期）"
    )


@router.message(F.text == MENU_KYC, StateFilter(None))
async def menu_kyc(message: Message) -> None:
    await message.answer(
        "🪪 证件补交\n\n需要身份核验的订单请发送：/kyc <订单号>\n（在私聊中提交证件材料）"
    )


@router.message(F.text == MENU_HELP, StateFilter(None))
async def menu_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@router.message(Command("query"))
async def cmd_query(
    message: Message, db: Database, epay: EPayClient | None, purchaser: Purchaser, bot: Bot
) -> None:
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
    if from_user.id not in get_settings().admin_ids and (user is None or order.user_id != user.id):
        await message.answer("只能查询自己的订单")
        return

    if order.status in (OrderStatus.CANCELLED, OrderStatus.DELIVERY_FAILED):
        await message.answer(f"订单 #{order.id} 当前状态：{order.status}，如需协助请联系管理员")
        return
    if order.status == OrderStatus.PENDING_PAYMENT:
        if epay is None:
            await message.answer(f"订单 #{order.id} 待支付，请稍后再试或联系管理员")
            return
        try:
            payment = await epay.query_order(str(order.id))
            if not payment.paid:
                await message.answer(f"订单 #{order.id} 尚未支付")
                return
            epay.validate_payment(order, payment)
        except EPayError:
            logger.warning("payment query or validation failed", extra={"order_id": order.id})
            await message.answer("支付信息暂时无法确认，请稍后再试或联系管理员")
            return
        trade_no = payment.trade_no
    else:
        trade_no = order.trade_no
    try:
        order = await orders.mark_paid(db, purchaser, order.id, trade_no=trade_no)
        # 查询即触发一次履约推进（提交一次/查询一次），不阻塞在等待状态
        order = await purchaser.fulfill(db, order.id)
    except OrderError:
        await message.answer(f"订单 #{order.id} 暂时无法发货，请联系管理员")
        return
    assert order is not None
    if order.status != OrderStatus.DELIVERED:
        await message.answer(f"订单 #{order.id} 已支付，正在履约（状态：{order.status}），请稍等")
        return
    notified = await notify_owner(db, bot, order.id, resend=True)
    if notified:
        await message.answer(f"订单 #{order.id} 的货品已私信发送给买家")
    else:
        await message.answer(f"订单 #{order.id} 的货品已保存，私信发送暂时失败，系统会重试")
