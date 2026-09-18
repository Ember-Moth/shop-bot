"""订单收款状态服务：唯一修改订单收款状态的地方。

履约（采购）由 services/purchasing.py 的 Purchaser 驱动，与本模块分离：
收款事实一旦落库，采购失败、等待、通知失败都不会让订单退回未付款。
"""

from __future__ import annotations

from ..db import Database
from ..logging_config import get_logger
from ..models import Order, OrderStatus, Product
from .epay import EPayClient, EPayQueryResult
from .purchasing import Purchaser

logger = get_logger(__name__)


class OrderError(Exception):
    def __init__(self, message: str, order: Order | None = None) -> None:
        super().__init__(message)
        self.order = order


async def confirm_epay_payment(
    db: Database,
    purchaser: Purchaser,
    epay: EPayClient,
    order: Order,
    payment: EPayQueryResult,
) -> Order:
    """核单后持久化每笔外部收款；多收的钱按原币种入钱包，不重复采购。"""
    epay.validate_payment(order, payment, allow_additional=True)
    try:
        confirmed, disposition = await db.record_epay_payment(order.id, payment.trade_no)
    except ValueError as exc:
        raise OrderError(str(exc), order) from None
    if disposition == "wallet_credit":
        logger.warning("additional payment credited to wallet", extra={"order_id": order.id})
    return confirmed


async def create_order(
    db: Database,
    user_id: int,
    product: Product,
    quantity: int,
    *,
    iccid: str | None = None,
    msisdn: str | None = None,
    days: int | None = None,
) -> Order:
    """创建订单：金额含按日套餐天数（上游计价公式 unitPrice × days，PDF 6.1），
    并锁定 SKU/业务类型/套餐快照，之后商品目录变更不影响本次采购与交付核验。"""
    days = days or 1
    order = await db.create_order(
        user_id=user_id,
        product_id=product.id,
        quantity=quantity,
        amount_cents=product.price_cents * quantity * days,
        currency=product.currency,
        iccid=iccid,
        msisdn=msisdn,
        days=days if days > 1 else None,
        sku=product.sku,
        request_type=product.request_type,
        plan_id=product.upstream_plan_id,
        expected_product=product,
    )
    logger.info(
        "order created",
        extra={"order_id": order.id, "user_id": user_id, "product_id": product.id},
    )
    return order


async def mark_paid(
    db: Database,
    purchaser: Purchaser,
    order_id: int,
    trade_no: str | None = None,
    *,
    retry_failed: bool = False,
) -> Order:
    """幂等确认付款并建立采购任务；履约由采购状态机异步推进（开发方案规则 2）。

    - pending_payment → paid：确认收款，保留付款事实。
    - delivery_failed（旧数据）仅在 retry_failed=True 时回退重试。
    - trade_no 一致性由 db.confirm_order_payment 校验（一交易一订单）。
    """
    try:
        confirmed = await db.confirm_order_payment(order_id, trade_no, retry_failed=retry_failed)
    except ValueError as exc:
        raise OrderError(str(exc)) from None
    logger.info("payment confirmed", extra={"order_id": order_id})
    return confirmed


async def cancel_order(db: Database, order_id: int) -> Order:
    order = await db.transition_order(order_id, OrderStatus.CANCELLED, from_status=OrderStatus.PENDING_PAYMENT)
    if order is None:
        raise OrderError(f"order {order_id} cannot be cancelled")
    logger.info("order cancelled", extra={"order_id": order_id})
    return order
