"""订单收款状态服务：唯一修改订单收款状态的地方。

履约（采购）由 services/purchasing.py 的 Purchaser 驱动，与本模块分离：
收款事实一旦落库，采购失败、等待、通知失败都不会让订单退回未付款。
"""

from __future__ import annotations

from ..db import Database
from ..logging_config import get_logger
from ..models import Order, OrderStatus, Product
from .purchasing import Purchaser

logger = get_logger(__name__)


class OrderError(Exception):
    def __init__(self, message: str, order: Order | None = None) -> None:
        super().__init__(message)
        self.order = order


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
    - trade_no 一致性由 db.transition_order 校验（一交易一订单）。
    """
    async with db.order_operation(order_id):
        order = await db.get_order(order_id)
        if order is None:
            raise OrderError(f"order {order_id} not found")
        if order.status == OrderStatus.CANCELLED:
            raise OrderError(f"order {order_id} is cancelled", order)
        status = OrderStatus.PAID if order.status == OrderStatus.PENDING_PAYMENT else order.status
        try:
            confirmed = await db.transition_order(order_id, status, from_status=order.status, trade_no=trade_no)
        except ValueError as exc:
            raise OrderError(str(exc), order) from None
        if confirmed is None:
            raise OrderError("order state changed", order)
        if confirmed.status == OrderStatus.DELIVERY_FAILED and retry_failed:
            retried = await db.transition_order(order_id, OrderStatus.PAID, from_status=OrderStatus.DELIVERY_FAILED)
            if retried is None:
                raise OrderError("order state changed", confirmed)
            confirmed = retried
        await purchaser.ensure_purchase(db, confirmed)
        logger.info("payment confirmed", extra={"order_id": order_id})
        return confirmed


async def cancel_order(db: Database, order_id: int) -> Order:
    order = await db.transition_order(order_id, OrderStatus.CANCELLED, from_status=OrderStatus.PENDING_PAYMENT)
    if order is None:
        raise OrderError(f"order {order_id} cannot be cancelled")
    logger.info("order cancelled", extra={"order_id": order_id})
    return order
