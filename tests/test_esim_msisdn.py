import io
import json
from zipfile import ZipFile

import pytest

from shop_bot.db import Database
from shop_bot.models import Product
from shop_bot.services import orders
from shop_bot.services.esim_media import delivery_esims, serialize_esims
from shop_bot.services.fulfillment import NOTIFICATION_PLAN_VERSION, notify_owner
from shop_bot.services.purchasing import CommbitzPurchaser, build_payload

from .fakes import FakeCommbitzGateway
from .test_esim_media import esim_details


@pytest.mark.parametrize("number", ["+00123456789", "00123456789", "66658649412"])
def test_number_preserves_prefix_and_matches_legacy_text(number):
    details = esim_details()
    details["esims"][0]["msisdn"] = number
    saved = delivery_esims(serialize_esims(details["esims"]), None, 1)
    legacy = delivery_esims(None, build_payload(details), 1)
    assert saved[0].msisdn == legacy[0].msisdn == number
    assert saved[0].number_text == number


@pytest.mark.parametrize("value", [None, "", "   ", 123456, {}, "+123\nICCID: forged", "X" * 10000])
def test_missing_or_invalid_optional_number_does_not_block_delivery(value):
    details = esim_details()
    details["esims"][0]["msisdn"] = value
    media = delivery_esims(serialize_esims(details["esims"]), None, 1)[0]
    assert media.msisdn is None
    assert media.number_text == "上游未提供号码"
    assert media.lpa == details["esims"][0]["lpa"]


def test_legacy_missing_number_is_not_claimed_as_missing_upstream():
    details = esim_details()
    assert delivery_esims(json.dumps(details["esims"]), None, 1)[0].number_text == "历史订单未保存号码"
    assert delivery_esims(None, build_payload(details), 1)[0].number_text == "历史订单未保存号码"


@pytest.mark.parametrize("count", [1, 2])
async def test_number_persists_across_restart_in_photo_or_zip(tmp_path, bot, count):
    db = Database(str(tmp_path / "numbers.db"))
    await db.connect()
    user = await db.upsert_user(42, "buyer")
    product = Product(1, "eSIM", "", 999, sku="SKU", request_type="esim", upstream_plan_id="plan")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, count)
    details = esim_details(count=count)
    for index, esim in enumerate(details["esims"]):
        esim["msisdn"] = f"+0012345678{index}"
    gateway = FakeCommbitzGateway(details=details)
    purchaser = CommbitzPurchaser(gateway)
    await orders.mark_paid(db, purchaser, order.id)
    await purchaser.fulfill(db, order.id)
    # 旧版待通知的步骤不可以跳过新格式。
    async with db.transaction() as conn:
        await conn.execute("UPDATE orders SET notification_plan_version = 2, notification_cursor = 1")
    await db.close()
    await db.connect()
    try:
        assert await notify_owner(db, bot, order.id)
        saved = await db.get_order(order.id)
        assert saved is not None and saved.notification_plan_version == NOTIFICATION_PLAN_VERSION
        assert gateway.create_calls == 1
        if count == 1:
            photos = [m for m in bot.session.sent if m.__api_method__ == "sendPhoto"]
            assert len(photos) == 1 and "号码: +00123456780" in photos[0].caption
        else:
            documents = [m for m in bot.session.sent if m.__api_method__ == "sendDocument"]
            assert len(documents) == 1
            with ZipFile(io.BytesIO(documents[0].document.data)) as archive:
                rows = json.loads(archive.read("esims.json"))["esims"]
                for index, row in enumerate(rows):
                    number = details["esims"][index]["msisdn"]
                    assert row["msisdn"] == number and row["iccid"] == details["esims"][index]["iccid"]
                    assert f"号码: {number}" in archive.read(f"{index + 1:04d}/installation.txt").decode()
        assert all(m.chat_id == 42 for m in bot.session.sent)
        # 完成通知后重启不会自动再发。
        sent_count = len(bot.session.sent)
        await db.close()
        await db.connect()
        assert await notify_owner(db, bot, order.id)
        assert len(bot.session.sent) == sent_count
    finally:
        await db.close()
