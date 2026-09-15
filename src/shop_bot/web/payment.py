"""EPay 支付网关回调端点。

EPay 协议：POST form-urlencoded，带 MD5 签名。验证通过后触发上游发货。
"""

from aiohttp import web

from ..config import get_settings
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

    # EPay 回调支持 GET 和 POST；GET 参数在 query string，POST 是 form-urlencoded
    if request.method == "GET":
        params = dict(request.query)
    else:
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

    # 核对订单金额、商户 ID、交易号
    order = await db.get_order(order_id)
    if order is None:
        logger.warning("epay callback order not found", extra={"order_id": order_id})
        return web.Response(text="fail", status=404)

    expected_amount = f"{order.amount_cents / 100:.2f}"
    if parsed["money"] != expected_amount:
        logger.warning(
            "epay callback amount mismatch",
            extra={"order_id": order_id, "expected": expected_amount, "received": parsed["money"]},
        )
        return web.Response(text="fail", status=422)

    if parsed["trade_no"] == "":
        logger.warning("epay callback missing trade_no", extra={"order_id": order_id})
        return web.Response(text="fail", status=400)

    # 商户 ID 校验（如果配置了的话）
    settings = get_settings()
    if settings.epay.pid and params.get("pid") != settings.epay.pid:
        logger.warning(
            "epay callback pid mismatch",
            extra={"order_id": order_id, "expected": settings.epay.pid, "received": params.get("pid")},
        )
        return web.Response(text="fail", status=422)

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
    # EPay 回调同时支持 GET 和 POST
    app.router.add_get(callback_path, epay_callback)
    app.router.add_post(callback_path, epay_callback)
