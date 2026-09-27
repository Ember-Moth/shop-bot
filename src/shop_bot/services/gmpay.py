"""epusdt GMPay 原生接口：服务端下单、HMAC-SHA256 签名、JSON 回调、状态查询与交易哈希补单。

买家在聊天里直接看到收款地址和精确转账数额，网页收银台作为备用入口。每次下单登记在
``gmpay_trades``：商户订单号带尝试序号（订单 20 为 ``20-1``、充值单 5 为 ``T5-1``），
epusdt 不允许重复使用商户订单号，也无法按商户订单号查单，所以下单结果不明的尝试直接作废，
下次付款换下一个序号。收款确认有三条路径：签名回调、到期核对、买家点「我已转账」，
入账都经 payment_receipts 幂等，重复确认不会重复记账。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from weakref import WeakValueDictionary

import httpx

from ..logging_config import get_logger
from ..models import Order, OrderStatus, Topup, TopupState
from ..money import normalize_currency
from .gateway import Checkout, GatewayError
from .payment_prompts import expire_trade_prompts

if TYPE_CHECKING:
    from aiogram import Bot

    from ..db import Database
    from .notification_transport import NotificationThrottle

logger = get_logger(__name__)

CREATE_PATH = "/payments/gmpay/v1/order/create-transaction"
CONFIG_PATH = "/payments/gmpay/v1/config"
STATUS_WAITING, STATUS_PAID, STATUS_EXPIRED = 1, 2, 3
ORDER_NOT_FOUND = 10008
REUSE_MARGIN_SECONDS = 120  # 剩余时间不足两分钟的收款信息不再复用，另开一笔
CHECK_GRACE_SECONDS = 20  # 到期后稍等 epusdt 的过期任务再核对


class GMPayError(GatewayError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def canonical_value(value: object) -> str:
    """与 epusdt 的 Go 签名一致：JSON 数字按 float64 的 strconv.FormatFloat(v, 'f', -1, 64) 输出。"""
    if isinstance(value, bool):
        raise GMPayError("boolean values cannot be signed")
    if isinstance(value, int):
        value = float(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GMPayError("non-finite numbers cannot be signed")
        if value.is_integer():
            return str(int(value))
        text = repr(value)
        return format(Decimal(text), "f") if "e" in text.lower() else text
    if isinstance(value, str):
        return value
    raise GMPayError("unsupported value in signed parameters")


def sign(params: Mapping[str, object], secret_key: str) -> str:
    """排除 signature、空值后把 key=value 按字典序排序拼接，以 secret_key 做 HMAC-SHA256。"""
    pairs = []
    for key, value in params.items():
        if key == "signature" or value is None:
            continue
        text = canonical_value(value)
        if text:
            pairs.append(f"{key}={text}")
    canonical = "&".join(sorted(pairs))
    return hmac.new(secret_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def parse_cents(value: object) -> int | None:
    """把 epusdt 返回的法币金额精确换算成分；有分以下的非零位或不是正数时返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        cents = Decimal(canonical_value(value)) * 100
    except GMPayError, InvalidOperation:
        return None
    if cents <= 0 or cents != cents.to_integral_value():
        return None
    return int(cents)


def format_amount(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


@dataclass(frozen=True, slots=True)
class GMPayConfig:
    url: str  # epusdt 地址，不含路径
    pid: str
    secret_key: str
    currency: str = "USD"
    token: str = "usdt"  # noqa: S105 - 收款币种代号，不是凭据
    network: str = "tron"
    timeout: float = 10.0


class GMPayClient:
    """epusdt HTTP 客户端。下单用表单编码，金额按原文参与签名。"""

    def __init__(self, config: GMPayConfig) -> None:
        self.config = replace(
            config,
            currency=normalize_currency(config.currency),
            token=config.token.strip().lower(),
            network=config.network.strip().lower(),
        )
        self._http = httpx.AsyncClient(base_url=config.url.rstrip("/"), timeout=config.timeout)

    @property
    def pid(self) -> str:
        return self.config.pid

    @property
    def currency(self) -> str:
        return self.config.currency

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise GMPayError("gateway unavailable") from None
        try:
            body = resp.json()
        except ValueError:
            raise GMPayError(f"gateway returned HTTP {resp.status_code}") from None
        if not isinstance(body, dict):
            raise GMPayError("unexpected gateway response")
        code = body.get("status_code")
        if resp.status_code != 200 or code != 200:
            message = str(body.get("message") or "gateway rejected the request")[:120]
            raise GMPayError(message, code if isinstance(code, int) else None)
        data = body.get("data")
        if not isinstance(data, dict):
            raise GMPayError("gateway response has no data")
        return data

    async def create_transaction(
        self, *, order_no: str, amount_cents: int, currency: str, name: str, notify_url: str, redirect_url: str
    ) -> dict[str, Any]:
        params = {
            "pid": self.pid,
            "order_id": order_no,
            "currency": currency.lower(),
            "token": self.config.token,
            "network": self.config.network,
            "amount": format_amount(amount_cents),
            "notify_url": notify_url,
            "redirect_url": redirect_url,
            "name": name,
        }
        params["signature"] = sign(params, self.config.secret_key)
        return await self._call("POST", CREATE_PATH, data=params)

    async def check_status(self, trade_id: str) -> int:
        data = await self._call("GET", f"/pay/check-status/{quote(trade_id, safe='')}")
        status = data.get("status")
        if data.get("trade_id") != trade_id or isinstance(status, bool) or not isinstance(status, int):
            raise GMPayError("unexpected status response")
        return status

    async def submit_tx_hash(self, trade_id: str, tx_hash: str) -> int:
        data = await self._call(
            "POST", f"/pay/submit-tx-hash/{quote(trade_id, safe='')}", json={"block_transaction_id": tx_hash}
        )
        status = data.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            raise GMPayError("unexpected tx hash response")
        return status

    async def supported_assets(self) -> set[tuple[str, str]]:
        data = await self._call("GET", CONFIG_PATH)
        assets = set()
        for entry in data.get("supported_assets") or []:
            if isinstance(entry, dict):
                network = str(entry.get("network", "")).lower()
                assets.update((network, str(token).lower()) for token in entry.get("tokens") or [])
        return assets

    def verify_callback(self, payload: Mapping[str, object]) -> bool:
        signature = payload.get("signature")
        if not isinstance(signature, str) or str(payload.get("pid", "")) != self.pid:
            return False
        try:
            expected = sign(payload, self.config.secret_key)
        except GMPayError:
            return False
        return hmac.compare_digest(expected.encode(), signature.encode())


def _parse_created(data: Mapping[str, Any], *, order_no: str, amount_cents: int, currency: str) -> dict[str, Any]:
    """只接受与本次请求完全对应的收款信息；任何不一致都作废这次尝试，不展示给买家。"""
    trade_id, address, token = data.get("trade_id"), data.get("receive_address"), data.get("token")
    actual, expires, url = data.get("actual_amount"), data.get("expiration_time"), data.get("payment_url")
    if data.get("order_id") != order_no or not isinstance(trade_id, str) or not trade_id.strip():
        raise GMPayError("create response does not match the request")
    if data.get("status") != STATUS_WAITING or not isinstance(address, str) or not address.strip():
        raise GMPayError("create response has no receiving address")
    if not isinstance(token, str) or not token.strip():
        raise GMPayError("create response has no token")
    if str(data.get("currency", "")).upper() != currency or parse_cents(data.get("amount")) != amount_cents:
        raise GMPayError("create response amount does not match")
    if isinstance(actual, bool) or not isinstance(actual, (int, float)) or actual <= 0:
        raise GMPayError("create response has no payable amount")
    if isinstance(expires, bool) or not isinstance(expires, int) or expires <= time.time():
        raise GMPayError("create response has no valid expiration")
    return {
        "trade_id": trade_id,
        "token": token.strip().upper(),
        "receive_address": address.strip(),
        "actual_amount": canonical_value(actual),
        "payment_url": url if isinstance(url, str) and url.startswith(("https://", "http://")) else "",
        "expires_at": float(expires),
    }


def _checkout(trade: Mapping[str, Any]) -> Checkout:
    return Checkout(
        web_url=trade["payment_url"] or "",
        address=trade["receive_address"],
        amount=trade["actual_amount"],
        token=trade["token"],
        network=trade["network"],
        expires_at=trade["expires_at"],
        trade_ref=trade["id"],
    )


class GMPayGateway:
    """PaymentGateway 的 GMPay 实现。同一订单或充值单的下单按目标串行，避免连点生成多笔收款信息。"""

    def __init__(self, client: GMPayClient, *, notify_url: str) -> None:
        self.client = client
        self.notify_url = notify_url
        self._locks: WeakValueDictionary[tuple[str, int], asyncio.Lock] = WeakValueDictionary()

    @property
    def currency(self) -> str:
        return self.client.currency

    async def order_checkout(self, bot: Bot, db: Database, order: Order) -> Checkout:
        if order.currency != self.currency:
            raise GMPayError("order currency is not accepted by the gateway")
        return await self._checkout(
            bot,
            db,
            ("order", order.id),
            amount_cents=order.amount_cents,
            currency=order.currency,
            name=f"订单 #{order.id}",
        )

    async def topup_checkout(self, bot: Bot, db: Database, topup: Topup, name: str) -> Checkout:
        if topup.currency != self.currency:
            raise GMPayError("top-up currency is not accepted by the gateway")
        return await self._checkout(
            bot, db, ("topup", topup.id), amount_cents=topup.amount_cents, currency=topup.currency, name=name
        )

    async def _checkout(
        self, bot: Bot, db: Database, target: tuple[str, int], *, amount_cents: int, currency: str, name: str
    ) -> Checkout:
        order_id = target[1] if target[0] == "order" else None
        topup_id = target[1] if target[0] == "topup" else None
        lock = self._locks.setdefault(target, asyncio.Lock())
        async with lock:
            active = await db.gmpay.active(
                order_id=order_id, topup_id=topup_id, after=time.time() + REUSE_MARGIN_SECONDS
            )
            if active is not None and active["amount_cents"] == amount_cents and active["currency"] == currency:
                return _checkout(active)
            me = await bot.me()
            trade_ref, order_no = await db.gmpay.begin(
                order_id=order_id, topup_id=topup_id, amount_cents=amount_cents, currency=currency
            )
            try:
                data = await self.client.create_transaction(
                    order_no=order_no,
                    amount_cents=amount_cents,
                    currency=currency,
                    name=name,
                    notify_url=self.notify_url,
                    redirect_url=f"https://t.me/{me.username}",
                )
                created = _parse_created(data, order_no=order_no, amount_cents=amount_cents, currency=currency)
            except BaseException as exc:
                # 超时等结果不明的情况：epusdt 可能已建单，但买家从未见过收款信息，作废即可。
                await db.gmpay.fail(trade_ref, str(exc) if isinstance(exc, GMPayError) else type(exc).__name__)
                if isinstance(exc, GMPayError):
                    logger.warning("gmpay order creation failed", extra={"error": str(exc), "order_id": target[1]})
                    raise
                if isinstance(exc, Exception):
                    raise GMPayError("gateway error") from None
                raise
            await db.gmpay.created(
                trade_ref,
                network=self.client.config.network,
                check_at=created["expires_at"] + CHECK_GRACE_SECONDS,
                **created,
            )
            trade = await db.gmpay.get(trade_ref)
            assert trade is not None
            return _checkout(trade)

    async def reconcile_order(self, db: Database, order: Order) -> Order | None:
        for trade in await db.gmpay.checkable(order.id):
            try:
                status = await self.client.check_status(trade["trade_id"])
            except GMPayError as exc:
                if exc.code == ORDER_NOT_FOUND:
                    continue  # epusdt 已没有这笔记录，继续核对其余尝试
                raise
            if status == STATUS_PAID:
                await self.apply_payment(db, trade)
                return await db.orders.get_order(order.id)
        return None

    async def apply_payment(self, db: Database, trade: Mapping[str, Any]) -> None:
        """入账一笔 epusdt 已确认的交易；回调、到期核对与买家查询共用，重复调用幂等。"""
        trade_id = trade["trade_id"]
        if trade["order_id"] is not None:
            order = await db.orders.get_order(trade["order_id"])
            if order is None or order.amount_cents != trade["amount_cents"] or order.currency != trade["currency"]:
                raise GMPayError("trade does not match its order")
            try:
                _, disposition = await db.payments.record_online_payment(order.id, trade_id)
            except ValueError:
                raise GMPayError("payment transaction conflicts with another order") from None
            if disposition == "wallet_credit":
                logger.warning("additional payment credited to wallet", extra={"order_id": order.id})
        else:
            topup = await db.wallet.get_topup(trade["topup_id"])
            if topup is None or topup.amount_cents != trade["amount_cents"] or topup.currency != trade["currency"]:
                raise GMPayError("trade does not match its top-up")
            try:
                credited = await db.wallet.complete_topup(topup.id, trade_no=trade_id)
            except ValueError:
                raise GMPayError("payment transaction conflicts with another top-up") from None
            if credited is None:
                raise GMPayError("top-up cannot be credited")
        await db.gmpay.mark_paid(trade["id"])

    async def apply_callback(self, db: Database, payload: Mapping[str, object]) -> None:
        """调用方已验签。按商户订单号找到本地交易，核对 epusdt 交易号和法币金额后入账。"""
        order_no, trade_id = payload.get("order_id"), payload.get("trade_id")
        if payload.get("status") != STATUS_PAID or not isinstance(trade_id, str) or not trade_id:
            raise GMPayError("callback is not a completed payment")
        trade = await db.gmpay.by_order_no(order_no) if isinstance(order_no, str) else None
        if trade is None:
            raise GMPayError("unknown merchant order", ORDER_NOT_FOUND)
        if trade["trade_id"] not in (None, trade_id):
            raise GMPayError("callback trade does not match the merchant order")
        if parse_cents(payload.get("amount")) != trade["amount_cents"]:
            raise GMPayError("callback amount does not match")
        if trade["trade_id"] is None:
            await db.gmpay.attach_trade_id(trade["id"], trade_id)
        await self.apply_payment(db, {**trade, "trade_id": trade_id})

    async def refresh(self, db: Database, trade: Mapping[str, Any]) -> int:
        """查询 epusdt 当前状态；已付款立即入账。"""
        status = await self.client.check_status(trade["trade_id"])
        if status == STATUS_PAID:
            await self.apply_payment(db, trade)
        return status

    async def submit_tx_hash(self, db: Database, trade: Mapping[str, Any], tx_hash: str) -> int:
        """买家提交交易哈希，由 epusdt 到链上核验这笔转账是否付给了该交易。"""
        status = await self.client.submit_tx_hash(trade["trade_id"], tx_hash)
        if status == STATUS_PAID:
            await self.apply_payment(db, trade)
        return status

    async def check_trade(
        self, db: Database, bot: Bot, trade_ref: int, throttle: NotificationThrottle | None = None
    ) -> bool:
        """到期核对：兼作回调丢失的兜底。返回 False 表示 epusdt 仍在等待，稍后再查。"""
        trade = await db.gmpay.get(trade_ref)
        if trade is None or trade["state"] != "pending":
            return True
        try:
            status = await self.client.check_status(trade["trade_id"])
        except GMPayError as exc:
            if exc.code != ORDER_NOT_FOUND:
                raise
            status = STATUS_EXPIRED
        if status == STATUS_PAID:
            await self.apply_payment(db, trade)
            return True
        if status == STATUS_EXPIRED:
            # 先改消息再记过期：限流中断后下次仍会继续处理没改完的消息。
            await expire_trade_prompts(db, bot, trade, throttle)
            await db.gmpay.mark_expired(trade_ref)
            return True
        return False

    async def trade_owner_ok(self, db: Database, trade: Mapping[str, Any], user_id: int) -> bool:
        if trade["order_id"] is not None:
            order = await db.orders.get_order(trade["order_id"])
            return order is not None and order.user_id == user_id
        topup = await db.wallet.get_topup(trade["topup_id"])
        return topup is not None and topup.user_id == user_id

    @staticmethod
    async def target_pending(db: Database, trade: Mapping[str, Any]) -> bool:
        if trade["order_id"] is not None:
            order = await db.orders.get_order(trade["order_id"])
            return order is not None and order.status == OrderStatus.PENDING_PAYMENT
        topup = await db.wallet.get_topup(trade["topup_id"])
        return topup is not None and topup.status == TopupState.PENDING
