"""Commbitz 分销商 API 客户端（只读部分）。

协议整理见 docs/commbitz-api-integration.md。当前实现鉴权（令牌缓存/刷新）、
目录查询与订单详情查询。采购提交（POST /v1/request）按开发方案
（docs/reseller-bot-development.md 规则 3/10）要求，等采购记录与
人工核对机制就绪后再接入，因此本客户端刻意不提供任何会创建上游订单的方法。

安全约定：错误信息只包含 HTTP 状态、接口路径与上游 message 字段，
不携带密钥、令牌或请求体。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..logging_config import get_logger

logger = get_logger(__name__)

UAT_BASE_URL = "https://api-uat.commbitz.com/distributor-api"
LIVE_BASE_URL = "https://api-cb.commbitz.com/distributor-api"

# 令牌到期前提前刷新的余量；文档建议在到期前刷新（例如每 50 分钟）
TOKEN_REFRESH_MARGIN_SECONDS = 120.0

PAGE_LIMIT = 100  # /v1/plans 单页上限（文档规定最大 100）


class CommbitzError(Exception):
    """上游请求失败。消息已脱敏，不含凭据。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def definite_rejection(self) -> bool:
        """4xx（除鉴权/超时/限流）代表上游明确拒绝本次请求，重试无意义。"""
        return (
            self.status_code is not None
            and 400 <= self.status_code < 500
            and self.status_code not in (401, 408, 429)
        )


def base_url_for(environment: str) -> str:
    env = environment.strip().lower()
    if env == "uat":
        return UAT_BASE_URL
    if env == "live":
        return LIVE_BASE_URL
    raise CommbitzError(f"unknown upstream environment: {environment!r}")


@dataclass(slots=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # time.monotonic() 时间刻度


@dataclass(slots=True)
class Plan:
    plan_id: str
    sku: str
    name: str
    sim_category: str
    plan_is_for: int
    currency: str
    retail_price: float | None = None
    override_price: float | None = None


def parse_plan(raw: dict[str, Any]) -> Plan:
    """从 /v1/plans 列表项解析套餐。报价字段优先级未定义，全部保留由人工定价。"""
    if "_id" not in raw:
        raise CommbitzError("plan response missing _id")
    pricing = raw.get("pricing") or {}
    currency_obj = pricing.get("currency") or {}
    return Plan(
        plan_id=str(raw["_id"]),
        sku=str(raw.get("sku") or ""),
        name=str(raw.get("name") or ""),
        sim_category=str(raw.get("simCategory") or ""),
        plan_is_for=int(raw.get("planIsFor", -1)),
        currency=str(currency_obj.get("code") or ""),
        retail_price=pricing.get("retailPrice"),
        override_price=pricing.get("overridePrice"),
    )


class CommbitzClient:
    """带令牌缓存与自动刷新的异步客户端。协程并发由锁协调，同一次刷新只发一个请求。"""

    def __init__(self, base_url: str, api_key: str, secret_key: str, timeout: float = 15.0) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self._api_key = api_key
        self._secret_key = secret_key
        self._tokens: Tokens | None = None
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    # ---- 鉴权 ----

    def _save_tokens(self, payload: dict[str, Any]) -> None:
        self._tokens = Tokens(
            access_token=str(payload["accessToken"]),
            refresh_token=str(payload["refreshToken"]),
            expires_at=time.monotonic() + float(payload.get("expiresIn", 3600)) - TOKEN_REFRESH_MARGIN_SECONDS,
        )

    async def _get_token(self) -> None:
        resp = await self._http.post("/v1/get-token", json={"apiKey": self._api_key, "secretKey": self._secret_key})
        self._save_tokens(self._unwrap(resp)["data"])

    async def _refresh(self) -> None:
        assert self._tokens is not None
        resp = await self._http.post("/v1/refresh-token", json={"refreshToken": self._tokens.refresh_token})
        self._save_tokens(self._unwrap(resp)["data"])

    async def _authenticate(self) -> None:
        # refresh token 7 天有效，优先刷新；失败（过期/撤销）回退到 get-token
        if self._tokens is not None and self._tokens.refresh_token:
            try:
                await self._refresh()
            except CommbitzError:
                logger.warning("refresh-token failed, falling back to get-token")
            else:
                return
        await self._get_token()

    async def _ensure_token(self) -> str:
        if self._tokens is not None and time.monotonic() < self._tokens.expires_at:
            return self._tokens.access_token
        async with self._token_lock:
            # 双重检查：等锁期间其他协程可能已完成刷新
            if self._tokens is not None and time.monotonic() < self._tokens.expires_at:
                return self._tokens.access_token
            await self._authenticate()
            assert self._tokens is not None
            return self._tokens.access_token

    async def _force_refresh(self) -> str:
        """服务端 401 后强制换新令牌。"""
        async with self._token_lock:
            await self._authenticate()
            assert self._tokens is not None
            return self._tokens.access_token

    # ---- 请求与响应 ----

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict[str, Any]:
        request = resp.request
        if resp.status_code >= 400:
            message = ""
            try:
                message = str(resp.json().get("message", ""))[:200]
            except ValueError:
                pass
            detail = f": {message}" if message else ""
            raise CommbitzError(
                f"commbitz HTTP {resp.status_code} {request.method} {request.url.path}{detail}",
                status_code=resp.status_code,
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise CommbitzError(f"commbitz non-JSON response from {request.url.path}") from exc
        if not isinstance(body, dict):
            raise CommbitzError(f"commbitz unexpected response shape from {request.url.path}")
        return body

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self._http.request(method, path, params=params, json=json, headers=headers)
        if resp.status_code == 401:
            # 令牌在服务端提前失效：强制刷新后重试一次；再失败由 _unwrap 抛错。
            # 401 发生在业务处理之前，这里重试不构成重复提交。
            headers = {"Authorization": f"Bearer {await self._force_refresh()}"}
            resp = await self._http.request(method, path, params=params, json=json, headers=headers)
        return self._unwrap(resp)

    # ---- 目录与详情（只读） ----

    async def get_regional_plan_types(self, region_name: str | None = None) -> list[dict[str, Any]]:
        params = {"regionName": region_name} if region_name else None
        body = await self._request("GET", "/v1/regional-plan-types", params=params)
        return list(body["data"]["regionalPlanTypes"])

    async def get_countries(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "/v1/countries")
        return list(body["data"]["countries"])

    async def get_plans(self, page: int = 1, **filters: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        query = {"page": page, "limit": PAGE_LIMIT, **{k: v for k, v in filters.items() if v is not None}}
        body = await self._request("GET", "/v1/plans", params=query)
        data = body["data"]
        return list(data.get("plans") or []), dict(data.get("pagination") or {})

    async def get_all_plans(self, **filters: Any) -> list[dict[str, Any]]:
        """拉取全部已分配套餐（自动翻页）。"""
        plans: list[dict[str, Any]] = []
        page = 1
        while True:
            batch, pagination = await self.get_plans(page=page, **filters)
            plans.extend(batch)
            if not batch or not pagination.get("hasNextPage"):
                return plans
            page += 1

    async def get_plan_detail(self, plan_id: str) -> dict[str, Any]:
        # 该接口响应是 {success, message, data}，没有 statusCode 外层
        body = await self._request("GET", f"/v1/plans/{plan_id}")
        return dict(body["data"])

    async def get_order_details(self, request_id: str) -> dict[str, Any]:
        """查询单据详情。业务数据在 data.data，不要递归剥离所有 data 层。"""
        body = await self._request("GET", f"/v1/details/{request_id}")
        return dict(body["data"]["data"])

    async def create_request(
        self,
        *,
        request_type: str,
        sku: str,
        quantity: int = 1,
        iccid: str | None = None,
        mobile_number: str | None = None,
        days: int | None = None,
        kyc_documents: dict[str, str] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """提交采购请求——本客户端唯一会创建上游订单的方法。

        上游没有幂等键（PDF 14.2 节）：调用方必须先持久化提交意图（开发方案规则 3），
        且不得因超时/5xx 重试本方法。客户端只做 401 鉴权重试（发生在业务处理之前，
        不构成重复提交）。响应业务数据在 data.data，成功时取 `_id` 立即持久化。

        字段按 PDF 第 6 节：activation 需 iccid；recharge 需 mobile_number（或 iccid）
        与按日套餐天数 days；esim/physical 需 quantity；账户级强制 KYC 时
        activation/esim 需 kycDocuments（HTTPS URL）。
        """
        payload: dict[str, Any] = {"requestType": request_type, "sku": sku, "quantity": quantity}
        if iccid:
            payload["iccid"] = iccid
        if mobile_number:
            payload["mobile_number"] = mobile_number
        if days is not None:
            payload["days"] = days
        if kyc_documents:
            payload["kycDocuments"] = kyc_documents
        if notes:
            payload["notes"] = notes
        body = await self._request("POST", "/v1/request", json=payload)
        return dict(body["data"]["data"])

    async def submit_kyc_documents_json(self, request_id: str, documents: dict[str, str]) -> dict[str, Any]:
        """为已有订单补交 KYC 证件（JSON 模式，证件为已上传的 HTTPS URL）。

        字段位于请求体顶层（不套 kycDocuments，PDF 8.3）；至少提供一份。
        响应 data 含 kycStatus 与可能的 esims[]。
        """
        allowed = ("passportFront", "passportBack", "visaFront")
        payload = {k: v for k, v in documents.items() if k in allowed and v}
        if not payload:
            raise CommbitzError("at least one KYC document URL must be provided")
        body = await self._request("POST", f"/v1/orders/{request_id}/kyc-documents", json=payload)
        return dict(body["data"])

    async def submit_kyc_documents_files(self, request_id: str, files: list[tuple[str, str, bytes]]) -> dict[str, Any]:
        """multipart 上传 KYC 证件文件（PDF 推荐方式，上游转存 S3）。

        files 为 (字段名, 文件名, 字节) 列表；字段名限 passportFront/passportBack/visaFront，
        至少一份。
        """
        allowed = {"passportFront", "passportBack", "visaFront"}
        upload: list[tuple[str, tuple[str, bytes]]] = []
        for field, filename, content in files:
            if field in allowed:
                upload.append((field, (filename, content)))
        if not upload:
            raise CommbitzError("at least one KYC document file must be provided")
        token = await self._ensure_token()
        resp = await self._http.post(
            f"/v1/orders/{request_id}/kyc-documents",
            files=upload,
            headers={"Authorization": f"Bearer {token}"},
        )
        return dict(self._unwrap(resp)["data"])

    async def get_esim_usage(
        self,
        *,
        coupon: str | None = None,
        cid: str | None = None,
        order_id: str | None = None,
        imsi: str | None = None,
    ) -> dict[str, Any]:
        """查询 eSIM 用量（PDF 第 9 节 + Swagger 16.4 节响应结构）。至少一个标识。

        返回平铺视图：用量明细（data.data）+ 套餐额度/设备信息（data 层）。
        """
        params = {k: v for k, v in
                  {"coupon": coupon, "cid": cid, "orderId": order_id, "imsi": imsi}.items() if v}
        if not params:
            raise CommbitzError("at least one of coupon/cid/orderId/imsi is required")
        body = await self._request("GET", "/esim/usage", params=params)
        data = body.get("data") or {}
        if not isinstance(data, dict):
            return {}
        view = dict(data.get("data") or {})
        for key in ("totalData", "totalDataFormatted", "profileInfo", "enhancedData"):
            if data.get(key) is not None:
                view[key] = data[key]
        return view
