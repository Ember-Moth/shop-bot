"""钱包相关交互：充值余额（预设档位/自定义金额 + EPay 链接）与我的余额（含流水）。

入口由 start.py 的 menu_router 分发（start_topup / render_balance）；
TopupFlow.amount 状态处理器注册在本模块 router 上。
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message

from .. import keyboards
from ..config import get_settings
from ..db import Database
from ..logging_config import get_logger
from ..models import User
from ..services.balance import format_cents, parse_topup_amount
from ..services.epay import EPayClient, EPayOrder

router = Router()
logger = get_logger(__name__)


class TopupFlow(StatesGroup):
    amount = State()


async def start_topup(message: Message, db: Database, epay: EPayClient | None, state: FSMContext) -> None:
    if epay is None:
        await message.answer("支付渠道未配置，充值功能暂不可用，请联系管理员")
        return
    await state.clear()  # 充值是新流程，丢弃可能残留的下单/证件状态
    balance = 0
    from_user = message.from_user
    if from_user is not None:
        user = await db.get_user_by_telegram_id(from_user.id)
        if user is not None:
            balance = await db.get_balance(user.id, epay.currency)
    await message.answer(
        f"💰 充值余额\n\n当前余额：{format_cents(balance)} {epay.currency}\n\n请选择充值金额：",
        reply_markup=keyboards.topup_amounts(epay.currency),
    )


async def _create_topup_invoice(
    message: Message, db: Database, epay: EPayClient, user: User, amount_cents: int
) -> tuple[str, InlineKeyboardMarkup]:
    """创建充值单并生成 EPay 收银台链接，返回账单文本与支付按钮。"""
    topup = await db.create_topup(user.id, amount_cents, epay.currency)
    settings = get_settings()
    bot = message.bot
    assert bot is not None
    me = await bot.get_me()
    pay_url = epay.create_pay_url(
        EPayOrder(
            name="余额充值",
            order_no=f"T{topup.id}",
            amount=amount_cents / 100,
            currency=topup.currency,
            notify_url=f"{settings.webhook.url.rstrip('/')}{settings.payment.callback_path}",
            return_url=f"https://t.me/{me.username}",
        )
    )
    text = (
        f"💰 充值单 `{topup.id}` 已创建\n金额：{format_cents(amount_cents)} {topup.currency}\n\n"
        "点击下方按钮完成支付，到账后自动通知："
    )
    return text, keyboards.order_created(topup.id, pay_url)


@router.callback_query(
    F.data.startswith(keyboards.CB_TOPUP_PREFIX) & ~F.data.in_({keyboards.CB_TOPUP_CUSTOM, keyboards.CB_TOPUP_CANCEL})
)
async def cb_topup_preset(callback: CallbackQuery, db: Database, epay: EPayClient | None, state: FSMContext) -> None:
    await state.clear()
    if epay is None:
        await callback.answer("支付渠道未配置，请联系管理员", show_alert=True)
        return
    # callback data 可被客户端伪造，金额边界必须在服务端复核
    raw = (callback.data or "").removeprefix(keyboards.CB_TOPUP_PREFIX)
    if not raw.isdecimal():
        await callback.answer("金额无效", show_alert=True)
        return
    amount_cents = int(raw)
    if not 100 <= amount_cents <= 1_000_000:  # 与 parse_topup_amount 同边界：1–10000
        await callback.answer("金额超出允许范围", show_alert=True)
        return
    user = await db.get_user_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer("请先发送 /start 完成注册", show_alert=True)
        return
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer("消息已过期，请重新发起充值", show_alert=True)
        return
    text, markup = await _create_topup_invoice(msg, db, epay, user, amount_cents)
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == keyboards.CB_TOPUP_CUSTOM)
async def cb_topup_custom(callback: CallbackQuery, epay: EPayClient | None, state: FSMContext) -> None:
    if epay is None:
        await callback.answer("支付渠道未配置，请联系管理员", show_alert=True)
        return
    await state.set_state(TopupFlow.amount)
    msg = callback.message
    if msg is not None and not isinstance(msg, InaccessibleMessage):
        await msg.edit_text(
            f"✏️ 自定义充值金额\n\n请直接回复充值金额（{epay.currency}，1–10000，最多两位小数），"
            "例如 `100` 或 `50.50`。\n发送 /start 取消。",
            parse_mode="Markdown",
        )
    await callback.answer()


@router.callback_query(F.data == keyboards.CB_TOPUP_CANCEL)
async def cb_topup_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    msg = callback.message
    if msg is not None and not isinstance(msg, InaccessibleMessage):
        await msg.edit_text("已取消充值。")
    await callback.answer()


@router.message(TopupFlow.amount, F.text)
async def topup_amount_input(message: Message, db: Database, epay: EPayClient | None, state: FSMContext) -> None:
    if epay is None:
        await message.answer("支付渠道未配置，充值功能暂不可用，请联系管理员")
        return
    text = message.text
    assert text is not None  # F.text 过滤后必有文本
    if text.startswith("/"):
        await state.clear()
        await message.answer("已取消充值。")
        return
    amount_cents = parse_topup_amount(text)
    if amount_cents is None:
        await message.answer(f"金额无效（需 1–10000 {epay.currency}、最多两位小数），请重新回复，或发 /start 取消。")
        return
    from_user = message.from_user
    assert from_user is not None
    user = await db.get_user_by_telegram_id(from_user.id)
    if user is None:
        await state.clear()
        await message.answer("请先发送 /start 完成注册")
        return
    await state.clear()
    text_out, markup = await _create_topup_invoice(message, db, epay, user, amount_cents)
    await message.answer(text_out, parse_mode="Markdown", reply_markup=markup)


@router.message(TopupFlow.amount)
async def topup_amount_non_text(message: Message) -> None:
    """等待金额时收到图片/贴纸等非文本消息：提示而非让上一个 handler 的 assert 崩溃。"""
    await message.answer("请回复文本形式的充值金额（元），或发 /start 取消。")


async def render_balance(message: Message, db: Database) -> None:
    from_user = message.from_user
    assert from_user is not None
    user = await db.get_user_by_telegram_id(from_user.id)
    if user is None:
        await message.answer("请先发送 /start 完成注册")
        return
    balances = await db.get_balances(user.id)
    balances.setdefault("USD", 0)
    lines = [f"当前余额：{format_cents(value)} {currency}" for currency, value in sorted(balances.items())]
    txs = await db.list_balance_transactions(user.id)
    if txs:
        lines.append("")
        lines.append("最近记录：")
        for tx in txs:
            sign = "+" if tx.amount_cents >= 0 else "-"
            kind = {
                "topup": "充值",
                "purchase": "消费",
                "refund": "退款",
                "adjust": "调账",
                "payment_credit": "重复/关单收款补偿",
            }.get(tx.kind, tx.kind)
            lines.append(
                f"{sign}{format_cents(abs(tx.amount_cents))} {tx.currency}（{kind}）"
                f"→ 余额 {format_cents(tx.balance_after)} {tx.currency}"
            )
    await message.answer("💳 我的余额\n\n" + "\n".join(lines))
