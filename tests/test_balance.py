"""钱包体系测试：充值入账幂等、回调核验、余额支付原子性、流水。"""

import asyncio

import pytest
from aiogram.types import Message
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shop_bot.db import Database
from shop_bot.handlers.balance import render_balance
from shop_bot.models import Product
from shop_bot.services import orders
from shop_bot.services.balance import format_cents, parse_topup_amount
from shop_bot.services.epay import _create_sign
from shop_bot.services.fulfillment import recover_once
from shop_bot.web.payment import register_epay_routes


@pytest.fixture
async def http_client(db, epay, purchaser, bot):
    app = web.Application()
    app.update({"db": db, "epay": epay, "purchaser": purchaser, "bot": bot})
    register_epay_routes(app, "/payment/callback")
    async with TestClient(TestServer(app)) as client:
        yield client


class FakeCommbitz:
    async def create_request(self, **kwargs):
        return {"_id": "up-fake", "status": "pending"}

    async def get_order_details(self, request_id):
        return {"status": "Success", "requestType": "esim", "quantity": 1,
                "esims": [{"iccid": "89", "lpa": "LPA:1", "qrCode": "https://q.png"}]}

    async def submit_kyc_documents_json(self, request_id, documents):
        return {}

    async def submit_kyc_documents_files(self, request_id, files):
        return {}

    async def get_esim_usage(self, **kwargs):
        return {}


def test_parse_topup_amount():
    assert parse_topup_amount("100") == 100_00
    assert parse_topup_amount("50.50") == 5050
    assert parse_topup_amount(" 1 ") == 100
    assert parse_topup_amount("0.5") is None  # 低于最低 1 元
    assert parse_topup_amount("10001") is None  # 超上限
    assert parse_topup_amount("1.234") is None  # 三位小数
    assert parse_topup_amount("-5") is None
    assert parse_topup_amount("abc") is None
    assert parse_topup_amount("") is None


def test_format_cents():
    assert format_cents(0) == "0.00"
    assert format_cents(13986) == "139.86"


async def test_topup_credit_is_idempotent(db, user):
    """充值到账只入账一次：重复回调（同/不同交易号）不重复加钱。"""
    topup = await db.create_topup(user.id, 100_00)
    first = await db.complete_topup(topup.id, trade_no="TX-1")
    assert first is not None and first.status.value == "paid"
    # 网关重试：再次回调 complete_topup 返回已有记录，不重复入账
    again = await db.complete_topup(topup.id, trade_no="TX-1")
    assert again is not None and again.status.value == "paid"
    user_after = await db.get_user(user.id)
    assert user_after is not None and user_after.balance_cents == 100_00
    txs = await db.list_balance_transactions(user.id)
    assert len(txs) == 1


async def test_pay_order_with_balance_success_writes_ledger(db, user):
    product = Product(1, "p", "", 100, "CNY", sku="S", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    # 先充值 10 元
    topup = await db.create_topup(user.id, 1000_00)
    await db.complete_topup(topup.id, trade_no="TX-0")

    paid, err = await db.pay_order_with_balance(order.id, user.id, order.amount_cents)
    assert err is None and paid is not None and paid.status.value == "paid"
    user_after = await db.get_user(user.id)
    assert user_after is not None
    assert user_after.balance_cents == 1000_00 - order.amount_cents
    txs = await db.list_balance_transactions(user.id)
    assert [t.kind for t in txs] == ["purchase", "topup"]
    assert txs[0].amount_cents == -order.amount_cents
    assert txs[0].balance_after == user_after.balance_cents


async def test_pay_order_with_balance_insufficient(db, user):
    product = Product(1, "p", "", 100, "CNY", sku="S", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)  # 余额 0
    paid, err = await db.pay_order_with_balance(order.id, user.id, order.amount_cents)
    assert paid is None and err == "insufficient"
    assert (await db.get_order(order.id)).status.value == "pending_payment"


async def test_concurrent_balance_pay_only_one_succeeds(db, user):
    """余额只够一单：并发支付两单只有一单成功，绝不透支。"""
    product = Product(1, "p", "", 100, "CNY", sku="S", request_type="esim")
    await db.seed_products([product])
    topup = await db.create_topup(user.id, 100)  # 余额 1 元，只够一单
    await db.complete_topup(topup.id, trade_no="TX")
    o1 = await orders.create_order(db, user.id, product, 1)
    o2 = await orders.create_order(db, user.id, product, 1)
    results = await asyncio.gather(
        db.pay_order_with_balance(o1.id, user.id, o1.amount_cents),
        db.pay_order_with_balance(o2.id, user.id, o2.amount_cents),
    )
    successes = [r for r, e in results if r is not None and e is None]
    assert len(successes) == 1
    user_after = await db.get_user(user.id)
    assert user_after is not None and user_after.balance_cents == 0


def _topup_callback_params(topup_id, amount="10.00", **changes):
    params = {
        "pid": "1000",
        "name": "余额充值",
        "out_trade_no": f"T{topup_id}",
        "trade_no": f"TT{topup_id}",
        "money": amount,
        "trade_status": "TRADE_SUCCESS",
    }
    params.update(changes)
    params["sign"] = _create_sign(params, "audit-secret")
    return params


async def test_topup_callback_credits_once(
    *, http_client, db, user, epay
):
    """T 前缀回调走充值分支：入账幂等，重复回调返回 success。"""
    topup = await db.create_topup(user.id, 1000_00)
    for _ in range(2):
        params = _topup_callback_params(topup.id, amount="1000.00")
        params["sign"] = _create_sign(params, "audit-secret")
        response = await http_client.post("/payment/callback", data=params)
        assert response.status == 200 and (await response.text()) == "success"
    user_after = await db.get_user(user.id)
    assert user_after is not None and user_after.balance_cents == 1000_00
    txs = await db.list_balance_transactions(user.id)
    assert len(txs) == 1 and txs[0].kind == "topup" and txs[0].amount_cents == 1000_00


async def test_topup_callback_rejects_amount_mismatch(*, http_client, db, user, epay):
    topup = await db.create_topup(user.id, 1000_00)
    params = _topup_callback_params(topup.id, amount="1.00")
    response = await http_client.post("/payment/callback", data=params)
    assert response.status == 422
    user_after = await db.get_user(user.id)
    assert user_after is not None and user_after.balance_cents == 0


async def test_order_callback_ignores_topup_namespace(*, http_client, db, user, epay):
    """纯数字回调不受 T 前缀影响；T 前缀不会命中商品订单。"""
    product = Product(1, "p", "", 100, "CNY")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    # 充值回调携带不存在的充值单 → 404，不影响商品订单
    topup = await db.create_topup(user.id, 100_00)
    params = _topup_callback_params(topup.id, amount="999.00")
    params["sign"] = _create_sign(params, "audit-secret")
    response = await http_client.post("/payment/callback", data=params)
    assert response.status == 422
    assert (await db.get_order(order.id)).status.value == "pending_payment"


async def test_menu_balance_shows_balance(db, user, bot):
    topup = await db.create_topup(user.id, 500_00)
    await db.complete_topup(topup.id, trade_no="TX")
    await render_balance(menu_message_of(bot), db)
    texts = [m.text or "" for m in bot.session.sent]
    assert any("500.00 CNY" in t and "充值" in t for t in texts)


def menu_message_of(bot):
    return Message.model_validate(
        {"message_id": 1, "date": 0, "chat": {"id": 42, "type": "private"},
         "from": {"id": 42, "is_bot": False, "first_name": "T"}, "text": "x"},
        context={"bot": bot},
    )




# ---- 余额支付回调（用户入口）----

import sqlite3  # noqa: E402

from aiogram.types import CallbackQuery  # noqa: E402

from shop_bot.handlers.order import cb_pay_with_balance  # noqa: E402


def _balance_callback(bot, order_id):
    return CallbackQuery.model_validate(
        {
            "id": "9",
            "from_user": {"id": 42, "is_bot": False, "first_name": "T"},
            "chat_instance": "test",
            "data": f"bal:{order_id}",
            "message": {"message_id": 5, "date": 0, "chat": {"id": 42, "type": "private"}, "text": "pay"},
        },
        context={"bot": bot},
    )


async def test_cb_pay_with_balance_success(db, user, purchaser, bot):
    product = Product(1, "p", "", 100, "CNY", sku="S", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    topup = await db.create_topup(user.id, 1000_00)
    await db.complete_topup(topup.id, trade_no="TX")

    await cb_pay_with_balance(_balance_callback(bot, order.id), db, purchaser, bot)

    # DemoPurchaser 立即履约 → 直接 delivered
    order = await db.get_order(order.id)
    assert order is not None and order.status.value == "delivered"
    purchase = await db.get_purchase_by_order(order.id)
    assert purchase is not None and purchase.state.value == "fulfilled"
    user_after = await db.get_user(user.id)
    assert user_after is not None and user_after.balance_cents == 1000_00 - order.amount_cents
    # 恢复循环完成履约并私信
    await recover_once(db, purchaser, bot)
    order = await db.get_order(order.id)
    assert order is not None and order.status.value == "delivered" and order.payload
    assert await db.get_purchase_by_order(order.id) is not None


async def test_cb_pay_with_balance_insufficient_alerts(db, user, purchaser, bot):
    product = Product(1, "p", "", 100, "CNY", sku="S", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)  # 无余额
    await cb_pay_with_balance(_balance_callback(bot, order.id), db, purchaser, bot)
    order = await db.get_order(order.id)
    assert order is not None and order.status.value == "pending_payment"
    assert await db.get_purchase_by_order(order.id) is None


async def test_migration_adds_user_balance_column(tmp_path):
    """旧库 users 表迁移后补 balance_cents。"""
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
        """)
    db = Database(path)
    await db.connect()
    try:
        user = await db.upsert_user(7, "legacy")
        assert user.balance_cents == 0
    finally:
        await db.close()
