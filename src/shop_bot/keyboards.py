from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

CB_PRODUCT_PREFIX = "p:"
CB_ORDER_PREFIX = "o:"
CB_CONFIRM_ORDER = "order:confirm"
CB_CANCEL_ORDER = "order:cancel"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍 商品目录", callback_data="catalog")],
            [InlineKeyboardButton(text="📦 我的订单", callback_data="myorders")],
        ]
    )


def catalog(products) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p.name} — {p.price_text}", callback_data=f"{CB_PRODUCT_PREFIX}{p.id}"
            )
        ]
        for p in products
    ]
    rows.append([InlineKeyboardButton(text="⬅️ 返回主菜单", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_detail(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 下单", callback_data=f"{CB_ORDER_PREFIX}{product_id}")],
            [InlineKeyboardButton(text="⬅️ 返回目录", callback_data="catalog")],
        ]
    )


def confirm_order() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ 确认下单", callback_data=CB_CONFIRM_ORDER)],
            [InlineKeyboardButton(text="❌ 取消", callback_data=CB_CANCEL_ORDER)],
        ]
    )


def order_created(order_id: int, pay_url: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if pay_url:
        # Web App 按钮：在 Telegram 内嵌打开支付页面
        rows.append(
            [InlineKeyboardButton(text="💳 立即支付", web_app=WebAppInfo(url=pay_url))]
        )
    rows.append([InlineKeyboardButton(text="📦 查看我的订单", callback_data="myorders")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
