"""订单生命周期服务：唯一修改订单状态的地方。"""

from __future__ import annotations

import logging

from ..db import Database
from ..models import Order, OrderStatus, Product
from .upstream import DeliveryResult, UpstreamClient

logger = logging.getLogger(__name__)


class OrderError(Exception):
    def __init__(self, message: str, order: Order | None = None) -> None:
        super().__init__(message)
        self.order = order


async def create_order(
    db: Database, user_id: int, product: Product, quantity: int
) -> Order:
    return await db.create_order(
        user_id=user_id,
        product_id=product.id,
        quantity=quantity,
        amount_cents=product.price_cents * quantity,
        currency=product.currency,
    )


async def mark_paid(
    db: Database, upstream: UpstreamClient, order_id: int
) -> tuple[Order, DeliveryResult]:
    """状态转换 pending_payment → paid → delivered（或 delivery_failed）。

    以后接 Telegram Payments 的 `successful_payment` 回调时也会调这个函数，
    所以支付集成只需要调这一个入口。
    """
    order = await db.get_order(order_id)
    if order is None:
        raise OrderError(f"order {order_id} not found")
    if order.status != OrderStatus.PENDING_PAYMENT:
        raise OrderError(f"order {order_id} is not pending payment")

    paid = await db.transition_order(
        order_id, OrderStatus.PAID, from_status=OrderStatus.PENDING_PAYMENT
    )
    if paid is None:
        raise OrderError(f"order {order_id} is not pending payment")

    product = await db.get_product(paid.product_id)
    if product is None:
        await db.transition_order(
            order_id,
            OrderStatus.DELIVERY_FAILED,
            from_status=OrderStatus.PAID,
            note="product record missing",
        )
        raise OrderError(f"product {paid.product_id} not found", paid)

    try:
        result = await upstream.deliver(paid, product)
    except Exception as exc:  # 绝不让订单卡在 `paid` 状态
        logger.exception("upstream deliver failed for order %s", order_id)
        await db.transition_order(
            order_id, OrderStatus.DELIVERY_FAILED, from_status=OrderStatus.PAID, note=f"upstream error: {exc}"
        )
        raise OrderError(f"upstream delivery failed: {exc}", paid) from exc

    if result.ok:
        final = await db.transition_order(
            order_id,
            OrderStatus.DELIVERED,
            from_status=OrderStatus.PAID,
            upstream_ref=result.upstream_ref,
        )
    else:
        final = await db.transition_order(
            order_id,
            OrderStatus.DELIVERY_FAILED,
            from_status=OrderStatus.PAID,
            note=result.error,
        )
        raise OrderError(result.error or "upstream delivery rejected", final)

    assert final is not None  # 从 PAID 转换到这里必然成功
    return final, result


async def cancel_order(db: Database, order_id: int) -> Order:
    order = await db.transition_order(
        order_id, OrderStatus.CANCELLED, from_status=OrderStatus.PENDING_PAYMENT
    )
    if order is None:
        raise OrderError(f"order {order_id} cannot be cancelled")
    return order
