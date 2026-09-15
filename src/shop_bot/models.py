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


class PurchaseState(StrEnum):
    """采购状态（docs/reseller-bot-development.md 5.3），与订单收款状态分开保存。"""

    READY = "ready"  # 已确认付款，尚未提交上游
    SUBMITTING = "submitting"  # 已记录提交意图，等待创建响应
    SUBMISSION_UNKNOWN = "submission_unknown"  # 上游可能已创建/扣款，无可靠 ID；停止自动重购
    UPSTREAM_PENDING = "upstream_pending"  # 已存上游 _id，业务未完成；只查询不重复创建
    FULFILLED = "fulfilled"  # 货品已确认并持久化
    REJECTED = "rejected"  # 上游明确拒绝；保留付款事实，由管理员重试/退款


@dataclass(slots=True)
class Product:
    id: int
    name: str
    description: str
    price_cents: int
    currency: str
    active: bool = True
    sku: str | None = None  # 上游 SKU（Commbitz 目录同步写入；手工商品为 None）
    upstream_plan_id: str | None = None  # 上游套餐 _id，用于交付时映射回上游请求
    request_type: str | None = None  # 上游业务类型（esim/physical 等）；人工商品需管理员指定

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
    payload: str | None  # 上游发货内容（卡密等），通知失败可恢复
    created_at: datetime
    updated_at: datetime
    notified_at: str | None = None
    notification_pending: bool = False

    @property
    def amount_text(self) -> str:
        return f"{self.amount_cents / 100:.2f} {self.currency}"


@dataclass(slots=True)
class User:
    id: int
    telegram_id: int
    username: str | None
    created_at: datetime


@dataclass(slots=True)
class Purchase:
    id: int
    order_id: int  # 唯一约束：一个本店订单最多一份采购记录
    state: PurchaseState
    request_type: str
    sku: str
    quantity: int
    upstream_request_id: str | None  # 上游 data.data._id；已有此 ID 时只查询不重复创建
    upstream_order_no: str | None  # 上游业务展示编号（DR.../AR...）
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
