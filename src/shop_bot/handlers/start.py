import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from .. import keyboards
from ..config import get_settings
from ..db import Database
from ..keyboards import (
    MENU_BALANCE,
    MENU_BUY,
    MENU_HELP,
    MENU_HISTORY,
    MENU_KYC,
    MENU_ORDERS,
    MENU_TOPUP,
    MENU_USAGE,
    main_menu,
    main_menu_reply,
)
from ..logging_config import get_logger
from ..models import OrderStatus
from ..services import orders
from ..services.epay import EPayClient, EPayError
from ..services.fulfillment import notify_owner, notify_refund
from ..services.orders import OrderError
from ..services.purchasing import Purchaser
from .balance import render_balance, start_topup

router = Router()
logger = get_logger(__name__)

def help_text(kyc_enabled: bool = True) -> str:
    """使用帮助；KYC 行随功能开关显示。"""
    lines = [
        "❓ **使用帮助**",
        "",
        "下单：点「🛒 购买商品」选商品 → 按提示回复数量 / ICCID / 手机号 → 确认并支付",
        "查询：/query <订单号> 查支付状态；📦 我的订单 查看全部订单",
        "用量：/usage <订单号>（已交付 eSIM 的流量与有效期）",
    ]
    if kyc_enabled:
        lines.append("证件：/kyc <订单号>（需要身份核验的订单，私聊提交材料）")
    lines += ["", "如有其他问题请联系管理员。"]
    return "\n".join(lines)


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
    kyc_enabled = get_settings().features.kyc
    await message.answer("点击下方按钮快速使用", reply_markup=main_menu_reply(kyc_enabled))


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
    kyc_enabled = get_settings().features.kyc
    await _safe_edit(
        callback, help_text(kyc_enabled), reply_markup=main_menu(), parse_mode="Markdown"
    )
    await callback.answer()


# ---- 回复键盘路由：点底部按钮即触发，无需打命令（仅空闲状态生效）----


# 连点防抖：同一用户短时间重复点菜单按钮只处理第一次（审计 P2：消息堆积）
MENU_DEBOUNCE_SECONDS = 3.0
_menu_last_seen: dict[tuple[int, str], float] = {}


def _menu_debounced(user_id: int, text: str) -> bool:
    key = (user_id, text)
    now = time.monotonic()
    last = _menu_last_seen.get(key, 0.0)
    if now - last < MENU_DEBOUNCE_SECONDS:
        return True
    _menu_last_seen[key] = now
    # 防止字典无限增长：粗略清理过期项
    if len(_menu_last_seen) > 1000:
        stale = [k for k, v in _menu_last_seen.items() if now - v > MENU_DEBOUNCE_SECONDS]
        for k in stale:
            _menu_last_seen.pop(k, None)
    return False


@router.message(
    F.text.in_(
        {MENU_BUY, MENU_TOPUP, MENU_BALANCE, MENU_ORDERS, MENU_HISTORY, MENU_USAGE, MENU_KYC, MENU_HELP}
    ),
    StateFilter(None),
)
async def menu_router(
    message: Message, db: Database, state: FSMContext, epay: EPayClient | None
) -> None:
    """菜单按钮统一入口：防抖后分发到对应处理（仅空闲状态生效）。"""
    from_user = message.from_user
    assert from_user is not None
    text = message.text or ""
    if _menu_debounced(from_user.id, text):
        return
    kyc_enabled = get_settings().features.kyc
    if text == MENU_BUY:
        await _send_catalog(message, db)
    elif text in (MENU_ORDERS, MENU_HISTORY):
        title = "📦 我的订单" if text == MENU_ORDERS else "🧾 交易记录"
        await _render_my_orders(message, db, title)
    elif text == MENU_TOPUP:
        await start_topup(message, db, epay, state)
    elif text == MENU_BALANCE:
        await render_balance(message, db)
    elif text == MENU_USAGE:
        await message.answer(
            "📶 用量 / 有效期查询\n\n请发送：/usage <订单号>\n（查询已交付 eSIM 的流量与有效期）"
        )
    elif text == MENU_KYC:
        if not kyc_enabled:
            await message.answer("证件补交功能未开放，如有需要请联系管理员")
            return
        await message.answer(
            "🪪 证件补交\n\n需要身份核验的订单请发送：/kyc <订单号>\n（在私聊中提交证件材料）"
        )
    else:
        await message.answer(help_text(kyc_enabled), parse_mode="Markdown")


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

    if order.status == OrderStatus.REFUNDED:
        await message.answer(f"订单 #{order.id} 已退款到余额，可在「💳 我的余额」查看")
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
    if order.status == OrderStatus.REFUNDED:
        await notify_refund(db, bot, order.id)
        await message.answer(f"订单 #{order.id} 无法交付，已退款到余额")
        return
    if order.status != OrderStatus.DELIVERED:
        await message.answer(f"订单 #{order.id} 已支付，正在履约（状态：{order.status}），请稍等")
        return
    notified = await notify_owner(db, bot, order.id, resend=True)
    if notified:
        await message.answer(f"订单 #{order.id} 的货品已私信发送给买家")
    else:
        await message.answer(f"订单 #{order.id} 的货品已保存，私信发送暂时失败，系统会重试")
