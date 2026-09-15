from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Product:
    id: int
    name: str
    description: str
    price_cents: int
    currency: str
    active: bool = True

    @property
    def price_text(self) -> str:
        return f"{self.price_cents / 100:.2f} {self.currency}"


@dataclass(slots=True)
class Order:
    id: int
    user_id: int
    product_id: int
    quantity: int
    amount_cents: int
    currency: str
    status: OrderStatus
    upstream_ref: str | None
    trade_no: str | None  # 支付网关交易号（EPay 回调时写入）
    payload: str | None   # 上游发货内容（卡密等），通知失败可恢复
    created_at: datetime
    updated_at: datetime

    @property
    def amount_text(self) -> str:
        return f"{self.amount_cents / 100:.2f} {self.currency}"


@dataclass(slots=True)
class User:
    id: int
    telegram_id: int
    username: str | None
    created_at: datetime
