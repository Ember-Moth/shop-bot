"""钱包相关交互：充值余额（FSM + EPay 链接）与我的余额（含流水）。

入口由 start.py 的 menu_router 分发（start_topup / render_balance）；
TopupFlow.amount 状态处理器注册在本模块 router 上。
"""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from .. import keyboards
from ..config import get_settings
from ..db import Database
from ..logging_config import get_logger
from ..services.balance import format_cents, parse_topup_amount
from ..services.epay import EPayClient, EPayOrder

router = Router()
logger = get_logger(__name__)


class TopupFlow(StatesGroup):
    amount = State()


async def start_topup(
    message: Message, db: Database, epay: EPayClient | None, state: FSMContext
) -> None:
    if epay is None:
        await message.answer("支付渠道未配置，充值功能暂不可用，请联系管理员")
        return
    await state.set_state(TopupFlow.amount)
    await message.answer(
        "💰 充值余额\n\n请直接回复充值金额（元，1–10000，最多两位小数），"
        "例如 `100` 或 `50.50`。\n完成后将生成支付链接；发送 /start 取消。"
    )


@router.message(TopupFlow.amount)
async def topup_amount_input(
    message: Message, db: Database, epay: EPayClient | None, state: FSMContext
) -> None:
    if epay is None:
        await message.answer("支付渠道未配置，充值功能暂不可用，请联系管理员")
        return
    text = message.text
    assert text is not None
    if text.startswith("/"):
        await state.clear()
        await message.answer("已取消充值。")
        return
    amount_cents = parse_topup_amount(text)
    if amount_cents is None:
        await message.answer("金额无效（需 1–10000 元、最多两位小数），请重新回复，或发 /start 取消。")
        return
    from_user = message.from_user
    assert from_user is not None
    user = await db.get_user_by_telegram_id(from_user.id)
    if user is None:
        await state.clear()
        await message.answer("请先发送 /start 完成注册")
        return
    topup = await db.create_topup(user.id, amount_cents)
    await state.clear()

    settings = get_settings()
    bot = message.bot
    assert bot is not None
    me = await bot.get_me()
    pay_url = epay.create_pay_url(
        EPayOrder(
            name="余额充值",
            order_no=f"T{topup.id}",
            amount=amount_cents / 100,
            notify_url=f"{settings.webhook.url.rstrip('/')}{settings.payment.callback_path}",
            return_url=f"https://t.me/{me.username}",
        )
    )
    await message.answer(
        f"💰 充值单 `{topup.id}` 已创建\n金额：{format_cents(amount_cents)} CNY\n\n"
        "点击下方按钮完成支付，到账后自动通知：",
        parse_mode="Markdown",
        reply_markup=keyboards.order_created(topup.id, pay_url),
    )


async def render_balance(message: Message, db: Database) -> None:
    from_user = message.from_user
    assert from_user is not None
    user = await db.get_user_by_telegram_id(from_user.id)
    if user is None:
        await message.answer("你还没有下过单")
        return
    lines = [f"当前余额：{format_cents(user.balance_cents)} CNY"]
    txs = await db.list_balance_transactions(user.id)
    if txs:
        lines.append("")
        lines.append("最近记录：")
        for tx in txs:
            sign = "+" if tx.amount_cents >= 0 else "-"
            kind = "充值" if tx.kind == "topup" else "消费"
            lines.append(
                f"{sign}{format_cents(abs(tx.amount_cents))}（{kind}）→ 余额 {format_cents(tx.balance_after)}"
            )
    await message.answer("💳 我的余额\n\n" + "\n".join(lines))
