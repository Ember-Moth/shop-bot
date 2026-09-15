from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from .. import keyboards
from ..config import get_settings
from ..db import Database
from ..models import Product
from ..services import orders
from ..services.epay import EPayClient, EPayOrder

router = Router()

MAX_QUANTITY = 9999


class OrderFlow(StatesGroup):
    quantity = State()


def _summary(product: Product, quantity: int) -> str:
    amount = product.price_cents * quantity
    return (
        f"**{product.name}** x{quantity}\n\n"
        f"单价：{product.price_text}\n"
        f"合计：{amount / 100:.2f} {product.currency}\n\n"
        "确认下单吗？"
    )


@router.callback_query(F.data.startswith(keyboards.CB_ORDER_PREFIX))
async def cb_start_order(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = callback.data
    assert data is not None  # 过滤器已保证非空
    product = await db.get_product(int(data.removeprefix(keyboards.CB_ORDER_PREFIX)))
    if product is None or not product.active:
        await callback.answer("商品不存在或已下架", show_alert=True)
        return
    await state.set_state(OrderFlow.quantity)
    await state.update_data(product_id=product.id)
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer()
        return
    await msg.edit_text(
        f"**{product.name}**\n\n要购买几个？请直接回复数量（正整数）。",
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
    await message.answer(
        _summary(product, quantity),
        reply_markup=keyboards.confirm_order(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == keyboards.CB_CONFIRM_ORDER)
async def cb_confirm(
    callback: CallbackQuery, state: FSMContext, db: Database, epay: EPayClient | None
) -> None:
    data = await state.get_data()
    await state.clear()
    product_id = data.get("product_id")
    quantity = data.get("quantity")
    if product_id is None or quantity is None:
        await callback.answer("会话已过期，请重新下单", show_alert=True)
        return
    product = await db.get_product(product_id)
    if product is None or not product.active:
        await callback.answer("商品不存在或已下架，下单失败", show_alert=True)
        return
    user = await db.get_user_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer("请先发 /start 再下单", show_alert=True)
        return
    order = await orders.create_order(db, user.id, product, quantity)
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer()
        return

    settings = get_settings()

    if epay is not None:
        # EPay 按人民币「元」计价，只支持 CNY 商品
        if order.currency != "CNY":
            await callback.answer("当前商品不支持在线支付，请联系管理员", show_alert=True)
            return
        # 生成 EPay 支付链接，Web App 按钮直接打开收银台
        notify_url = f"{settings.webhook.url.rstrip('/')}{settings.payment.callback_path}"
        bot = callback.bot
        assert bot is not None  # aiogram 保证非空
        me = await bot.get_me()
        return_url = f"https://t.me/{me.username}"
        pay_url = epay.create_pay_url(
            EPayOrder(
                name=product.name,
                order_no=str(order.id),
                amount=order.amount_cents / 100,
                notify_url=notify_url,
                return_url=return_url,
            )
        )
        await msg.edit_text(
            f"✅ 下单成功！\n\n订单号：`{order.id}`\n金额：{order.amount_text}\n\n"
            "点击下方按钮在 Telegram 内完成支付：",
            parse_mode="Markdown",
            reply_markup=keyboards.order_created(order.id, pay_url),
        )
    else:
        await msg.edit_text(
            f"✅ 下单成功！\n\n订单号：`{order.id}`\n金额：{order.amount_text}\n\n"
            "付款完成后我们会立即为你发货。",
            parse_mode="Markdown",
        )
    await callback.answer()


@router.callback_query(F.data == keyboards.CB_CANCEL_ORDER)
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer()
        return
    await msg.edit_text("已取消下单。")
    await callback.answer()
