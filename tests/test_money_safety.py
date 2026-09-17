"""资金生命周期回归：中断、并发、真实用户入口与币种隔离。全部使用离线上游。"""

import asyncio
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shop_bot.config import EPaySettings, Settings
from shop_bot.db import Database, FSMStorage
from shop_bot.handlers.admin import cmd_adjust, cmd_currency, cmd_refund
from shop_bot.handlers.balance import start_topup, topup_amount_input
from shop_bot.handlers.kyc import cmd_kyc
from shop_bot.handlers.order import cb_confirm, cb_pay_online, cb_pay_with_balance, product_quote
from shop_bot.models import OrderStatus, Product, PurchaseState
from shop_bot.services import orders
from shop_bot.services.commbitz_api import CommbitzError
from shop_bot.services.epay import (
    EPayClient,
    EPayConfig,
    EPayError,
    EPayOrder,
    EPayQueryResult,
    _create_sign,
    parse_money_cents,
)
from shop_bot.services.fulfillment import recover_once
from shop_bot.services.purchasing import CommbitzPurchaser
from shop_bot.web.payment import register_epay_routes
from tests.fakes import FakeCommbitzGateway
from tests.test_balance import _balance_callback, menu_message_of


async def new_order(db, *, currency="USD"):
    user = await db.upsert_user(42, "audit")
    product = Product(1, "audit", "", 10000, currency, sku="AUDIT", request_type="esim")
    await db.seed_products([product])
    return user, await orders.create_order(db, user.id, product, 1)


class RejectedGateway(FakeCommbitzGateway):
    async def create_request(self, **kwargs):
        self.create_calls += 1
        raise CommbitzError("invalid sku", status_code=400)


@pytest.mark.parametrize("after_creation", [False, True])
async def test_refund_failure_rolls_back_money_and_recovers_after_restart(tmp_path, bot, after_creation):
    path = str(tmp_path / "restart.db")
    db = Database(path)
    await db.connect()
    gateway = FakeCommbitzGateway(details={"status": "failed"}) if after_creation else RejectedGateway()
    purchaser = CommbitzPurchaser(gateway)
    try:
        user, order = await new_order(db)
        await orders.mark_paid(db, purchaser, order.id, trade_no="PAID")
        async with db.transaction() as conn:
            await conn.execute("""CREATE TRIGGER stop_refund BEFORE INSERT ON balance_transactions
                WHEN NEW.kind = 'refund' BEGIN SELECT RAISE(ABORT, 'interrupted refund'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="interrupted refund"):
            await purchaser.fulfill(db, order.id)
        saved_order = await db.get_order(order.id)
        saved_purchase = await db.get_purchase_by_order(order.id)
        assert saved_order is not None and saved_order.status == OrderStatus.PAID
        assert saved_purchase is not None and saved_purchase.state == PurchaseState.REFUND_PENDING
        assert await db.get_balance(user.id, "USD") == 0
        assert await db.list_balance_transactions(user.id) == []
        async with db.transaction() as conn:
            await conn.execute("DROP TRIGGER stop_refund")
    finally:
        await db.close()
    db = Database(path)
    await db.connect()
    try:
        await recover_once(db, purchaser, bot)
        await recover_once(db, purchaser, bot)
        saved_order = await db.get_order(order.id)
        saved_purchase = await db.get_purchase_by_order(order.id)
        assert saved_order is not None and saved_order.status == OrderStatus.REFUNDED
        assert saved_purchase is not None and saved_purchase.state == PurchaseState.REFUNDED
        assert await db.get_balance(user.id, "USD") == 10000
        assert await db.get_balance(user.id, "CNY") == 0
        assert len(await db.list_balance_transactions(user.id)) == 1
        assert gateway.create_calls == 1
        assert sum("已退回余额" in (m.text or "") for m in bot.session.sent) == 1
    finally:
        await db.close()


async def test_old_paid_rejected_is_refunded_without_repurchase(db, bot):
    user, order = await new_order(db)
    gateway = FakeCommbitzGateway()
    purchaser = CommbitzPurchaser(gateway)
    await orders.mark_paid(db, purchaser, order.id, trade_no="OLD")
    purchase = await db.get_purchase_by_order(order.id)
    await db.transition_purchase(purchase.id, PurchaseState.REJECTED)
    await recover_once(db, purchaser, bot)
    assert await db.get_balance(user.id, "USD") == 10000
    assert gateway.create_calls == 0


class BlockingGateway(FakeCommbitzGateway):
    def __init__(self, delivered):
        super().__init__(
            details={
                "status": "Success",
                "esims": [{"iccid": "89-audit", "lpa": "LPA:audit", "qrCode": "https://example.invalid/qr"}],
            }
            if delivered
            else {"status": "pending"}
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def create_request(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super().create_request(**kwargs)


@pytest.mark.parametrize("delivered", [False, True])
async def test_admin_refund_waits_for_active_purchase_and_refuses(db, bot, delivered):
    user, order = await new_order(db)
    gateway = BlockingGateway(delivered)
    purchaser = CommbitzPurchaser(gateway)
    await orders.mark_paid(db, purchaser, order.id, trade_no="PAID")
    fulfillment = asyncio.create_task(purchaser.fulfill(db, order.id))
    await gateway.entered.wait()
    refund = asyncio.create_task(
        cmd_refund(menu_message_of(bot).model_copy(update={"text": f"/refund {order.id}"}), db, bot)
    )
    await asyncio.sleep(0)
    assert not refund.done()
    gateway.release.set()
    await asyncio.wait_for(asyncio.gather(fulfillment, refund), timeout=2)
    assert (await db.get_order(order.id)).status == (OrderStatus.DELIVERED if delivered else OrderStatus.PAID)
    assert await db.get_balance(user.id, "USD") == 0
    assert "无法退款" in bot.session.sent[-1].text


@pytest.mark.parametrize("state", [PurchaseState.AWAITING_KYC, PurchaseState.KYC_SUBMITTED])
async def test_legacy_refunded_order_cannot_enter_or_submit_kyc(db, bot, state):
    _, order = await new_order(db)
    gateway = FakeCommbitzGateway()
    purchaser = CommbitzPurchaser(gateway)
    await orders.mark_paid(db, purchaser, order.id, trade_no="PAID")
    purchase = await db.get_purchase_by_order(order.id)
    await db.transition_purchase(purchase.id, state, upstream_request_id="known-id")
    # 旧版关单遗留的采购状态；服务必须独立检查订单终态。
    await db.transition_order(order.id, OrderStatus.REFUNDED)
    context = FSMContext(storage=FSMStorage(db), key=StorageKey(bot_id=1, chat_id=42, user_id=42))
    await cmd_kyc(menu_message_of(bot).model_copy(update={"text": f"/kyc {order.id}"}), db, purchaser, context)
    assert await context.get_state() is None
    ok, _ = await purchaser.submit_kyc(db, order.id, documents={"passportFront": "https://example.invalid/doc"})
    assert not ok
    assert (await db.get_purchase_by_order(order.id)).state == state


async def test_refund_notification_is_recoverable(db, bot):
    user, order = await new_order(db)
    purchaser = CommbitzPurchaser(RejectedGateway())
    await orders.mark_paid(db, purchaser, order.id, trade_no="PAID")
    bot.session.fail_send = True
    await recover_once(db, purchaser, bot)
    assert (await db.get_order(order.id)).notification_pending
    bot.session.fail_send = False
    await recover_once(db, purchaser, bot)
    assert not (await db.get_order(order.id)).notification_pending
    assert await db.get_balance(user.id, "USD") == 10000


async def test_refund_cannot_interleave_with_kyc_upload(db):
    user, order = await new_order(db)

    class KycGateway(FakeCommbitzGateway):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def submit_kyc_documents_json(self, request_id, documents):
            self.entered.set()
            await self.release.wait()
            return {"kycStatus": "submitted"}

    gateway = KycGateway()
    purchaser = CommbitzPurchaser(gateway)
    await orders.mark_paid(db, purchaser, order.id, trade_no="PAID")
    purchase = await db.get_purchase_by_order(order.id)
    await db.transition_purchase(purchase.id, PurchaseState.AWAITING_KYC, upstream_request_id="known")
    upload = asyncio.create_task(
        purchaser.submit_kyc(db, order.id, documents={"passportFront": "https://example.invalid/doc"})
    )
    await gateway.entered.wait()
    refund = asyncio.create_task(db.refund_order_to_balance(order.id, "admin checked"))
    await asyncio.sleep(0)
    assert not refund.done()
    gateway.release.set()
    uploaded, (refunded, error) = await asyncio.wait_for(asyncio.gather(upload, refund), timeout=2)
    assert uploaded[0] and refunded is None and error
    assert (await db.get_purchase_by_order(order.id)).state == PurchaseState.KYC_SUBMITTED
    assert await db.get_balance(user.id, "USD") == 0


async def test_usd_order_never_spends_cny_and_refunds_only_usd(db, bot, purchaser):
    user, order = await new_order(db)
    await db.adjust_balance(user.id, 10000, "CNY funding")
    await cb_pay_with_balance(_balance_callback(bot, order.id), db, purchaser, bot)
    assert (await db.get_order(order.id)).status == OrderStatus.PENDING_PAYMENT
    assert await db.get_balance(user.id, "CNY") == 10000
    await db.adjust_balance(user.id, 10000, "USD funding", "USD")
    paid, error = await db.pay_order_with_balance(order.id, user.id, 10000)
    assert error is None and paid is not None
    assert await db.get_balance(user.id, "USD") == 0
    await db.refund_order_to_balance(order.id, "refund same currency")
    assert await db.get_balance(user.id, "USD") == 10000
    assert await db.get_balance(user.id, "CNY") == 10000
    assert [(t.kind, t.currency) for t in await db.list_balance_transactions(user.id)][:2] == [
        ("refund", "USD"),
        ("purchase", "USD"),
    ]


async def test_online_reservation_and_balance_payment_are_mutually_exclusive(db):
    user, order = await new_order(db)
    await db.adjust_balance(user.id, 10000, "funding", "USD")
    reserved, (paid, error) = await asyncio.gather(
        db.reserve_epay(order.id, user.id, "USD"),
        db.pay_order_with_balance(order.id, user.id, 10000),
    )
    assert (reserved is not None) != (paid is not None)
    if reserved is not None:
        assert error == "online payment selected"
        assert await db.get_balance(user.id, "USD") == 10000
    else:
        assert await db.get_balance(user.id, "USD") == 0


async def test_confirmation_only_creates_link_after_channel_selection(db, bot):
    user, _ = await new_order(db)
    await db.adjust_balance(user.id, 10000, "funding", "USD")
    context = FSMContext(storage=FSMStorage(db), key=StorageKey(bot_id=1, chat_id=42, user_id=42))
    await context.set_data({"product_id": 1, "quantity": 1, "product_quote": product_quote(await db.get_product(1))})
    epay = EPayClient(EPayConfig("1000", "audit-secret", "https://pay.example.com", currency="USD"))
    try:
        callback = _balance_callback(bot, 1).model_copy(update={"data": "order:confirm"})
        await cb_confirm(callback, context, db, epay)
        options = bot.session.sent[-2].reply_markup.inline_keyboard
        assert all(button.web_app is None for row in options for button in row)
        online = next(b.callback_data for row in options for b in row if (b.callback_data or "").startswith("epay:"))
        await cb_pay_online(callback.model_copy(update={"data": online}), db, epay)
        buttons = bot.session.sent[-2].reply_markup.inline_keyboard
        pay_url = next(b.web_app.url for row in buttons for b in row if b.web_app)
        assert parse_qs(urlparse(pay_url).query)["money"] == ["100.00"]
        order_id = int(online.split(":")[1])
        assert (await db.get_order(order_id)).payment_method == "epay"
        paid, error = await db.pay_order_with_balance(order_id, user.id, 10000)
        assert paid is None and error == "online payment selected"
    finally:
        await epay.close()


@pytest.mark.parametrize("closed", [OrderStatus.CANCELLED, OrderStatus.REFUNDED, OrderStatus.DELIVERED])
async def test_valid_late_payment_credits_once_without_reopening(db, purchaser, closed):
    user, order = await new_order(db)
    await db.transition_order(order.id, closed, trade_no="ORIGINAL")
    for _ in range(2):
        saved, disposition = await db.record_epay_payment(order.id, "LATE")
        assert saved.status == closed and disposition == "wallet_credit"
    assert await db.get_balance(user.id, "USD") == 10000
    assert await db.get_purchase_by_order(order.id) is None


@pytest.mark.parametrize("status", [OrderStatus.PAID, OrderStatus.DELIVERED, OrderStatus.REFUNDED])
async def test_first_callback_after_manual_confirmation_only_attaches_receipt(db, purchaser, status):
    user, order = await new_order(db)
    await orders.mark_paid(db, purchaser, order.id)
    await db.transition_order(order.id, status)
    for _ in range(2):
        saved, disposition = await db.record_epay_payment(order.id, "ORIGINAL")
        assert saved.status == status and saved.trade_no == "ORIGINAL" and disposition == "order"
    assert await db.get_balance(user.id, "USD") == 0


async def test_compensation_survives_restart_and_replayed_callbacks(tmp_path):
    path = str(tmp_path / "receipts.db")
    db = Database(path)
    await db.connect()
    try:
        user, order = await new_order(db)
        await db.adjust_balance(user.id, 10000, "funding", "USD")
        await db.pay_order_with_balance(order.id, user.id, 10000)
        await db.record_epay_payment(order.id, "LATE")
    finally:
        await db.close()
    db = Database(path)
    await db.connect()
    try:
        await asyncio.gather(*(db.record_epay_payment(order.id, "LATE") for _ in range(4)))
        assert await db.get_balance(user.id, "USD") == 10000
        assert len([tx for tx in await db.list_balance_transactions(user.id) if tx.kind == "payment_credit"]) == 1
    finally:
        await db.close()


async def test_one_usd_balance_cannot_pay_two_orders(db):
    user, first = await new_order(db)
    _, second = await new_order(db)
    await db.adjust_balance(user.id, 10000, "USD funding", "USD")
    await db.adjust_balance(user.id, 10000, "CNY funding", "CNY")
    results = await asyncio.gather(
        db.pay_order_with_balance(first.id, user.id, 10000),
        db.pay_order_with_balance(second.id, user.id, 10000),
    )
    assert sum(order is not None for order, _ in results) == 1
    assert await db.get_balance(user.id, "USD") == 0
    assert await db.get_balance(user.id, "CNY") == 10000


async def test_one_external_trade_cannot_credit_topup_and_order(db):
    user, order = await new_order(db)
    topup = await db.create_topup(user.id, 10000, "USD")
    await db.complete_topup(topup.id, "SHARED")
    with pytest.raises(ValueError, match="belongs to another"):
        await db.record_epay_payment(order.id, "SHARED")
    assert (await db.get_order(order.id)).status == OrderStatus.PENDING_PAYMENT
    assert await db.get_balance(user.id, "USD") == 10000


async def test_compensation_and_receipt_roll_back_together(db):
    user, order = await new_order(db)
    await db.transition_order(order.id, OrderStatus.CANCELLED)
    async with db.transaction() as conn:
        await conn.execute("""CREATE TRIGGER stop_receipt BEFORE INSERT ON payment_receipts
            BEGIN SELECT RAISE(ABORT, 'receipt failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="receipt failure"):
        await db.record_epay_payment(order.id, "LATE")
    assert await db.get_balance(user.id, "USD") == 0
    assert await db.list_balance_transactions(user.id) == []


async def test_currency_command_keeps_existing_order_snapshot_and_sync(db, bot):
    _, order = await new_order(db)
    message = menu_message_of(bot)
    await cmd_currency(message.model_copy(update={"text": "/currency 1 eur"}), db)
    assert (await db.get_product(1)).currency == "EUR"
    assert (await db.get_order(order.id)).currency == "USD"
    await db.upsert_product_from_upstream(
        sku="AUDIT", name="updated", description="", upstream_plan_id="plan", request_type="esim"
    )
    assert (await db.get_product(1)).currency == "EUR"
    await cmd_currency(message.model_copy(update={"text": "/currency 1 FAKE"}), db)
    assert (await db.get_product(1)).currency == "EUR"
    await cmd_adjust(message.model_copy(update={"text": "/adjust 1 USD +25 manual"}), db, bot)
    assert await db.get_balance(order.user_id, "USD") == 2500
    assert await db.get_balance(order.user_id, "CNY") == 0


async def test_legacy_cny_wallet_and_exposed_links_survive_migration(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER, username TEXT,
                balance_cents INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')));
            INSERT INTO users (id, telegram_id, balance_cents) VALUES (1, 42, 50000);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER,
                quantity INTEGER, amount_cents INTEGER, currency TEXT, status TEXT, upstream_ref TEXT,
                created_at TEXT, updated_at TEXT);
            INSERT INTO orders VALUES (1, 1, 1, 1, 100, 'CNY', 'pending_payment', NULL, '', '');
        """)
    for _ in range(2):
        db = Database(path)
        await db.connect()
        try:
            assert await db.get_balance(1, "CNY") == 50000
            assert await db.get_balance(1, "USD") == 0
            legacy_order = await db.get_order(1)
            assert legacy_order is not None and legacy_order.payment_method == "epay"
            paid, error = await db.pay_order_with_balance(1, 1, 100)
            assert paid is None and error == "online payment selected"
        finally:
            await db.close()


async def test_usd_epay_topup_and_order_callback_use_usd_wallet(db, bot, purchaser):
    user, order = await new_order(db)
    epay = EPayClient(EPayConfig("1000", "audit-secret", "https://pay.example.com", currency="USD"))
    app = web.Application()
    app.update({"db": db, "bot": bot, "purchaser": purchaser, "epay": epay})
    register_epay_routes(app, "/callback")
    context = FSMContext(storage=FSMStorage(db), key=StorageKey(bot_id=1, chat_id=42, user_id=42))
    try:
        await start_topup(menu_message_of(bot), db, epay, context)
        await topup_amount_input(menu_message_of(bot).model_copy(update={"text": "100"}), db, epay, context)
        topup = await db.get_topup(1)
        assert topup.currency == "USD"
        async with TestClient(TestServer(app)) as client:
            for ref, trade in (("T1", "TOPUP"), (str(order.id), "ORDER")):
                params = {
                    "pid": "1000",
                    "out_trade_no": ref,
                    "trade_no": trade,
                    "money": "100.00",
                    "trade_status": "TRADE_SUCCESS",
                    "currency": "USD",
                }
                params["sign"] = _create_sign(params, "audit-secret")
                response = await client.post("/callback", data=params)
                assert response.status == 200
        assert await db.get_balance(user.id, "USD") == 10000
        assert await db.get_balance(user.id, "CNY") == 0
        assert (await db.get_order(order.id)).status == OrderStatus.PAID
        with pytest.raises(EPayError, match="currency"):
            epay.validate_payment(order, EPayQueryResult("ORDER", str(order.id), "100.00", True, "", "1000", "CNY"))
        with pytest.raises(EPayError, match="currency"):
            epay.create_pay_url(EPayOrder("test", "1", 1, "https://example.invalid", "https://example.invalid", "CNY"))
    finally:
        await epay.close()


def test_currency_defaults_and_legacy_epay_config():
    assert Product(1, "p", "", 100).currency == "USD"
    # 未提供币种的旧配置保持原有网关行为；示例配置显式选择 USD。
    assert Settings().epay.currency == "CNY"
    assert Settings(epay=EPaySettings(currency="usd")).epay.currency == "USD"
    with pytest.raises(ValueError, match="unsupported currency"):
        Settings(epay=EPaySettings(currency="FAKE"))


async def test_topup_callback_accepts_epay_four_decimal_money(db, bot, purchaser):
    """真实生产案例：EPay 回调 money=20.0000（4 位小数）必须入账而非 422。"""
    user, _order = await new_order(db)
    epay = EPayClient(EPayConfig("1000", "audit-secret", "https://pay.example.com", currency="USD"))
    app = web.Application()
    app.update({"db": db, "bot": bot, "purchaser": purchaser, "epay": epay})
    register_epay_routes(app, "/callback")
    topup = await db.create_topup(user.id, 2000, "USD")
    try:
        async with TestClient(TestServer(app)) as client:
            params = {
                "pid": "1000",
                "out_trade_no": f"T{topup.id}",
                "trade_no": "TRADE-4DP",
                "money": "20.0000",
                "trade_status": "TRADE_SUCCESS",
                "currency": "USD",
            }
            params["sign"] = _create_sign(params, "audit-secret")
            response = await client.get("/callback", params=params)
            assert response.status == 200
        assert await db.get_balance(user.id, "USD") == 2000
        assert (await db.get_topup(topup.id)).status.value == "paid"
    finally:
        await epay.close()


def test_parse_money_cents_accepts_epay_four_decimal_places():
    """EPay 实际回传最多 4 位小数（20.0000）；此前只收 2 位会误杀真实回调。"""
    assert parse_money_cents("20.0000") == 2000
    assert parse_money_cents("100") == 10000
    assert parse_money_cents("50.50") == 5050
    assert parse_money_cents("0.01") == 1
    # 非法：非数字、5 位小数、负数、空
    assert parse_money_cents("abc") is None
    assert parse_money_cents("1.00001") is None
    assert parse_money_cents("-5") is None
    assert parse_money_cents("") is None
