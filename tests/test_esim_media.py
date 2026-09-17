import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendPhoto
from aiogram.types import BufferedInputFile

from shop_bot.db import Database
from shop_bot.handlers.order import cb_pay_with_balance
from shop_bot.handlers.start import cmd_query
from shop_bot.models import OrderStatus, Product, PurchaseState
from shop_bot.services import fulfillment, orders
from shop_bot.services.esim_media import delivery_esims, qr_png
from shop_bot.services.fulfillment import notify_owner, recover_once
from shop_bot.services.purchasing import CommbitzPurchaser, _delivery_content, build_payload
from tests.fakes import FakeCommbitzGateway
from tests.test_balance import _balance_callback
from tests.test_payment_flow import query_message


def esim_details(label="A", count=1):
    return {
        "status": "Success",
        "quantity": count,
        "requestType": "esim",
        "planId": "plan",
        "sku": "SKU",
        "esims": [
            {
                "iccid": f"89000000000000000{index}",
                "lpa": f"LPA:1$rsp.example.invalid$CODE-{label}-{index}",
                "qrCode": f"https://expired.example.invalid/{label}-{index}.png",
            }
            for index in range(count)
        ],
    }


async def delivered_order(db, count=1) -> tuple[int, CommbitzPurchaser, FakeCommbitzGateway]:
    user = await db.upsert_user(42, "buyer")
    product = Product(1, "eSIM", "", 999, sku="SKU", request_type="esim", upstream_plan_id="plan")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, count)
    gateway = FakeCommbitzGateway(details=esim_details(count=count))
    purchaser = CommbitzPurchaser(gateway)
    await orders.mark_paid(db, purchaser, order.id, trade_no="paid")
    await purchaser.fulfill(db, order.id)
    return order.id, purchaser, gateway


def photos(bot):
    return [method for method in bot.session.sent if method.__api_method__ == "sendPhoto"]


async def test_photo_contains_local_png_and_only_goes_to_owner(db, bot):
    order_id, purchaser, _gateway = await delivered_order(db, count=2)
    saved = await db.get_order(order_id)
    assert json.loads(saved.delivery_esims)[0]["lpa"] == esim_details()["esims"][0]["lpa"]
    assert await notify_owner(db, bot, order_id)
    sent = photos(bot)
    assert len(sent) == 2 and all(photo.chat_id == 42 for photo in sent)
    for index, photo in enumerate(sent):
        assert isinstance(photo.photo, BufferedInputFile)
        assert photo.photo.data.startswith(b"\x89PNG\r\n\x1a\n")
        assert photo.photo.data == qr_png(esim_details(count=2)["esims"][index]["lpa"])
        assert f"eSIM {index + 1}/2" in photo.caption and "ICCID:" in photo.caption
        assert photo.parse_mode is None and len(photo.caption) <= 1024
    saved = await db.get_order(order_id)
    assert not saved.notification_pending and saved.notification_cursor == 4
    await recover_once(db, purchaser, bot)
    assert len(photos(bot)) == 2


async def test_photo_failure_resumes_after_restart_without_resending_first(tmp_path, bot, monkeypatch):
    path = str(tmp_path / "restart.db")
    db = Database(path)
    await db.connect()
    original_send = bot.send_photo
    fail_second = True

    async def send(chat_id, photo, **kwargs):
        nonlocal fail_second
        if photo.filename.endswith("-2.png") and fail_second:
            fail_second = False
            raise RuntimeError("simulated image failure")
        return await original_send(chat_id, photo, **kwargs)

    monkeypatch.setattr(bot, "send_photo", send)
    try:
        order_id, purchaser, gateway = await delivered_order(db, count=2)
        assert not await notify_owner(db, bot, order_id)
        saved = await db.get_order(order_id)
        assert saved is not None and saved.notification_pending and saved.notification_cursor == 3
        assert len(photos(bot)) == 1
    finally:
        await db.close()
    restored = Database(path)
    await restored.connect()
    try:
        await recover_once(restored, purchaser, bot)
        assert [photo.photo.filename for photo in photos(bot)] == [f"esim-{order_id}-1.png", f"esim-{order_id}-2.png"]
        saved = await restored.get_order(order_id)
        assert saved is not None and not saved.notification_pending and saved.notified_at
        assert gateway.create_calls == 1
    finally:
        await restored.close()


async def test_rate_limit_defers_photo_and_keeps_text_progress(db, bot, monkeypatch):
    order_id, _, _gateway = await delivered_order(db)
    now = [1000.0]
    monkeypatch.setattr(fulfillment, "time", SimpleNamespace(time=lambda: now[0]))
    original_send = bot.send_photo
    first = True

    async def send(chat_id, photo, **kwargs):
        nonlocal first
        if first:
            first = False
            raise TelegramRetryAfter(method=SendPhoto(chat_id=chat_id, photo="fake"), message="wait", retry_after=30)
        return await original_send(chat_id, photo, **kwargs)

    monkeypatch.setattr(bot, "send_photo", send)
    assert not await notify_owner(db, bot, order_id)
    saved = await db.get_order(order_id)
    assert saved.notification_cursor == 2 and saved.notification_retry_at == 1030
    calls = len(bot.session.sent)
    assert not await notify_owner(db, bot, order_id, resend=True)
    assert len(bot.session.sent) == calls
    now[0] = 1030
    assert await notify_owner(db, bot, order_id)
    assert len(bot.session.sent) == calls + 1
    assert (await db.get_order(order_id)).notification_retry_at is None


async def test_manual_query_resends_images_to_buyer_not_admin_chat(db, bot, monkeypatch):
    order_id, purchaser, gateway = await delivered_order(db)
    assert await notify_owner(db, bot, order_id)
    order = await db.get_order(order_id)
    monkeypatch.setattr("shop_bot.handlers.start.get_settings", lambda: SimpleNamespace(admin_ids=[700]))
    await cmd_query(query_message(bot, order, user_id=700, chat_id=-100), db, None, purchaser, bot)
    assert len(photos(bot)) == 2 and all(photo.chat_id == 42 for photo in photos(bot))
    assert photos(bot)[0].photo.data == photos(bot)[1].photo.data
    assert gateway.create_calls == 1


async def test_rebinding_clears_media_progress_and_sends_new_qr_only(db, bot):
    order_id, purchaser, gateway = await delivered_order(db)
    bot.session.fail_photo = True
    assert not await notify_owner(db, bot, order_id)
    purchase = await db.get_purchase_by_order(order_id)
    await db.transition_purchase(purchase.id, PurchaseState.SUBMISSION_UNKNOWN)
    gateway.details = esim_details("NEW")
    ok, _ = await purchaser.bind_unknown_purchase(db, order_id, "new-reference")
    assert ok
    rebound = await db.get_order(order_id)
    assert rebound.delivery_esims is None and rebound.notification_cursor == 0 and rebound.notification_retry_at is None
    previous_attempts = len(photos(bot))
    assert not await notify_owner(db, bot, order_id, resend=True)
    assert len(photos(bot)) == previous_attempts
    await purchaser.fulfill(db, order_id)
    bot.session.fail_photo = False
    assert await notify_owner(db, bot, order_id)
    assert photos(bot)[-1].photo.data == qr_png(esim_details("NEW")["esims"][0]["lpa"])
    assert gateway.create_calls == 1


async def test_legacy_text_esim_can_be_sent_as_photo(db, user, bot):
    payload = build_payload(esim_details())
    order = await db.create_order(user.id, 1, 1, 999, "USD")
    await db.transition_order(order.id, OrderStatus.DELIVERED, payload=payload, upstream_ref="legacy")
    assert (await db.get_order(order.id)).delivery_esims is None
    assert await notify_owner(db, bot, order.id)
    assert len(photos(bot)) == 1


async def test_kyc_blocks_photo_until_release(db, bot):
    user = await db.upsert_user(42, "buyer")
    product = Product(1, "eSIM", "", 999, sku="SKU", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    gateway = FakeCommbitzGateway(details={**esim_details(), "isKycRequired": True, "isKycVerified": False})
    purchaser = CommbitzPurchaser(gateway)
    await orders.mark_paid(db, purchaser, order.id)
    await purchaser.fulfill(db, order.id)
    assert not await notify_owner(db, bot, order.id)
    assert photos(bot) == [] and (await db.get_order(order.id)).delivery_esims is None
    gateway.details["isKycVerified"] = True
    await purchaser.fulfill(db, order.id)
    assert await notify_owner(db, bot, order.id)
    assert len(photos(bot)) == 1


@pytest.mark.parametrize("lpa", [123, "not an LPA", "LPA:a\nLPA:b", "LPA:" + "x" * 2000])
def test_invalid_lpa_cannot_be_finalized_for_image_delivery(lpa):
    details = esim_details()
    details["esims"][0]["lpa"] = lpa
    complete, _, _ = _delivery_content("esim", details, 1)
    assert not complete


def test_non_esim_payload_does_not_produce_images():
    assert delivery_esims(None, "兑换券：TOKEN", 1) == []
    assert delivery_esims(None, "[stub goods for order #1]", 1) == []


async def test_balance_payment_does_not_claim_photo_sent_when_upload_failed(db, bot):
    user = await db.upsert_user(42, "buyer")
    product = Product(1, "eSIM", "", 999, sku="SKU", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    await db.adjust_balance(user.id, 1000, "funding", "USD")
    purchaser = CommbitzPurchaser(FakeCommbitzGateway(details=esim_details()))
    bot.session.fail_photo = True
    await cb_pay_with_balance(_balance_callback(bot, order.id), db, purchaser, bot)
    assert "尚未完成" in bot.session.sent[-1].text
    assert (await db.get_order(order.id)).notification_pending
    assert await db.get_balance(user.id, "USD") == 1
