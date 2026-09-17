"""EPay GET/POST 回调：验签、统一核单、保存采购任务后快速应答（开发方案规则 1/2）。

履约由后台恢复循环驱动（services/purchasing.py），回调不做耗时的上游请求。
"""

import re
from decimal import Decimal

from aiohttp import web

from ..db import Database
from ..logging_config import get_logger
from ..services.epay import EPayClient, EPayError
from ..services.orders import OrderError, mark_paid
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
    if order_no[:1] == "T" and order_no[1:].isdecimal() and len(order_no) <= 19:
        return await _handle_topup_callback(request, int(order_no[1:]), payment)
    if not order_no.isascii() or not order_no.isdecimal() or len(order_no) > 18:
        return web.Response(text="fail", status=400)
    order_id = int(order_no)
    order = await db.get_order(order_id)
    if order is None:
        return web.Response(text="fail", status=404)
    try:
        epay.validate_payment(order, payment)
    except EPayError:
        logger.warning("payment validation failed", extra={"order_id": order_id})
        return web.Response(text="fail", status=422)
    try:
        await mark_paid(db, purchaser, order.id, trade_no=payment.trade_no)
    except OrderError:
        return web.Response(text="fail", status=422)
    # 收款已持久化、采购任务已建立；重复成功回调幂等返回 success，不重复履约。
    return web.Response(text="success")


async def _handle_topup_callback(request: web.Request, topup_id: int, payment) -> web.Response:
    """充值单回调：金额/商户/交易号核验 → 到账入账（幂等）→ 通知买家。"""
    db: Database = request.app["db"]
    epay: EPayClient = request.app["epay"]
    topup = await db.get_topup(topup_id)
    if topup is None:
        logger.warning("topup callback for unknown topup", extra={"order_id": topup_id})
        return web.Response(text="fail", status=404)

    # 核验强度与商品订单一致（validate_payment 同款规则）：
    # 商户一致 + 交易号无首尾空白 + 金额格式合法且数值精确匹配
    if payment.pid != epay.pid:
        logger.warning("topup callback merchant mismatch", extra={"order_id": topup_id})
        return web.Response(text="fail", status=422)
    if not payment.trade_no.strip() or payment.trade_no != payment.trade_no.strip():
        return web.Response(text="fail", status=400)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", payment.money):
        logger.warning("topup callback amount format invalid", extra={"order_id": topup_id})
        return web.Response(text="fail", status=422)
    if Decimal(payment.money) * 100 != topup.amount_cents:
        logger.warning("topup callback amount mismatch", extra={"order_id": topup_id})
        return web.Response(text="fail", status=422)

    # complete_topup 幂等：已到账不重复入账
    credited = await db.complete_topup(topup.id, trade_no=payment.trade_no)
    if credited is None:
        return web.Response(text="fail", status=422)

    buyer = await db.get_user(topup.user_id)
    bot = request.app["bot"]
    if buyer is not None and bot is not None:
        try:
            text = (
                f"💰 充值到账 {topup.amount_cents / 100:.2f} CNY\n"
                f"当前余额：{buyer.balance_cents / 100:.2f} CNY"
            )
            await bot.send_message(buyer.telegram_id, text)
        except Exception as exc:
            logger.warning(
                "topup notification failed", extra={"order_id": topup_id, "error": type(exc).__name__}
            )
    logger.info(
        "topup credited", extra={"order_id": topup_id, "upstream_ref": payment.trade_no}
    )
    return web.Response(text="success")


def register_epay_routes(app: web.Application, callback_path: str) -> None:
    app.router.add_get(callback_path, epay_callback)
    app.router.add_post(callback_path, epay_callback)
