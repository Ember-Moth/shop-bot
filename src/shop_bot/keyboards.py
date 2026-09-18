from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from .telegram_text import truncate_text

CB_PRODUCT_PREFIX = "p:"
CB_ORDER_PREFIX = "o:"
CB_CONFIRM_ORDER = "order:confirm"
CB_CANCEL_ORDER = "order:cancel"
CB_BALANCE_PAY = "bal:"
CB_EPAY_PAY = "epay:"
CB_TOPUP_PREFIX = "topup:"  # topup:<金额分> 预设档位
CB_TOPUP_CUSTOM = "topup:custom"
CB_TOPUP_CANCEL = "topup:cancel"
CB_CATALOG_PAGE = "catalog:"
CATALOG_PAGE_SIZE = 5

TOPUP_PRESETS = (10, 20, 30, 50, 100)  # 预设充值档位（元），自定义金额走文本输入

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


def catalog(products, page: int = 0, page_count: int = 1) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=truncate_text(f"{p.name} — {p.price_text}", 100),
                callback_data=f"{CB_PRODUCT_PREFIX}{p.id}:{page}",
            )
        ]
        for p in products[:CATALOG_PAGE_SIZE]
    ]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"{CB_CATALOG_PAGE}{page - 1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton(text="下一页 ➡️", callback_data=f"{CB_CATALOG_PAGE}{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="⬅️ 返回主菜单", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def catalog_text(products, page: int = 0, page_count: int = 1) -> str:
    """单页最多五款，以 UTF-16 限制摘要长度，使用纯文本显示。完整描述保留在详情页。"""
    lines = [f"🛍 选择eSIM套餐 · 第 {page + 1}/{page_count} 页", "━━━━━━━━━━━━━━━━━━", ""]
    for p in products[:CATALOG_PAGE_SIZE]:
        lines.append(f"{truncate_text(p.name, 100)} — {truncate_text(p.price_text, 40)}")
        if p.description:
            lines.append(truncate_text(p.description, 500))
        lines.append("")
    return "\n".join(lines).rstrip()


def product_detail(product_id: int, page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 下单", callback_data=f"{CB_ORDER_PREFIX}{product_id}")],
            [InlineKeyboardButton(text="⬅️ 返回目录", callback_data=f"{CB_CATALOG_PAGE}{page}")],
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


def topup_amounts(currency: str) -> InlineKeyboardMarkup:
    """充值档位键盘：预设金额三列排列，自定义金额走文本输入。"""
    rows = []
    row = []
    for amount in TOPUP_PRESETS:
        row.append(
            InlineKeyboardButton(text=f"💵 {amount} {currency}", callback_data=f"{CB_TOPUP_PREFIX}{amount * 100}")
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ 自定义金额", callback_data=CB_TOPUP_CUSTOM)])
    rows.append([InlineKeyboardButton(text="❌ 取消", callback_data=CB_TOPUP_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def escape_markdown(text: str) -> str:
    """转义 legacy Markdown 标记字符。

    商品名等动态文本来自上游目录同步，不受我们控制；含 * _ ` [ 字符时
    轻则样式错乱，重则 Telegram 解析失败返回 400（审计 P1）。
    """
    for ch in ("*", "_", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text
