import asyncio

import pytest

from shop_bot.models import OrderStatus
from shop_bot.services import orders
from shop_bot.services.orders import OrderError
from shop_bot.services.upstream import DeliveryResult, StubUpstreamClient


async def test_create_order_computes_amount(db, user, product):
    order = await orders.create_order(db, user.id, product, 3)
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert order.amount_cents == product.price_cents * 3
    assert order.currency == product.currency


async def test_mark_paid_delivers_and_notifies(db, user, product):
    order = await orders.create_order(db, user.id, product, 1)
    final, result = await orders.mark_paid(db, StubUpstreamClient(), order.id)
    assert final.status == OrderStatus.DELIVERED
    assert final.upstream_ref == f"STUB-{order.id:06d}"
    assert result.ok
    assert result.payload is not None


async def test_double_pay_reuses_persisted_delivery(db, user, product):
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, StubUpstreamClient(), order.id)
    final, result = await orders.mark_paid(db, StubUpstreamClient(), order.id)
    assert final.payload == result.payload
    assert final.status == OrderStatus.DELIVERED


async def test_concurrent_mark_paid_only_delivers_once(db, user, product):
    calls = []

    class CountingStub(StubUpstreamClient):
        async def deliver(self, order, product):
            calls.append(order.id)
            return await super().deliver(order, product)

    order = await orders.create_order(db, user.id, product, 1)
    results = await asyncio.gather(
        orders.mark_paid(db, CountingStub(), order.id),
        orders.mark_paid(db, CountingStub(), order.id),
    )
    assert all(order.status == OrderStatus.DELIVERED for order, _ in results)
    assert results[0][1].payload == results[1][1].payload
    assert len(calls) == 1


async def test_upstream_failure_marks_delivery_failed(db, user, product):
    class FailStub(StubUpstreamClient):
        async def deliver(self, order, product):
            return DeliveryResult(ok=False, error="out of stock")

    order = await orders.create_order(db, user.id, product, 1)
    with pytest.raises(OrderError, match="upstream rejected delivery"):
        await orders.mark_paid(db, FailStub(), order.id)
    final = await db.get_order(order.id)
    assert final.status == OrderStatus.DELIVERY_FAILED


async def test_upstream_exception_marks_delivery_failed(db, user, product):
    class CrashStub(StubUpstreamClient):
        async def deliver(self, order, product):
            raise RuntimeError("network down")

    order = await orders.create_order(db, user.id, product, 1)
    with pytest.raises(OrderError, match="upstream delivery unavailable"):
        await orders.mark_paid(db, CrashStub(), order.id)
    final = await db.get_order(order.id)
    assert final.status == OrderStatus.DELIVERY_FAILED


async def test_cancel_pending_order(db, user, product):
    order = await orders.create_order(db, user.id, product, 1)
    cancelled = await orders.cancel_order(db, order.id)
    assert cancelled.status == OrderStatus.CANCELLED


async def test_cancel_paid_order_rejected(db, user, product):
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, StubUpstreamClient(), order.id)
    with pytest.raises(OrderError, match="cannot be cancelled"):
        await orders.cancel_order(db, order.id)
