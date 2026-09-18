"""持久化调度契约：快速收款、通道隔离、领取限额、中断恢复和通知幂等。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiosqlite
import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shop_bot.db import Database
from shop_bot.handlers.start import cmd_query
from shop_bot.models import OrderStatus, PurchaseState
from shop_bot.services import orders
from shop_bot.services.epay import _create_sign
from shop_bot.services.fulfillment import process_work, recover_once, recovery_loop
from shop_bot.services.invoices import payment_url
from shop_bot.services.purchasing import CommbitzPurchaser, DemoPurchaser
from shop_bot.web.payment import register_epay_routes

from .fakes import FakeCommbitzGateway
from .test_esim_media import delivered_order
from .test_payment_flow import callback_params, query_message


@asynccontextmanager
async def callback_server(db, epay, bot):
    app = web.Application()
    app.update(db=db, epay=epay, bot=bot, purchaser=DemoPurchaser())
    register_epay_routes(app, "/callback")
    async with TestClient(TestServer(app)) as client:
        yield client


async def test_duplicate_payment_and_admin_confirmation_do_not_wait_for_order_lock(db, user, product, epay, bot):
    order = await orders.create_order(db, user.id, product, 1)
    async with callback_server(db, epay, bot) as client:
        async with db.order_operation(order.id):
            async with asyncio.timeout(1):
                for _ in range(2):
                    response = await client.get("/callback", params=callback_params(order))
                    assert response.status == 200 and await response.text() == "success"
                await orders.mark_paid(db, DemoPurchaser(), order.id)
        purchase = await db.get_purchase_by_order(order.id)
        assert purchase.state == PurchaseState.READY
        assert await db.get_balance(user.id, product.currency) == 0
        item = await db.claim_work("purchase")
        assert item.entity_id == order.id
        assert await db.claim_work("purchase") is None


@pytest.mark.parametrize("source", ["epay", "balance", "manual", "topup", "adjust"])
async def test_payment_and_task_intent_roll_back_together(db, user, product, source):
    order = await orders.create_order(db, user.id, product, 1)
    await db.adjust_balance(user.id, 10000, "funding")
    topup = await db.create_topup(user.id, 999)
    async with db.transaction() as conn:
        await conn.execute("""CREATE TRIGGER reject_work BEFORE INSERT ON work_items
            BEGIN SELECT RAISE(ABORT, 'simulated queue unavailable'); END""")
    with pytest.raises(aiosqlite.IntegrityError, match="queue unavailable"):
        if source == "epay":
            await db.record_epay_payment(order.id, "TRADE")
        elif source == "balance":
            await db.pay_order_with_balance(order.id, user.id, order.amount_cents)
        elif source == "manual":
            await orders.mark_paid(db, DemoPurchaser(), order.id)
        elif source == "topup":
            await db.complete_topup(topup.id, "TRADE")
        else:
            await db.adjust_balance(user.id, 100, "adjust")
    assert await db.get_balance(user.id, "CNY") == 10000
    assert (await db.get_order(order.id)).status == OrderStatus.PENDING_PAYMENT
    assert await db.get_purchase_by_order(order.id) is None
    assert (await db.get_topup(topup.id)).status == "pending"
    assert len(await db.list_balance_transactions(user.id)) == 1
    async with db.connection() as conn:
        assert (await (await conn.execute("SELECT COUNT(*) FROM payment_receipts")).fetchone())[0] == 0


async def test_zip_upload_does_not_block_new_procurement(db, bot, monkeypatch):
    first_id, _, _ = await delivered_order(db, count=2)
    product = await db.get_product(1)
    assert product is not None
    started, release, second_delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = bot.send_document

    async def slow_document(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    class RecordingPurchaser(DemoPurchaser):
        async def fulfill(self, db, order_id):
            result = await super().fulfill(db, order_id)
            if order_id != first_id and result is not None and result.status == OrderStatus.DELIVERED:
                second_delivered.set()
            return result

    monkeypatch.setattr(bot, "send_document", slow_document)
    worker = asyncio.create_task(recovery_loop(db, RecordingPurchaser(), bot))
    try:
        async with asyncio.timeout(4):
            await started.wait()
            other = await db.upsert_user(43, "other")
            order = await orders.create_order(db, other.id, product, 1)
            await orders.mark_paid(db, DemoPurchaser(), order.id)
            await second_delivered.wait()
        assert not release.is_set()
        assert (await db.get_order(first_id)).notification_pending
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_procurement_concurrency_is_bounded_and_slow_supplier_does_not_block_notifications(
    db,
    user,
    product,
    bot,
):
    entered, release, notified = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = 0
    peak = 0

    class SlowPurchaser(DemoPurchaser):
        async def fulfill(self, db, order_id):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                entered.set()
            try:
                await release.wait()
                return await super().fulfill(db, order_id)
            finally:
                active -= 1

    for _ in range(8):
        order = await orders.create_order(db, user.id, product, 1)
        await orders.mark_paid(db, DemoPurchaser(), order.id)
    original_send = bot.send_message

    async def send(*args, **kwargs):
        result = await original_send(*args, **kwargs)
        if "充值到账" in args[1]:
            notified.set()
        return result

    bot.send_message = send
    worker = asyncio.create_task(recovery_loop(db, SlowPurchaser(), bot))
    try:
        async with asyncio.timeout(4):
            await entered.wait()
            topup = await db.create_topup(user.id, 100)
            await db.complete_topup(topup.id, "TOPUP")
            await notified.wait()
        assert peak == 3 and active == 3
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_cancelled_submission_never_recreates_upstream_purchase(db, user, product):
    order = await orders.create_order(db, user.id, product, 1)
    entered = asyncio.Event()

    class InterruptedGateway(FakeCommbitzGateway):
        async def create_request(self, **kwargs):
            self.create_calls += 1
            entered.set()
            await asyncio.Event().wait()
            return {}

    gateway = InterruptedGateway()
    purchaser = CommbitzPurchaser(gateway)
    await orders.mark_paid(db, purchaser, order.id)
    item = await db.claim_work("purchase")
    task = asyncio.create_task(process_work(db, purchaser, None, item))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await recover_once(db, purchaser, None)
    assert gateway.create_calls == 1
    assert (await db.get_purchase_by_order(order.id)).state == PurchaseState.SUBMISSION_UNKNOWN
    assert await db.claim_work("purchase") is None


async def test_duplicate_topup_callback_is_fast_and_queues_one_notification(db, user, epay, bot):
    topup = await db.create_topup(user.id, 999)
    params = {
        "pid": epay.pid,
        "out_trade_no": f"T{topup.id}",
        "trade_no": "T-ONE",
        "money": "9.99",
        "trade_status": "TRADE_SUCCESS",
        "sign_type": "MD5",
    }
    params["sign"] = _create_sign(params, "audit-secret")
    bot.session.fail_send = True
    async with callback_server(db, epay, bot) as client:
        for _ in range(2):
            response = await client.get("/callback", params=params)
            assert response.status == 200
    assert not bot.session.sent
    assert await db.get_balance(user.id, "CNY") == 999
    bot.session.fail_send = False
    await recover_once(db, DemoPurchaser(), bot)
    await recover_once(db, DemoPurchaser(), bot)
    assert len(bot.session.sent) == 1
    assert bot.session.sent[0].chat_id == user.telegram_id


async def test_wallet_failure_survives_restart_and_respects_retry_after(tmp_path, bot, monkeypatch, queue_clock):
    path = str(tmp_path / "queue.db")
    db = Database(path)
    await db.connect()
    user = await db.upsert_user(42, "buyer")
    topup = await db.create_topup(user.id, 999)
    await db.complete_topup(topup.id, "T1")
    original = bot.send_message

    async def limited(*args, **kwargs):
        raise TelegramRetryAfter(method=SendMessage(chat_id=42, text="test"), message="wait", retry_after=60)

    monkeypatch.setattr(bot, "send_message", limited)
    await recover_once(db, DemoPurchaser(), bot)
    await db.close()
    await db.connect()
    try:
        monkeypatch.setattr(bot, "send_message", original)
        await recover_once(db, DemoPurchaser(), bot)
        assert not bot.session.sent
        queue_clock(59)
        assert await db.claim_work("wallet") is None
        queue_clock(2)
        await recover_once(db, DemoPurchaser(), bot)
        assert len(bot.session.sent) == 1
        await db.complete_topup(topup.id, "T1")
        await recover_once(db, DemoPurchaser(), bot)
        assert len(bot.session.sent) == 1
        # 另一笔真实付款仍独立入账、独立通知。
        await db.complete_topup(topup.id, "T2")
        await recover_once(db, DemoPurchaser(), bot)
        assert len(bot.session.sent) == 2
        assert await db.get_balance(user.id, "CNY") == 1998
    finally:
        await db.close()


async def test_stale_completion_cannot_erase_new_work(db, user, product):
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, DemoPurchaser(), order.id)
    item = await db.claim_work("purchase")
    purchase = await db.get_purchase_by_order(order.id)
    await db.transition_purchase(purchase.id, PurchaseState.UPSTREAM_PENDING, upstream_request_id="known")
    await db.finish_work(item, done=True)
    replacement = await db.claim_work("purchase")
    assert replacement and replacement.revision > item.revision


async def test_frozen_orders_do_not_generate_per_order_recovery_reads(db, user, product):
    async with db.transaction() as conn:
        await conn.executemany(
            """INSERT INTO orders (user_id, product_id, quantity, amount_cents, currency, status)
            VALUES (?, ?, 1, 999, 'CNY', 'paid')""",
            [(user.id, product.id)] * 1000,
        )
        await conn.execute("UPDATE purchases SET state = 'submission_unknown'")
    statements = []
    async with db.connection() as conn:
        await conn.set_trace_callback(statements.append)
    await recover_once(db, DemoPurchaser(), None)
    async with db.connection() as conn:
        await conn.set_trace_callback(None)
        plan = await (
            await conn.execute("""EXPLAIN QUERY PLAN SELECT id FROM work_items
            WHERE kind = 'purchase' AND claimed = 0 AND due_at <= 1 ORDER BY due_at, id LIMIT 1""")
        ).fetchall()
    assert len(statements) == 3
    assert all("idx_work_due" in row["detail"] for row in plan)


async def test_invoice_builder_reuses_bot_identity(bot, epay, monkeypatch):
    monkeypatch.setattr(
        "shop_bot.services.invoices.get_settings",
        lambda: SimpleNamespace(
            webhook=SimpleNamespace(url="https://shop.example"),
            payment=SimpleNamespace(callback_path="/callback"),
        ),
    )
    for number in range(3):
        url = await payment_url(bot, epay, name="test", order_no=str(number), amount_cents=999, currency="CNY")
        assert "audit_bot" in url
    assert sum(method.__api_method__ == "getMe" for method in bot.session.sent) == 1


async def test_claimed_wallet_task_recovers_once_after_restart(tmp_path, bot):
    db = Database(str(tmp_path / "claimed.db"))
    await db.connect()
    user = await db.upsert_user(42, "buyer")
    await db.adjust_balance(user.id, 250, "test")
    assert await db.claim_work("wallet") is not None
    assert await db.claim_work("wallet") is None
    await db.close()
    await db.connect()
    try:
        await recover_once(db, DemoPurchaser(), bot)
        assert len(bot.session.sent) == 1
        await db.close()
        await db.connect()
        # 完成过的任务不按历史流水重新创建。
        await recover_once(db, DemoPurchaser(), bot)
        assert len(bot.session.sent) == 1
    finally:
        await db.close()


async def test_trade_number_lookup_uses_topup_index(db):
    async with db.connection() as conn:
        rows = await (
            await conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM balance_topups WHERE trade_no = ?",
                ("missing",),
            )
        ).fetchall()
    assert any("idx_topups_trade_no" in row["detail"] for row in rows)


async def test_query_payment_status_does_not_reconfirm_or_wait_for_fulfillment(db, user, product, purchaser, bot):
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, purchaser, order.id)
    statements = []
    async with db.connection() as conn:
        await conn.set_trace_callback(statements.append)
    try:
        async with db.order_operation(order.id), asyncio.timeout(1):
            await cmd_query(query_message(bot, order), db, None, purchaser, bot)
    finally:
        async with db.connection() as conn:
            await conn.set_trace_callback(None)
    assert "正在履约" in bot.session.sent[-1].text
    assert not any("UPDATE" in statement or "INSERT" in statement for statement in statements)
