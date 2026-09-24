"""仓储基类。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Database


class Repository:
    """仓储不持有连接：所有读写都经 Database 的连接锁、事务与订单锁执行。

    需要跨聚合的原子操作（收款补入钱包、绑定上游单撤销旧交付等）由调用方仓储
    持有事务，把 ``conn`` 传给其他仓储的 ``*(conn, ...)`` 事务内方法。
    """

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db
