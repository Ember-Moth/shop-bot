"""超时未付款订单自动关闭。

关闭只针对仍在 pending_payment 的订单，状态转换带前置状态条件，与付款并发时只有一方成功。
三道保护避免关掉买家正在付的订单：
- 订单或其补差价充值单还有有效的 GMPay 收款信息（截止后再留宽限）时暂不关闭；
- 已选在线支付的订单先向网关核对一次，已付款就入账而不关闭；
- 网关查询失败时本轮跳过，下一轮再试。
关闭后若仍有真实款项到账，回调按原币种存入买家余额，不会丢失。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..logging_config import get_logger
from ..models import OrderStatus
from .gateway import GatewayError, PaymentGateway
from .orders import OrderError

if TYPE_CHECKING:
    from ..db import Database
    from .operations import RuntimeState

logger = get_logger(__name__)

SWEEP_INTERVAL_SECONDS = 60
TRADE_GRACE_SECONDS = 120  # 收款信息截止后再等两分钟，留给最后一刻的转账和回调
MAX_PER_SWEEP = 500
SKIP_WARNING_INTERVAL_SECONDS = 1800  # 网关长期不支持查单时，跳过告警最多半小时记一次


def _utc_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S")


async def close_expired_orders(
    db: Database,
    gateway: PaymentGateway | None,
    *,
    timeout_seconds: float,
    now: float | None = None,
    batch: int = 50,
) -> tuple[int, int]:
    """关闭超时的待付订单，返回 (关闭数, 因网关查询失败跳过数)。"""
    now = time.time() if now is None else now
    cutoff = _utc_text(now - timeout_seconds)
    closed = skipped = seen = 0
    after_id = 0
    while seen < MAX_PER_SWEEP:
        candidates = await db.orders.list_expirable(cutoff, after_id=after_id, limit=batch)
        if not candidates:
            break
        for order in candidates:
            seen += 1
            after_id = order.id
            if await db.gmpay.has_active_for_order(order.id, now=now, grace=TRADE_GRACE_SECONDS):
                continue
            if order.payment_method == "epay" and gateway is not None:
                try:
                    if await gateway.reconcile_order(db, order) is not None:
                        continue  # 核对发现已付款，已入账
                except GatewayError, OrderError:
                    skipped += 1
                    continue
            expired = await db.orders.transition_order(
                order.id,
                OrderStatus.EXPIRED,
                from_status=OrderStatus.PENDING_PAYMENT,
                note="auto-closed: payment timeout",
            )
            if expired is not None:
                closed += 1
                logger.info("order auto-closed after payment timeout", extra={"order_id": order.id})
        if len(candidates) < batch:
            break
    return closed, skipped


async def order_expiry_loop(
    db: Database,
    gateway: PaymentGateway | None,
    timeout_seconds: float,
    runtime: RuntimeState | None = None,
) -> None:
    last_skip_warning = float("-inf")
    while True:
        if runtime is not None:
            runtime.beat("expiry")
        try:
            closed, skipped = await close_expired_orders(db, gateway, timeout_seconds=timeout_seconds)
        except Exception as exc:
            logger.warning("order auto-close sweep failed", extra={"error": type(exc).__name__})
        else:
            if skipped and time.monotonic() - last_skip_warning >= SKIP_WARNING_INTERVAL_SECONDS:
                last_skip_warning = time.monotonic()
                logger.warning("order auto-close skipped %d orders: payment gateway query unavailable", skipped)
            if closed:
                logger.info("auto-closed %d unpaid orders", closed)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
