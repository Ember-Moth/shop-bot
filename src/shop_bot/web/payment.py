"""EPay GET/POST 回调：验签、统一核单、保存采购任务后快速应答（开发方案规则 1/2）。

履约由后台恢复循环驱动（services/purchasing.py），回调不做耗时的上游请求。
"""

from aiohttp import web

from ..db import Database
from ..logging_config import get_logger
from ..services.epay import EPayClient, EPayError, parse_money_cents
from ..services.orders import OrderError, confirm_epay_payment
from ..services.purchasing import Purchaser

logger = get_logger(__name__)


async def epay_callback(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    purchaser: Purchaser = request.app["purchaser"]
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
    order_no = payment.order_no
    # 充值单使用 T<id> 前缀，与商品订单（纯数字）区分
    if order_no[:1] == "T" and order_no.isascii() and order_no[1:].isdecimal() and len(order_no) <= 19:
        return await _handle_topup_callback(request, int(order_no[1:]), payment)
    if not order_no.isascii() or not order_no.isdecimal() or len(order_no) > 18:
        return web.Response(text="fail", status=400)
    order_id = int(order_no)
    order = await db.orders.get_order(order_id)
    if order is None:
        return web.Response(text="fail", status=404)
    try:
        await confirm_epay_payment(db, purchaser, epay, order, payment)
    except EPayError, OrderError:
        logger.warning("payment validation failed", extra={"order_id": order_id})
        return web.Response(text="fail", status=422)
    # 收款已持久化、采购任务已建立；重复成功回调幂等返回 success，不重复履约。
    return web.Response(text="success")


async def _handle_topup_callback(request: web.Request, topup_id: int, payment) -> web.Response:
    """充值单回调：金额/商户/交易号核验 → 到账入账（幂等）→ 通知买家。"""
    db: Database = request.app["db"]
    epay: EPayClient = request.app["epay"]
    topup = await db.wallet.get_topup(topup_id)
    if topup is None:
        logger.warning("topup callback for unknown topup", extra={"order_id": topup_id})
        return web.Response(text="fail", status=404)

    # 核验强度与商品订单一致（validate_payment 同款规则）：
    # 商户一致 + 交易号无首尾空白 + 金额格式合法且数值精确匹配
    if payment.pid != epay.pid:
        logger.warning("topup callback merchant mismatch", extra={"order_id": topup_id})
        return web.Response(text="fail", status=422)
    if payment.order_no != f"T{topup.id}":
        return web.Response(text="fail", status=422)
    if topup.currency != epay.currency or (payment.currency and payment.currency != topup.currency):
        return web.Response(text="fail", status=422)
    if not payment.trade_no.strip() or payment.trade_no != payment.trade_no.strip():
        return web.Response(text="fail", status=400)
    money_cents = parse_money_cents(payment.money)
    if money_cents is None:
        logger.warning("topup callback amount format invalid", extra={"order_id": topup_id})
        return web.Response(text="fail", status=422)
    if money_cents != topup.amount_cents:
        logger.warning("topup callback amount mismatch", extra={"order_id": topup_id})
        return web.Response(text="fail", status=422)

    # 同一外部交易幂等；网关若确实收取另一笔款，则记录该交易并入账。
    try:
        credited = await db.wallet.complete_topup(topup.id, trade_no=payment.trade_no)
    except ValueError:
        return web.Response(text="fail", status=422)
    if credited is None:
        return web.Response(text="fail", status=422)

    logger.info("topup credited", extra={"order_id": topup_id, "upstream_ref": payment.trade_no})
    return web.Response(text="success")


def register_epay_routes(app: web.Application, callback_path: str) -> None:
    app.router.add_get(callback_path, epay_callback)
    app.router.add_post(callback_path, epay_callback)
