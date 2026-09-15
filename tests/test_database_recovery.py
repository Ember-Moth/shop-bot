import asyncio
import sqlite3

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiosqlite.context import Result

from shop_bot.db import Database, FSMStorage
from shop_bot.models import OrderStatus
from shop_bot.services import orders
from shop_bot.services.purchasing import DemoPurchaser


async def test_different_orders_and_fsm_writes_can_run_concurrently(db, user, product):
    created = [await orders.create_order(db, user.id, product, 1) for _ in range(8)]
    storage = FSMStorage(db)
    results, _ = await asyncio.gather(
        asyncio.gather(*(orders.mark_paid(db, DemoPurchaser(), o.id) for o in created)),
        asyncio.gather(
            storage.set_state(StorageKey(bot_id=1, chat_id=42, user_id=42), "quantity"), db.upsert_user(43, "other")
        ),
    )
    assert all(o.status == OrderStatus.PAID for o in results)
    async with db.connection() as conn:
        async with conn.execute("SELECT COUNT(*) FROM order_events") as cur:
            assert (await cur.fetchone())[0] == 8


async def test_event_failure_rolls_back_status_and_goods(db, user, product):
    order = await orders.create_order(db, user.id, product, 1)
    async with db.transaction() as conn:
        await conn.execute("""CREATE TRIGGER reject_event BEFORE INSERT ON order_events
            BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated disk failure"):
        await db.transition_order(order.id, OrderStatus.DELIVERED, payload="secret goods")
    unchanged = await db.get_order(order.id)
    assert unchanged.status == OrderStatus.PENDING_PAYMENT
    assert unchanged.payload is None


async def test_cancelled_transaction_rolls_back_and_unlocks(db, user):
    entered = asyncio.Event()

    async def interrupted():
        async with db.transaction() as conn:
            await conn.execute("UPDATE users SET username = 'uncommitted' WHERE id = ?", (user.id,))
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(interrupted())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await db.get_user(user.id)).username == user.username
    assert (await db.upsert_user(43, "new")).username == "new"


async def test_other_writer_cannot_commit_half_transition(tmp_path, monkeypatch):
    path = tmp_path / "atomic.db"
    db = Database(str(path))
    await db.connect()
    try:
        user = await db.upsert_user(42, "original")
        order = await db.create_order(user.id, 1, 1, 999, "CNY")
        entered, release = asyncio.Event(), asyncio.Event()
        async with db.connection() as conn:
            original_execute = conn.execute

        async def pause_update(sql, *args, **kwargs):
            cursor = await original_execute(sql, *args, **kwargs)
            entered.set()
            await release.wait()
            return cursor

        def execute(sql, *args, **kwargs):
            if sql.lstrip().startswith("UPDATE orders SET status"):
                return Result(pause_update(sql, *args, **kwargs))
            return original_execute(sql, *args, **kwargs)

        monkeypatch.setattr(conn, "execute", execute)
        transition = asyncio.create_task(db.transition_order(order.id, OrderStatus.PAID))
        await asyncio.wait_for(entered.wait(), timeout=2)
        writer = asyncio.create_task(db.upsert_user(43, "other"))
        try:
            await asyncio.sleep(0)
            assert not writer.done()
            with sqlite3.connect(path) as observer:
                assert observer.execute("SELECT status FROM orders").fetchone()[0] == "pending_payment"
                assert observer.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == 0
        finally:
            release.set()
            await asyncio.gather(transition, writer)
        with sqlite3.connect(path) as observer:
            assert observer.execute("SELECT status FROM orders").fetchone()[0] == "paid"
            assert observer.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == 1
    finally:
        await db.close()


async def test_migration_keeps_complete_old_session_and_adds_order_columns(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, quantity INTEGER,
                amount_cents INTEGER, currency TEXT, status TEXT, upstream_ref TEXT,
                created_at TEXT, updated_at TEXT
            );
            INSERT INTO orders VALUES (1, 1, 1, 2, 1998, 'CNY', 'pending_payment', NULL, '', '');
            INSERT INTO orders VALUES (2, 1, 1, 1, 999, 'CNY', 'delivered', 'old-upstream', '', '');
            CREATE TABLE fsm_state (
                bot_id INTEGER, chat_id INTEGER, user_id INTEGER, thread_id INTEGER,
                business_connection_id TEXT, destiny TEXT DEFAULT 'default', state TEXT,
                data TEXT DEFAULT '{}', updated_at TEXT,
                PRIMARY KEY (bot_id, chat_id, user_id, thread_id, business_connection_id, destiny)
            );
            INSERT INTO fsm_state(bot_id, chat_id, user_id, state, data)
            VALUES (1,42,42,'quantity','{"product_id":1,"quantity":2}');
            INSERT INTO fsm_state(bot_id, chat_id, user_id, state, data)
            VALUES (1,42,42,'quantity','{}');
        """)
    for _ in range(2):
        db = Database(str(path))
        await db.connect()
        try:
            state = FSMStorage(db)
            key = StorageKey(bot_id=1, chat_id=42, user_id=42)
            assert await state.get_data(key) == {"product_id": 1, "quantity": 2}
            assert await state.get_state(key) == "quantity"
            await state.set_state(key, "quantity")
            async with db.connection() as conn:
                async with conn.execute("SELECT COUNT(*) FROM fsm_state") as cur:
                    row = await cur.fetchone()
                    assert row is not None and row[0] == 1
            order = await db.get_order(1)
            assert order is not None
            assert order.trade_no is None and order.payload is None and order.notified_at is None
            assert await db.list_recovery_orders() == []
        finally:
            await db.close()
