import asyncio
import hashlib
import logging
from types import SimpleNamespace

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Message
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shop_bot import build_dispatcher
from shop_bot.db import Database, FSMStorage
from shop_bot.handlers.admin import cmd_paid
from shop_bot.handlers.order import cb_start_order
from shop_bot.handlers.start import cmd_query
from shop_bot.models import OrderStatus, Product, PurchaseState
from shop_bot.services import orders
from shop_bot.services.epay import _create_sign
from shop_bot.services.fulfillment import notify_owner, recover_once
from shop_bot.services.purchasing import CommbitzPurchaser, DemoPurchaser
from shop_bot.web.payment import register_epay_routes
from shop_bot.web.telegram import register_telegram_routes
from tests.fakes import FakeCommbitzGateway


@pytest.fixture
async def pending(db, user):
    product = Product(1, "测试商品", "", 999, "CNY")
    await db.seed_products([product])
    return await orders.create_order(db, user.id, product, 1)


@pytest.fixture
async def http_client(db, epay, purchaser, bot):
    app = web.Application()
    app.update({"db": db, "epay": epay, "purchaser": purchaser, "bot": bot})
    register_epay_routes(app, "/payment/callback")
    async with TestClient(TestServer(app)) as client:
        yield client


def callback_params(order, **changes):
    params = {
        "pid": "1000",
        "name": "测试商品",
        "out_trade_no": str(order.id),
        "trade_no": f"T{order.id}",
        "money": "9.99",
        "trade_status": "TRADE_SUCCESS",
    }
    params.update(changes)
    params["sign"] = _create_sign(params, "audit-secret")
    return params


def query_message(bot, order, user_id=42, chat_id=42):
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Audit"},
            "text": f"/query {order.id}",
        },
        context={"bot": bot},
    )


def query_result(order, **changes):
    return {
        "code": 1,
        "status": 1,
        "pid": "1000",
        "trade_no": f"T{order.id}",
        "out_trade_no": str(order.id),
        "money": "9.99",
        **changes,
    }


def test_signature_with_non_ascii_and_urls():
    values = {"name": "测试 商品", "notify_url": "https://bot.example.com/callback?a=1&b=2"}
    expected = hashlib.md5(  # noqa: S324 - EPay protocol fixture
        "name=测试 商品&notify_url=https://bot.example.com/callback?a=1&b=2audit-secret".encode()
    ).hexdigest()
    assert _create_sign(values, "audit-secret") == expected


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_callback_confirms_payment_and_worker_delivers_once(
    *, http_client, db, pending, purchaser, bot, method
):
    # 回调快速应答：只确认收款并建立采购任务（开发方案规则 1/2）
    kwargs = {"params" if method == "GET" else "data": callback_params(pending)}
    for _ in range(2):
        response = await http_client.request(method, "/payment/callback", **kwargs)
        assert response.status == 200
        assert await response.text() == "success"
    paid = await db.get_order(pending.id)
    assert paid is not None and paid.status == OrderStatus.PAID
    purchases = await db.list_purchases_by_states(tuple(PurchaseState))
    assert len(purchases) == 1
    # 后台恢复循环驱动履约；重复回调/重复扫描都不重复采购
    await recover_once(db, purchaser, bot)
    await recover_once(db, purchaser, bot)
    final = await db.get_order(pending.id)
    assert final is not None and final.payload and final.notified_at
    assert len(bot.session.sent) == 2  # 头条 + 货品分条


@pytest.mark.parametrize(
    "changes", [{"money": "0.01"}, {"pid": "9999"}, {"trade_no": ""}, {"money": "NaN"}, {"money": "9.990"}]
)
async def test_callback_rejects_inconsistent_payment(*, http_client, db, pending, purchaser, changes):
    response = await http_client.post("/payment/callback", data=callback_params(pending, **changes))
    assert response.status == 422
    assert (await db.get_order(pending.id)).status == OrderStatus.PENDING_PAYMENT
    assert await db.list_purchases_by_states(tuple(PurchaseState)) == []


async def test_callback_rejects_bad_signature_and_duplicate_keys(*, http_client, pending):
    params = callback_params(pending)
    response = await http_client.get("/payment/callback", params={**params, "sign": "bad"})
    assert response.status == 401
    response = await http_client.get("/payment/callback", params=[*params.items(), ("money", "0.01")])
    assert response.status == 400


async def test_trade_number_cannot_pay_two_orders(*, http_client, db, pending, purchaser):
    first = await http_client.get("/payment/callback", params=callback_params(pending))
    assert first.status == 200
    second = await db.create_order(pending.user_id, pending.product_id, 1, 999, "CNY")
    response = await http_client.get("/payment/callback", params=callback_params(second, trade_no=f"T{pending.id}"))
    assert response.status == 422
    assert (await db.get_order(second.id)).status == OrderStatus.PENDING_PAYMENT
    assert await db.get_purchase_by_order(second.id) is None


@pytest.mark.parametrize(
    "changes", [{"money": "0.01"}, {"out_trade_no": "9999"}, {"trade_no": ""}, {"trade_no": None}, {"pid": "9999"}]
)
async def test_query_uses_same_payment_checks(*, db, pending, epay, purchaser, bot, httpx_mock, changes):
    httpx_mock.add_response(json=query_result(pending, **changes))
    await cmd_query(query_message(bot, pending), db, epay, purchaser, bot)
    assert (await db.get_order(pending.id)).status == OrderStatus.PENDING_PAYMENT
    assert await db.get_purchase_by_order(pending.id) is None


@pytest.mark.parametrize("user_id,chat_id", [(42, -100123), (700, 700)])
async def test_query_only_sends_goods_to_owner(
    *, db, pending, epay, purchaser, bot, httpx_mock, monkeypatch, user_id, chat_id
):
    monkeypatch.setattr("shop_bot.handlers.start.get_settings", lambda: SimpleNamespace(admin_ids=[700]))
    httpx_mock.add_response(json=query_result(pending))
    await cmd_query(query_message(bot, pending, user_id, chat_id), db, epay, purchaser, bot)
    goods = [m for m in bot.session.sent if "stub goods" in m.text]
    assert len(goods) == 1 and goods[0].chat_id == 42
    assert bot.session.sent[-1].chat_id == chat_id
    assert "stub goods" not in bot.session.sent[-1].text


async def test_query_denies_other_buyers(*, db, pending, epay, purchaser, bot):
    await db.upsert_user(43, "other")
    await cmd_query(query_message(bot, pending, 43, 43), db, epay, purchaser, bot)
    assert await db.get_purchase_by_order(pending.id) is None
    assert "只能查询自己的订单" in bot.session.sent[-1].text


async def test_payment_error_does_not_leak_key_in_reply_or_logs(
    *, db, pending, epay, purchaser, bot, httpx_mock, caplog
):
    httpx_mock.add_response(status_code=503)
    with caplog.at_level(logging.DEBUG):
        await cmd_query(query_message(bot, pending), db, epay, purchaser, bot)
    assert "audit-secret" not in caplog.text
    assert all("audit-secret" not in m.text for m in bot.session.sent)
    assert await db.get_purchase_by_order(pending.id) is None


async def test_notification_failure_recovers_without_purchasing_again(
    *, http_client, db, pending, purchaser, bot, epay
):
    bot.session.fail_send = True
    response = await http_client.post("/payment/callback", data=callback_params(pending))
    assert response.status == 200
    await recover_once(db, purchaser, bot)
    persisted = await db.get_order(pending.id)
    assert persisted is not None and persisted.payload and persisted.notified_at is None
    bot.session.fail_send = False
    await recover_once(db, purchaser, bot)
    final = await db.get_order(pending.id)
    assert final is not None and final.notified_at
    purchase = await db.get_purchase_by_order(pending.id)
    assert purchase is not None and purchase.state == PurchaseState.FULFILLED
    # 已通知的订单仍可通过 /query 明确请求补发，只发送同一份货品。
    await cmd_query(query_message(bot, pending), db, epay, purchaser, bot)
    assert final.payload is not None
    assert sum(final.payload in m.text for m in bot.session.sent) == 2


async def test_restart_before_submit_becomes_manual_not_repurchase(*, tmp_path, bot, httpx_mock):
    """提交中途中断：意图已留痕，重启后转 submission_unknown，绝不第二次创建。"""
    path = str(tmp_path / "recovery.db")
    db = Database(path)
    await db.connect()
    try:
        user = await db.upsert_user(42, "audit")
        product = Product(1, "test", "", 999, "CNY", sku="US-1", request_type="esim")
        await db.seed_products([product])
        pending = await orders.create_order(db, user.id, product, 1)
        await orders.mark_paid(db, DemoPurchaser(), pending.id, trade_no="T1")
        # 模拟：提交意图已持久化，但进程在等待上游响应时崩溃
        purchase = await db.get_purchase_by_order(pending.id)
        assert purchase is not None
        await db.transition_purchase(purchase.id, PurchaseState.SUBMITTING, from_state=PurchaseState.READY)
    finally:
        await db.close()

    recovered = Database(path)
    await recovered.connect()
    try:
        gateway = FakeCommbitzGateway()
        purchaser = CommbitzPurchaser(gateway)
        await recover_once(recovered, purchaser, bot)
        purchase = await recovered.get_purchase_by_order(pending.id)
        assert purchase is not None
        assert purchase.state == PurchaseState.SUBMISSION_UNKNOWN
        assert gateway.create_calls == 0  # 恢复只归档状态，不重新购买
        saved = await recovered.get_order(pending.id)
        assert saved is not None and saved.status == OrderStatus.PAID
    finally:
        await recovered.close()


@pytest.mark.parametrize("status", [OrderStatus.PAID, OrderStatus.DELIVERY_FAILED])
async def test_admin_can_resume_paid_or_failed_without_pending_reset(
    *, db, pending, purchaser, bot, status
):
    await db.transition_order(pending.id, status, trade_no="T1")
    message = Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 700, "type": "private"},
            "from": {"id": 700, "is_bot": False, "first_name": "Admin"},
            "text": f"/paid {pending.id}",
        },
        context={"bot": bot},
    )
    await cmd_paid(message, db, purchaser, bot)
    final = await db.get_order(pending.id)
    assert final is not None and final.status == OrderStatus.DELIVERED and final.trade_no == "T1"
    async with db.connection() as conn:
        async with conn.execute("SELECT to_status FROM order_events") as cur:
            assert "pending_payment" not in [r[0] for r in await cur.fetchall()]


async def test_webhook_checks_secret_before_dispatch(*, bot, monkeypatch):
    app, dispatcher = web.Application(), Dispatcher()
    received = asyncio.Event()

    async def feed_raw_update(**kwargs):
        received.set()

    monkeypatch.setattr(dispatcher, "feed_raw_update", feed_raw_update)
    register_telegram_routes(app, dispatcher, bot, "/webhook", "configured-secret")
    async with TestClient(TestServer(app)) as client:
        for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}):
            response = await client.post("/webhook", json={"update_id": 1}, headers=headers)
            assert response.status == 401
            assert not received.is_set()
        response = await client.post(
            "/webhook", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "configured-secret"}
        )
        assert response.status == 200
        await asyncio.wait_for(received.wait(), timeout=1)


@pytest.mark.parametrize("secret", ["", "with spaces", "x" * 257])
async def test_missing_or_invalid_webhook_secret_cannot_start(*, bot, secret):
    with pytest.raises(ValueError, match=r"webhook\.secret_token"):
        register_telegram_routes(web.Application(), Dispatcher(), bot, "/webhook", secret)


async def test_switching_product_resets_stale_order_context(db, pending, bot):
    """P1-a：切换商品时清空上一单残留的天数/号码，防止报价与订单金额不一致。"""
    context = FSMContext(
        storage=FSMStorage(db), key=StorageKey(bot_id=1, chat_id=42, user_id=42)
    )
    await context.set_state("OrderFlow:quantity")
    await context.set_data({
        "product_id": pending.product_id, "quantity": 2, "days": 30, "msisdn": "+8613800138000",
    })
    callback = CallbackQuery.model_validate(
        {
            "id": "9",
            "from_user": {"id": 42, "is_bot": False, "first_name": "Audit"},
            "chat_instance": "test",
            "data": f"o:{pending.product_id}",
            "message": {"message_id": 5, "date": 0, "chat": {"id": 42, "type": "private"}, "text": "cat"},
        },
        context={"bot": bot},
    )
    await cb_start_order(callback, context, db)
    data = await context.get_data()
    assert data["days"] is None and data["msisdn"] is None and data["quantity"] is None
    assert data["product_id"] == pending.product_id
    assert await context.get_state() == "OrderFlow:quantity"


async def test_dispatcher_serializes_duplicate_order_confirmation(db, pending, bot):
    dispatcher = build_dispatcher(db, DemoPurchaser(), None, None)
    context = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
    await context.set_state("OrderFlow:quantity")
    await context.set_data({"product_id": pending.product_id, "quantity": 2})

    def confirm(update_id):
        return {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "from": {"id": 42, "is_bot": False, "first_name": "Audit"},
                "chat_instance": "test",
                "data": "order:confirm",
                "message": {"message_id": 10, "date": 0, "chat": {"id": 42, "type": "private"}, "text": "confirm"},
            },
        }

    await asyncio.gather(
        dispatcher.feed_raw_update(bot, confirm(1)),
        dispatcher.feed_raw_update(bot, confirm(2)),
    )
    created = await db.list_orders()
    assert len(created) == 2  # 一个 fixture 订单，加一个确认订单。
    assert sum(o.quantity == 2 for o in created) == 1
    assert await context.get_data() == {}


async def test_failed_explicit_resend_remains_pending_for_worker(db, pending, purchaser, bot):
    await orders.mark_paid(db, purchaser, pending.id)
    await recover_once(db, purchaser, bot)
    assert (await db.get_order(pending.id)).notified_at
    bot.session.fail_send = True
    assert not await notify_owner(db, bot, pending.id, resend=True)
    assert (await db.get_order(pending.id)).notification_pending
    bot.session.fail_send = False
    await recover_once(db, purchaser, bot)
    assert not (await db.get_order(pending.id)).notification_pending
    purchase = await db.get_purchase_by_order(pending.id)
    assert purchase is not None and purchase.state == PurchaseState.FULFILLED
