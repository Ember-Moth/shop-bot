"""EPay 支付网关回调端点。

EPay 协议：POST form-urlencoded，带 MD5 签名。验证通过后触发上游发货。
"""

from aiohttp import web

from ..db import Database
from ..logging_config import get_logger
from ..services import orders
from ..services.epay import EPayClient
from ..services.orders import OrderError
from ..services.upstream import UpstreamClient

logger = get_logger(__name__)


async def epay_callback(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    upstream: UpstreamClient = request.app["upstream"]
    epay: EPayClient = request.app["epay"]

    # EPay 回调是 form-urlencoded；aiohttp 返回 MultiDict，可能含文件字段，统一转成 str
    raw = await request.post()
    params = {k: str(v) for k, v in raw.items()}

    if not epay.verify_callback(params):
        logger.warning("epay callback signature mismatch")
        return web.Response(text="fail", status=401)

    parsed = epay.parse_callback(params)
    if not parsed["paid"]:
        logger.info("epay callback not paid", extra={"order_no": parsed["order_no"]})
        return web.Response(text="success")  # 收到但未支付，正常应答避免网关重试

    order_no = parsed["order_no"]
    if not order_no:
        logger.warning("epay callback missing order_no")
        return web.Response(text="fail", status=400)

    try:
        order_id = int(order_no)
    except ValueError:
        logger.warning("epay callback invalid order_no", extra={"order_no": order_no})
        return web.Response(text="fail", status=400)

    try:
        order, result = await orders.mark_paid(db, upstream, order_id, trade_no=parsed["trade_no"])
    except OrderError as exc:
        logger.warning(
            "epay callback failed",
            extra={"order_id": order_id, "error": str(exc)},
        )
        return web.Response(text="fail", status=422)

    # 通知买家
    bot = request.app["bot"]
    owner = await db.get_user(order.user_id)
    if owner is not None:
        text = f"🎉 你的订单 #{order.id} 已发货！"
        if result.payload:
            text += f"\n\n{result.payload}"
        await bot.send_message(owner.telegram_id, text)

    logger.info(
        "epay callback delivered",
        extra={"order_id": order_id, "upstream_ref": result.upstream_ref},
    )
    return web.Response(text="success")  # EPay 协议要求返回 "success"


def register_epay_routes(app: web.Application, callback_path: str) -> None:
    app.router.add_post(callback_path, epay_callback)
