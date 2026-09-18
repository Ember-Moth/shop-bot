import json

import pytest
from aiogram.methods import SendMessage, SendPhoto

from shop_bot.db import Database
from shop_bot.models import OrderStatus, Product
from shop_bot.services import orders
from shop_bot.services.fulfillment import NOTIFICATION_PLAN_VERSION, notify_owner, recover_once
from shop_bot.services.purchasing import CommbitzPurchaser
from shop_bot.telegram_text import text_units
from tests.fakes import FakeCommbitzGateway
from tests.test_esim_media import delivered_order, esim_details, photos


@pytest.mark.parametrize("quantity,old_cursor", [(1, 1), (1, 2), (1, 3), (2, 2), (2, 3), (2, 4)])
async def test_unversioned_pending_notification_replays_safely_after_upgrade(tmp_path, bot, quantity, old_cursor):
    path = str(tmp_path / "upgrade.db")
    db = Database(path)
    await db.connect()
    try:
        order_id, purchaser, gateway = await delivered_order(db, count=quantity)
        saved = await db.get_order(order_id)
        assert saved is not None
        original_payload, original_media = saved.payload, saved.delivery_esims
        async with db.transaction() as conn:
            # 旧库没有方案版本；cursor 可能来自“文本+图片”或“图文”两种排列。
            await conn.execute("ALTER TABLE orders DROP COLUMN notification_plan_version")
            await conn.execute("UPDATE orders SET notification_cursor = ? WHERE id = ?", (old_cursor, order_id))
    finally:
        await db.close()
    db = Database(path)
    await db.connect()
    try:
        await recover_once(db, purchaser, bot)
        saved = await db.get_order(order_id)
        assert saved is not None and not saved.notification_pending and saved.notified_at
        assert saved.notification_plan_version == NOTIFICATION_PLAN_VERSION
        assert saved.payload == original_payload and saved.delivery_esims == original_media
        assert saved.status == OrderStatus.DELIVERED and saved.trade_no == "paid"
        media = [m for m in bot.session.sent if m.__api_method__ in ("sendPhoto", "sendDocument")]
        assert len(media) == 1 and gateway.create_calls == 1
        assert media[0].__api_method__ == ("sendPhoto" if quantity == 1 else "sendDocument")
        await recover_once(db, purchaser, bot)
        assert len([m for m in bot.session.sent if m.__api_method__ in ("sendPhoto", "sendDocument")]) == 1
    finally:
        await db.close()


async def test_upgrade_preserves_completed_notifications_until_manual_resend(tmp_path, bot):
    path = str(tmp_path / "complete.db")
    db = Database(path)
    await db.connect()
    try:
        order_id, purchaser, gateway = await delivered_order(db)
        await db.mark_notified(order_id)
        async with db.transaction() as conn:
            await conn.execute("ALTER TABLE orders DROP COLUMN notification_plan_version")
            await conn.execute("UPDATE orders SET notification_cursor = 2 WHERE id = ?", (order_id,))
    finally:
        await db.close()
    db = Database(path)
    await db.connect()
    try:
        await recover_once(db, purchaser, bot)
        assert bot.session.sent == []
        assert await notify_owner(db, bot, order_id, resend=True)
        assert len(photos(bot)) == 1 and gateway.create_calls == 1
    finally:
        await db.close()


@pytest.mark.parametrize("suffix", ["A" * 1100, "😀" * 480])
async def test_long_lpa_is_delivered_whole_and_never_truncated(db, bot, suffix):
    user = await db.upsert_user(42, "buyer")
    product = Product(1, "eSIM", "", 999, sku="SKU", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    details = esim_details()
    lpa = "LPA:1$rsp.example.invalid$" + suffix
    details["esims"][0]["lpa"] = lpa
    purchaser = CommbitzPurchaser(FakeCommbitzGateway(details=details))
    await orders.mark_paid(db, purchaser, order.id)
    await purchaser.fulfill(db, order.id)
    assert await notify_owner(db, bot, order.id)
    sent_photos = photos(bot)
    assert len(sent_photos) == 1
    assert text_units(sent_photos[0].caption) <= 1024
    install_texts = [m.text for m in bot.session.sent if isinstance(m, SendMessage) and lpa in m.text]
    assert len(install_texts) == 1 and text_units(install_texts[0]) <= 4096
    assert "LPA: " not in sent_photos[0].caption  # 不给买家一份看似可复制的截断安装码
    saved = await db.get_order(order.id)
    assert not saved.notification_pending and saved.notification_cursor == 3


async def test_long_lpa_text_failure_resumes_without_repeating_photo(tmp_path, bot, monkeypatch):
    path = str(tmp_path / "long.db")
    db = Database(path)
    await db.connect()
    original_send = bot.send_message
    fail = True

    async def send(chat_id, text, **kwargs):
        if "完整 LPA 安装码：" in text and fail:
            raise RuntimeError("simulated Telegram unavailable")
        return await original_send(chat_id, text, **kwargs)

    monkeypatch.setattr(bot, "send_message", send)
    try:
        order_id, purchaser, gateway = await delivered_order(db)
        # 准备完整的长码，与文本货品同时保存，模拟真实上游交付。
        lpa = "LPA:1$rsp.example.invalid$" + "B" * 1100
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE orders SET delivery_esims = ?, payload = ? WHERE id = ?",
                (json.dumps([{"iccid": "89000", "lpa": lpa}]), f"[1]\nICCID: 89000\nLPA: {lpa}", order_id),
            )
        assert not await notify_owner(db, bot, order_id)
        saved = await db.get_order(order_id)
        assert saved is not None and saved.notification_cursor == 2
        assert saved.notification_plan_version == NOTIFICATION_PLAN_VERSION and saved.notification_pending
        assert len(photos(bot)) == 1
    finally:
        await db.close()
    fail = False
    db = Database(path)
    await db.connect()
    try:
        await recover_once(db, purchaser, bot)
        assert len(photos(bot)) == 1 and gateway.create_calls == 1
        assert isinstance(bot.session.sent[-1], SendMessage) and lpa in bot.session.sent[-1].text
        saved = await db.get_order(order_id)
        assert saved is not None and not saved.notification_pending and saved.notification_cursor == 3
    finally:
        await db.close()


async def test_short_lpa_stays_in_photo_caption(db, bot):
    order_id, _, _ = await delivered_order(db)
    assert await notify_owner(db, bot, order_id)
    assert len(bot.session.sent) == 2
    assert isinstance(bot.session.sent[0], SendMessage) and isinstance(bot.session.sent[1], SendPhoto)
    assert esim_details()["esims"][0]["lpa"] in bot.session.sent[1].caption
