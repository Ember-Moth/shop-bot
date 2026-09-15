"""EPay（易支付）支付网关客户端。

协议要点：
- 创建支付：GET submit.php，参数带 MD5 签名
- 异步回调：POST notify_url，form-urlencoded，带签名验证
- 查询订单：GET api.php?act=order，返回 JSON

金额单位：元，保留两位小数（下游转成「分」存库）。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from ..logging_config import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class EPayConfig:
    pid: str      # 商户 ID
    key: str      # 商户密钥
    url: str      # 网关地址，例如 https://pay.example.com
    type: str = "alipay"  # 默认支付方式


@dataclass(slots=True)
class EPayOrder:
    name: str         # 商品名称
    order_no: str     # 商户订单号（我们的 order_id）
    amount: float     # 金额，元
    notify_url: str   # 异步回调地址
    return_url: str   # 同步跳转地址


@dataclass(slots=True)
class EPayQueryResult:
    trade_no: str
    order_no: str
    money: str
    paid: bool
    message: str


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()  # noqa: S324  EPay 协议要求 MD5


def _create_sign(params: dict[str, str], key: str) -> str:
    """EPay 签名：按 key 排序拼接，末尾加商户密钥，MD5 摘要。"""
    filtered = {k: v for k, v in params.items() if v and k not in ("sign", "sign_type")}
    query = urlencode(sorted(filtered.items()))
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
        self._http = httpx.AsyncClient(timeout=5.0)

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
        """主动查询订单支付状态。"""
        base = self._config.url.rstrip("/")
        resp = await self._http.get(
            f"{base}/api.php",
            params={
                "act": "order",
                "pid": self._config.pid,
                "key": self._config.key,
                "out_trade_no": order_no,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 1:
            raise RuntimeError(f"EPay query failed: code={data.get('code')}")
        return EPayQueryResult(
            trade_no=data.get("trade_no", ""),
            order_no=data.get("out_trade_no", ""),
            money=data.get("money", ""),
            paid=data.get("status") == 1,
            message=data.get("msg", ""),
        )

    def verify_callback(self, params: dict[str, str]) -> bool:
        """验证异步回调的签名。"""
        return _verify_sign(params, self._config.key)

    def parse_callback(self, params: dict[str, str]) -> dict[str, Any]:
        """解析回调参数，返回标准化字段。

        回调是 form-urlencoded，字段名和 EPay 协议一致：
        - out_trade_no: 商户订单号
        - trade_no: 网关交易号
        - money: 金额（元，字符串）
        - trade_status: TRADE_SUCCESS 表示支付成功
        - type: 支付方式
        """
        return {
            "order_no": params.get("out_trade_no", ""),
            "trade_no": params.get("trade_no", ""),
            "money": params.get("money", ""),
            "trade_status": params.get("trade_status", ""),
            "type": params.get("type", ""),
            "paid": params.get("trade_status") == "TRADE_SUCCESS",
        }
