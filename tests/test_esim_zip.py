import io
import json
from pathlib import PurePosixPath
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendDocument, SendPhoto
from aiogram.types import BufferedInputFile

from shop_bot.db import Database
from shop_bot.handlers.start import cmd_query
from shop_bot.models import OrderStatus, Product, PurchaseState
from shop_bot.services import esim_media, fulfillment, orders
from shop_bot.services.esim_media import EsimMedia, esim_zip, qr_png
from shop_bot.services.fulfillment import NOTIFICATION_PLAN_VERSION, notify_owner, recover_once
from shop_bot.services.purchasing import CommbitzPurchaser
from tests.fakes import FakeCommbitzGateway
from tests.test_esim_media import delivered_order, esim_details
from tests.test_payment_flow import query_message


def documents(bot) -> list[SendDocument]:
    return [method for method in bot.session.sent if isinstance(method, SendDocument)]


def document_bytes(document: SendDocument) -> bytes:
    assert isinstance(document.document, BufferedInputFile)
    return document.document.data


def manifest(document: SendDocument) -> dict:
    with ZipFile(io.BytesIO(document_bytes(document))) as archive:
        assert archive.testzip() is None
        return json.loads(archive.read("esims.json"))


def test_zip_keeps_qr_iccid_and_long_lpa_matched_without_unsafe_paths():
    items = [
        EsimMedia("../../outside", "LPA:1$rsp.example.invalid$" + "A" * 1100),
        EsimMedia("../../outside", "LPA:1$rsp.example.invalid$SECOND"),
    ]
    blob = esim_zip(42, items)
    assert blob == esim_zip(42, items)  # 重试生成相同内容和 ZIP 元数据
    with ZipFile(io.BytesIO(blob)) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == 8  # README + manifest + 每张 3 个文件
        assert all(
            not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
            for name in archive.namelist()
        )
        data = json.loads(archive.read("esims.json"))
        assert data["order_id"] == 42 and len(data["esims"]) == 2
        for index, item in enumerate(items, 1):
            record = data["esims"][index - 1]
            assert record["index"] == index and record["iccid"] == item.iccid and record["lpa"] == item.lpa
            assert archive.read(record["qrcode"]) == qr_png(item.lpa)
            assert archive.read(f"{index:04d}/lpa.txt").decode() == item.lpa
            installation = archive.read(f"{index:04d}/installation.txt").decode()
            assert item.iccid in installation and item.lpa in installation


@pytest.mark.parametrize("quantity", [2, 5, 100])
async def test_multiple_esims_are_one_document_with_no_individual_messages(db, bot, quantity):
    order_id, purchaser, gateway = await delivered_order(db, count=quantity)
    assert await notify_owner(db, bot, order_id)
    assert len(bot.session.sent) == 1 and len(documents(bot)) == 1
    document = documents(bot)[0]
    assert document.chat_id == 42 and document.parse_mode is None
    assert isinstance(document.document, BufferedInputFile)
    assert document.document.filename == f"esims-{order_id}.zip"
    assert f"共 {quantity} 张" in (document.caption or "")
    assert len(manifest(document)["esims"]) == quantity
    saved = await db.get_order(order_id)
    assert not saved.notification_pending and saved.notification_cursor == 1
    assert saved.notification_plan_version == NOTIFICATION_PLAN_VERSION
    await recover_once(db, purchaser, bot)
    assert len(documents(bot)) == 1 and gateway.create_calls == 1


async def test_archive_failure_retries_after_restart_without_repurchase(tmp_path, bot):
    path = str(tmp_path / "retry.db")
    db = Database(path)
    await db.connect()
    try:
        order_id, purchaser, gateway = await delivered_order(db, count=2)
        bot.session.fail_document = True
        assert not await notify_owner(db, bot, order_id)
        saved = await db.get_order(order_id)
        assert saved is not None and saved.notification_pending and saved.notification_cursor == 0
        first_attempt = document_bytes(documents(bot)[0])
    finally:
        await db.close()
    db = Database(path)
    await db.connect()
    try:
        bot.session.fail_document = False
        await recover_once(db, purchaser, bot)
        assert document_bytes(documents(bot)[-1]) == first_attempt
        saved = await db.get_order(order_id)
        assert saved is not None and not saved.notification_pending and saved.notification_cursor == 1
        assert gateway.create_calls == 1
    finally:
        await db.close()


async def test_large_order_resumes_only_unsent_zip_part(db, bot, monkeypatch):
    order_id, purchaser, gateway = await delivered_order(db, count=101)
    original_send = bot.send_document
    fail_second = True

    async def send(chat_id, document, **kwargs):
        nonlocal fail_second
        if "0101-0101" in document.filename and fail_second:
            fail_second = False
            raise RuntimeError("simulated upload failure")
        return await original_send(chat_id, document, **kwargs)

    monkeypatch.setattr(bot, "send_document", send)
    assert not await notify_owner(db, bot, order_id)
    assert len(documents(bot)) == 1 and (await db.get_order(order_id)).notification_cursor == 1
    await recover_once(db, purchaser, bot)
    assert len(documents(bot)) == 2 and not any(isinstance(m, SendPhoto) for m in bot.session.sent)
    records = [record for doc in documents(bot) for record in manifest(doc)["esims"]]
    assert [record["index"] for record in records] == list(range(1, 102))
    assert len(manifest(documents(bot)[0])["esims"]) == 100
    assert len(manifest(documents(bot)[1])["esims"]) == 1
    assert gateway.create_calls == 1


async def test_manual_query_sends_archive_to_buyer_not_admin_chat(db, bot, monkeypatch):
    order_id, purchaser, gateway = await delivered_order(db, count=2)
    assert await notify_owner(db, bot, order_id)
    order = await db.get_order(order_id)
    monkeypatch.setattr("shop_bot.handlers.start.get_settings", lambda: SimpleNamespace(admin_ids=[700]))
    await cmd_query(query_message(bot, order, user_id=700, chat_id=-100), db, None, purchaser, bot)
    reply = bot.session.sent[-1]
    await recover_once(db, purchaser, bot)
    assert len(documents(bot)) == 2 and all(doc.chat_id == 42 for doc in documents(bot))
    assert document_bytes(documents(bot)[0]) == document_bytes(documents(bot)[1])
    assert reply.chat_id == -100 and gateway.create_calls == 1


@pytest.mark.parametrize("old_cursor", [1, 2, 3])
async def test_version_one_photo_progress_does_not_skip_archive(db, bot, old_cursor):
    order_id, _, gateway = await delivered_order(db, count=2)
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE orders SET notification_plan_version = 1, notification_cursor = ? WHERE id = ?",
            (old_cursor, order_id),
        )
    assert await notify_owner(db, bot, order_id)
    saved = await db.get_order(order_id)
    assert saved.notification_plan_version == NOTIFICATION_PLAN_VERSION and saved.notification_cursor == 1
    assert len(documents(bot)) == 1 and gateway.create_calls == 1


async def test_archive_rate_limit_is_respected(db, bot, monkeypatch):
    order_id, _, _ = await delivered_order(db, count=2)
    now = [1000.0]
    monkeypatch.setattr(fulfillment, "time", SimpleNamespace(time=lambda: now[0]))
    original_send = bot.send_document
    first = True

    async def send(chat_id, document, **kwargs):
        nonlocal first
        if first:
            first = False
            raise TelegramRetryAfter(
                method=SendDocument(chat_id=chat_id, document="fake"),
                message="wait",
                retry_after=30,
            )
        return await original_send(chat_id, document, **kwargs)

    monkeypatch.setattr(bot, "send_document", send)
    assert not await notify_owner(db, bot, order_id)
    saved = await db.get_order(order_id)
    assert saved.notification_cursor == 0 and saved.notification_retry_at == 1030
    assert not await notify_owner(db, bot, order_id, resend=True)
    assert bot.session.sent == []
    now[0] = 1030
    assert await notify_owner(db, bot, order_id)
    assert len(documents(bot)) == 1


async def test_rebinding_replaces_archive_contents_and_blocks_frozen_delivery(db, bot):
    order_id, purchaser, gateway = await delivered_order(db, count=2)
    assert await notify_owner(db, bot, order_id)
    purchase = await db.get_purchase_by_order(order_id)
    await db.transition_purchase(purchase.id, PurchaseState.SUBMISSION_UNKNOWN)
    assert not await notify_owner(db, bot, order_id, resend=True)
    assert len(documents(bot)) == 1
    gateway.details = esim_details("NEW", count=2)
    ok, _ = await purchaser.bind_unknown_purchase(db, order_id, "new-reference")
    assert ok
    assert not await notify_owner(db, bot, order_id)
    await purchaser.fulfill(db, order_id)
    assert await notify_owner(db, bot, order_id)
    assert all("CODE-NEW" in item["lpa"] for item in manifest(documents(bot)[-1])["esims"])
    assert gateway.create_calls == 1


@pytest.mark.parametrize("blocked_by", ["kyc", "partial"])
async def test_archive_waits_for_kyc_and_complete_delivery(db, bot, blocked_by):
    user = await db.upsert_user(42, "buyer")
    product = Product(1, "eSIM", "", 999, sku="SKU", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 2)
    details = esim_details(count=2)
    if blocked_by == "kyc":
        details.update({"isKycRequired": True, "isKycVerified": False})
    else:
        details["esims"] = details["esims"][:1]
    purchaser = CommbitzPurchaser(FakeCommbitzGateway(details=details))
    await orders.mark_paid(db, purchaser, order.id)
    await purchaser.fulfill(db, order.id)
    assert not await notify_owner(db, bot, order.id)
    assert (await db.get_order(order.id)).status == OrderStatus.PAID
    assert bot.session.sent == []


async def test_oversized_archive_never_marks_notification_complete(db, bot, monkeypatch):
    order_id, purchaser, gateway = await delivered_order(db, count=2)
    with monkeypatch.context() as limits:
        limits.setattr(esim_media, "MAX_ARCHIVE_BYTES", 100)
        assert not await notify_owner(db, bot, order_id)
    assert bot.session.sent == []
    saved = await db.get_order(order_id)
    assert saved.notification_pending and saved.notification_cursor == 0 and saved.notified_at is None
    await recover_once(db, purchaser, bot)
    assert len(documents(bot)) == 1 and gateway.create_calls == 1
