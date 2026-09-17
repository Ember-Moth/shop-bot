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
    REFUNDED = "refunded"  # 履约失败，已退款到买家余额（终态，不再履约/取消）


class PurchaseState(StrEnum):
    """采购状态（docs/reseller-bot-development.md 5.3），与订单收款状态分开保存。"""

    READY = "ready"  # 已确认付款，尚未提交上游
    SUBMITTING = "submitting"  # 已记录提交意图，等待创建响应
    SUBMISSION_UNKNOWN = "submission_unknown"  # 上游可能已创建/扣款，无可靠 ID；停止自动重购
    UPSTREAM_PENDING = "upstream_pending"  # 已存上游 _id，业务未完成；只查询不重复创建
    AWAITING_KYC = "awaiting_kyc"  # INR/强制 KYC 订单待买家补交证件
    KYC_SUBMITTED = "kyc_submitted"  # 证件已提交，等待审核释放
    AWAITING_DISPATCH = "awaiting_dispatch"  # 实体 SIM 已受理，物流未确认；不自动记为已发货
    FULFILLED = "fulfilled"  # 货品已确认并持久化
    REJECTED = "rejected"  # 上游明确拒绝；订单自动退款到买家余额并关闭（终态）
    REFUND_PENDING = "refund_pending"  # 已确认应退款；中断后继续同币种退款
    REFUNDED = "refunded"  # 退款已落账，所有采购操作停止


@dataclass(slots=True)
class Product:
    id: int
    name: str
    description: str
    price_cents: int
    currency: str = "USD"
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
    input_iccid: str | None = None  # 激活目标 ICCID / 充值备选
    input_msisdn: str | None = None  # 充值手机号
    input_days: int | None = None  # 按日套餐天数
    input_sku: str | None = None  # 下单时锁定的上游 SKU（防止商品后续变更影响采购）
    input_request_type: str | None = None  # 下单时锁定的业务类型
    input_plan_id: str | None = None  # 下单时锁定的上游套餐 ID（交付/绑定核验依据）
    payment_method: str | None = None  # epay / balance；生成收银台链接前锁定渠道

    @property
    def amount_text(self) -> str:
        return f"{self.amount_cents / 100:.2f} {self.currency}"


@dataclass(slots=True)
class User:
    id: int
    telegram_id: int
    username: str | None
    created_at: datetime
    balance_cents: int = 0  # 旧接口的 CNY 余额镜像；收付使用按币种的 wallet_balances


class TopupState(StrEnum):
    PENDING = "pending"  # 充值单已创建，等待支付
    PAID = "paid"  # 已到账（幂等终点，只入账一次）


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
    kyc_documents: str | None  # 账户级强制 KYC：建单前暂存的买家证件（JSON：字段→HTTPS URL）
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class Topup:
    id: int
    user_id: int
    amount_cents: int
    status: TopupState
    trade_no: str | None  # 支付网关交易号（到账时写入）
    created_at: datetime
    updated_at: datetime
    currency: str = "CNY"


@dataclass(slots=True)
class BalanceTransaction:
    """钱包流水（只增不改的账本）：amount_cents 正为入账、负为出账。"""

    id: int
    user_id: int
    amount_cents: int
    balance_after: int
    kind: str  # topup / purchase / adjust / refund / payment_credit
    order_id: int | None
    topup_id: int | None
    note: str | None
    created_at: datetime
    currency: str = "CNY"
