"""重绑整条链路：冻结、核验绑定、等待/失败/重启、重新取货、买家私信。"""

import asyncio
import copy
import sqlite3
from typing import Any

import pytest
from aiogram.types import Message

from shop_bot.db import Database
from shop_bot.handlers.admin import cmd_paid
from shop_bot.handlers.start import cmd_query
from shop_bot.models import OrderStatus, Product, PurchaseState
from shop_bot.services import orders
from shop_bot.services.commbitz_api import CommbitzError
from shop_bot.services.fulfillment import notify_owner, recover_once
from shop_bot.services.purchasing import CommbitzPurchaser

from .fakes import FakeCommbitzGateway

OLD_REF = "old-shared"
NEW_REF = "new-correct"
STALE_GOODS = "LPA:STALE-SHARED"


def details(reference, label):
    return {
        "_id": reference,
        "orderId": f"DR-{reference}",
        "status": "Success",
        "requestType": "esim",
        "planId": "PLAN",
        "sku": "SKU",
        "quantity": 1,
        "kycStatus": None,
        "isKycRequired": False,
        "esims": [{"iccid": f"89-{label}", "lpa": f"LPA:{label}", "qrCode": f"https://example.invalid/{label}.png"}],
    }


class Gateway(FakeCommbitzGateway):
    def __init__(self):
        super().__init__()
        # 值可以是响应字典，也可以是模拟查询失败的异常
        self.responses: dict[str, Any] = {
            OLD_REF: details(OLD_REF, "OLD-VERIFIED"),
            NEW_REF: details(NEW_REF, "NEW-VERIFIED"),
        }
        self.queries = []

    async def create_request(self, **kwargs):
        self.create_calls += 1
        raise AssertionError("重绑后的恢复不得重新采购")

    async def get_order_details(self, request_id):
        self.queries.append(request_id)
        response = self.responses[request_id]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


@pytest.fixture
async def frozen_orders(tmp_path):
    """模拟旧库的重复交付资料，使用真实买家行，启动迁移后两笔都应冻结。"""
    path = str(tmp_path / "rebinding.db")
    database = Database(path)
    await database.connect()
    purchaser = CommbitzPurchaser(Gateway())
    order_ids = []
    try:
        async with database.transaction() as conn:
            await conn.execute("DROP INDEX idx_purchases_upstream")
        product = Product(1, "audit", "", 999, "CNY", sku="SKU", upstream_plan_id="PLAN", request_type="esim")
        await database.products.seed_products([product])
        for telegram_id in (42, 43):
            user = await database.users.upsert_user(telegram_id, f"buyer-{telegram_id}")
            order = await orders.create_order(database, user.id, product, 1)
            await orders.mark_paid(database, purchaser, order.id, trade_no=f"PAY-{telegram_id}")
            purchase = await database.purchases.get_purchase_by_order(order.id)
            assert purchase is not None
            await database.purchases.transition_purchase(
                purchase.id,
                PurchaseState.FULFILLED,
                upstream_request_id=OLD_REF,
                upstream_order_no="OLD-NO",
            )
            await database.orders.transition_order(
                order.id, OrderStatus.DELIVERED, upstream_ref=OLD_REF, payload=STALE_GOODS
            )
            order_ids.append(order.id)
    finally:
        await database.close()
    recovered = Database(path)
    await recovered.connect()
    try:
        yield recovered, order_ids, path
    finally:
        await recovered.close()


def goods_messages(bot, buyer_id):
    # eSIM 的 LPA 现随二维码图片 caption 发送（不再是独立文本）；返回 text 或 caption 字符串
    return [
        (getattr(m, "text", "") or "") + (getattr(m, "caption", "") or "")
        for m in bot.session.sent
        if m.chat_id == buyer_id
        and ("LPA:" in (getattr(m, "text", "") or "") or "LPA:" in (getattr(m, "caption", "") or ""))
    ]


@pytest.mark.parametrize("already_notified", [False, True])
async def test_rebind_invalidates_old_delivery_and_rechecks_both_buyers(frozen_orders, bot, already_notified):
    db, (first, second), _ = frozen_orders
    gateway = Gateway()
    purchaser = CommbitzPurchaser(gateway)
    if already_notified:
        await db.deliveries.mark_notified(second)
    await recover_once(db, purchaser, bot)
    for order_id in (first, second):
        assert not await notify_owner(db, bot, order_id, resend=True)
    assert bot.session.sent == []

    # 绑定不能沿用旧业务展示编号，也不能沿用旧货品和通知时间。
    gateway.responses[NEW_REF].pop("orderId")
    ok, reason = await purchaser.bind_unknown_purchase(db, second, NEW_REF)
    assert ok, reason
    order = await db.orders.get_order(second)
    purchase = await db.purchases.get_purchase_by_order(second)
    assert order.status == OrderStatus.PAID
    assert order.amount_cents == 999 and order.trade_no == "PAY-43"
    assert order.payload is None and order.upstream_ref is None
    assert order.notified_at is None and not order.notification_pending
    assert purchase.state == PurchaseState.UPSTREAM_PENDING and purchase.upstream_request_id == NEW_REF
    assert purchase.upstream_order_no is None
    assert not await notify_owner(db, bot, second, resend=True)
    assert bot.session.sent == []

    await recover_once(db, purchaser, bot)
    final = await db.orders.get_order(second)
    assert final.status == OrderStatus.DELIVERED and final.upstream_ref == NEW_REF
    assert final.notified_at and "NEW-VERIFIED" in final.payload
    assert len(goods_messages(bot, 43)) == 1
    assert "NEW-VERIFIED" in goods_messages(bot, 43)[0]
    assert not goods_messages(bot, 42)
    assert all(STALE_GOODS not in (getattr(m, "text", "") or "") for m in bot.session.sent)

    # 第一名买家绑定回原单也必须重新读取，不能直接解冻旧缓存。
    ok, reason = await purchaser.bind_unknown_purchase(db, first, OLD_REF)
    assert ok, reason
    assert (await db.orders.get_order(first)).status == OrderStatus.PAID
    assert not await notify_owner(db, bot, first, resend=True)
    await recover_once(db, purchaser, bot)
    assert len(goods_messages(bot, 42)) == 1 and "OLD-VERIFIED" in goods_messages(bot, 42)[0]
    assert (await db.purchases.get_purchase_by_order(first)).state == PurchaseState.FULFILLED
    assert (await db.purchases.get_purchase_by_order(second)).state == PurchaseState.FULFILLED
    assert gateway.queries == [NEW_REF, NEW_REF, OLD_REF, OLD_REF]
    assert gateway.create_calls == 0


@pytest.mark.parametrize("mode", ["pending", "missing_goods", "kyc", "query_error"])
async def test_rebound_order_waits_without_sending_old_goods(frozen_orders, bot, mode, queue_clock):
    db, (_, order_id), _ = frozen_orders
    gateway = Gateway()
    purchaser = CommbitzPurchaser(gateway)
    assert (await purchaser.bind_unknown_purchase(db, order_id, NEW_REF))[0]
    response = gateway.responses[NEW_REF]
    if mode == "pending":
        response["status"] = "pending"
    elif mode == "missing_goods":
        response["esims"] = []
    elif mode == "kyc":
        response.update(kycStatus="submitted", isKycRequired=True, isKycVerified=False)
    else:
        gateway.responses[NEW_REF] = CommbitzError("simulated query failure", 503)
    await recover_once(db, purchaser, bot)
    assert (await db.orders.get_order(order_id)).status == OrderStatus.PAID
    assert (await db.orders.get_order(order_id)).payload is None
    assert not await notify_owner(db, bot, order_id, resend=True)
    assert bot.session.sent == [] and gateway.create_calls == 0
    gateway.responses[NEW_REF] = details(NEW_REF, "NEW-VERIFIED")
    queue_clock()
    await recover_once(db, purchaser, bot)
    assert "NEW-VERIFIED" in goods_messages(bot, 43)[0]
    assert gateway.create_calls == 0


@pytest.mark.parametrize("command", ["query", "paid"])
async def test_manual_entry_only_sends_new_goods_to_owner(frozen_orders, bot, command):
    db, (_, order_id), _ = frozen_orders
    gateway = Gateway()
    purchaser = CommbitzPurchaser(gateway)
    assert (await purchaser.bind_unknown_purchase(db, order_id, NEW_REF))[0]
    user_id = 43 if command == "query" else 700
    message = Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Audit"},
            "text": f"/{command} {order_id}",
        },
        context={"bot": bot},
    )
    if command == "query":
        await cmd_query(message, db, None, purchaser, bot)
    else:
        await cmd_paid(message, db, purchaser, bot)
    await recover_once(db, purchaser, bot)
    assert len(goods_messages(bot, 43)) == 1 and "NEW-VERIFIED" in goods_messages(bot, 43)[0]
    assert not goods_messages(bot, 700)
    assert all(STALE_GOODS not in (getattr(m, "text", "") or "") for m in bot.session.sent)
    assert gateway.create_calls == 0


async def test_rebind_survives_restart_and_only_queries_existing_order(frozen_orders, bot):
    db, (_, order_id), path = frozen_orders
    gateway = Gateway()
    purchaser = CommbitzPurchaser(gateway)
    assert (await purchaser.bind_unknown_purchase(db, order_id, NEW_REF))[0]
    await db.close()
    restarted = Database(path)
    await restarted.connect()
    try:
        rebound = await restarted.orders.get_order(order_id)
        assert rebound is not None and rebound.payload is None
        await recover_once(restarted, purchaser, bot)
        final = await restarted.orders.get_order(order_id)
        assert final is not None and final.upstream_ref == NEW_REF
        assert "NEW-VERIFIED" in goods_messages(bot, 43)[0]
        assert gateway.create_calls == 0
    finally:
        await restarted.close()


@pytest.mark.parametrize("notified", [False, True])
@pytest.mark.parametrize("state", [PurchaseState.UPSTREAM_PENDING, PurchaseState.FULFILLED])
async def test_repairs_old_rebinding_bug_even_if_already_notified(frozen_orders, bot, state, notified):
    db, (_, order_id), _ = frozen_orders
    gateway = Gateway()
    purchaser = CommbitzPurchaser(gateway)
    purchase = await db.purchases.get_purchase_by_order(order_id)
    # 模拟旧版 /bind 只更新采购引用，订单仍保存旧引用/旧货品。
    await db.purchases.transition_purchase(purchase.id, state, upstream_request_id=NEW_REF)
    if notified:
        await db.deliveries.mark_notified(order_id)
    assert not await notify_owner(db, bot, order_id, resend=True)
    assert order_id in [o.id for o in await db.work.list_recovery_orders()]
    await recover_once(db, purchaser, bot)
    final = await db.orders.get_order(order_id)
    assert final.status == OrderStatus.DELIVERED and final.upstream_ref == NEW_REF
    assert final.trade_no == "PAY-43" and final.amount_cents == 999
    assert "NEW-VERIFIED" in final.payload and final.notified_at
    assert (await db.purchases.get_purchase_by_order(order_id)).state == PurchaseState.FULFILLED
    assert gateway.queries == [NEW_REF] and gateway.create_calls == 0
    assert "NEW-VERIFIED" in goods_messages(bot, 43)[0]


async def test_rebind_update_and_old_payload_invalidation_rollback_together(frozen_orders, bot):
    db, (_, order_id), _ = frozen_orders
    gateway = Gateway()
    purchaser = CommbitzPurchaser(gateway)
    before_order = await db.orders.get_order(order_id)
    before_purchase = await db.purchases.get_purchase_by_order(order_id)
    before_events = await db.fetch_all("SELECT * FROM order_events WHERE order_id = ?", (order_id,))
    async with db.transaction() as conn:
        await conn.execute("""CREATE TRIGGER reject_binding BEFORE UPDATE OF upstream_request_id ON purchases
            WHEN NEW.upstream_request_id = 'new-correct'
            BEGIN SELECT RAISE(ABORT, 'simulated binding failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated binding failure"):
        await purchaser.bind_unknown_purchase(db, order_id, NEW_REF)
    assert await db.orders.get_order(order_id) == before_order
    assert await db.purchases.get_purchase_by_order(order_id) == before_purchase
    assert len(await db.fetch_all("SELECT * FROM order_events WHERE order_id = ?", (order_id,))) == len(before_events)
    assert not await notify_owner(db, bot, order_id, resend=True)
    assert bot.session.sent == []


async def test_notification_waits_for_rebind_and_rejects_old_payload(frozen_orders, bot):
    db, (_, order_id), _ = frozen_orders
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowGateway(Gateway):
        async def get_order_details(self, request_id):
            entered.set()
            await release.wait()
            return await super().get_order_details(request_id)

    purchaser = CommbitzPurchaser(SlowGateway())
    binding = asyncio.create_task(purchaser.bind_unknown_purchase(db, order_id, NEW_REF))
    await asyncio.wait_for(entered.wait(), timeout=2)
    notifying = asyncio.create_task(notify_owner(db, bot, order_id, resend=True))
    try:
        await asyncio.sleep(0)
        assert not notifying.done()
    finally:
        release.set()
        result, sent = await asyncio.gather(binding, notifying)
    assert result[0] and not sent
    assert bot.session.sent == [] and (await db.orders.get_order(order_id)).payload is None


@pytest.mark.parametrize("problem", ["state_changed", "wrong_purchase", "wrong_reference"])
async def test_finalize_delivery_refuses_stale_identity(frozen_orders, problem):
    db, (first, second), _ = frozen_orders
    purchaser = CommbitzPurchaser(Gateway())
    assert (await purchaser.bind_unknown_purchase(db, second, NEW_REF))[0]
    purchase = await db.purchases.get_purchase_by_order(second)
    purchase_id = purchase.id
    reference = NEW_REF
    if problem == "state_changed":
        await db.purchases.transition_purchase(purchase.id, PurchaseState.SUBMISSION_UNKNOWN)
    elif problem == "wrong_purchase":
        purchase_id = (await db.purchases.get_purchase_by_order(first)).id
    else:
        reference = OLD_REF
    final = await db.deliveries.finalize_delivery(
        second,
        purchase_id,
        from_purchase_state=PurchaseState.UPSTREAM_PENDING,
        upstream_ref=reference,
        payload="unverified goods",
    )
    assert final is None
    assert (await db.orders.get_order(second)).status == OrderStatus.PAID
    assert (await db.orders.get_order(second)).payload is None


async def test_rebound_kyc_order_can_submit_documents_before_new_delivery(frozen_orders, bot):
    db, (_, order_id), _ = frozen_orders
    gateway = Gateway()
    gateway.responses[NEW_REF].update(status="pending", kycStatus="pending", isKycRequired=True, isKycVerified=False)
    purchaser = CommbitzPurchaser(gateway)
    assert (await purchaser.bind_unknown_purchase(db, order_id, NEW_REF))[0]
    await recover_once(db, purchaser, bot)
    assert (await db.purchases.get_purchase_by_order(order_id)).state == PurchaseState.AWAITING_KYC
    assert (await db.orders.get_order(order_id)).payload is None and bot.session.sent == []
    accepted, reason = await purchaser.submit_kyc(
        db,
        order_id,
        documents={"passportFront": "https://example.invalid/kyc.jpg"},
    )
    assert accepted, reason
    assert (await db.purchases.get_purchase_by_order(order_id)).state == PurchaseState.KYC_SUBMITTED
    gateway.responses[NEW_REF].update(status="Success", kycStatus="verified", isKycVerified=True)
    await recover_once(db, purchaser, bot)
    assert "NEW-VERIFIED" in goods_messages(bot, 43)[0]
    assert gateway.create_calls == 0


async def test_concurrent_rebinding_keeps_losing_order_frozen_without_index(frozen_orders, bot):
    db, order_ids, _ = frozen_orders
    assert await db.fetch_one("SELECT name FROM sqlite_master WHERE name = 'idx_purchases_upstream'") is None
    ready = asyncio.Event()

    class ConcurrentGateway(Gateway):
        arrivals = 0

        async def get_order_details(self, request_id):
            self.arrivals += 1
            if self.arrivals == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=2)
            return await super().get_order_details(request_id)

    purchaser = CommbitzPurchaser(ConcurrentGateway())
    results = await asyncio.gather(*(purchaser.bind_unknown_purchase(db, oid, NEW_REF) for oid in order_ids))
    assert sum(ok for ok, _ in results) == 1
    for order_id, (accepted, _) in zip(order_ids, results, strict=True):
        order = await db.orders.get_order(order_id)
        purchase = await db.purchases.get_purchase_by_order(order_id)
        if accepted:
            assert order.status == OrderStatus.PAID and order.payload is None
            assert purchase.state == PurchaseState.UPSTREAM_PENDING and purchase.upstream_request_id == NEW_REF
        else:
            assert order.status == OrderStatus.DELIVERED and order.payload == STALE_GOODS
            assert purchase.state == PurchaseState.SUBMISSION_UNKNOWN and purchase.upstream_request_id == OLD_REF
        assert not await notify_owner(db, bot, order_id, resend=True)
    assert bot.session.sent == []


async def test_new_goods_and_purchase_completion_rollback_together(frozen_orders, bot):
    db, (_, order_id), _ = frozen_orders
    gateway = Gateway()
    purchaser = CommbitzPurchaser(gateway)
    assert (await purchaser.bind_unknown_purchase(db, order_id, NEW_REF))[0]
    before = await db.orders.get_order(order_id)
    async with db.transaction() as conn:
        await conn.execute("""CREATE TRIGGER reject_completion BEFORE UPDATE OF state ON purchases
            WHEN NEW.state = 'fulfilled'
            BEGIN SELECT RAISE(ABORT, 'simulated completion failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated completion failure"):
        await purchaser.fulfill(db, order_id)
    assert await db.orders.get_order(order_id) == before
    assert (await db.purchases.get_purchase_by_order(order_id)).state == PurchaseState.UPSTREAM_PENDING
    assert not await notify_owner(db, bot, order_id, resend=True)
    assert bot.session.sent == []
    async with db.transaction() as conn:
        await conn.execute("DROP TRIGGER reject_completion")
    await recover_once(db, purchaser, bot)
    assert "NEW-VERIFIED" in goods_messages(bot, 43)[0]
    assert gateway.create_calls == 0
