"""补差价充值：余额不足时在线付差额，到账同一事务内自动用余额付清订单。"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shop_bot.handlers import start
from shop_bot.handlers.order import cb_pay_with_balance, cb_topup_gap
from shop_bot.keyboards import MENU_HISTORY
from shop_bot.models import OrderStatus, Product
from shop_bot.services import orders
from shop_bot.services.epay import _create_sign
from shop_bot.services.fulfillment import recover_once
from shop_bot.services.payment_prompts import HEADER_PENDING, order_prompt
from shop_bot.web.payment import register_epay_routes
from tests.test_payment_prompts import buttons, callback, calls, text_message


@pytest.fixture
async def shop(db):
    product = Product(1, "美国 eSIM", "", 1400, "CNY")
    await db.products.seed_products([product])
    return product


@pytest.fixture
async def http_client(db, epay, purchaser, bot):
    app = web.Application()
    app.update({"db": db, "epay": epay, "purchaser": purchaser, "bot": bot})
    register_epay_routes(app, "/payment/callback")
    async with TestClient(TestServer(app)) as client:
        yield client


async def order_with_balance(db, user, product, balance_cents):
    if balance_cents:
        await db.wallet.adjust_balance(user.id, balance_cents, "seed", "CNY")
    return await orders.create_order(db, user.id, product, 1)


async def ledger(db, user):
    rows = await db.fetch_all(
        """SELECT kind, amount_cents, currency, order_id, topup_id FROM balance_transactions
        WHERE user_id = ? AND kind != 'adjust' ORDER BY id""",
        (user.id,),
    )
    return [dict(row) for row in rows]


def gateway_topup_callback(topup_id, money, trade_no):
    params = {
        "pid": "1000",
        "name": "补差价",
        "out_trade_no": f"T{topup_id}",
        "trade_no": trade_no,
        "money": money,
        "trade_status": "TRADE_SUCCESS",
    }
    params["sign"] = _create_sign(params, "audit-secret")
    return params


@pytest.mark.parametrize(
    ("balance", "options", "gap_button"),
    [
        (0, ["epay"], None),  # 没有余额：补差价等于全额在线付，不重复提供
        (300, ["gap", "epay"], "➕ 补差价 11.00 CNY 并支付"),
        (1350, ["gap", "epay"], "➕ 补差价 1.00 CNY 并支付"),  # 差额低于单笔最低充值额
        (2000, ["bal", "epay"], None),
    ],
)
async def test_prompt_offers_gap_only_when_balance_partly_covers(
    *, db, user, shop, bot, epay, balance, options, gap_button
):
    order = await order_with_balance(db, user, shop, balance)
    _, markup = await order_prompt(bot, db, epay, order, HEADER_PENDING)
    datas = [b.callback_data or "" for b in buttons(markup)]
    assert datas[-1] == "myorders"
    assert [d.split(":")[0] for d in datas[:-1]] == options
    assert [b.text for b in buttons(markup) if (b.callback_data or "").startswith("gap:")] == (
        [gap_button] if gap_button else []
    )


async def test_gap_is_not_offered_without_online_collection(db, user, shop, bot):
    order = await order_with_balance(db, user, shop, 300)
    text, markup = await order_prompt(bot, db, None, order, HEADER_PENDING)
    assert "请联系管理员" in text
    assert [b.callback_data for b in buttons(markup)] == ["myorders"]


async def test_gap_topup_pays_the_order_when_credited(*, db, user, shop, bot, epay, purchaser):
    order = await order_with_balance(db, user, shop, 300)
    await cb_topup_gap(callback(bot, f"gap:{order.id}"), db, epay)
    invoice = calls(bot, "sendMessage")[-1]
    assert "补差价充值单" in invoice.text and "11.00 CNY" in invoice.text and f"订单 #{order.id}" in invoice.text
    assert buttons(invoice.reply_markup)[0].web_app is not None
    topups = await db.fetch_all("SELECT * FROM balance_topups")
    assert [(t["order_id"], t["amount_cents"], t["status"]) for t in topups] == [(order.id, 1100, "pending")]
    await cb_topup_gap(callback(bot, f"gap:{order.id}", message_id=6), db, epay)
    assert len(await db.fetch_all("SELECT * FROM balance_topups")) == 1  # 重复点按钮复用同一张待付单

    topup_id = topups[0]["id"]
    await db.wallet.complete_topup(topup_id, trade_no="GAP-1")
    paid = await db.orders.get_order(order.id)
    assert paid is not None and paid.status == OrderStatus.PAID
    assert paid.payment_method == "balance" and paid.trade_no == f"BAL{order.id}"
    assert await db.wallet.get_balance(user.id, "CNY") == 0
    assert await ledger(db, user) == [
        {"kind": "topup", "amount_cents": 1100, "currency": "CNY", "order_id": order.id, "topup_id": topup_id},
        {"kind": "purchase", "amount_cents": -1400, "currency": "CNY", "order_id": order.id, "topup_id": None},
    ]
    purchase = await db.purchases.get_purchase_by_order(order.id)
    assert purchase is not None and purchase.state.value == "ready"  # 与付款同事务建立采购任务

    await db.wallet.complete_topup(topup_id, trade_no="GAP-1")  # 同一笔回调重放
    assert len(await ledger(db, user)) == 2 and await db.wallet.get_balance(user.id, "CNY") == 0
    await db.wallet.complete_topup(topup_id, trade_no="GAP-2")  # 网关又收了一笔真实款项
    assert await db.wallet.get_balance(user.id, "CNY") == 1100
    assert (await ledger(db, user))[-1]["order_id"] is None  # 第二笔没有付款，不能标记到订单

    await recover_once(db, purchaser, bot)
    texts = [m.text for m in calls(bot, "sendMessage")]
    assert any(
        f"补差价 11.00 CNY 已到账，已自动支付订单 #{order.id}" in t and "付款后余额：0.00 CNY" in t for t in texts
    )
    assert any("未能自动付款" in t and "入账后余额：11.00 CNY" in t for t in texts)
    assert any(f"订单 #{order.id} 已用余额付款" in m.text for m in calls(bot, "editMessageText"))


@pytest.mark.parametrize("change", ["online", "cancelled", "paid"])
async def test_gap_credit_stays_in_wallet_when_order_cannot_be_paid(*, db, user, shop, bot, purchaser, change):
    order = await order_with_balance(db, user, shop, 300)
    topup = await db.wallet.gap_topup(user.id, order.id, 1100, "CNY")
    if change == "online":
        assert await db.payments.reserve_epay(order.id, user.id, "CNY") is not None
    elif change == "cancelled":
        await orders.cancel_order(db, order.id)
    else:
        await db.payments.record_epay_payment(order.id, "ORDER-PAID")
    before = await db.orders.get_order(order.id)
    await db.wallet.complete_topup(topup.id, trade_no="GAP-1")
    assert await db.orders.get_order(order.id) == before
    assert await db.wallet.get_balance(user.id, "CNY") == 1400
    assert [row["order_id"] for row in await ledger(db, user)] == [None]
    await recover_once(db, purchaser, bot)
    texts = [m.text for m in calls(bot, "sendMessage")]
    assert any(f"订单 #{order.id} 未能自动付款" in t and "入账后余额：14.00 CNY" in t for t in texts)


async def test_minimum_gap_leaves_the_rest_in_wallet(db, user, shop):
    order = await order_with_balance(db, user, shop, 1350)
    topup = await db.wallet.gap_topup(user.id, order.id, 100, "CNY")
    await db.wallet.complete_topup(topup.id, trade_no="GAP-MIN")
    paid = await db.orders.get_order(order.id)
    assert paid is not None and paid.status == OrderStatus.PAID
    assert await db.wallet.get_balance(user.id, "CNY") == 50


async def test_gap_credit_never_spends_another_currency(db, user):
    usd = Product(2, "USD eSIM", "", 1400, "USD")
    await db.products.seed_products([usd])
    order = await orders.create_order(db, user.id, usd, 1)
    await db.wallet.adjust_balance(user.id, 2000, "usd funds", "USD")
    topup = await db.wallet.gap_topup(user.id, order.id, 1100, "CNY")  # 币种与订单不一致的异常数据
    await db.wallet.complete_topup(topup.id, trade_no="GAP-CNY")
    current = await db.orders.get_order(order.id)
    assert current is not None and current.status == OrderStatus.PENDING_PAYMENT
    assert await db.wallet.get_balance(user.id, "USD") == 2000
    assert await db.wallet.get_balance(user.id, "CNY") == 1100


async def test_gap_payment_through_gateway_callback(*, http_client, db, user, shop, bot, epay):
    order = await order_with_balance(db, user, shop, 300)
    await cb_topup_gap(callback(bot, f"gap:{order.id}"), db, epay)
    topup_id = (await db.fetch_all("SELECT id FROM balance_topups"))[0]["id"]
    for _ in range(2):  # 网关重复通知
        response = await http_client.post("/payment/callback", data=gateway_topup_callback(topup_id, "11.00", "GW-1"))
        assert response.status == 200 and await response.text() == "success"
    paid = await db.orders.get_order(order.id)
    assert paid is not None and paid.status == OrderStatus.PAID
    assert await db.wallet.get_balance(user.id, "CNY") == 0


async def test_gap_button_guards(*, db, user, shop, bot, epay):
    order = await order_with_balance(db, user, shop, 300)
    await db.users.upsert_user(43, "other")
    await cb_topup_gap(callback(bot, f"gap:{order.id}", user_id=43), db, epay)
    assert calls(bot, "answerCallbackQuery")[-1].text == "订单不存在"
    await cb_topup_gap(callback(bot, f"gap:{order.id}", chat_id=-100, chat_type="supergroup"), db, epay)
    assert "私聊" in calls(bot, "answerCallbackQuery")[-1].text
    await cb_topup_gap(callback(bot, f"gap:{order.id}"), db, None)
    assert "暂未开通" in calls(bot, "answerCallbackQuery")[-1].text

    empty = await db.users.upsert_user(44, "empty")
    empty_order = await orders.create_order(db, empty.id, shop, 1)
    await cb_topup_gap(callback(bot, f"gap:{empty_order.id}", user_id=44), db, epay)
    assert "没有可用余额" in calls(bot, "answerCallbackQuery")[-1].text
    assert await db.fetch_all("SELECT * FROM balance_topups") == []

    await db.wallet.adjust_balance(user.id, 2000, "funding", "CNY")
    await cb_topup_gap(callback(bot, f"gap:{order.id}"), db, epay)
    assert "余额已足够" in calls(bot, "answerCallbackQuery")[-1].text
    assert f"bal:{order.id}" in [b.callback_data for b in buttons(calls(bot, "sendMessage")[-1].reply_markup)]
    current = await db.orders.get_order(order.id)
    assert current is not None and current.status == OrderStatus.PENDING_PAYMENT  # 不替买家直接扣款

    await db.payments.reserve_epay(order.id, user.id, "CNY")
    await cb_topup_gap(callback(bot, f"gap:{order.id}"), db, epay)
    assert "已选择在线支付" in calls(bot, "answerCallbackQuery")[-1].text
    assert await db.fetch_all("SELECT * FROM balance_topups") == []


async def test_short_balance_alert_points_to_gap(*, db, user, shop, bot, epay, purchaser):
    order = await order_with_balance(db, user, shop, 300)
    await cb_pay_with_balance(callback(bot, f"bal:{order.id}"), db, purchaser, bot, epay)
    alert = calls(bot, "answerCallbackQuery")[-1]
    assert "当前 3.00，本单 14.00" in alert.text and "补差价" in alert.text


async def test_history_shows_gap_topup_and_its_payment(db, user, shop, bot):
    order = await order_with_balance(db, user, shop, 300)
    topup = await db.wallet.gap_topup(user.id, order.id, 1100, "CNY")
    await db.wallet.complete_topup(topup.id, trade_no="GAP-1")
    start._menu_last_seen.clear()
    await start.menu_router(text_message(bot, MENU_HISTORY), db, None, None)
    history = calls(bot, "sendMessage")[-1].text
    assert f"补差价充值 订单 #{order.id} · +11.00 CNY" in history
    assert f"余额支付 订单 #{order.id} · -14.00 CNY" in history
