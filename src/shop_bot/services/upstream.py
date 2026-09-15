"""上游供应商 API 客户端。

`UpstreamClient` 是集成接缝：拿到上游 API 文档后，用真实 HTTP 客户端实现这个协议
（httpx 已是依赖），然后在 `main.py` 里替换即可。bot 其他部分只和协议打交道。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..logging_config import get_logger
from ..models import Order, Product

logger = get_logger(__name__)


@dataclass(slots=True)
class DeliveryResult:
    ok: bool
    upstream_ref: str | None = None
    payload: str | None = None  # 例如发给用户的卡密/许可密钥
    error: str | None = None


class UpstreamClient(Protocol):
    async def deliver(self, order: Order, product: Product) -> DeliveryResult:
        """通过上游供应商 API 履约已支付订单。"""
        ...


class StubUpstreamClient:
    """打日志模拟发货，返回假单号。

    在真实上游 API 接入前，让「下单 → 支付 → 发货」整条链路能端到端跑通。
    """

    async def deliver(self, order: Order, product: Product) -> DeliveryResult:
        ref = f"STUB-{order.id:06d}"
        logger.info(
            "stub deliver: order=%s product=%s x%s ref=%s",
            order.id,
            product.name,
            order.quantity,
            ref,
        )
        return DeliveryResult(
            ok=True,
            upstream_ref=ref,
            payload=f"[stub goods for order #{order.id}]",
        )


class HttpUpstreamClient:
    """真实集成骨架，拿到 API 文档后填充。

    鉴权方式、端点、请求体结构目前都是占位；构造函数签名是 main.py 已经知道怎么构建的。
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def deliver(self, order: Order, product: Product) -> DeliveryResult:
        raise NotImplementedError("拿到上游 API 文档后实现")
