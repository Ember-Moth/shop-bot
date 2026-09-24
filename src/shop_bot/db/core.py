"""连接与事务边界：单连接、连接锁、写事务与进程内订单锁；仓储通过属性组合。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary

import aiosqlite

from .business_notifications import migrate_business_notifications
from .repositories import (
    DeliveryRepository,
    OperationsRepository,
    OrderRepository,
    PaymentRepository,
    ProductRepository,
    PurchaseRepository,
    UserRepository,
    WalletRepository,
    WorkRepository,
)
from .schema import SCHEMA, migrate
from .work_queue import migrate_work_queue


class Database:
    """单连接数据库。所有读写通过锁保护的连接上下文，事务不能跨请求共享。

    数据访问按聚合拆到仓储属性：``users`` / ``products`` / ``orders`` / ``purchases`` /
    ``deliveries`` / ``wallet`` / ``payments`` / ``operations`` / ``work``。本类只负责
    连接生命周期、锁与事务原语，以及 ``fetch_one`` / ``fetch_all`` 原始查询入口。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._order_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self.users = UserRepository(self)
        self.products = ProductRepository(self)
        self.orders = OrderRepository(self)
        self.purchases = PurchaseRepository(self)
        self.deliveries = DeliveryRepository(self)
        self.wallet = WalletRepository(self)
        self.payments = PaymentRepository(self)
        self.operations = OperationsRepository(self)
        self.work = WorkRepository(self)

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        try:
            await self._conn.executescript(SCHEMA)
            async with self.transaction() as conn:
                await migrate(conn)
                await migrate_work_queue(conn)
                await migrate_business_notifications(conn)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """持有连接期间不得调用其他 Database 方法；业务代码使用仓储。"""
        async with self._lock:
            if self._conn is None:
                raise RuntimeError("Database.connect() must be called first")
            yield self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.connection() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                yield conn
                await conn.commit()
            except BaseException:
                # 包括任务取消；先清理事务，再允许下一个协程访问连接。
                await conn.rollback()
                raise

    @asynccontextmanager
    async def order_operation(self, order_id: int) -> AsyncIterator[None]:
        """单进程内串行处理同一订单的履约和通知，不占用数据库连接。"""
        lock = self._order_locks.setdefault(order_id, asyncio.Lock())
        async with lock:
            yield

    async def fetch_one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.connection() as conn, conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.connection() as conn, conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def ping(self) -> None:
        await self.fetch_one("SELECT 1 FROM users LIMIT 1")
