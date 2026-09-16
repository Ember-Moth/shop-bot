"""EPay（易支付）支付网关客户端。

协议要点：
- 创建支付：GET submit.php，参数带 MD5 签名
- 异步回调：GET 或 POST notify_url，带签名验证
- 查询订单：GET api.php?act=order，返回 JSON

金额单位：元，保留两位小数（下游转成「分」存库）。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from ..logging_config import get_logger
from ..models import Order

logger = get_logger(__name__)


@dataclass(slots=True)
class EPayConfig:
    pid: str  # 商户 ID
    key: str  # 商户密钥
    url: str  # 网关地址，例如 https://pay.example.com
    type: str = "alipay"  # 默认支付方式


@dataclass(slots=True)
class EPayOrder:
    name: str  # 商品名称
    order_no: str  # 商户订单号（我们的 order_id）
    amount: float  # 金额，元
    notify_url: str  # 异步回调地址
    return_url: str  # 同步跳转地址


@dataclass(slots=True)
class EPayQueryResult:
    trade_no: str
    order_no: str
    money: str
    paid: bool
    message: str
    pid: str = ""


class EPayError(Exception):
    """可安全记录的支付错误，不携带含密钥的 HTTP 请求或响应。"""


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()  # noqa: S324  EPay 协议要求 MD5


def _create_sign(params: dict[str, str], key: str) -> str:
    """EPay 签名：按 key 排序，直接拼接原始参数值，末尾加商户密钥，MD5 摘要。

    注意：EPay V1 协议要求拼接原始值，不能用 urlencode 编码。
    """
    filtered = {k: v for k, v in params.items() if v and k not in ("sign", "sign_type")}
    # 按 key 排序后直接拼接 key=value&，不做 URL 编码
    query = "&".join(f"{k}={v}" for k, v in sorted(filtered.items()))
    return _md5(query + key)


def _verify_sign(params: dict[str, str], key: str) -> bool:
    """验证回调签名，防时序攻击用常数时间比较。"""
    received = params.get("sign", "").lower()
    if not received:
        return False
    expected = _create_sign(params, key)
    if len(expected) != len(received):
        return False
    return hmac.compare_digest(expected.encode(), received.encode())


def _format_money(amount: float) -> str:
    """格式化成两位小数，和 EPay 协议对齐。"""
    return f"{amount:.2f}"


class EPayClient:
    def __init__(self, config: EPayConfig) -> None:
        self._config = config
        # V1 查询必须把商户密钥放在 URL 中，禁用第三方请求/线路调试日志。
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self._http = httpx.AsyncClient(timeout=5.0)

    @property
    def pid(self) -> str:
        """商户 ID，供回调/查询核验使用。"""
        return self._config.pid

    async def close(self) -> None:
        await self._http.aclose()

    def create_pay_url(self, order: EPayOrder) -> str:
        """生成用户跳转的支付链接。"""
        params = {
            "money": _format_money(order.amount),
            "name": order.name,
            "notify_url": order.notify_url,
            "out_trade_no": order.order_no,
            "pid": self._config.pid,
            "type": self._config.type,
            "return_url": order.return_url,
        }
        params["sign"] = _create_sign(params, self._config.key)
        params["sign_type"] = "MD5"
        base = self._config.url.rstrip("/")
        return f"{base}/submit.php?{urlencode(params)}"

    async def query_order(self, order_no: str) -> EPayQueryResult:
        """查询由本客户端商户凭据授权的订单，不向调用方暴露底层 URL。"""
        base = self._config.url.rstrip("/")
        try:
            resp = await self._http.get(
                f"{base}/api.php",
                params={"act": "order", "pid": self._config.pid, "key": self._config.key, "out_trade_no": order_no},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError, ValueError:
            raise EPayError("payment query unavailable") from None
        if not isinstance(data, dict) or data.get("code") not in (1, "1"):
            raise EPayError("payment query rejected")
        return EPayQueryResult(
            trade_no=str(data.get("trade_no") or ""),
            order_no=str(data.get("out_trade_no", "")),
            money=str(data.get("money", "")),
            paid=data.get("status") in (1, "1"),
            message="",
            # 部分 V1 网关不回传 pid；此时商户身份来自已鉴权的查询上下文。
            pid=str(data.get("pid", self._config.pid)),
        )

    def validate_payment(self, order: Order, payment: EPayQueryResult) -> None:
        """回调与主动查询共用核单规则；验签/鉴权必须在调用此方法前完成。"""
        if not payment.paid or payment.order_no != str(order.id):
            raise EPayError("payment order does not match")
        if payment.pid != self._config.pid:
            raise EPayError("payment merchant does not match")
        if not payment.trade_no.strip() or payment.trade_no != payment.trade_no.strip():
            raise EPayError("payment transaction is missing or invalid")
        if order.trade_no and order.trade_no != payment.trade_no:
            raise EPayError("payment transaction does not match")
        if order.currency != "CNY" or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", payment.money):
            raise EPayError("payment currency or amount is invalid")
        if Decimal(payment.money) <= 0 or Decimal(payment.money) * 100 != order.amount_cents:
            raise EPayError("payment amount does not match")

    def verify_callback(self, params: dict[str, str]) -> bool:
        """验证异步回调的签名。"""
        return _verify_sign(params, self._config.key)

    def parse_callback(self, params: dict[str, str]) -> EPayQueryResult:
        return EPayQueryResult(
            order_no=params.get("out_trade_no", ""),
            trade_no=params.get("trade_no", ""),
            money=params.get("money", ""),
            pid=params.get("pid", ""),
            paid=params.get("trade_status") == "TRADE_SUCCESS",
            message="",
        )
