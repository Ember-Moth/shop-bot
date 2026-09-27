"""付款提示：待付订单的收银消息，以及付款或关单后把旧消息更新为最终状态。

买家在多处看到同一张待付订单（下单确认、订单列表继续支付、/query）。每条提示都经
``db.prompts`` 登记；订单或充值单离开待支付状态时，触发器同事务入队，后台去掉旧的
付款按钮，避免买家对已付订单重复付款。
"""

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup

from .. import keyboards
from ..db import Database
from ..logging_config import get_logger
from ..models import Order, OrderStatus, Topup, TopupState
from .balance import MAX_TOPUP_CENTS, format_cents, topup_gap
from .epay import EPayClient
from .invoices import payment_url
from .notification_transport import NotificationThrottle

logger = get_logger(__name__)

HEADER_CREATED = "✅ 下单成功！"
HEADER_PENDING = "💳 待支付订单"
HEADER_UNPAID = "⏳ 订单尚未支付"


def gap_offer(order: Order, balance_cents: int, epay: EPayClient | None) -> int:
    """可补差价的金额；余额已够、没有余额、已锁定在线渠道、在线收款不可用或超出单笔上限时为 0。"""
    if order.payment_method == "epay" or epay is None or epay.currency != order.currency or balance_cents <= 0:
        return 0
    gap = topup_gap(order.amount_cents, balance_cents)
    return gap if gap <= MAX_TOPUP_CENTS else 0


async def order_prompt(
    bot: Bot, db: Database, epay: EPayClient | None, order: Order, header: str
) -> tuple[str, InlineKeyboardMarkup]:
    """待支付订单的付款提示（Markdown）。已锁定在线渠道的订单只给收银台，不再提供余额。"""
    lines = [header, "", f"订单号：`{order.id}`", f"金额：{order.amount_text}"]
    if order.payment_method == "epay":
        if epay is None or epay.currency != order.currency:
            lines += ["", "本订单已选择在线支付，但该收款渠道暂不可用，请联系管理员。"]
            return "\n".join(lines), keyboards.order_created(order.id)
        url = await payment_url(
            bot,
            epay,
            name=f"订单 #{order.id}",
            order_no=str(order.id),
            amount_cents=order.amount_cents,
            currency=order.currency,
        )
        lines += ["", "已选择在线支付，请打开收银台完成付款。"]
        return "\n".join(lines), keyboards.order_created(order.id, url)

    balance = await db.wallet.get_balance(order.user_id, order.currency)
    enough = balance >= order.amount_cents > 0
    online = epay is not None and epay.currency == order.currency
    balance_line = f"当前余额：{format_cents(balance)} {order.currency}"
    if not enough:
        balance_line += f"，还差 {format_cents(order.amount_cents - balance)} {order.currency}"
    lines.append(balance_line)
    if not enough and not online:
        lines += ["", f"暂未开通 {order.currency} 在线收款，且同币种余额不足，请联系管理员。"]
        return "\n".join(lines), keyboards.order_created(order.id)
    gap = 0 if enough else gap_offer(order, balance, epay)
    if enough:
        lines += ["", "请选择支付方式。选择在线支付后，本订单只能通过该收银台付款。"]
    elif gap:
        lines += [
            "",
            "补差价：在线支付差额，到账后自动用余额付清本单。",
            "在线支付：全额通过收银台付款，选择后本订单只能用该收银台。",
        ]
    else:
        lines += ["", "请在线支付。选择后本订单只能通过该收银台付款。"]
    markup = keyboards.order_created(
        order.id,
        allow_balance=enough,
        allow_online=online,
        gap_label=f"{format_cents(gap)} {order.currency}" if gap else None,
    )
    return "\n".join(lines), markup


def settled_order_view(order: Order) -> tuple[str, InlineKeyboardMarkup] | None:
    """订单离开待支付后的提示内容（纯文本）；仍待支付返回 None。"""
    if order.status == OrderStatus.PENDING_PAYMENT:
        return None
    if order.status == OrderStatus.CANCELLED:
        text = f"🚫 订单 #{order.id} 已取消"
    elif order.status == OrderStatus.REFUNDED:
        text = f"↩️ 订单 #{order.id} 已退款，{order.amount_text} 已退回余额"
    elif order.status == OrderStatus.DELIVERED:
        text = f"✅ 订单 #{order.id} 已付款 · {order.amount_text}\n货品已私信发送。"
    else:
        text = f"✅ 订单 #{order.id} 已付款 · {order.amount_text}\n货品会自动私信发送。"
    return text, keyboards.settled_order()


def settled_topup_view(topup: Topup, applied_order_id: int | None = None) -> tuple[str, InlineKeyboardMarkup] | None:
    """applied_order_id 为补差价到账后自动付清的订单。"""
    if topup.status != TopupState.PAID:
        return None
    amount = f"{format_cents(topup.amount_cents)} {topup.currency}"
    if topup.order_id is None:
        return f"✅ 充值单 {topup.id} 已到账 · {amount}", keyboards.settled_topup()
    if applied_order_id is not None:
        return f"✅ 补差价 {amount} 已到账，订单 #{applied_order_id} 已用余额付款", keyboards.settled_order()
    return (
        f"✅ 补差价 {amount} 已到账，订单 #{topup.order_id} 未能自动付款，款项已存入余额",
        keyboards.settled_topup(),
    )


async def close_payment_prompt(
    db: Database, bot: Bot, prompt_id: int, throttle: NotificationThrottle | None = None
) -> bool:
    """去掉已结算提示上的付款按钮。纯展示更新：消息已删或内容相同即视为完成，不重试。"""
    prompt = await db.prompts.get_open(prompt_id)
    if prompt is None:
        return True
    view = None
    if prompt["order_id"] is not None:
        order = await db.orders.get_order(prompt["order_id"])
        if order is not None:
            view = settled_order_view(order)
            if view is None:
                return True  # 仍待支付：保持打开，结算时触发器会再次入队
    else:
        topup = await db.wallet.get_topup(prompt["topup_id"])
        if topup is not None:
            applied = await db.wallet.topup_applied_order(topup.id) if topup.order_id is not None else None
            view = settled_topup_view(topup, applied)
            if view is None:
                return True
    if view is not None:
        text, markup = view
        if throttle is not None:
            await throttle.wait(prompt["chat_id"])
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=prompt["chat_id"],
                message_id=prompt["message_id"],
                reply_markup=markup,
                parse_mode=None,
                request_timeout=20,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.info("payment prompt left unchanged", extra={"error": type(exc).__name__})
    await db.prompts.close(prompt_id)
    return True
