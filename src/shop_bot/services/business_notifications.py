"""管理员/频道业务广播：每个收件人独立确认，文本不包含交付资料或客户身份。"""

import json

from aiogram import Bot

from ..db import Database
from .notification_transport import NotificationThrottle


def render_business_notification(event: str, payload: dict) -> str:
    amount = f"{payload['amount_cents'] / 100:.2f} {payload['currency']}"
    if event == "order_paid":
        title = "✅ 订单已付款"
        product = " ".join(str(payload["product"]).split())[:200]
        lines = [
            title,
            f"订单：#{payload['order_id']}",
            f"商品：{product}",
            f"数量：{payload['quantity']}",
            f"金额：{amount}",
        ]
        method = {"balance": "钱包余额", "epay": "在线支付"}.get(str(payload.get("payment_method")), "管理员确认")
        lines.append(f"付款方式：{method}")
        return "\n".join(lines)
    title = "💰 充值已到账"
    return f"{title}\n充值单：T{payload['topup_id']}\n金额：{amount}"


async def notify_business(
    db: Database,
    bot: Bot,
    delivery_id: int,
    throttle: NotificationThrottle | None = None,
) -> bool:
    delivery = await db.get_business_delivery(delivery_id)
    if delivery is None or delivery["state"] != "pending":
        return True
    if throttle is not None:
        await throttle.wait(delivery["chat_id"])
    await bot.send_message(
        delivery["chat_id"],
        render_business_notification(delivery["event"], json.loads(delivery["payload"])),
        parse_mode=None,
        request_timeout=20,
    )
    await db.mark_business_sent(delivery_id)
    return True
