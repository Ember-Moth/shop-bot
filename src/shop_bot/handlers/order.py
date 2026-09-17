from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from .. import keyboards
from ..config import get_settings
from ..db import Database
from ..keyboards import escape_markdown
from ..models import OrderStatus, Product
from ..services import orders
from ..services.epay import EPayClient, EPayOrder
from ..services.fulfillment import notify_owner, notify_refund
from ..services.purchasing import Purchaser

router = Router()

MAX_QUANTITY = 9999

# 按业务类型需要的额外输入提示（开发方案 4：按业务类型采集 ICCID、号码、天数）
_EXTRA_PROMPTS = {
    "activation": "**{name}** x{quantity}\n\n请回复要激活的 ICCID（SIM 卡上的 18 位以上数字）。",
    "recharge": (
        "**{name}** x{quantity}\n\n请回复充值目标手机号（含国家区号，如 +919876543210）；\n"
        "按日套餐请同时回复天数，格式：`+919876543210 7`。"
    ),
}
ICCID_PREFIX = "iccid:"


class OrderFlow(StatesGroup):
    quantity = State()
    extra = State()  # activation/recharge 的 ICCID、号码、天数采集


def product_quote(product: Product) -> dict:
    return {
        key: getattr(product, key) for key in ("price_cents", "currency", "sku", "request_type", "upstream_plan_id")
    }


def _summary(product: Product, quantity: int, days: int | None = None, extra: str = "") -> str:
    # 按日套餐计价公式：unitPrice × quantity × days（与上游 totalAmount 公式一致）
    amount = product.price_cents * quantity * (days or 1)
    lines = [f"**{escape_markdown(product.name)}** x{quantity}"]
    if days:
        lines.append(f"天数：{days} 天")
    lines += ["", f"单价：{product.price_text}", f"合计：{amount / 100:.2f} {product.currency}"]
    if extra:
        lines += ["", extra]
    lines += ["", "确认下单吗？"]
    return "\n".join(lines)


def _parse_extra(product: Product, quantity: int, text: str) -> tuple[str | None, str | None, int | None, str | None]:
    """解析业务输入，返回 (iccid, msisdn, days, 错误提示)。"""
    request_type = product.request_type or "esim"
    iccid = msisdn = None
    days = None
    if request_type == "activation":
        iccid = text.strip()
        if not iccid.isdecimal() or len(iccid) < 18:
            return None, None, None, "ICCID 应为 18 位以上数字，请重新回复。"
        return iccid, None, None, None
    if request_type == "recharge":
        parts = text.split()
        target = parts[0]
        if target.lower().startswith(ICCID_PREFIX):
            iccid = target[len(ICCID_PREFIX) :]
            if not iccid.isdecimal() or len(iccid) < 18:
                return None, None, None, "ICCID 应为 18 位以上数字，请重新回复。"
        else:
            msisdn = target
            if not msisdn.lstrip("+").isdecimal():
                return None, None, None, "手机号格式不对（应含国家区号，如 +919876543210），请重新回复。"
        if len(parts) > 1:
            try:
                days = int(parts[1])
                if days < 1 or days > 365:
                    return None, None, None, "天数需在 1–365 之间，请重新回复。"
            except ValueError:
                return None, None, None, "天数应为数字，请重新回复。"
        return iccid, msisdn, days, None
    return None, None, None, None


def _extra_hint(product: Product, quantity: int) -> str | None:
    request_type = product.request_type or "esim"
    template = _EXTRA_PROMPTS.get(request_type)
    return template.format(name=escape_markdown(product.name), quantity=quantity) if template else None


@router.callback_query(F.data.startswith(keyboards.CB_ORDER_PREFIX))
async def cb_start_order(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = callback.data
    assert data is not None  # 过滤器已保证非空
    product = await db.get_product(int(data.removeprefix(keyboards.CB_ORDER_PREFIX)))
    if product is None or not product.active:
        await callback.answer("商品不存在或已下架", show_alert=True)
        return
    await state.set_state(OrderFlow.quantity)
    # 重置上一单可能残留的上下文（天数/ICCID/号码），防止切换商品后报价与订单不一致
    await state.update_data(
        product_id=product.id, quantity=None, iccid=None, msisdn=None, days=None, product_quote=None
    )
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer()
        return
    await msg.edit_text(
        f"**{escape_markdown(product.name)}**\n\n要购买几个？请直接回复数量（正整数）。",
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(OrderFlow.quantity)
async def msg_quantity(message: Message, state: FSMContext, db: Database) -> None:
    text = message.text
    assert text is not None  # 只有文本消息会进入这个状态
    try:
        quantity = int(text.strip())
    except ValueError:
        await message.answer("请输入正整数数量，例如 `2`。")
        return
    if quantity < 1 or quantity > MAX_QUANTITY:
        await message.answer(f"数量需在 1–{MAX_QUANTITY} 之间。")
        return
    data = await state.get_data()
    product = await db.get_product(data["product_id"])
    if product is None or not product.active:
        await state.clear()
        await message.answer("商品不存在或已下架，下单已取消。")
        return
    await state.update_data(quantity=quantity)
    hint = _extra_hint(product, quantity)
    if hint:
        await state.set_state(OrderFlow.extra)
        await message.answer(hint, parse_mode="Markdown")
        return
    await state.update_data(product_quote=product_quote(product))
    await message.answer(
        _summary(product, quantity),
        reply_markup=keyboards.confirm_order(),
        parse_mode="Markdown",
    )


@router.message(OrderFlow.extra)
async def msg_extra(message: Message, state: FSMContext, db: Database) -> None:
    text = message.text
    assert text is not None
    data = await state.get_data()
    product = await db.get_product(data["product_id"])
    if product is None or not product.active:
        await state.clear()
        await message.answer("商品不存在或已下架，下单已取消。")
        return
    iccid, msisdn, days, error = _parse_extra(product, data["quantity"], text)
    if error:
        await message.answer(error)
        return
    extra = []
    if iccid:
        extra.append(f"ICCID：`{iccid}`")
    if msisdn:
        extra.append(f"手机号：`{msisdn}`")
    await state.update_data(iccid=iccid, msisdn=msisdn, days=days, product_quote=product_quote(product))
    await message.answer(
        _summary(product, data["quantity"], days=days, extra="、".join(extra)),
        reply_markup=keyboards.confirm_order(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == keyboards.CB_CONFIRM_ORDER)
async def cb_confirm(callback: CallbackQuery, state: FSMContext, db: Database, epay: EPayClient | None) -> None:
    data = await state.get_data()
    product_id = data.get("product_id")
    quantity = data.get("quantity")
    if product_id is None or quantity is None:
        await callback.answer("会话已过期，请重新下单", show_alert=True)
        return
    product = await db.get_product(product_id)
    if product is None or not product.active:
        await state.clear()
        await callback.answer("商品不存在或已下架，下单失败", show_alert=True)
        return
    if data.get("product_quote") != product_quote(product):
        # 旧会话或调价/改币种后必须重新确认；业务类型变动须重新采集输入。
        previous = data.get("product_quote") or {}
        if previous.get("request_type") != product.request_type:
            await state.clear()
            await callback.answer("商品业务信息已变化，请重新选择商品下单", show_alert=True)
            return
        await state.update_data(product_quote=product_quote(product))
        msg = callback.message
        if msg is not None and not isinstance(msg, InaccessibleMessage):
            await msg.edit_text(
                "商品报价已更新，请重新确认：\n\n" + _summary(product, quantity, days=data.get("days")),
                parse_mode="Markdown",
                reply_markup=keyboards.confirm_order(),
            )
        await callback.answer("请确认最新报价", show_alert=True)
        return
    user = await db.get_user_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer("请先发 /start 再下单", show_alert=True)
        return
    try:
        order = await orders.create_order(
            db,
            user.id,
            product,
            quantity,
            iccid=data.get("iccid"),
            msisdn=data.get("msisdn"),
            days=data.get("days"),
        )
    except ValueError:
        await state.clear()
        await callback.answer("商品已下架或报价已变化，请重新下单", show_alert=True)
        return
    await state.clear()
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer()
        return

    balance = await db.get_balance(user.id, order.currency)
    allow_balance = balance >= order.amount_cents and order.amount_cents > 0
    allow_online = epay is not None and order.currency == epay.currency
    hint = "请选择支付方式。选择在线支付后，本订单只能通过该收银台付款。"
    if not allow_balance and not allow_online:
        hint = f"暂未开通 {order.currency} 在线收款，且同币种余额不足，请联系管理员。"
    await msg.edit_text(
        f"✅ 下单成功！\n\n订单号：`{order.id}`\n金额：{order.amount_text}\n\n{hint}",
        parse_mode="Markdown",
        reply_markup=keyboards.order_created(order.id, allow_balance=allow_balance, allow_online=allow_online),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(keyboards.CB_EPAY_PAY))
async def cb_pay_online(callback: CallbackQuery, db: Database, epay: EPayClient | None) -> None:
    raw = (callback.data or "").removeprefix(keyboards.CB_EPAY_PAY)
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 18 or epay is None:
        await callback.answer("在线支付暂不可用", show_alert=True)
        return
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer()
        return
    user = await db.get_user_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer("请先发 /start 再操作", show_alert=True)
        return
    order = await db.reserve_epay(int(raw), user.id, epay.currency)
    if order is None:
        await callback.answer("订单状态或币种不支持此收款渠道", show_alert=True)
        return
    settings = get_settings()
    bot = callback.bot
    assert bot is not None
    me = await bot.get_me()
    pay_url = epay.create_pay_url(
        EPayOrder(
            name=f"订单 #{order.id}",
            order_no=str(order.id),
            amount=order.amount_cents / 100,
            currency=order.currency,
            notify_url=f"{settings.webhook.url.rstrip('/')}{settings.payment.callback_path}",
            return_url=f"https://t.me/{me.username}",
        )
    )
    await msg.edit_text(
        f"订单 #{order.id} · {order.amount_text}\n已选择在线支付，请打开收银台完成付款。",
        reply_markup=keyboards.order_created(order.id, pay_url),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(keyboards.CB_BALANCE_PAY))
async def cb_pay_with_balance(callback: CallbackQuery, db: Database, purchaser: Purchaser, bot: Bot) -> None:
    """余额支付：扣款与订单转 paid 同一事务，随后走统一履约链路。"""
    data = callback.data or ""
    if not data.removeprefix(keyboards.CB_BALANCE_PAY).isdecimal():
        await callback.answer("参数无效")
        return
    order_id = int(data.removeprefix(keyboards.CB_BALANCE_PAY))
    from_user = callback.from_user
    assert from_user is not None
    user = await db.get_user_by_telegram_id(from_user.id)
    if user is None:
        await callback.answer("请先发 /start 再操作", show_alert=True)
        return
    async with db.order_operation(order_id):
        order = await db.get_order(order_id)
        if order is None or order.user_id != user.id:
            await callback.answer("订单不存在", show_alert=True)
            return
        if order.status != OrderStatus.PENDING_PAYMENT:
            await callback.answer("订单当前状态不可支付", show_alert=True)
            return
    # pay_order_with_balance 本身是原子的；fulfill 内部会自行持有订单锁，
    # 这里不能先持有——否则 fulfill 重入同一把非重入锁会死锁。
    paid, err = await db.pay_order_with_balance(order_id, user.id, order.amount_cents)
    if err == "online payment selected":
        await callback.answer("此订单已选择在线支付，请使用原收银台完成付款", show_alert=True)
        return
    if err == "insufficient":
        await callback.answer(f"{order.currency} 余额不足，请选择在线支付或先充值同币种余额", show_alert=True)
        return
    if err is not None or paid is None:
        await callback.answer("支付失败，请稍后再试", show_alert=True)
        return
    await purchaser.ensure_purchase(db, paid)
    final = await purchaser.fulfill(db, order_id)
    if final is not None and final.status == OrderStatus.REFUNDED:
        await notify_refund(db, bot, order_id)
        await callback.answer("❌ 订单无法交付，已退款到余额", show_alert=True)
        return
    if final is None or final.status != OrderStatus.DELIVERED:
        await callback.answer("✅ 已用余额支付，系统正在履约", show_alert=True)
        return
    notified = await notify_owner(db, bot, order_id)
    await callback.answer(
        "✅ 支付成功，货品已私信发送" if notified else "✅ 已支付，交付资料已保存；私信发送尚未完成，系统会继续重试",
        show_alert=True,
    )


@router.callback_query(F.data == keyboards.CB_CANCEL_ORDER)
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer()
        return
    await msg.edit_text("已取消下单。")
    await callback.answer()
