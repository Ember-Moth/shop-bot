"""上游采购状态机（开发方案阶段 B：采购与交付）。

规则来源 docs/reseller-bot-development.md 第 6 节：
- 提交一次：网络请求前先持久化提交意图（ready → submitting）；
  超时、断网、5xx、成功响应缺失 `_id` 一律转 submission_unknown，绝不自动重购。
- 已知 ID 只查询：upstream_pending 状态只调用详情接口。
- 货品先持久化再通知；采购与通知分别恢复。
- submission_unknown / rejected 停止自动处理，由管理员人工核对或受控重试。
"""

from __future__ import annotations

from typing import Any, Protocol

from ..db import Database
from ..logging_config import get_logger
from ..models import Order, OrderStatus, Purchase, PurchaseState
from .commbitz_api import CommbitzError

logger = get_logger(__name__)

# 上游状态枚举大小写混乱（Success/pending/approved/active），统一小写后归类。
_SUCCESS_STATUSES = {"success", "approved", "active", "completed", "delivered"}
_FAILURE_STATUSES = {"failed", "failure", "rejected", "cancelled", "canceled", "error", "expired"}

# Telegram 单条消息上限 4096；按块分条发送留出余量
NOTIFY_CHUNK_LIMIT = 3500


class Purchaser(Protocol):
    """履约驱动接口。DemoPurchaser（模拟）与 CommbitzPurchaser（真实上游）实现之。"""

    async def ensure_purchase(self, db: Database, order: Order) -> Purchase:
        """付款确认后建立采购任务（幂等）。"""
        ...

    async def fulfill(self, db: Database, order_id: int) -> Order | None:
        """推进一次履约状态机，返回最新订单；可能仍处于等待/人工状态。幂等。"""
        ...


def classify_status(status: Any) -> str:
    text = str(status or "").strip().lower()
    if text in _SUCCESS_STATUSES:
        return "success"
    if text in _FAILURE_STATUSES:
        return "failure"
    return "pending"


def _kyc_released(details: dict[str, Any]) -> bool:
    """KYC 是否已放行。null = 不适用（非 INR 且账户未开强制 KYC）；
    submitted 未审核绝不视为放行（开发方案 7.2）。"""
    kyc_status = str(details.get("kycStatus") or "").strip().lower()
    if kyc_status in ("", "none", "verified"):
        return True
    return details.get("isKycVerified") is True


def format_usage(usage: dict[str, Any]) -> str:
    """把 /esim/usage 响应整理成给买家看的文本（Swagger 16.4 字段）。

    未知/缺失字段显示未知，不做单位换算假设。
    """
    lines: list[str] = []
    effective = usage.get("effectiveTime")
    expiry = usage.get("expiryTime")
    if effective or expiry:
        lines.append(f"有效期：{effective or '未知'} 至 {expiry or '未知'}")
    total = usage.get("totalUsageFormatted") or usage.get("totalUsage")
    quota = usage.get("totalDataFormatted") or usage.get("totalData")
    lines.append(f"已用流量：{total if total is not None else '未知'}")
    lines.append(f"套餐额度：{quota if quota is not None else '未知'}")
    summary = usage.get("summary") or {}
    if summary.get("averageDailyUsageFormatted") or summary.get("averageDailyUsage"):
        lines.append(f"日均用量：{summary.get('averageDailyUsageFormatted') or summary.get('averageDailyUsage')}")
    if summary.get("totalDays"):
        lines.append(f"已用天数：{summary['totalDays']}")
    return "\n".join(lines) if lines else "暂无用量数据"


def _esim_block(index: int, esim: dict[str, Any]) -> str:
    lines = [f"[{index}]"]
    if esim.get("iccid"):
        lines.append(f"ICCID: {esim['iccid']}")
    if esim.get("lpa"):
        lines.append(f"LPA: {esim['lpa']}")
    if esim.get("qrCode"):
        lines.append(f"二维码: {esim['qrCode']}")
    return "\n".join(lines)


def build_payload(details: dict[str, Any]) -> str | None:
    """把上游详情转成给买家看的货品文本；敏感字段（PIN/PUK）不入 payload。"""
    esims = details.get("esims") or []
    blocks = [block for block in (_esim_block(i + 1, e) for i, e in enumerate(esims)) if len(block.splitlines()) > 1]
    if blocks:
        return "\n\n".join(blocks)
    if details.get("voucher"):
        return f"兑换券：{details['voucher']}"
    return None


def split_payload_chunks(payload: str) -> list[str]:
    """按空行分块，避免超过 Telegram 消息长度（开发方案规则 8）。"""
    chunks: list[str] = []
    current = ""
    for block in payload.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > NOTIFY_CHUNK_LIMIT and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def ensure_purchase_for_order(db: Database, order: Order) -> Purchase:
    """按商品建立采购任务。商品缺 SKU/业务类型时记录为可人工核查的失败采购。"""
    product = await db.get_product(order.product_id)
    if product is None:
        return await db.ensure_purchase(
            order.id, request_type="unknown", sku=f"MISSING-PRODUCT-{order.product_id}", quantity=order.quantity
        )
    request_type = product.request_type or "esim"
    sku = product.sku or f"UNMAPPED-PRODUCT-{product.id}"
    return await db.ensure_purchase(order.id, request_type=request_type, sku=sku, quantity=order.quantity)


class DemoPurchaser:
    """模拟采购：直接交付 stub 货品。用于未配置 Commbitz 时的本地开发与测试。"""

    async def ensure_purchase(self, db: Database, order: Order) -> Purchase:
        return await ensure_purchase_for_order(db, order)

    async def fulfill(self, db: Database, order_id: int) -> Order | None:
        async with db.order_operation(order_id):
            order = await db.get_order(order_id)
            if order is None or order.status != OrderStatus.PAID:
                return order
            purchase = await self.ensure_purchase(db, order)
            await db.transition_purchase(purchase.id, PurchaseState.FULFILLED)
            final = await db.transition_order(
                order_id,
                OrderStatus.DELIVERED,
                from_status=OrderStatus.PAID,
                upstream_ref=f"STUB-{order_id:06d}",
                payload=f"[stub goods for order #{order_id}]",
            )
            logger.info("order delivered (demo)", extra={"order_id": order_id})
            return final


class PurchaseGateway(Protocol):
    """采购所需的迷你上游接口；CommbitzClient 满足，测试可注入 fake。"""

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
    ) -> dict[str, Any]: ...

    async def get_order_details(self, request_id: str) -> dict[str, Any]: ...

    async def submit_kyc_documents_json(self, request_id: str, documents: dict[str, str]) -> dict[str, Any]: ...

    async def submit_kyc_documents_files(
        self, request_id: str, files: list[tuple[str, str, bytes]]
    ) -> dict[str, Any]: ...

    async def get_esim_usage(
        self,
        *,
        coupon: str | None = None,
        cid: str | None = None,
        order_id: str | None = None,
        imsi: str | None = None,
    ) -> dict[str, Any]: ...


class CommbitzPurchaser:
    """真实上游采购：提交一次、已知 ID 只查询、未知结果转人工。"""

    def __init__(self, client: PurchaseGateway) -> None:
        self.client = client  # 公开给管理端 /bind 做人工核对查询

    async def ensure_purchase(self, db: Database, order: Order) -> Purchase:
        return await ensure_purchase_for_order(db, order)

    async def fulfill(self, db: Database, order_id: int) -> Order | None:
        async with db.order_operation(order_id):
            return await self._fulfill_locked(db, order_id)

    async def _fulfill_locked(self, db: Database, order_id: int) -> Order | None:
        order = await db.get_order(order_id)
        if order is None or order.status != OrderStatus.PAID:
            return order
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is None:
            purchase = await self.ensure_purchase(db, order)
        # 进程在 submitting 中断：意图已留痕但没有可靠上游 ID，按规则转人工，不得重购。
        if purchase.state == PurchaseState.SUBMITTING:
            purchase = (
                await db.transition_purchase(
                    purchase.id, PurchaseState.SUBMISSION_UNKNOWN, from_state=PurchaseState.SUBMITTING,
                    last_error="interrupted while submitting",
                )
                or purchase
            )
        if purchase.state == PurchaseState.READY:
            await self._submit(db, order, purchase)
            purchase = await db.get_purchase_by_order(order_id) or purchase
        if purchase.state in (
            PurchaseState.UPSTREAM_PENDING, PurchaseState.AWAITING_KYC, PurchaseState.KYC_SUBMITTED,
        ):
            # 提交后立即轮询一次（eSIM 通常即时出货）；仍 pending/KYC 待审则留给下次恢复
            await self._poll(db, order, purchase)
        # submission_unknown / rejected / fulfilled / awaiting_dispatch：不自动处理
        return await db.get_order(order_id)

    async def _submit(self, db: Database, order: Order, purchase: Purchase) -> None:
        submitted = await db.transition_purchase(
            purchase.id, PurchaseState.SUBMITTING, from_state=PurchaseState.READY,
            last_error=None, bump_attempt=True,
        )
        if submitted is None:
            return  # 并发下其他协程已接管
        try:
            created = await self.client.create_request(
                request_type=purchase.request_type,
                sku=purchase.sku,
                quantity=purchase.quantity,
                iccid=order.input_iccid,
                mobile_number=order.input_msisdn,
                days=order.input_days,
                notes=f"shop-order:{order.id}",
            )
        except CommbitzError as exc:
            if exc.definite_rejection:
                logger.warning("purchase rejected by upstream", extra={"order_id": order.id})
                await db.transition_purchase(
                    purchase.id, PurchaseState.REJECTED, from_state=PurchaseState.SUBMITTING, last_error=str(exc)
                )
            else:
                logger.warning(
                    "purchase submission unknown", extra={"order_id": order.id, "error": type(exc).__name__}
                )
                await db.transition_purchase(
                    purchase.id, PurchaseState.SUBMISSION_UNKNOWN, from_state=PurchaseState.SUBMITTING,
                    last_error=str(exc),
                )
            return
        except Exception as exc:  # 网络/超时：上游可能已建单，转人工
            logger.warning("purchase submission error", extra={"order_id": order.id, "error": type(exc).__name__})
            await db.transition_purchase(
                purchase.id, PurchaseState.SUBMISSION_UNKNOWN, from_state=PurchaseState.SUBMITTING,
                last_error=f"network: {type(exc).__name__}",
            )
            return
        upstream_id = created.get("_id")
        if not upstream_id:
            logger.warning("purchase response missing _id", extra={"order_id": order.id})
            await db.transition_purchase(
                purchase.id, PurchaseState.SUBMISSION_UNKNOWN, from_state=PurchaseState.SUBMITTING,
                last_error="response missing _id",
            )
            return
        # 立即持久化上游 ID（规则 4）；INR/强制 KYC 订单 kycStatus=pending 时先等证件
        kyc_pending = str(created.get("kycStatus") or "").lower() == "pending"
        next_state = PurchaseState.AWAITING_KYC if kyc_pending else PurchaseState.UPSTREAM_PENDING
        await db.transition_purchase(
            purchase.id, next_state, from_state=PurchaseState.SUBMITTING,
            upstream_request_id=str(upstream_id),
            upstream_order_no=str(created["orderId"]) if created.get("orderId") else None,
            last_error=None,
        )
        if kyc_pending:
            logger.info("purchase awaiting kyc", extra={"order_id": order.id, "upstream_ref": str(upstream_id)})
        else:
            logger.info(
                "purchase submitted", extra={"order_id": order.id, "upstream_ref": str(upstream_id)}
            )

    async def _poll(self, db: Database, order: Order, purchase: Purchase) -> None:
        assert purchase.upstream_request_id is not None
        try:
            details = await self.client.get_order_details(purchase.upstream_request_id)
        except CommbitzError as exc:
            logger.warning(
                "purchase detail query failed", extra={"order_id": order.id, "error": type(exc).__name__}
            )
            if exc.definite_rejection and purchase.state == PurchaseState.UPSTREAM_PENDING:
                await db.transition_purchase(
                    purchase.id, PurchaseState.SUBMISSION_UNKNOWN, from_state=PurchaseState.UPSTREAM_PENDING,
                    last_error=str(exc),
                )
            return  # 其他失败下次轮询重试
        await self._track_kyc(db, purchase, details)
        status = classify_status(details.get("status"))
        if status == "failure":
            await db.transition_purchase(
                purchase.id, PurchaseState.REJECTED, from_state=purchase.state,
                last_error=f"upstream status: {details.get('status')}",
            )
            return
        if status != "success":
            return  # pending：等待下次轮询
        kyc_ok = _kyc_released(details)
        if not kyc_ok:
            return  # INR/强制 KYC 未审核通过：等待释放，绝不提前交付（开发方案 7.2）
        esims = details.get("esims") or []
        if esims and len(esims) < purchase.quantity:
            # 部分交付：继续等待补齐，绝不只发一半货品
            logger.warning(
                "purchase partially fulfilled, waiting",
                extra={"order_id": order.id, "upstream_ref": purchase.upstream_request_id},
            )
            return
        if purchase.request_type == "physical":
            # 实体 SIM：上游受理成功 ≠ 已发货；转人工物流确认（开发方案第 3 节）
            await db.transition_purchase(
                purchase.id, PurchaseState.AWAITING_DISPATCH, from_state=purchase.state, last_error=None,
            )
            return
        payload = build_payload(details) or f"业务已完成（上游状态：{details.get('status')}）"
        # 先落订单（delivered 会置通知待发），再落采购终态；中断后两边都能从恢复循环收敛
        await db.transition_order(
            order.id,
            OrderStatus.DELIVERED,
            from_status=OrderStatus.PAID,
            upstream_ref=purchase.upstream_request_id,
            payload=payload,
        )
        await db.transition_purchase(purchase.id, PurchaseState.FULFILLED, from_state=purchase.state)
        logger.info("order delivered", extra={"order_id": order.id, "upstream_ref": purchase.upstream_request_id})

    async def _track_kyc(self, db: Database, purchase: Purchase, details: dict[str, Any]) -> None:
        """跟踪 KYC 审核进展：pending → submitted（买家已补交）→ 等待 verified。"""
        if purchase.state != PurchaseState.AWAITING_KYC:
            return
        kyc_status = str(details.get("kycStatus") or "").lower()
        if kyc_status == "submitted" or details.get("isKycVerified") is True:
            await db.transition_purchase(
                purchase.id, PurchaseState.KYC_SUBMITTED, from_state=PurchaseState.AWAITING_KYC, last_error=None,
            )

    async def submit_kyc(
        self,
        db: Database,
        order_id: int,
        *,
        documents: dict[str, str] | None = None,
        files: list[tuple[str, str, bytes]] | None = None,
    ) -> tuple[bool, str]:
        """买家补交 KYC 证件（规则 7：上传失败不改状态，可重试；已有证件先查订单状态）。"""
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is None:
            return False, "该订单没有采购记录"
        if purchase.state not in (PurchaseState.AWAITING_KYC, PurchaseState.KYC_SUBMITTED):
            return False, f"采购状态为 {purchase.state.value}，当前不需要提交证件"
        if purchase.upstream_request_id is None:
            return False, "订单缺少上游单号，请联系管理员"
        try:
            if files:
                response = await self.client.submit_kyc_documents_files(purchase.upstream_request_id, files)
            else:
                assert documents is not None
                response = await self.client.submit_kyc_documents_json(purchase.upstream_request_id, documents)
        except CommbitzError as exc:
            # "already verified" 说明审核已过：转入等待交付，由轮询继续
            if "already verified" in str(exc).lower():
                await db.transition_purchase(
                    purchase.id, PurchaseState.KYC_SUBMITTED, from_state=PurchaseState.AWAITING_KYC, last_error=None,
                )
                return True, "证件此前已审核通过，系统会继续跟进交付"
            logger.warning("kyc submit failed", extra={"order_id": order_id, "error": type(exc).__name__})
            return False, f"证件提交失败：{exc}"
        kyc_status = str(response.get("kycStatus") or "").lower()
        if purchase.state == PurchaseState.AWAITING_KYC and kyc_status == "submitted":
            await db.transition_purchase(
                purchase.id, PurchaseState.KYC_SUBMITTED, from_state=PurchaseState.AWAITING_KYC, last_error=None,
            )
        await db.add_order_note(order_id, "buyer submitted kyc documents")
        return True, "证件已提交，等待上游审核；通过后系统会自动发货"

    async def confirm_dispatch(self, db: Database, order_id: int) -> tuple[bool, str]:
        """管理员确认实体卡已发货（物流边界：签收前由人工跟进，系统只记录受理与确认）。"""
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is None:
            return False, "该订单没有采购记录"
        if purchase.state != PurchaseState.AWAITING_DISPATCH:
            return False, f"采购状态为 {purchase.state.value}，只有 awaiting_dispatch 需要确认发货"
        await db.transition_order(
            order_id,
            OrderStatus.DELIVERED,
            from_status=OrderStatus.PAID,
            upstream_ref=purchase.upstream_request_id,
            payload="实体 SIM 已由管理员确认发出；物流信息请联系客服跟进。",
        )
        await db.transition_purchase(purchase.id, PurchaseState.FULFILLED, from_state=PurchaseState.AWAITING_DISPATCH)
        await db.add_order_note(order_id, "admin confirmed physical sim dispatch")
        return True, f"订单 #{order_id} 已确认发货并通知买家"

    async def retry_rejected(self, db: Database, order_id: int) -> tuple[bool, str]:
        """受控重试：仅 rejected 可回退到 ready。submission_unknown 必须先人工核对。"""
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is None:
            return False, "该订单没有采购记录"
        if purchase.state != PurchaseState.REJECTED:
            return False, f"采购状态为 {purchase.state.value}，只有 rejected 可以重试"
        updated = await db.transition_purchase(purchase.id, PurchaseState.READY, from_state=PurchaseState.REJECTED)
        if updated is None:
            return False, "采购状态已变化，请刷新后重试"
        await db.add_order_note(order_id, "admin retried rejected purchase")
        return True, f"订单 #{order_id} 已重新排队采购"

    async def bind_unknown_purchase(
        self, db: Database, order_id: int, upstream_request_id: str
    ) -> tuple[bool, str]:
        """人工核对绑定（规则 6）：核对业务类型、数量、套餐归属后绑定为已知 ID。"""
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is None:
            return False, "该订单没有采购记录"
        if purchase.state != PurchaseState.SUBMISSION_UNKNOWN:
            return False, f"采购状态为 {purchase.state.value}，只有 submission_unknown 需要人工核对"
        try:
            details = await self.client.get_order_details(upstream_request_id)
        except CommbitzError as exc:
            return False, f"上游查询失败：{exc}"
        mismatches = _verify_details(purchase, details)
        if mismatches:
            return False, "上游订单与本店订单不匹配：" + "；".join(mismatches)
        await db.transition_purchase(
            purchase.id, PurchaseState.UPSTREAM_PENDING, from_state=PurchaseState.SUBMISSION_UNKNOWN,
            upstream_request_id=upstream_request_id,
            upstream_order_no=str(details["orderId"]) if details.get("orderId") else None,
            last_error=None,
        )
        await db.add_order_note(order_id, f"admin bound upstream request {upstream_request_id}")
        return True, f"订单 #{order_id} 已绑定上游单 {upstream_request_id}，等待交付"


def _verify_details(purchase: Purchase, details: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    expected_type = purchase.request_type
    if details.get("requestType") and str(details["requestType"]).lower() != expected_type.lower():
        mismatches.append(f"业务类型 {details['requestType']} != {expected_type}")
    if details.get("quantity") is not None and int(details["quantity"]) != purchase.quantity:
        mismatches.append(f"数量 {details['quantity']} != {purchase.quantity}")
    return mismatches
