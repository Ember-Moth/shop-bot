"""EPay 商品/充值收银台链接共用入口，Bot 公共资料使用 aiogram 缓存。"""

from aiogram import Bot

from ..config import get_settings
from .epay import EPayClient, EPayOrder


async def payment_url(
    bot: Bot,
    epay: EPayClient,
    *,
    name: str,
    order_no: str,
    amount_cents: int,
    currency: str,
) -> str:
    settings = get_settings()
    me = await bot.me()
    return epay.create_pay_url(
        EPayOrder(
            name=name,
            order_no=order_no,
            amount=amount_cents / 100,
            currency=currency,
            notify_url=f"{settings.webhook.url.rstrip('/')}{settings.payment.callback_path}",
            return_url=f"https://t.me/{me.username}",
        )
    )
