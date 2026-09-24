from decimal import localcontext

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shop_bot.handlers.start import cmd_query
from shop_bot.models import OrderStatus, Product
from shop_bot.services import orders
from shop_bot.services.epay import _create_sign, parse_money_cents
from shop_bot.web.payment import register_epay_routes
from tests.test_payment_flow import callback_params, query_message, query_result


@pytest.mark.parametrize(
    "value,expected",
    [
        ("20.0000", 2000),
        ("9.990", 999),
        ("9.9900", 999),
        ("9.99", 999),
        ("9.9", 990),
        ("100", 10000),
        ("0009.9900", 999),
        ("0.0100", 1),
        ("92233720368547758.0700", 2**63 - 1),
    ],
)
def test_exact_cents_accept_extra_zeroes_without_decimal_context_rounding(value, expected):
    with localcontext() as context:
        context.prec = 2
        assert parse_money_cents(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "9.9851",
        "9.9949",
        "9.995",
        "9.9901",
        "0.0099",
        "0.0101",
        "1.001",
        "20.0001",
        "92233720368547758.08",
        "9" * 1000,
        "1.00000",
        " 20.0000",
        "20.0000 ",
        "1e2",
        "+9.99",
    ],
)
def test_fractional_cents_and_out_of_range_money_are_rejected(value):
    assert parse_money_cents(value) is None


@pytest.mark.parametrize("target", ["order", "topup"])
@pytest.mark.parametrize("money", ["9.9851", "9.9949", "9.9901"])
async def test_signed_fractional_callback_cannot_create_payment_or_credit(
    *, db, user, epay, purchaser, bot, target, money
):
    product = Product(1, "test", "", 999, "CNY")
    await db.products.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    topup = await db.wallet.create_topup(user.id, 999)
    ref = str(order.id) if target == "order" else f"T{topup.id}"
    app = web.Application()
    app.update({"db": db, "epay": epay, "purchaser": purchaser, "bot": bot})
    register_epay_routes(app, "/callback")
    params = callback_params(order, out_trade_no=ref, money=money)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/callback", data=params)
        assert response.status == 422
        assert (await db.orders.get_order(order.id)).status == OrderStatus.PENDING_PAYMENT
        assert (await db.wallet.get_topup(topup.id)).status.value == "pending"
        assert await db.wallet.get_balance(user.id, "CNY") == 0
        assert await db.purchases.get_purchase_by_order(order.id) is None
        assert await db.fetch_all("SELECT * FROM payment_receipts") == []
        # 同一交易重新回传精确金额时可以入账，拒绝路径不留下半笔交易。
        params["money"] = "9.9900"
        params["sign"] = _create_sign(params, "audit-secret")
        response = await client.post("/callback", data=params)
        assert response.status == 200
        if target == "order":
            assert (await db.orders.get_order(order.id)).status == OrderStatus.PAID
            assert await db.wallet.get_balance(user.id, "CNY") == 0
        else:
            assert (await db.wallet.get_topup(topup.id)).status.value == "paid"
            assert await db.wallet.get_balance(user.id, "CNY") == 999


async def test_payment_query_also_rejects_fractional_shortfall(*, db, user, epay, purchaser, bot, httpx_mock):
    product = Product(1, "test", "", 999, "CNY")
    await db.products.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    httpx_mock.add_response(json=query_result(order, money="9.9851"))
    await cmd_query(query_message(bot, order), db, epay, purchaser, bot)
    assert (await db.orders.get_order(order.id)).status == OrderStatus.PENDING_PAYMENT
    assert await db.purchases.get_purchase_by_order(order.id) is None
    assert await db.fetch_all("SELECT * FROM payment_receipts") == []
