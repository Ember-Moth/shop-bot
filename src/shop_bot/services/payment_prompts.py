"""付款提示：待付订单的收银消息，以及付款、关单或收款信息过期后把旧消息更新为最终状态。

买家在多处看到同一张待付订单（下单确认、订单列表继续支付、/query）。每条提示都经
``db.prompts`` 登记；订单或充值单离开待支付状态时，触发器同事务入队，后台去掉旧的
付款按钮，避免买家对已付订单重复付款。GMPay 提示还记下显示的交易，交易过期时去掉
失效的收款地址和金额。
"""

import math
import time
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, NamedTuple

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup

from .. import keyboards
from ..config import get_settings
from ..db import Database
from ..logging_config import get_logger
from ..models import Order, OrderStatus, Topup, TopupState
from ..timefmt import from_db, from_timestamp, zone_label
from .balance import MAX_TOPUP_CENTS, format_cents, topup_gap
from .gateway import Checkout, GatewayError, PaymentGateway
from .notification_transport import NotificationThrottle

logger = get_logger(__name__)

HEADER_CREATED = "✅ 下单成功！"
HEADER_PENDING = "💳 待支付订单"
HEADER_UNPAID = "⏳ 订单尚未支付"

NETWORK_LABELS = {
    "tron": "TRON（TRC20）",
    "ethereum": "Ethereum（ERC20）",
    "bsc": "BSC（BEP20）",
    "polygon": "Polygon",
    "solana": "Solana",
}


class PromptView(NamedTuple):
    text: str
    markup: InlineKeyboardMarkup
    trade_ref: int | None = None  # 显示的 GMPay 交易，登记提示时一并保存


def checkout_lines(checkout: Checkout) -> list[str]:
    """付款方式说明（Markdown）。GMPay 直接给出网络、精确金额和地址，买家可以不打开网页。"""
    if checkout.address is None:
        return ["请点击下方按钮打开收银台完成付款。"]
    lines = []
    if checkout.expires_at is not None:
        minutes = max(1, math.ceil((checkout.expires_at - time.time()) / 60))
        deadline = from_timestamp(checkout.expires_at).strftime("%H:%M")
        lines.append(f"请在 {minutes} 分钟内转账，截止 {deadline}（{zone_label()}）")
    network = checkout.network or ""
    lines += [
        f"网络：{NETWORK_LABELS.get(network, network.upper())}",
        f"转账金额：`{checkout.amount}` {checkout.token}",
        f"收款地址：`{checkout.address}`",
        "",
        "⚠️ 到账金额必须与上面完全一致，转账手续费需另付，否则无法自动确认。",
        "转账后如未自动确认，可点「🔄 我已转账」。需要二维码可打开网页收银台。",
    ]
    return lines


def order_deadline_line(order: Order) -> str | None:
    """超时自动关闭的截止提示；未开启、无法解析创建时间或已过截止时间时不显示。"""
    minutes = get_settings().payment.order_timeout_minutes
    if minutes <= 0:
        return None
    created = from_db(order.created_at)  # SQLite 返回 UTC 文本
    if created is None:
        return None
    deadline = created + timedelta(minutes=minutes)
    if deadline.timestamp() <= time.time():
        return None
    return f"请在 {deadline:%H:%M}（{zone_label()}）前付款，逾期订单自动关闭。"


def gap_offer(order: Order, balance_cents: int, gateway: PaymentGateway | None) -> int:
    """可补差价的金额；余额已够、没有余额、已锁定在线渠道、在线收款不可用或超出单笔上限时为 0。"""
    if order.payment_method == "epay" or gateway is None or gateway.currency != order.currency or balance_cents <= 0:
        return 0
    gap = topup_gap(order.amount_cents, balance_cents)
    return gap if gap <= MAX_TOPUP_CENTS else 0


async def order_prompt(bot: Bot, db: Database, gateway: PaymentGateway | None, order: Order, header: str) -> PromptView:
    """待支付订单的付款提示（Markdown）。已锁定在线渠道的订单只给在线付款信息，不再提供余额。"""
    lines = [header, "", f"订单号：`{order.id}`", f"金额：{order.amount_text}"]
    if order.payment_method == "epay":
        if gateway is None or gateway.currency != order.currency:
            lines += ["", "本订单已选择在线支付，但该收款渠道暂不可用，请联系管理员。"]
            return PromptView("\n".join(lines), keyboards.order_created(order.id))
        try:
            checkout = await gateway.order_checkout(bot, db, order)
        except GatewayError:
            logger.warning("payment checkout unavailable", extra={"order_id": order.id})
            lines += ["", "已选择在线支付，但暂时无法生成付款信息，请稍后点下方按钮重新获取。"]
            return PromptView("\n".join(lines), keyboards.order_retry(order.id))
        # GMPay 收款信息自带更短的截止时间，只给网页收银台时才提示订单截止时间。
        deadline = order_deadline_line(order) if checkout.address is None else None
        lines += ["", "已选择在线支付。", *checkout_lines(checkout), *([deadline] if deadline else [])]
        markup = keyboards.checkout(checkout.web_url, checkout.trade_ref, keyboards.back_to_orders())
        return PromptView("\n".join(lines), markup, checkout.trade_ref)

    balance = await db.wallet.get_balance(order.user_id, order.currency)
    enough = balance >= order.amount_cents > 0
    online = gateway is not None and gateway.currency == order.currency
    balance_line = f"当前余额：{format_cents(balance)} {order.currency}"
    if not enough:
        balance_line += f"，还差 {format_cents(order.amount_cents - balance)} {order.currency}"
    lines.append(balance_line)
    if deadline := order_deadline_line(order):
        lines.append(deadline)
    if not enough and not online:
        lines += ["", f"暂未开通 {order.currency} 在线收款，且同币种余额不足，请联系管理员。"]
        return PromptView("\n".join(lines), keyboards.order_created(order.id))
    gap = 0 if enough else gap_offer(order, balance, gateway)
    if enough:
        lines += ["", "请选择支付方式。选择在线支付后，本订单不能再改用余额。"]
    elif gap:
        lines += [
            "",
            "补差价：在线支付差额，到账后自动用余额付清本单。",
            "在线支付：全额在线付款，选择后本订单不能再改用余额。",
        ]
    else:
        lines += ["", "请选择在线支付。"]
    markup = keyboards.order_created(
        order.id,
        allow_balance=enough,
        allow_online=online,
        gap_label=f"{format_cents(gap)} {order.currency}" if gap else None,
    )
    return PromptView("\n".join(lines), markup)


def expired_trade_view(trade: Mapping[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """GMPay 收款信息过期后的提示（纯文本）：去掉失效的地址和金额，给出重新获取入口。"""
    warning = "该收款地址和金额已失效，请勿再转账。"
    if trade["order_id"] is not None:
        return (
            f"⌛ 订单 #{trade['order_id']} 的付款信息已过期\n{warning}\n如需继续付款，请点下方按钮重新获取。",
            keyboards.order_retry(trade["order_id"]),
        )
    return (
        f"⌛ 充值单 {trade['topup_id']} 的付款信息已过期\n{warning}\n如需继续充值，请点下方按钮重新获取。",
        keyboards.topup_retry(trade["topup_id"]),
    )


async def expire_trade_prompts(
    db: Database, bot: Bot, trade: Mapping[str, Any], throttle: NotificationThrottle | None = None
) -> None:
    """把显示这笔交易的消息改成过期提示。目标已结算时不动，交由结算更新显示最终状态。"""
    if trade["order_id"] is not None:
        order = await db.orders.get_order(trade["order_id"])
        if order is None or order.status != OrderStatus.PENDING_PAYMENT:
            return
    else:
        topup = await db.wallet.get_topup(trade["topup_id"])
        if topup is None or topup.status != TopupState.PENDING:
            return
    text, markup = expired_trade_view(trade)
    for prompt in await db.prompts.for_trade(trade["id"]):
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
            logger.info("expired payment prompt left unchanged", extra={"error": type(exc).__name__})
        await db.prompts.clear_trade(prompt["id"])


def settled_order_view(order: Order) -> tuple[str, InlineKeyboardMarkup] | None:
    """订单离开待支付后的提示内容（纯文本）；仍待支付返回 None。"""
    if order.status == OrderStatus.PENDING_PAYMENT:
        return None
    if order.status == OrderStatus.CANCELLED:
        text = f"🚫 订单 #{order.id} 已取消"
    elif order.status == OrderStatus.EXPIRED:
        text = f"⌛ 订单 #{order.id} 超时未付款，已自动关闭\n如需购买请重新下单。"
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
