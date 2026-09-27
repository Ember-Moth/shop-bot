"""付款体验：付款后旧按钮更新、继续支付入口、余额不足引导、中文状态和资金记录。"""

import sqlite3
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Message

from shop_bot.db import Database, FSMStorage
from shop_bot.handlers import start
from shop_bot.handlers.balance import cb_topup_preset, cb_wallet_view, topup_amount_non_text
from shop_bot.handlers.order import (
    cb_confirm,
    cb_pay_online,
    cb_pay_with_balance,
    cb_resume_payment,
    product_quote,
)
from shop_bot.keyboards import MENU_HISTORY, MENU_ORDERS
from shop_bot.models import OrderStatus, Product
from shop_bot.services import orders
from shop_bot.services.fulfillment import recover_once

BOT_ID = 123456  # FakeSession 的 getMe 返回的 Bot ID


def callback(bot, data, *, message_id=5, chat_id=42, chat_type="private", user_id=42):
    """按钮所在消息由 Bot 发出：message.from_user 是 Bot，点击者在 callback.from_user。"""
    return CallbackQuery.model_validate(
        {
            "id": "cb",
            "from_user": {"id": user_id, "is_bot": False, "first_name": "Buyer"},
            "chat_instance": "test",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": chat_id, "type": chat_type},
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Audit"},
                "text": "prompt",
            },
        },
        context={"bot": bot},
    )


def text_message(bot, text, *, user_id=42, chat_id=42):
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Buyer"},
            "text": text,
        },
        context={"bot": bot},
    )


def fsm(db):
    return FSMContext(storage=FSMStorage(db), key=StorageKey(bot_id=1, chat_id=42, user_id=42))


def calls(bot, api):
    return [m for m in bot.session.sent if m.__api_method__ == api]


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


async def confirm_order(db, bot, epay, product):
    context = fsm(db)
    await context.set_data({"product_id": product.id, "quantity": 1, "product_quote": product_quote(product)})
    await cb_confirm(callback(bot, "order:confirm"), context, db, epay)
    return (await db.orders.list_orders())[0]


async def prompts(db):
    return [dict(row) for row in await db.fetch_all("SELECT * FROM payment_prompts ORDER BY id")]


async def prompt_work(db):
    return await db.fetch_all("SELECT * FROM work_items WHERE kind = 'prompt'")


def test_every_order_status_has_a_chinese_label():
    assert {status.label for status in OrderStatus} == {
        "待支付",
        "已付款",
        "已交付",
        "交付失败",
        "已取消",
        "已退款",
        "已超时关闭",
    }


async def test_confirm_prompt_shows_balance_and_only_usable_options(db, user, product, bot, epay):
    order = await confirm_order(db, bot, epay, product)
    prompt = calls(bot, "editMessageText")[-1]
    assert "当前余额：0.00 CNY，还差 9.99 CNY" in prompt.text
    # 没有余额：余额支付和补差价都用不上，只给在线支付
    assert [b.callback_data for b in buttons(prompt.reply_markup)] == [f"epay:{order.id}", "myorders"]
    assert all(b.web_app is None for b in buttons(prompt.reply_markup))  # 选渠道前不生成收银台链接
    assert [(p["chat_id"], p["message_id"], p["order_id"], p["state"]) for p in await prompts(db)] == [
        (42, 5, order.id, "open")
    ]


async def test_balance_payment_explains_shortfall_then_updates_prompt(*, db, user, product, bot, epay, purchaser):
    """旧消息或伪造的余额支付回调：按点击时的余额校验，付款成功后原地更新提示。"""
    order = await confirm_order(db, bot, epay, product)
    await cb_pay_with_balance(callback(bot, f"bal:{order.id}"), db, purchaser, bot)
    alert = calls(bot, "answerCallbackQuery")[-1]
    assert alert.show_alert and "余额不足" in alert.text and "当前 0.00" in alert.text and "本单 9.99" in alert.text
    assert (await db.orders.get_order(order.id)).status == OrderStatus.PENDING_PAYMENT

    topup = await db.wallet.create_topup(user.id, 1000, "CNY")
    await db.wallet.complete_topup(topup.id, trade_no="TOPUP-1")
    await cb_pay_with_balance(callback(bot, f"bal:{order.id}"), db, purchaser, bot)
    assert (await db.orders.get_order(order.id)).status == OrderStatus.PAID
    updated = calls(bot, "editMessageText")[-1]
    assert updated.message_id == 5 and "已付款" in updated.text
    assert [b.callback_data for b in buttons(updated.reply_markup)] == ["myorders"]

    await recover_once(db, purchaser, bot)
    assert [p["state"] for p in await prompts(db)] == ["closed"]
    assert await prompt_work(db) == []


async def test_online_payment_removes_pay_buttons_from_every_prompt(*, db, user, product, bot, epay, purchaser):
    order = await confirm_order(db, bot, epay, product)
    await cb_pay_online(callback(bot, f"epay:{order.id}"), db, epay)
    # 另一处打开的继续支付提示：已锁定在线渠道，只给收银台
    await cb_resume_payment(callback(bot, f"resume:{order.id}", message_id=6), db, epay)
    resumed = calls(bot, "sendMessage")[-1]
    assert "已选择在线支付" in resumed.text
    assert [b.web_app is not None for b in buttons(resumed.reply_markup)] == [True, False]
    assert all(not (b.callback_data or "").startswith("bal:") for b in buttons(resumed.reply_markup))
    assert sorted(p["message_id"] for p in await prompts(db)) == [5, 99]

    await db.payments.record_online_payment(order.id, "TRADE-1")
    assert len(await prompt_work(db)) == 2  # 与付款同事务入队
    before = len(bot.session.sent)
    await recover_once(db, purchaser, bot)
    edits = [m for m in bot.session.sent[before:] if m.__api_method__ == "editMessageText"]
    assert sorted(m.message_id for m in edits) == [5, 99]
    assert all("已付款" in m.text and all(b.web_app is None for b in buttons(m.reply_markup)) for m in edits)
    assert [p["state"] for p in await prompts(db)] == ["closed", "closed"]


async def test_prompt_recorded_after_payment_is_closed_right_away(db, user, product, bot, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    await db.payments.record_online_payment(order.id, "TRADE-EARLY")
    await db.prompts.record(42, 77, order_id=order.id)
    assert len(await prompt_work(db)) == 1
    await recover_once(db, purchaser, bot)
    assert [m.message_id for m in calls(bot, "editMessageText")] == [77]


async def test_cancelled_order_prompt_says_cancelled(*, db, user, product, bot, epay, purchaser):
    order = await confirm_order(db, bot, epay, product)
    await orders.cancel_order(db, order.id)
    await recover_once(db, purchaser, bot)
    edit = calls(bot, "editMessageText")[-1]
    assert edit.message_id == 5 and "已取消" in edit.text


async def test_unchanged_or_deleted_prompt_is_not_retried(db, user, product, bot, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    await db.prompts.record(42, 5, order_id=order.id)
    bot.session.edit_failure = lambda method: TelegramBadRequest(method, "Bad Request: message to edit not found")
    await db.payments.record_online_payment(order.id, "TRADE-1")
    await recover_once(db, purchaser, bot)
    assert [p["state"] for p in await prompts(db)] == ["closed"]
    assert await prompt_work(db) == []


async def test_rate_limited_prompt_update_is_retried(*, db, user, product, bot, purchaser, queue_clock):
    order = await orders.create_order(db, user.id, product, 1)
    await db.prompts.record(42, 5, order_id=order.id)
    bot.session.edit_failure = lambda method: TelegramRetryAfter(method, "Too Many Requests", 5)
    await db.payments.record_online_payment(order.id, "TRADE-1")
    await recover_once(db, purchaser, bot)
    assert [p["state"] for p in await prompts(db)] == ["open"]
    assert len(await prompt_work(db)) == 1
    bot.session.edit_failure = None
    queue_clock()
    await recover_once(db, purchaser, bot)
    assert [p["state"] for p in await prompts(db)] == ["closed"]


async def test_restart_requeues_settled_prompts_still_open(tmp_path):
    path = str(tmp_path / "prompts.db")
    db = Database(path)
    await db.connect()
    try:
        user = await db.users.upsert_user(42, "buyer")
        product = Product(1, "demo", "", 999, "CNY")
        await db.products.seed_products([product])
        order = await orders.create_order(db, user.id, product, 1)
        await db.prompts.record(42, 5, order_id=order.id)
        await db.payments.record_online_payment(order.id, "TRADE-1")
    finally:
        await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM work_items WHERE kind = 'prompt'")
    restarted = Database(path)
    await restarted.connect()
    try:
        assert len(await prompt_work(restarted)) == 1
    finally:
        await restarted.close()


async def test_topup_invoice_points_to_wallet_and_updates_on_credit(db, user, bot, epay, purchaser):
    await cb_topup_preset(callback(bot, "topup:1000"), db, epay, fsm(db))
    invoice = calls(bot, "editMessageText")[-1]
    invoice_buttons = buttons(invoice.reply_markup)
    assert invoice_buttons[0].web_app is not None
    assert [b.callback_data for b in invoice_buttons[1:]] == ["wallet:view"]
    topup_id = (await prompts(db))[0]["topup_id"]

    await db.wallet.complete_topup(topup_id, trade_no="TOPUP-1")
    await recover_once(db, purchaser, bot)
    edit = calls(bot, "editMessageText")[-1]
    assert f"充值单 {topup_id} 已到账" in edit.text and "10.00 CNY" in edit.text
    assert [b.callback_data for b in buttons(edit.reply_markup)] == ["wallet:view"]

    await cb_wallet_view(callback(bot, "wallet:view"), db)
    assert "我的余额" in calls(bot, "sendMessage")[-1].text and "10.00 CNY" in calls(bot, "sendMessage")[-1].text


async def test_my_orders_shows_chinese_status_and_resume_buttons(db, user, product, bot):
    pending = await orders.create_order(db, user.id, product, 1)
    delivered = await orders.create_order(db, user.id, product, 1)
    await db.orders.transition_order(delivered.id, OrderStatus.DELIVERED)
    start._menu_last_seen.clear()
    await start.menu_router(text_message(bot, MENU_ORDERS), db, None, None)
    listing = calls(bot, "sendMessage")[-1]
    assert "待支付" in listing.text and "已交付" in listing.text
    assert "pending_payment" not in listing.text and "delivered" not in listing.text
    resume = [b.callback_data for b in buttons(listing.reply_markup) if (b.callback_data or "").startswith("resume:")]
    assert resume == [f"resume:{pending.id}"]

    # 从付款提示切到订单列表后，付款时不再覆盖这条消息
    await db.prompts.record(42, 5, order_id=pending.id)
    await start.cb_my_orders(callback(bot, "myorders"), db)
    assert [p["state"] for p in await prompts(db)] == ["closed"]


async def test_resume_payment_checks_owner_status_and_chat(db, user, product, bot, epay):
    order = await orders.create_order(db, user.id, product, 1)
    await db.users.upsert_user(43, "other")
    await cb_resume_payment(callback(bot, f"resume:{order.id}", user_id=43), db, epay)
    assert calls(bot, "answerCallbackQuery")[-1].text == "订单不存在"
    await cb_resume_payment(callback(bot, f"resume:{order.id}", chat_id=-100, chat_type="supergroup"), db, epay)
    assert "私聊" in calls(bot, "answerCallbackQuery")[-1].text
    assert calls(bot, "sendMessage") == []

    await cb_resume_payment(callback(bot, f"resume:{order.id}"), db, epay)
    prompt = calls(bot, "sendMessage")[-1]
    assert "待支付订单" in prompt.text
    assert [b.callback_data for b in buttons(prompt.reply_markup)] == [f"epay:{order.id}", "myorders"]
    assert [p["message_id"] for p in await prompts(db)] == [99]

    await orders.cancel_order(db, order.id)
    await cb_resume_payment(callback(bot, f"resume:{order.id}"), db, epay)
    assert calls(bot, "answerCallbackQuery")[-1].text == f"订单 #{order.id} 当前状态：已取消"


def unpaid(order):
    return {"code": 1, "status": 0, "pid": "1000", "trade_no": "", "out_trade_no": str(order.id), "money": "9.99"}


async def test_query_offers_payment_only_to_owner_in_private(
    *, db, user, product, bot, epay, purchaser, httpx_mock, monkeypatch
):
    order = await orders.create_order(db, user.id, product, 1)
    httpx_mock.add_response(json=unpaid(order))
    await start.cmd_query(text_message(bot, f"/query {order.id}"), db, epay, purchaser, bot)
    reply = calls(bot, "sendMessage")[-1]
    assert "订单尚未支付" in reply.text and f"epay:{order.id}" in [b.callback_data for b in buttons(reply.reply_markup)]
    assert [p["order_id"] for p in await prompts(db)] == [order.id]

    httpx_mock.add_response(json=unpaid(order))
    await start.cmd_query(text_message(bot, f"/query {order.id}", chat_id=-100123), db, epay, purchaser, bot)
    group_reply = calls(bot, "sendMessage")[-1]
    assert group_reply.text == f"订单 #{order.id} 尚未支付" and group_reply.reply_markup is None

    monkeypatch.setattr("shop_bot.handlers.start.get_settings", lambda: SimpleNamespace(admin_ids=[700]))
    await db.users.upsert_user(700, "admin")
    httpx_mock.add_response(json=unpaid(order))
    await start.cmd_query(text_message(bot, f"/query {order.id}", user_id=700, chat_id=700), db, epay, purchaser, bot)
    admin_reply = calls(bot, "sendMessage")[-1]
    assert admin_reply.text == f"订单 #{order.id} 尚未支付" and admin_reply.reply_markup is None


async def test_history_lists_money_movements_not_orders(db, user, product, bot):
    topup = await db.wallet.create_topup(user.id, 2000, "CNY")
    await db.wallet.complete_topup(topup.id, trade_no="TOPUP-1")
    by_balance = await orders.create_order(db, user.id, product, 1)
    await db.payments.pay_order_with_balance(by_balance.id, user.id, by_balance.amount_cents)
    online = await orders.create_order(db, user.id, product, 1)
    await db.payments.record_online_payment(online.id, "TRADE-ONLINE")
    still_pending = await orders.create_order(db, user.id, product, 1)

    start._menu_last_seen.clear()
    await start.menu_router(text_message(bot, MENU_HISTORY), db, None, None)
    history = calls(bot, "sendMessage")[-1].text
    assert history.startswith("🧾 交易记录")
    assert "充值到余额 · +20.00 CNY" in history
    assert f"余额支付 订单 #{by_balance.id} · -9.99 CNY" in history
    assert f"在线支付 订单 #{online.id} · 9.99 CNY" in history
    assert f"#{still_pending.id}" not in history  # 未付款订单不是资金记录


async def test_topup_hint_uses_gateway_currency(db, bot, epay):
    photo = Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Buyer"},
            "photo": [{"file_id": "p", "file_unique_id": "u", "width": 1, "height": 1}],
        },
        context={"bot": bot},
    )
    await topup_amount_non_text(photo, epay)
    hint = calls(bot, "sendMessage")[-1].text
    assert "（CNY）" in hint and "元" not in hint
