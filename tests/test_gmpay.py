"""epusdt GMPay：签名、下单与复用、回调入账、到期核对、我已转账、/query 核对与启动接线。"""

import json
import time
from urllib.parse import parse_qsl

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shop_bot import build_gmpay_gateway
from shop_bot.config import GMPaySettings, Settings, WebhookSettings
from shop_bot.handlers import start
from shop_bot.handlers.gmpay import cb_check_transfer, tx_hash_input
from shop_bot.handlers.order import cb_pay_online
from shop_bot.models import OrderStatus
from shop_bot.services import orders
from shop_bot.services.epay import _create_sign
from shop_bot.services.fulfillment import recover_once
from shop_bot.services.gmpay import GMPayError, canonical_value, sign
from shop_bot.services.payment_prompts import HEADER_PENDING, order_prompt
from shop_bot.web.payment import register_payment_routes
from tests.conftest import GM_URL, SECRET
from tests.test_payment_prompts import buttons, callback, calls, fsm, prompts, text_message

CREATE_URL = f"{GM_URL}/payments/gmpay/v1/order/create-transaction"


@pytest.fixture
async def http_client(db, epay, gmpay, purchaser, bot):
    app = web.Application()
    app.update({"db": db, "epay": epay, "gmpay": gmpay, "purchaser": purchaser, "bot": bot})
    register_payment_routes(app, "/payment/callback")
    async with TestClient(TestServer(app)) as client:
        yield client


def mock_create(httpx_mock, order_no, *, trade_id="T100", amount=9.99, **data):
    body = {
        "trade_id": trade_id,
        "order_id": order_no,
        "amount": amount,
        "currency": "CNY",
        "actual_amount": 1.41,
        "receive_address": "TAddr1",
        "token": "USDT",
        "status": 1,
        "expiration_time": int(time.time()) + 600,
        "payment_url": f"{GM_URL}/pay/checkout-counter/{trade_id}",
        **data,
    }
    httpx_mock.add_response(
        method="POST", url=CREATE_URL, json={"status_code": 200, "message": "success", "data": body}
    )


def mock_status(httpx_mock, trade_id, status):
    httpx_mock.add_response(
        method="GET",
        url=f"{GM_URL}/pay/check-status/{trade_id}",
        json={"status_code": 200, "message": "success", "data": {"trade_id": trade_id, "status": status}},
    )


def callback_payload(order_no, *, trade_id="T100", amount=9.99, **changes):
    payload = {
        "pid": "1000",
        "trade_id": trade_id,
        "order_id": order_no,
        "amount": amount,
        "actual_amount": 1.41,
        "receive_address": "TAddr1",
        "token": "USDT",
        "block_transaction_id": "ab" * 32,
        "status": 2,
        **changes,
    }
    payload["signature"] = sign(payload, SECRET)
    return payload


async def locked_order(db, user, product):
    order = await orders.create_order(db, user.id, product, 1)
    locked = await db.payments.reserve_epay(order.id, user.id, "CNY")
    assert locked is not None
    return locked


def test_signature_matches_epusdt_reference_vectors():
    documented = {
        "pid": "1000",
        "order_id": "ORD202605230001",
        "currency": "cny",
        "token": "usdt",
        "network": "tron",
        "amount": 100,
        "notify_url": "https://merchant.example/notify",
        "redirect_url": "https://merchant.example/return",
        "name": "VIP",
    }
    assert sign(documented, "epusdt_secret_key") == "6f874b1919d95081835e2809b620e354a5866f5a6dbb2e432d1627f1eb10059d"
    go_vector = {"signature": "ignored", "pid": "1000", "empty": "", "nil": None, "name": "VIP", "amount": 100}
    assert sign(go_vector, "test-secret") == "ced9141fab53a83d1178f903e7c22d8a2a7033520f31e319934e92455a008b6c"
    # JSON 数字按 Go float64 的最短表示参与签名，字符串保持原文
    assert [canonical_value(v) for v in (100.0, 14.01, 1e-05, 7, "14.00")] == ["100", "14.01", "0.00001", "7", "14.00"]


async def test_checkout_creates_signed_trade_and_reuses_it(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    checkout = await gmpay.order_checkout(bot, db, order)
    form = dict(parse_qsl(httpx_mock.get_request().content.decode()))
    assert (form["order_id"], form["amount"], form["currency"], form["token"], form["network"]) == (
        f"{order.id}-1",
        "9.99",
        "cny",
        "usdt",
        "tron",
    )
    assert form["notify_url"] == "https://bot.example.com/payment/callback"
    assert form["redirect_url"] == "https://t.me/audit_bot"
    assert form["signature"] == sign({k: v for k, v in form.items() if k != "signature"}, SECRET)
    assert (checkout.address, checkout.amount, checkout.token, checkout.network) == ("TAddr1", "1.41", "USDT", "tron")

    trade = await db.gmpay.get(checkout.trade_ref)
    assert trade is not None and trade["state"] == "pending" and trade["trade_id"] == "T100"
    work = await db.fetch_all("SELECT due_at FROM work_items WHERE kind = 'gmpay' AND entity_id = ?", (trade["id"],))
    assert [row["due_at"] for row in work] == [trade["expires_at"] + 20]  # 到期后核对
    assert await gmpay.order_checkout(bot, db, order) == checkout  # 有效期内复用，不再请求 epusdt
    assert len(httpx_mock.get_requests()) == 1


async def test_failed_or_mismatched_creation_is_never_reused(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    httpx_mock.add_response(
        method="POST",
        url=CREATE_URL,
        status_code=400,
        json={"status_code": 10003, "message": "no wallet", "data": None},
    )
    with pytest.raises(GMPayError) as rejected:
        await gmpay.order_checkout(bot, db, order)
    assert rejected.value.code == 10003
    mock_create(httpx_mock, f"{order.id}-2", amount=10)  # 金额与请求不符：不展示给买家
    with pytest.raises(GMPayError, match="amount"):
        await gmpay.order_checkout(bot, db, order)
    mock_create(httpx_mock, f"{order.id}-3")
    checkout = await gmpay.order_checkout(bot, db, order)
    rows = await db.fetch_all("SELECT merchant_order_no, state FROM gmpay_trades ORDER BY id")
    assert [tuple(row) for row in rows] == [
        (f"{order.id}-1", "failed"),
        (f"{order.id}-2", "failed"),
        (f"{order.id}-3", "pending"),
    ]
    assert checkout.address == "TAddr1"


async def test_locked_prompt_shows_chain_details_and_cashier_fallback(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    view = await order_prompt(bot, db, gmpay, order, HEADER_PENDING)
    assert "TRON（TRC20）" in view.text and "`1.41` USDT" in view.text and "`TAddr1`" in view.text
    assert "分钟内转账" in view.text and view.trade_ref is not None
    rows = buttons(view.markup)
    assert rows[0].callback_data == f"gmchk:{view.trade_ref}"
    assert rows[1].web_app is not None and rows[1].web_app.url.endswith("/pay/checkout-counter/T100")
    assert rows[2].callback_data == "myorders"


async def test_http_cashier_url_falls_back_to_plain_link(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1", payment_url="http://pay.local/pay/checkout-counter/T100")
    view = await order_prompt(bot, db, gmpay, order, HEADER_PENDING)
    cashier = buttons(view.markup)[1]
    assert cashier.web_app is None and cashier.url == "http://pay.local/pay/checkout-counter/T100"


async def test_prompt_offers_retry_when_gateway_is_down(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    httpx_mock.add_response(method="POST", url=CREATE_URL, status_code=502, text="bad gateway")
    view = await order_prompt(bot, db, gmpay, order, HEADER_PENDING)
    assert "暂时无法生成付款信息" in view.text and view.trade_ref is None
    assert [b.callback_data for b in buttons(view.markup)] == [f"epay:{order.id}", "myorders"]


async def test_callback_pays_order_once(*, http_client, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    await gmpay.order_checkout(bot, db, order)
    for _ in range(2):  # epusdt 重复通知
        response = await http_client.post("/payment/callback", json=callback_payload(f"{order.id}-1"))
        assert response.status == 200 and await response.text() == "success"
    paid = await db.orders.get_order(order.id)
    assert paid is not None and paid.status == OrderStatus.PAID
    assert paid.trade_no == "T100" and paid.payment_method == "epay"
    receipts = await db.fetch_all("SELECT trade_no, disposition FROM payment_receipts")
    assert [tuple(row) for row in receipts] == [("T100", "order")]
    trade = await db.gmpay.by_order_no(f"{order.id}-1")
    assert trade is not None and trade["state"] == "paid"
    assert await db.purchases.get_purchase_by_order(order.id) is not None


@pytest.mark.parametrize(
    ("changes", "expected_status"),
    [
        ({"signature": "0" * 64}, 401),
        ({"pid": "2000"}, 401),
        ({"amount": 1}, 422),
        ({"trade_id": "OTHER"}, 422),
        ({"status": 1}, 422),
        ({"order_id": "999-1"}, 404),
    ],
)
async def test_callback_rejections_change_nothing(
    *, http_client, db, user, product, bot, gmpay, httpx_mock, changes, expected_status
):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    await gmpay.order_checkout(bot, db, order)
    signature = changes.pop("signature", None)
    payload = callback_payload(f"{order.id}-1", **changes)
    if signature is not None:
        payload["signature"] = signature
    response = await http_client.post("/payment/callback", json=payload)
    assert response.status == expected_status
    current = await db.orders.get_order(order.id)
    assert current is not None and current.status == OrderStatus.PENDING_PAYMENT
    assert await db.fetch_all("SELECT * FROM payment_receipts") == []


async def test_epay_callbacks_keep_working_after_the_switch(*, http_client, db, user, product):
    """切换前生成的 EPay 链接仍按原协议回调入账。"""
    order = await orders.create_order(db, user.id, product, 1)
    params = {
        "pid": "1000",
        "out_trade_no": str(order.id),
        "trade_no": "EP-1",
        "money": "9.99",
        "trade_status": "TRADE_SUCCESS",
    }
    params["sign"] = _create_sign(params, "audit-secret")
    response = await http_client.get("/payment/callback", params=params)
    assert response.status == 200
    paid = await db.orders.get_order(order.id)
    assert paid is not None and paid.status == OrderStatus.PAID and paid.trade_no == "EP-1"


async def test_gap_topup_through_gmpay_pays_the_order(*, http_client, db, user, product, bot, gmpay, httpx_mock):
    await db.wallet.adjust_balance(user.id, 300, "seed", "CNY")
    order = await orders.create_order(db, user.id, product, 1)
    topup = await db.wallet.gap_topup(user.id, order.id, 699, "CNY")
    mock_create(httpx_mock, f"T{topup.id}-1", trade_id="T200", amount=6.99)
    checkout = await gmpay.topup_checkout(bot, db, topup, f"订单 #{order.id} 补差价")
    assert checkout.trade_ref is not None
    payload = callback_payload(f"T{topup.id}-1", trade_id="T200", amount=6.99)
    response = await http_client.post("/payment/callback", json=payload)
    assert response.status == 200
    paid = await db.orders.get_order(order.id)
    assert paid is not None and paid.status == OrderStatus.PAID and paid.payment_method == "balance"
    assert await db.wallet.get_balance(user.id, "CNY") == 0


async def test_expired_trade_removes_address_and_offers_refetch(
    *, db, user, product, bot, gmpay, httpx_mock, queue_clock, purchaser
):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    checkout = await gmpay.order_checkout(bot, db, order)
    await db.prompts.record(42, 5, order_id=order.id, trade_ref=checkout.trade_ref)
    mock_status(httpx_mock, "T100", 3)
    queue_clock(700)
    await recover_once(db, purchaser, bot, gateway=gmpay)
    edit = calls(bot, "editMessageText")[-1]
    assert edit.message_id == 5 and "已过期" in edit.text and "TAddr1" not in edit.text
    assert [b.callback_data for b in buttons(edit.reply_markup)] == [f"epay:{order.id}", "myorders"]
    trade = await db.gmpay.get(checkout.trade_ref)
    assert trade is not None and trade["state"] == "expired"
    assert [(p["state"], p["trade_ref"]) for p in await prompts(db)] == [("open", None)]

    # 在过期提示上点「重新获取」：同一条消息换成下一笔收款信息
    mock_create(httpx_mock, f"{order.id}-2", trade_id="T101", receive_address="TAddr2")
    await cb_pay_online(callback(bot, f"epay:{order.id}"), db, gmpay)
    refreshed = calls(bot, "editMessageText")[-1]
    assert refreshed.message_id == 5 and "TAddr2" in refreshed.text
    newer = await db.gmpay.by_order_no(f"{order.id}-2")
    assert newer is not None and [p["trade_ref"] for p in await prompts(db)] == [newer["id"]]


async def test_expiry_check_recovers_a_lost_callback(
    *, db, user, product, bot, gmpay, httpx_mock, queue_clock, purchaser
):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    await gmpay.order_checkout(bot, db, order)
    mock_status(httpx_mock, "T100", 2)
    queue_clock(700)
    await recover_once(db, purchaser, bot, gateway=gmpay)
    paid = await db.orders.get_order(order.id)
    assert paid is not None and paid.status in (OrderStatus.PAID, OrderStatus.DELIVERED) and paid.trade_no == "T100"


async def test_check_transfer_then_tx_hash_verification(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    checkout = await gmpay.order_checkout(bot, db, order)
    context = fsm(db)

    await db.users.upsert_user(43, "other")
    await cb_check_transfer(callback(bot, f"gmchk:{checkout.trade_ref}", user_id=43), db, gmpay, context)
    assert calls(bot, "answerCallbackQuery")[-1].text == "付款信息不存在"  # 只能查自己的付款

    mock_status(httpx_mock, "T100", 1)
    await cb_check_transfer(callback(bot, f"gmchk:{checkout.trade_ref}"), db, gmpay, context)
    assert await context.get_state() == "TxHashFlow:hash"
    assert "交易哈希" in calls(bot, "sendMessage")[-1].text

    await tx_hash_input(text_message(bot, "not-a-hash"), db, gmpay, context)
    assert "不是有效的交易哈希" in calls(bot, "sendMessage")[-1].text
    assert await context.get_state() == "TxHashFlow:hash"

    tx_hash = "ab" * 32
    httpx_mock.add_response(
        method="POST",
        url=f"{GM_URL}/pay/submit-tx-hash/T100",
        json={"status_code": 200, "message": "success", "data": {"trade_id": "T100", "status": 2}},
    )
    await tx_hash_input(text_message(bot, tx_hash), db, gmpay, context)
    submitted = httpx_mock.get_requests(method="POST", url=f"{GM_URL}/pay/submit-tx-hash/T100")
    assert [json.loads(r.content) for r in submitted] == [{"block_transaction_id": tx_hash}]
    assert await context.get_state() is None and "已确认到账" in calls(bot, "sendMessage")[-1].text
    paid = await db.orders.get_order(order.id)
    assert paid is not None and paid.status == OrderStatus.PAID


async def test_rejected_tx_hash_explains_why(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    checkout = await gmpay.order_checkout(bot, db, order)
    context = fsm(db)
    await context.set_state("TxHashFlow:hash")
    await context.update_data(trade_ref=checkout.trade_ref)
    httpx_mock.add_response(
        method="POST",
        url=f"{GM_URL}/pay/submit-tx-hash/T100",
        status_code=400,
        json={"status_code": 10038, "message": "verify failed", "data": None},
    )
    await tx_hash_input(text_message(bot, "cd" * 32), db, gmpay, context)
    assert "未能核验这笔转账" in calls(bot, "sendMessage")[-1].text
    current = await db.orders.get_order(order.id)
    assert current is not None and current.status == OrderStatus.PENDING_PAYMENT


async def test_query_reconciles_through_gmpay(*, db, user, product, bot, gmpay, purchaser, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    await gmpay.order_checkout(bot, db, order)
    mock_status(httpx_mock, "T100", 1)
    await start.cmd_query(text_message(bot, f"/query {order.id}"), db, gmpay, purchaser, bot)
    reply = calls(bot, "sendMessage")[-1]
    assert "订单尚未支付" in reply.text and "TAddr1" in reply.text  # 复用同一笔收款信息
    assert len(httpx_mock.get_requests(method="POST", url=CREATE_URL)) == 1

    mock_status(httpx_mock, "T100", 2)
    await start.cmd_query(text_message(bot, f"/query {order.id}"), db, gmpay, purchaser, bot)
    assert "已支付" in calls(bot, "sendMessage")[-1].text
    paid = await db.orders.get_order(order.id)
    assert paid is not None and paid.status == OrderStatus.PAID


async def test_startup_builds_gmpay_gateway_only_when_configured():
    webhook = WebhookSettings(url="https://bot.example.com/")
    assert build_gmpay_gateway(Settings(webhook=webhook)) is None
    settings = Settings(
        webhook=webhook,
        gmpay=GMPaySettings(url=GM_URL, pid="1000", secret_key=SECRET, currency="usd", token=" USDT ", network="TRON"),
    )
    gateway = build_gmpay_gateway(settings)
    assert gateway is not None
    try:
        assert gateway.notify_url == "https://bot.example.com/payment/callback"
        assert (gateway.currency, gateway.client.config.token, gateway.client.config.network) == ("USD", "usdt", "tron")
    finally:
        await gateway.client.close()
