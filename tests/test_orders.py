import asyncio

import pytest

from shop_bot.models import OrderStatus, PurchaseState
from shop_bot.services import orders
from shop_bot.services.orders import OrderError


async def test_create_order_computes_amount(db, user, product):
    order = await orders.create_order(db, user.id, product, 3)
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert order.amount_cents == product.price_cents * 3
    assert order.currency == product.currency


async def test_mark_paid_confirms_payment_and_creates_purchase(db, user, product, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    final = await orders.mark_paid(db, purchaser, order.id)
    assert final.status == OrderStatus.PAID
    purchase = await db.purchases.get_purchase_by_order(order.id)
    assert purchase is not None
    assert purchase.state.value == "ready"
    assert purchase.sku  # 演示商品也有兜底 SKU


async def test_double_mark_paid_is_idempotent(db, user, product, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, purchaser, order.id, trade_no="T1")
    final = await orders.mark_paid(db, purchaser, order.id, trade_no="T1")
    assert final.status == OrderStatus.PAID
    assert final.trade_no == "T1"
    purchases = await db.purchases.list_purchases_by_states(tuple(PurchaseState))
    assert len(purchases) == 1  # 采购任务不重复


async def test_concurrent_mark_paid_single_payment_and_purchase(db, user, product, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    results = await asyncio.gather(
        orders.mark_paid(db, purchaser, order.id, trade_no="T1"),
        orders.mark_paid(db, purchaser, order.id, trade_no="T1"),
        return_exceptions=True,
    )
    # 同交易重复确认幂等成功，采购任务只建一份
    assert all(not isinstance(r, Exception) for r in results)
    purchases = await db.purchases.list_purchases_by_states(tuple(PurchaseState))
    assert len(purchases) == 1


async def test_mark_paid_rejects_mismatched_trade_no(db, user, product, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, purchaser, order.id, trade_no="T1")
    with pytest.raises(OrderError, match="does not match"):
        await orders.mark_paid(db, purchaser, order.id, trade_no="T2")


async def test_mark_paid_not_found(db, purchaser):
    with pytest.raises(OrderError, match="not found"):
        await orders.mark_paid(db, purchaser, 999)


async def test_cancel_pending_order(db, user, product):
    order = await orders.create_order(db, user.id, product, 1)
    cancelled = await orders.cancel_order(db, order.id)
    assert cancelled.status == OrderStatus.CANCELLED


async def test_cancel_paid_order_rejected(db, user, product, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, purchaser, order.id)
    with pytest.raises(OrderError, match="cannot be cancelled"):
        await orders.cancel_order(db, order.id)


async def test_demo_fulfill_delivers_and_purchases_once(db, user, product, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, purchaser, order.id)
    first = await purchaser.fulfill(db, order.id)
    assert first is not None and first.status == OrderStatus.DELIVERED
    assert first.payload == f"[stub goods for order #{order.id}]"
    second = await purchaser.fulfill(db, order.id)
    assert second is not None and second.status == OrderStatus.DELIVERED
    purchase = await db.purchases.get_purchase_by_order(order.id)
    assert purchase is not None and purchase.state == PurchaseState.FULFILLED
    assert purchase.attempts == 0  # 模拟模式不经过提交
