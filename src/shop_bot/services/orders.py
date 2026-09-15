"""订单生命周期服务：唯一修改订单状态的地方。"""

from __future__ import annotations

from ..db import Database
from ..logging_config import get_logger
from ..models import Order, OrderStatus, Product
from .upstream import DeliveryResult, UpstreamClient

logger = get_logger(__name__)


class OrderError(Exception):
    def __init__(self, message: str, order: Order | None = None) -> None:
        super().__init__(message)
        self.order = order


class DeliveryError(OrderError):
    """付款已记录，但履约需要恢复或管理员重试。"""


async def create_order(db: Database, user_id: int, product: Product, quantity: int) -> Order:
    order = await db.create_order(
        user_id=user_id,
        product_id=product.id,
        quantity=quantity,
        amount_cents=product.price_cents * quantity,
        currency=product.currency,
    )
    logger.info(
        "order created",
        extra={"order_id": order.id, "user_id": user_id, "product_id": product.id},
    )
    return order


async def mark_paid(
    db: Database,
    upstream: UpstreamClient,
    order_id: int,
    trade_no: str | None = None,
    *,
    retry_failed: bool = False,
) -> tuple[Order, DeliveryResult]:
    """幂等确认付款并履约。PAID 可恢复；失败订单由管理员明确重试。

    上游必须按 order.id 幂等履约，以覆盖上游成功但本地尚未落盘就中断的窗口。
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
        if confirmed.status == OrderStatus.DELIVERED:
            return confirmed, DeliveryResult(ok=True, upstream_ref=confirmed.upstream_ref, payload=confirmed.payload)
        if confirmed.status == OrderStatus.DELIVERY_FAILED:
            if not retry_failed:
                raise DeliveryError("delivery failed; administrator retry required", confirmed)
            retried = await db.transition_order(order_id, OrderStatus.PAID, from_status=OrderStatus.DELIVERY_FAILED)
            assert retried is not None
            confirmed = retried
        return await _deliver(db, upstream, confirmed)


async def _deliver(db: Database, upstream: UpstreamClient, paid: Order) -> tuple[Order, DeliveryResult]:
    product = await db.get_product(paid.product_id)
    if product is None:
        await db.transition_order(
            paid.id, OrderStatus.DELIVERY_FAILED, from_status=OrderStatus.PAID, note="product record missing"
        )
        raise DeliveryError("product record missing", paid)
    try:
        result = await upstream.deliver(paid, product)
    except Exception as exc:
        logger.warning("upstream delivery failed", extra={"order_id": paid.id, "error": type(exc).__name__})
        await db.transition_order(
            paid.id, OrderStatus.DELIVERY_FAILED, from_status=OrderStatus.PAID, note=type(exc).__name__
        )
        raise DeliveryError("upstream delivery unavailable", paid) from None
    if not result.ok:
        final = await db.transition_order(
            paid.id, OrderStatus.DELIVERY_FAILED, from_status=OrderStatus.PAID, note="upstream rejected delivery"
        )
        raise DeliveryError("upstream rejected delivery", final)
    final = await db.transition_order(
        paid.id,
        OrderStatus.DELIVERED,
        from_status=OrderStatus.PAID,
        upstream_ref=result.upstream_ref,
        payload=result.payload,
    )
    assert final is not None
    logger.info("order delivered", extra={"order_id": paid.id})
    return final, result


async def cancel_order(db: Database, order_id: int) -> Order:
    order = await db.transition_order(order_id, OrderStatus.CANCELLED, from_status=OrderStatus.PENDING_PAYMENT)
    if order is None:
        raise OrderError(f"order {order_id} cannot be cancelled")
    logger.info("order cancelled", extra={"order_id": order_id})
    return order
