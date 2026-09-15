"""EPay GET/POST 回调：验签、统一核单、幂等履约与私信通知。"""

from aiohttp import web

from ..db import Database
from ..logging_config import get_logger
from ..services.epay import EPayClient, EPayError
from ..services.fulfillment import notify_owner
from ..services.orders import DeliveryError, OrderError, mark_paid
from ..services.upstream import UpstreamClient

logger = get_logger(__name__)


async def epay_callback(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    upstream: UpstreamClient = request.app["upstream"]
    epay: EPayClient = request.app["epay"]
    raw = request.query if request.method == "GET" else await request.post()
    # 重复键不能让验签与业务解析看到不同的字段值。
    if any(len(raw.getall(key)) != 1 for key in raw):
        return web.Response(text="fail", status=400)
    params = {k: str(v) for k, v in raw.items()}
    if not epay.verify_callback(params):
        return web.Response(text="fail", status=401)
    payment = epay.parse_callback(params)
    if not payment.paid:
        return web.Response(text="success")
    if not payment.order_no.isascii() or not payment.order_no.isdecimal() or len(payment.order_no) > 18:
        return web.Response(text="fail", status=400)
    order_id = int(payment.order_no)
    order = await db.get_order(order_id)
    if order is None:
        return web.Response(text="fail", status=404)
    try:
        epay.validate_payment(order, payment)
        await mark_paid(db, upstream, order.id, trade_no=payment.trade_no)
    except EPayError:
        logger.warning("payment validation failed", extra={"order_id": order_id})
        return web.Response(text="fail", status=422)
    except DeliveryError:
        # 收款已持久化。履约失败由管理员重试，不能要求用户再次付款。
        return web.Response(text="success")
    except OrderError:
        return web.Response(text="fail", status=422)
    await notify_owner(db, request.app["bot"], order.id)
    return web.Response(text="success")


def register_epay_routes(app: web.Application, callback_path: str) -> None:
    app.router.add_get(callback_path, epay_callback)
    app.router.add_post(callback_path, epay_callback)
