"""在线收款网关的公共接口。新付款都经 PaymentGateway 发起，EPay 与 GMPay 各自实现。

处理器只依赖这里的接口：生成订单/充值单的付款信息，以及 /query 时向网关核对订单。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aiogram import Bot

    from ..db import Database
    from ..models import Order, Topup


class GatewayError(Exception):
    """可安全记录的网关错误，不携带密钥、签名或完整请求。"""


@dataclass(frozen=True, slots=True)
class Checkout:
    """一次在线付款的展示信息。

    EPay 只有网页收银台链接；GMPay 另有链上收款地址、精确转账数额和截止时间，
    买家可以不打开网页直接转账，trade_ref 指向本地 gmpay_trades 记录。
    """

    web_url: str
    address: str | None = None
    amount: str | None = None
    token: str | None = None
    network: str | None = None
    expires_at: float | None = None
    trade_ref: int | None = None


class PaymentGateway(Protocol):
    @property
    def currency(self) -> str: ...

    async def order_checkout(self, bot: Bot, db: Database, order: Order) -> Checkout: ...

    async def topup_checkout(self, bot: Bot, db: Database, topup: Topup, name: str) -> Checkout: ...

    async def reconcile_order(self, db: Database, order: Order) -> Order | None:
        """向网关核对待付订单；已付款则入账并返回最新订单，未付款返回 None，查询失败抛 GatewayError。"""
        ...
