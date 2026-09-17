import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.types import ErrorEvent, Update
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shop_bot.config import BackupSettings, OperationsSettings
from shop_bot.models import OrderStatus, PurchaseState
from shop_bot.services.backup import BackupManager
from shop_bot.services.operations import Operations, RuntimeState
from shop_bot.web.health import register_health_routes


@pytest.fixture
async def operations(db, bot, tmp_path):
    state = RuntimeState(webhook_ready=True)
    for name in ("recovery", "monitor"):
        state.tasks[name] = asyncio.create_task(asyncio.Event().wait())
        state.beat(name)
    service = Operations(
        db,
        bot,
        [700, 701],
        OperationsSettings(),
        runtime=state,
        backups=BackupManager(str(tmp_path / "test.db"), BackupSettings(enabled=False)),
    )
    yield service
    for task in state.tasks.values():
        task.cancel()
    await asyncio.gather(*state.tasks.values(), return_exceptions=True)


@pytest.fixture
async def health_client(operations):
    app = web.Application()
    register_health_routes(app, operations)
    async with TestClient(TestServer(app)) as client:
        yield client


async def test_health_ready_and_dead_worker(operations, health_client):
    response = await health_client.get("/readyz")
    assert response.status == 200
    assert all((await response.json())["checks"].values())
    task = operations.runtime.tasks["recovery"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    response = await health_client.get("/readyz")
    assert response.status == 503 and not (await response.json())["checks"]["recovery"]
    assert (await health_client.get("/healthz")).status == 200


async def test_ready_database_timeout_is_bounded_and_releases_lock(db, operations, health_client):
    operations.settings.health_timeout_seconds = 0.03
    async with db.connection():
        response = await asyncio.wait_for(health_client.get("/readyz"), timeout=2)
        assert response.status == 503 and not (await response.json())["checks"]["database"]
    assert (await health_client.get("/readyz")).status == 200


async def test_readiness_detects_stalled_worker_and_shutdown(operations, health_client):
    operations.runtime.heartbeats["recovery"] -= 1000
    assert (await health_client.get("/readyz")).status == 503
    operations.runtime.beat("recovery")
    operations.runtime.stopping = True
    assert (await health_client.get("/readyz")).status == 503


async def test_health_error_does_not_expose_exception(db, operations, health_client, monkeypatch):
    monkeypatch.setattr(db, "ping", AsyncMock(side_effect=ValueError("PRIVATE-KEY /private/database")))
    response = await health_client.get("/readyz")
    assert response.status == 503
    text = await response.text()
    assert "PRIVATE" not in text and "/private" not in text and "ValueError" not in text


async def test_alert_dedup_retry_per_recipient_and_recovery(db, user, operations, bot, monkeypatch):
    order = await db.create_order(user.id, 1, 1, 100, "USD")
    await db.transition_order(order.id, OrderStatus.PAID)
    purchase = await db.ensure_purchase(order.id, request_type="esim", sku="S", quantity=1)
    await db.transition_purchase(purchase.id, PurchaseState.SUBMISSION_UNKNOWN)
    original = bot.send_message
    fail_first = True

    async def send(chat_id, text, **kwargs):
        nonlocal fail_first
        if chat_id == 700 and fail_first:
            fail_first = False
            raise RuntimeError("unavailable")
        return await original(chat_id, text, **kwargs)

    monkeypatch.setattr(bot, "send_message", send)
    await operations.monitor_once()
    assert [m.chat_id for m in bot.session.sent] == [701]
    await operations.monitor_once()
    assert [m.chat_id for m in bot.session.sent] == [701, 700]
    # 新实例沿用数据库去重记录，模拟进程重启后的告警恢复。
    restarted = Operations(
        db,
        bot,
        [700, 701],
        operations.settings,
        runtime=operations.runtime,
        backups=operations.backups,
    )
    await restarted.monitor_once()
    assert len(bot.session.sent) == 2
    await db.transition_purchase(purchase.id, PurchaseState.UPSTREAM_PENDING, upstream_request_id="known")
    await restarted.monitor_once()
    assert len(bot.session.sent) == 4
    assert all("告警恢复" in m.text for m in bot.session.sent[-2:])
    await restarted.monitor_once()
    assert len(bot.session.sent) == 4


@pytest.mark.parametrize(
    "state, expected",
    [
        (PurchaseState.REFUND_PENDING, True),
        (PurchaseState.UPSTREAM_PENDING, True),
        (PurchaseState.AWAITING_KYC, False),
        (PurchaseState.AWAITING_DISPATCH, False),
    ],
)
async def test_stalled_orders_exclude_expected_manual_wait(db, user, state, expected):
    order = await db.create_order(user.id, 1, 1, 100, "USD")
    await db.transition_order(order.id, OrderStatus.PAID)
    purchase = await db.ensure_purchase(order.id, request_type="esim", sku="S", quantity=1)
    await db.transition_purchase(purchase.id, state)
    async with db.transaction() as conn:
        await conn.execute("UPDATE purchases SET updated_at = datetime('now', '-1 hour')")
    issues = await db.operational_issues(900, 300)
    assert ("stalled_orders" in issues) == expected


async def test_failed_backup_alert_resolves_once(operations, bot):
    operations.backups.error = "OSError"
    await operations.monitor_once()
    assert len(bot.session.sent) == 2 and all("备份失败" in m.text for m in bot.session.sent)
    await operations.monitor_once()
    assert len(bot.session.sent) == 2
    operations.backups.error = None
    await operations.monitor_once()
    assert len(bot.session.sent) == 4 and all("告警恢复" in m.text for m in bot.session.sent[-2:])


async def test_database_outage_fallback_alerts_without_database(db, operations, bot):
    await db.close()
    await operations.monitor_once()
    assert len(bot.session.sent) == 2
    await operations.monitor_once()
    assert len(bot.session.sent) == 2


async def test_handler_exception_alert_is_redacted(operations, bot, caplog):
    await operations.on_handler_error(ErrorEvent(update=Update(update_id=1), exception=ValueError("PRIVATE_SECRET")))
    await operations.monitor_once()
    assert len(bot.session.sent) == 2
    assert all("telegram_handler" in m.text and "PRIVATE_SECRET" not in m.text for m in bot.session.sent)
    assert "PRIVATE_SECRET" not in caplog.text


async def test_delivery_failure_does_not_mark_alert_as_sent(db, operations, bot):
    await db.set_alert("backup", "备份失败")
    bot.session.fail_send = True
    await operations.deliver_alerts()
    assert await db._all("SELECT * FROM alert_deliveries") == []
    bot.session.fail_send = False
    await asyncio.gather(operations.deliver_alerts(), operations.deliver_alerts())
    assert len(await db._all("SELECT * FROM alert_deliveries")) == 2
