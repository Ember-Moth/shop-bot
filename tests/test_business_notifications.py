"""付款广播：事务一致性、逐收件人去重、独立恢复及配置撤销。"""

import json

import aiosqlite
import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from pydantic import ValidationError

from shop_bot.config import BusinessNotificationsSettings, Settings
from shop_bot.db import Database
from shop_bot.services import orders
from shop_bot.services.business_notifications import notify_business
from shop_bot.services.fulfillment import recover_once

TARGETS = [700, -1001234567890]
EVENTS = ["order_paid", "topup_paid"]


async def broadcasts(db):
    return await db._all("SELECT * FROM business_deliveries ORDER BY id")


def sent_broadcasts(bot):
    return [message for message in bot.session.sent if getattr(message, "chat_id", None) in TARGETS]


async def test_no_broadcast_for_unpaid_orders_and_topups(db, user, product):
    await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
    await orders.create_order(db, user.id, product, 1)
    await db.create_topup(user.id, 999)
    assert await broadcasts(db) == []
    assert await db.claim_work("business") is None


@pytest.mark.parametrize("method", ["epay", "balance", "manual"])
async def test_paid_order_broadcasts_once_to_each_target(*, db, user, product, purchaser, bot, method):
    await db.configure_business_notifications({event: [*TARGETS, 700] for event in EVENTS})
    order = await orders.create_order(db, user.id, product, 2)
    if method == "epay":
        await db.record_epay_payment(order.id, "PAY")
        await db.record_epay_payment(order.id, "PAY")
    elif method == "balance":
        await db.adjust_balance(user.id, 10000, "funding")
        await db.pay_order_with_balance(order.id, user.id, order.amount_cents)
        await db.pay_order_with_balance(order.id, user.id, order.amount_cents)
    else:
        await orders.mark_paid(db, purchaser, order.id)
        await orders.mark_paid(db, purchaser, order.id)
    assert len(await broadcasts(db)) == 2
    await recover_once(db, purchaser, bot)
    await recover_once(db, purchaser, bot)
    messages = sent_broadcasts(bot)
    assert len(messages) == len(TARGETS)
    assert {message.chat_id for message in messages} == set(TARGETS)
    assert all("19.98 CNY" in message.text and "数量：2" in message.text for message in messages)
    assert all(message.parse_mode is None for message in messages)
    assert all(row["state"] == "sent" for row in await broadcasts(db))
    for row in await broadcasts(db):
        assert set(json.loads(row["payload"])) == {
            "order_id",
            "product",
            "quantity",
            "amount_cents",
            "currency",
            "payment_method",
        }


async def test_topup_receipts_deduplicate_but_real_second_payment_has_own_notice(db, user, purchaser, bot):
    await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
    topup = await db.create_topup(user.id, 999, "USD")
    await db.complete_topup(topup.id, "FIRST")
    await db.complete_topup(topup.id, "FIRST")
    await db.complete_topup(topup.id, "SECOND")
    assert len(await broadcasts(db)) == 4
    await recover_once(db, purchaser, bot)
    assert len(sent_broadcasts(bot)) == 4
    assert all("充值已到账" in message.text and "9.99 USD" in message.text for message in sent_broadcasts(bot))
    assert await db.get_balance(user.id, "USD") == 1998


async def test_channel_failure_does_not_resend_to_admin(*, db, user, purchaser, bot, monkeypatch, queue_clock):
    await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
    topup = await db.create_topup(user.id, 999)
    await db.complete_topup(topup.id, "PAY")
    original = bot.send_message

    async def send(chat_id, text, **kwargs):
        if chat_id < 0:
            raise TelegramRetryAfter(method=SendMessage(chat_id=chat_id, text=text), message="wait", retry_after=60)
        return await original(chat_id, text, **kwargs)

    monkeypatch.setattr(bot, "send_message", send)
    await recover_once(db, purchaser, bot)
    assert [message.chat_id for message in sent_broadcasts(bot)] == [700]
    monkeypatch.setattr(bot, "send_message", original)
    queue_clock(59)
    await recover_once(db, purchaser, bot)
    assert len(sent_broadcasts(bot)) == 1
    queue_clock(2)
    await recover_once(db, purchaser, bot)
    assert [message.chat_id for message in sent_broadcasts(bot)] == TARGETS


async def test_successful_recipient_survives_restart_without_duplicate(tmp_path, bot, purchaser):
    db = Database(str(tmp_path / "broadcast.db"))
    await db.connect()
    user = await db.upsert_user(42, "buyer")
    await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
    topup = await db.create_topup(user.id, 999)
    await db.complete_topup(topup.id, "PAY")
    first = await db.claim_work("business")
    assert first is not None
    # 发出后已记录成功，但调度任务尚未来得及删除时进程退出。
    await notify_business(db, bot, first.entity_id)
    await db.close()
    await db.connect()
    try:
        await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
        await recover_once(db, purchaser, bot)
        assert len(sent_broadcasts(bot)) == len(TARGETS)
        assert {message.chat_id for message in sent_broadcasts(bot)} == set(TARGETS)
    finally:
        await db.close()


async def test_configuration_removal_cancels_backlog_and_does_not_replay(db, user, purchaser, bot):
    await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
    topup = await db.create_topup(user.id, 999)
    await db.complete_topup(topup.id, "PAY")
    await db.configure_business_notifications({event: [700] for event in EVENTS})
    await recover_once(db, purchaser, bot)
    assert [message.chat_id for message in sent_broadcasts(bot)] == [700]
    assert {row["chat_id"]: row["state"] for row in await broadcasts(db)} == {700: "sent", TARGETS[1]: "skipped"}
    await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
    await recover_once(db, purchaser, bot)
    assert len(sent_broadcasts(bot)) == 1
    await db.configure_business_notifications({})
    second = await db.create_topup(user.id, 999)
    await db.complete_topup(second.id, "SECOND")
    assert len(await broadcasts(db)) == 2


async def test_broadcast_uses_payment_time_snapshot(db, user, product, purchaser, bot):
    await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, purchaser, order.id)
    async with db.transaction() as conn:
        await conn.execute("UPDATE products SET name = 'changed', currency = 'USD', price_cents = 5000")
    await recover_once(db, purchaser, bot)
    assert all("商品：demo" in message.text and "9.99 CNY" in message.text for message in sent_broadcasts(bot))


async def test_broadcast_insertion_failure_rolls_back_payment(db, user, product):
    await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
    order = await orders.create_order(db, user.id, product, 1)
    async with db.transaction() as conn:
        await conn.execute("""CREATE TRIGGER fail_business BEFORE INSERT ON business_deliveries
            BEGIN SELECT RAISE(ABORT, 'test broadcast failure'); END""")
    with pytest.raises(aiosqlite.IntegrityError, match="broadcast failure"):
        await db.record_epay_payment(order.id, "PAY")
    assert (await db.get_order(order.id)).status == "pending_payment"
    assert await broadcasts(db) == []
    assert await db._all("SELECT * FROM payment_receipts") == []


async def test_enabling_broadcasts_does_not_send_historical_payments(db, user, product, purchaser, bot):
    order = await orders.create_order(db, user.id, product, 1)
    await db.record_epay_payment(order.id, "PAY")
    await db.configure_business_notifications(dict.fromkeys(EVENTS, TARGETS))
    await db.record_epay_payment(order.id, "PAY")
    await recover_once(db, purchaser, bot)
    assert not sent_broadcasts(bot)


@pytest.mark.parametrize("values", [{"chat_ids": [0]}, {"chat_ids": ["@channel"]}, {"events": ["order_created"]}])
def test_invalid_broadcast_config_is_rejected(values):
    with pytest.raises(ValidationError):
        BusinessNotificationsSettings(**values)


def test_nested_environment_config(monkeypatch):
    monkeypatch.setenv("SHOP_BOT_BUSINESS_NOTIFICATIONS__CHAT_IDS", "[-1001234567890]")
    settings = Settings()
    assert settings.business_notifications.notify_admins
    assert settings.business_notifications.chat_ids == [-1001234567890]
    assert settings.business_notifications.events == EVENTS


async def test_order_to_channel_topup_to_admin_without_cross_delivery(db, user, product, purchaser, bot):
    settings = BusinessNotificationsSettings.model_validate(
        {
            "routes": {
                "order_paid": {"chat_ids": [TARGETS[1]], "notify_admins": False},
                "topup_paid": {"notify_admins": True},
            },
        }
    )
    await db.configure_business_notifications(settings.resolve_routes([700]))
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, purchaser, order.id)
    topup = await db.create_topup(user.id, 999)
    await db.complete_topup(topup.id, "TOPUP")
    await recover_once(db, purchaser, bot)
    messages = sent_broadcasts(bot)
    assert len(messages) == 2
    assert {(message.chat_id, message.text.splitlines()[0]) for message in messages} == {
        (TARGETS[1], "✅ 订单已付款"),
        (700, "💰 充值已到账"),
    }


def test_event_override_does_not_inherit_global_channel_or_admins():
    settings = BusinessNotificationsSettings.model_validate(
        {
            "notify_admins": True,
            "chat_ids": [TARGETS[1]],
            "routes": {"topup_paid": {"notify_admins": True}},
        }
    )
    assert settings.resolve_routes([700]) == {"order_paid": TARGETS, "topup_paid": [700]}
    settings.routes["topup_paid"].enabled = False
    assert settings.resolve_routes([700]) == {"order_paid": TARGETS}
    settings.enabled = False
    assert settings.resolve_routes([700]) == {}


def test_nested_event_route_environment_override(monkeypatch):
    monkeypatch.setenv("SHOP_BOT_BUSINESS_NOTIFICATIONS__ROUTES__ORDER_PAID__CHAT_IDS", "[-1001234567890]")
    monkeypatch.setenv("SHOP_BOT_BUSINESS_NOTIFICATIONS__ROUTES__ORDER_PAID__NOTIFY_ADMINS", "false")
    assert Settings().business_notifications.resolve_routes([700]) == {
        "order_paid": [TARGETS[1]],
        "topup_paid": [700],
    }


@pytest.mark.parametrize("routes", [{"unknown": {}}, {"order_paid": {"chat_ids": [0]}}])
def test_invalid_event_routes_rejected(routes):
    with pytest.raises(ValidationError):
        BusinessNotificationsSettings.model_validate({"routes": routes})
