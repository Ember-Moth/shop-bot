"""支付网关回调端点。

支付成功后，网关 POST 一条带签名的通知过来。我们验证签名、查订单、触发上游发货。
"""

import hashlib
import hmac
import json
import logging

from aiohttp import web

from ..db import Database
from ..services import orders
from ..services.orders import OrderError
from ..services.upstream import UpstreamClient

logger = logging.getLogger(__name__)


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """验证网关的 HMAC-SHA256 签名。

    具体方案（Header 名、摘要前缀等）拿到支付网关文档后再定，这是标准模式。
    """
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def payment_callback(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    upstream: UpstreamClient = request.app["upstream"]
    secret: str = request.app["payment_secret"]

    body = await request.read()
    signature = request.headers.get("X-Payment-Signature", "")
    if secret and not _verify_signature(body, signature, secret):
        logger.warning("payment callback signature mismatch")
        return web.Response(status=401, text="invalid signature")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return web.Response(status=400, text="invalid json")

    order_id = data.get("order_id")
    if order_id is None:
        return web.Response(status=400, text="missing order_id")

    try:
        order, result = await orders.mark_paid(db, upstream, int(order_id))
    except OrderError as exc:
        logger.warning("payment callback for order %s failed: %s", order_id, exc)
        return web.Response(status=422, text=str(exc))

    # 通知买家
    bot = request.app["bot"]
    owner = await db.get_user(order.user_id)
    if owner is not None:
        text = f"🎉 你的订单 #{order.id} 已发货！"
        if result.payload:
            text += f"\n\n{result.payload}"
        await bot.send_message(owner.telegram_id, text)

    logger.info("payment callback: order %s delivered, ref=%s", order_id, result.upstream_ref)
    return web.json_response({"ok": True, "order_id": order.id})


def register_payment_routes(app: web.Application, callback_path: str) -> None:
    app.router.add_post(callback_path, payment_callback)
