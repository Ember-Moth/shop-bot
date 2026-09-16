"""上游采购状态机（开发方案阶段 B：采购与交付）。

规则来源 docs/reseller-bot-development.md 第 6 节：
- 提交一次：网络请求前先持久化提交意图（ready → submitting）；
  超时、断网、5xx、成功响应缺失 `_id` 一律转 submission_unknown，绝不自动重购。
- 已知 ID 只查询：upstream_pending 状态只调用详情接口。
- 货品先持久化再通知；采购与通知分别恢复。
- submission_unknown / rejected 停止自动处理，由管理员人工核对或受控重试。
"""

from __future__ import annotations

import json
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
    """KYC 是否已放行。

    - 账户/订单要求 KYC（isKycRequired=true）时，必须 isKycVerified=true 或
      kycStatus=verified 才放行——kycStatus 缺失不能当作通过（审计 P1-6）。
    - 不要求 KYC 时（isKycRequired 非 true 且 kycStatus 为空），视为不适用。
    """
    kyc_status = str(details.get("kycStatus") or "").strip().lower()
    required = details.get("isKycRequired") is True or kyc_status not in ("", "none")
    if not required:
        return True
    return details.get("isKycVerified") is True or kyc_status == "verified"


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


def _esim_content_complete(esim: dict[str, Any]) -> bool:
    # 安装必需：ICCID + LPA；二维码缺失时上游可能后补，先要求齐备避免发出不可用货品
    return bool(esim.get("iccid") and esim.get("lpa") and esim.get("qrCode"))


def _delivery_content(
    request_type: str, details: dict[str, Any], quantity: int
) -> tuple[bool, str | None, str | None]:
    """按业务类型校验交付内容完整性（审计 P1-2）。

    返回 (是否完整, payload, 缺失原因)。不完整时不交付，继续轮询等待补齐。
    activation/recharge 以成功状态为交付结果，无货品内容。
    """
    if request_type == "esim":
        esims = details.get("esims") or []
        if len(esims) != quantity:
            return False, None, f"esim count {len(esims)} != {quantity}"
        if not all(_esim_content_complete(e) for e in esims):
            return False, None, "esim missing iccid/lpa/qrCode"
        return True, build_payload(details), None
    if request_type == "voucher":
        if not details.get("voucher"):
            return False, None, "voucher content missing"
        return True, f"兑换券：{details['voucher']}", None
    # activation / recharge：成功状态即业务结果
    return True, f"业务已完成（上游状态：{details.get('status')}）", None


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
    """按订单快照建立采购任务（下单时锁定的 SKU/业务类型优先）。

    快照缺失（旧订单）时回退读取当前商品；商品缺 SKU/业务类型记录为可人工核查。
    """
    sku = order.input_sku
    request_type = order.input_request_type
    if not sku or not request_type:
        product = await db.get_product(order.product_id)
        if product is not None:
            sku = sku or product.sku
            request_type = request_type or product.request_type
    if not sku or not request_type:
        return await db.ensure_purchase(
            order.id, request_type=request_type or "unknown",
            sku=sku or f"UNMAPPED-PRODUCT-{order.product_id}", quantity=order.quantity,
        )
    return await db.ensure_purchase(order.id, request_type=request_type, sku=sku, quantity=order.quantity)


class DemoPurchaser:
    """模拟采购：直接交付 stub 货品。用于未配置 Commbitz 时的本地开发与测试。"""

    async def ensure_purchase(self, db: Database, order: Order) -> Purchase:
        return await ensure_purchase_for_order(db, order)

    async def fulfill(self, db: Database, order_id: int) -> Order | None:
        async with db.order_operation(order_id):
            order = await db.get_order(order_id)
            if order is None or order.status == OrderStatus.DELIVERED:
                # 已交付：补齐采购终态后返回（幂等收敛；人工状态不自动翻转）
                purchase = await db.get_purchase_by_order(order_id)
                if purchase is not None and purchase.state not in (
                    PurchaseState.FULFILLED, PurchaseState.REJECTED, PurchaseState.SUBMISSION_UNKNOWN,
                ):
                    await db.transition_purchase(purchase.id, PurchaseState.FULFILLED, last_error=None)
                return order
            if order.status != OrderStatus.PAID:
                return order
            purchase = await self.ensure_purchase(db, order)
            final = await db.finalize_delivery(
                order_id,
                purchase.id,
                from_purchase_state=PurchaseState.READY,
                upstream_ref=f"STUB-{order_id:06d}",
                payload=f"[stub goods for order #{order_id}]",
            )
            if final is None:
                return await db.get_order(order_id)
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
        if order is None:
            return None
        if order.status == OrderStatus.DELIVERED:
            # 历史中断窗口残留（订单已交付、采购未落终态）：幂等收敛，不影响已发内容。
            # submission_unknown/rejected 属人工核对范畴，不由收敛自动翻转。
            purchase = await db.get_purchase_by_order(order_id)
            if purchase is not None and purchase.state not in (
                PurchaseState.FULFILLED, PurchaseState.REJECTED, PurchaseState.SUBMISSION_UNKNOWN,
            ):
                await db.transition_purchase(purchase.id, PurchaseState.FULFILLED, last_error=None)
            return order
        if order.status != OrderStatus.PAID:
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
        ) and purchase.upstream_request_id is not None:
            # 提交后立即轮询一次（eSIM 通常即时出货）；仍 pending/KYC 待审则留给下次恢复。
            # awaiting_kyc 且尚无上游单（账户级 KYC 待证件）时不轮询，等重新创建。
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
        # 账户级强制 KYC：买家已补交的证件随建单一起提交（PDF 4.1）
        kyc_documents = None
        if purchase.kyc_documents:
            try:
                kyc_documents = json.loads(purchase.kyc_documents)
            except ValueError:
                logger.warning("stored kyc documents unreadable", extra={"order_id": order.id})
        try:
            created = await self.client.create_request(
                request_type=purchase.request_type,
                sku=purchase.sku,
                quantity=purchase.quantity,
                iccid=order.input_iccid,
                mobile_number=order.input_msisdn,
                days=order.input_days,
                kyc_documents=kyc_documents,
                notes=f"shop-order:{order.id}",
            )
        except CommbitzError as exc:
            # 仅 HTTP 400 且明确要求证件时才转等待证件（审计 P1-3：500/未知错误
            # 携带相同消息时不得允许重购，必须保持 submission_unknown）
            if (
                exc.status_code == 400
                and "kycdocuments is mandatory" in str(exc).lower()
            ):
                logger.warning("purchase requires kyc documents before creation", extra={"order_id": order.id})
                await db.transition_purchase(
                    purchase.id, PurchaseState.AWAITING_KYC, from_state=PurchaseState.SUBMITTING,
                    last_error=str(exc),
                )
            elif exc.definite_rejection:
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
        # _track_kyc 可能已推进状态，取最新值做后续转换基准（审计 P2 状态收敛）
        purchase = await db.get_purchase_by_order(order.id) or purchase
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
        if purchase.request_type == "physical":
            # 实体 SIM：上游受理成功 ≠ 已发货；转人工物流确认（开发方案第 3 节）
            await db.transition_purchase(
                purchase.id, PurchaseState.AWAITING_DISPATCH, from_state=purchase.state, last_error=None,
            )
            return
        complete, payload, reason = _delivery_content(purchase.request_type, details, purchase.quantity)
        if not complete:
            # 空数组/缺安装信息/缺券面：等待上游补齐，绝不以"业务已完成"替代货品（审计 P1-2）
            logger.warning(
                "purchase delivery content incomplete, waiting",
                extra={"order_id": order.id, "upstream_ref": purchase.upstream_request_id, "error": reason},
            )
            return
        assert payload is not None
        # 同一事务内完成订单交付与采购终态，中断后可由恢复循环收敛（审计 P2）
        await db.finalize_delivery(
            order.id,
            purchase.id,
            from_purchase_state=purchase.state,
            upstream_ref=purchase.upstream_request_id,
            payload=payload,
        )
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
        """买家补交 KYC 证件，覆盖两种场景（审计 P1-6）：

        - 已有上游单号（INR 流程）：调用订单 KYC 接口上传/提交链接。
        - 尚未建单（账户级强制 KYC，创建被 400 拒绝）：证件暂存到采购记录，
          转回 ready，下次提交创建请求时随单携带 kycDocuments。
        规则 7：上传失败不改状态，可重试；"已审核过"视为通过继续推进。
        """
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is None:
            return False, "该订单没有采购记录"
        if purchase.state not in (PurchaseState.AWAITING_KYC, PurchaseState.KYC_SUBMITTED):
            return False, f"采购状态为 {purchase.state.value}，当前不需要提交证件"

        if purchase.upstream_request_id is None:
            # 未建单：暂存证件到采购记录，回到 ready 等待带证件重新创建
            if files:
                return False, "尚未生成上游订单，请提供证件图片的 HTTPS 链接（暂不支持文件直传）"
            assert documents is not None
            if not documents:
                return False, "请至少提供一个证件链接"
            updated = await db.transition_purchase(
                purchase.id, PurchaseState.READY, from_state=PurchaseState.AWAITING_KYC,
                last_error=None, set_kyc_documents=json.dumps(documents),
            )
            if updated is None:
                return False, "采购状态已变化，请重试"
            await db.add_order_note(order_id, "buyer submitted kyc documents before creation")
            return True, "证件已登记，系统会携带证件重新提交订单"

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
        await db.finalize_delivery(
            order_id,
            purchase.id,
            from_purchase_state=PurchaseState.AWAITING_DISPATCH,
            upstream_ref=purchase.upstream_request_id,
            payload="实体 SIM 已由管理员确认发出；物流信息请联系客服跟进。",
        )
        await db.add_order_note(order_id, "admin confirmed physical sim dispatch")
        return True, f"订单 #{order_id} 已确认发货并通知买家"

    async def retry_rejected(self, db: Database, order_id: int) -> tuple[bool, str]:
        """受控重试：仅「创建前被拒绝」（无上游单号）可回退 ready。

        已建单但履约失败（有 upstream_request_id）的 rejected 绝不能重试——
        重新创建会造成重复扣款，且丢失原单关联（审计 P1-3）。
        """
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is None:
            return False, "该订单没有采购记录"
        if purchase.state != PurchaseState.REJECTED:
            return False, f"采购状态为 {purchase.state.value}，只有 rejected 可以重试"
        if purchase.upstream_request_id is not None:
            return False, (
                f"该订单已存在上游单 {purchase.upstream_request_id}，不能重新购买；"
                "请人工核对上游订单状态后处理"
            )
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
        # 上游单全局唯一：同一份货品不能绑给两个买家（审计 P1-4）
        existing = await db.get_purchase_by_upstream_request_id(upstream_request_id)
        if existing is not None and existing.order_id != order_id:
            return False, f"上游单 {upstream_request_id} 已绑定到订单 #{existing.order_id}"
        try:
            details = await self.client.get_order_details(upstream_request_id)
        except CommbitzError as exc:
            return False, f"上游查询失败：{exc}"
        # 套餐核验：使用下单时的套餐/SKU 快照，而非可能已被修改的当前商品
        order = await db.get_order(order_id)
        assert order is not None  # 前置状态检查已确认采购存在，订单必然存在
        mismatches = _verify_details(purchase, details, order)
        if mismatches:
            return False, "上游订单与本店订单不匹配：" + "；".join(mismatches)
        # 事务内复核唯一性：并发绑定同一上游单只有一个成功，不依赖唯一索引
        updated, conflict_order_id = await db.bind_upstream_request(
            purchase.id,
            from_state=PurchaseState.SUBMISSION_UNKNOWN,
            upstream_request_id=upstream_request_id,
            upstream_order_no=str(details["orderId"]) if details.get("orderId") else None,
        )
        if conflict_order_id is not None:
            return False, f"上游单 {upstream_request_id} 已绑定到订单 #{conflict_order_id}"
        if updated is None:
            return False, "采购状态已变化，请刷新后重试"
        await db.add_order_note(order_id, f"admin bound upstream request {upstream_request_id}")
        return True, f"订单 #{order_id} 已绑定上游单 {upstream_request_id}，等待交付"


def _verify_details(purchase: Purchase, details: dict[str, Any], order: Order) -> list[str]:
    """人工绑定前的归属核验（审计 P1-4）：业务类型、数量、套餐/SKU 交叉核对。

    核对依据用下单时的套餐快照（input_plan_id），不用可能已被修改的当前商品。
    无套餐快照的旧订单必须靠上游 SKU 与订单快照 SKU 交叉核对；两者皆缺时
    没有任何可确认的匹配标识，拒绝绑定（审计第三轮 P1-2）。
    """
    mismatches: list[str] = []
    expected_type = purchase.request_type
    if details.get("requestType") and str(details["requestType"]).lower() != expected_type.lower():
        mismatches.append(f"业务类型 {details['requestType']} != {expected_type}")
    if details.get("quantity") is not None and int(details["quantity"]) != purchase.quantity:
        mismatches.append(f"数量 {details['quantity']} != {purchase.quantity}")

    expected_plan = order.input_plan_id
    upstream_plan = details.get("planId")
    expected_sku = order.input_sku
    upstream_sku = details.get("sku") or ((details.get("plan") or {}).get("sku"))
    if expected_plan:
        if not upstream_plan or str(upstream_plan) != str(expected_plan):
            mismatches.append(f"套餐 {upstream_plan or '缺失'} != {expected_plan}")
    elif upstream_sku and expected_sku:
        if str(upstream_sku) != str(expected_sku):
            mismatches.append(f"SKU {upstream_sku} != {expected_sku}")
    else:
        mismatches.append("本地缺少套餐快照且上游响应无 SKU，无法核对归属")
    return mismatches
