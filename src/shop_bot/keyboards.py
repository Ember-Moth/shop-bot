from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

CB_PRODUCT_PREFIX = "p:"
CB_ORDER_PREFIX = "o:"
CB_CONFIRM_ORDER = "order:confirm"
CB_CANCEL_ORDER = "order:cancel"
CB_BALANCE_PAY = "bal:"
CB_EPAY_PAY = "epay:"

# 主菜单文案（回复键盘与文本路由共用，改文案需同步 handlers/start.py）
MENU_BUY = "🛒 购买商品"
MENU_ORDERS = "📦 我的订单"
MENU_HISTORY = "🧾 交易记录"
MENU_TOPUP = "💰 充值余额"
MENU_BALANCE = "💳 我的余额"
MENU_USAGE = "📶 用量 / 有效期"
MENU_KYC = "🪪 证件补交"
MENU_HELP = "❓ 使用帮助"


def main_menu() -> InlineKeyboardMarkup:
    """双列商城风主菜单（消息内嵌按钮）。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=MENU_BUY, callback_data="catalog"),
                InlineKeyboardButton(text=MENU_ORDERS, callback_data="myorders"),
            ],
            [
                InlineKeyboardButton(text=MENU_USAGE, callback_data="usage_hint"),
                InlineKeyboardButton(text=MENU_HELP, callback_data="help"),
            ],
        ]
    )


def main_menu_reply(kyc_enabled: bool = True) -> ReplyKeyboardMarkup:
    """常驻回复键盘：不用打命令，点底部按钮即可触发功能。

    kyc_enabled=False 时隐藏证件补交按钮（功能开关 features.kyc）。
    """
    rows = [
        [KeyboardButton(text=MENU_BUY), KeyboardButton(text=MENU_ORDERS)],
        [KeyboardButton(text=MENU_TOPUP), KeyboardButton(text=MENU_BALANCE)],
        [KeyboardButton(text=MENU_HISTORY), KeyboardButton(text=MENU_USAGE)],
    ]
    if kyc_enabled:
        rows.append([KeyboardButton(text=MENU_KYC), KeyboardButton(text=MENU_HELP)])
    else:
        rows.append([KeyboardButton(text=MENU_HELP)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="选择功能或…",
    )


def catalog(products) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{p.name} — {p.price_text}", callback_data=f"{CB_PRODUCT_PREFIX}{p.id}")]
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


def order_created(
    order_id: int, pay_url: str | None = None, allow_balance: bool = False, allow_online: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    if allow_balance:
        rows.append([InlineKeyboardButton(text="💰 余额支付", callback_data=f"{CB_BALANCE_PAY}{order_id}")])
    if pay_url:
        # Web App 按钮：在 Telegram 内嵌打开支付页面
        rows.append([InlineKeyboardButton(text="💳 立即支付", web_app=WebAppInfo(url=pay_url))])
    elif allow_online:
        rows.append([InlineKeyboardButton(text="💳 在线支付", callback_data=f"{CB_EPAY_PAY}{order_id}")])
    rows.append([InlineKeyboardButton(text="📦 查看我的订单", callback_data="myorders")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def escape_markdown(text: str) -> str:
    """转义 legacy Markdown 标记字符。

    商品名等动态文本来自上游目录同步，不受我们控制；含 * _ ` [ 字符时
    轻则样式错乱，重则 Telegram 解析失败返回 400（审计 P1）。
    """
    for ch in ("*", "_", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text
