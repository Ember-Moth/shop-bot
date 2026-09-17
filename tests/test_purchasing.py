"""Commbitz 采购状态机测试：提交一次、已知 ID 只查询、未知转人工、并发与恢复。"""

import asyncio
import logging
import sqlite3
import time

import httpx
import pytest

from shop_bot.db import Database
from shop_bot.models import OrderStatus, Product, PurchaseState
from shop_bot.services import orders
from shop_bot.services.catalog_sync import sync_catalog
from shop_bot.services.commbitz_api import CommbitzClient, Tokens
from shop_bot.services.fulfillment import notify_owner, recover_once
from shop_bot.services.purchasing import (
    CommbitzPurchaser,
    DemoPurchaser,
    _kyc_released,
    classify_status,
    split_payload_chunks,
)
from tests.fakes import FakeCommbitzGateway

UAT = "https://api-uat.commbitz.com/distributor-api"
TOKEN_RESPONSE = {
    "statusCode": 201,
    "data": {"accessToken": "acc-1", "refreshToken": "ref-1", "expiresIn": 3600},
}


def _plan_response(skus: list[str]) -> dict:
    return {
        "statusCode": 200,
        "data": {
            "plans": [
                {"_id": f"id-{s}", "sku": s, "name": s, "simCategory": "esim", "planIsFor": 2,
                 "pricing": {"currency": {"code": "USD"}}}
                for s in skus
            ],
            "pagination": {"hasNextPage": False},
        },
    }


def _create_ok(request_id="up-1", order_no="DR001"):
    return {"statusCode": 201, "data": {"success": True, "data": {"_id": request_id, "orderId": order_no,
            "status": "pending", "requestType": "esim", "quantity": 1}}}


def _details(status="Success", esims=None, quantity=1, request_type="esim", plan_id="id-US-1"):
    return {
        "statusCode": 200,
        "data": {"success": True, "data": {
            "_id": "up-1", "status": status, "requestType": request_type, "quantity": quantity,
            "planId": plan_id,
            "esims": esims if esims is not None else [],
        }},
    }


def _details_with_esims(status="Success", count=1, plan_id="id-US-1", request_type="esim", quantity=None):
    esims = [{"iccid": f"89-{i}", "lpa": f"LPA:{i}", "qrCode": f"https://qr/{i}.png"} for i in range(count)]
    body = _details(status=status, quantity=quantity if quantity is not None else count,
                    plan_id=plan_id, request_type=request_type)
    body["data"]["data"]["esims"] = esims
    return body


@pytest.fixture
async def esim_order(db, user):
    product = Product(1, "US 1GB", "", 4999, "CNY", sku="US-1", upstream_plan_id="id-US-1", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    return order


@pytest.fixture
async def commbitz_purchaser():
    client = CommbitzClient(UAT, "k", "s", timeout=5.0)
    # 令牌直接置为长期有效，跳过鉴权请求；各用例按请求顺序注册业务响应
    client._tokens = Tokens(access_token="acc", refresh_token="ref", expires_at=time.monotonic() + 3600)
    yield CommbitzPurchaser(client)
    await client.close()


def _details_mock(httpx_mock, **kwargs):
    httpx_mock.add_response(json=_details_with_esims(**kwargs))


async def test_submit_once_then_deliver_from_details(*, db, esim_order, commbitz_purchaser, httpx_mock):
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock)
    purchaser = commbitz_purchaser
    await orders.mark_paid(db, purchaser, esim_order.id, trade_no="T1")
    order = await purchaser.fulfill(db, esim_order.id)
    # 第一次 fulfill 同时完成提交和轮询交付
    assert order is not None and order.status == OrderStatus.DELIVERED
    assert order.upstream_ref == "up-1"
    assert "ICCID: 89-0" in (order.payload or "") and "LPA: LPA:0" in (order.payload or "")
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None
    assert purchase.state == PurchaseState.FULFILLED
    assert purchase.upstream_request_id == "up-1"
    assert purchase.attempts == 1
    # 重复 fulfill 幂等：不再发请求
    await purchaser.fulfill(db, esim_order.id)
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths.count("/distributor-api/v1/request") == 1
    assert paths.count("/distributor-api/v1/details/up-1") == 1


async def test_pending_upstream_stays_paid_and_polls_later(*, db, esim_order, commbitz_purchaser, httpx_mock):
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock, status="pending")
    purchaser = commbitz_purchaser
    await orders.mark_paid(db, purchaser, esim_order.id)
    order = await purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.PAID  # 等待不是失败
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.UPSTREAM_PENDING
    # 下次轮询成功交付
    _details_mock(httpx_mock)
    order = await purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.DELIVERED


async def test_network_failure_becomes_submission_unknown_never_repurchases(
    *, db, esim_order, commbitz_purchaser, httpx_mock
):
    httpx_mock.add_response(status_code=503)
    purchaser = commbitz_purchaser
    await orders.mark_paid(db, purchaser, esim_order.id)
    order = await purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.PAID
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.SUBMISSION_UNKNOWN
    # 再多次恢复也绝不重新购买
    for _ in range(2):
        await purchaser.fulfill(db, esim_order.id)
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths.count("/distributor-api/v1/request") == 1


async def test_timeout_becomes_submission_unknown(*, db, esim_order, commbitz_purchaser, httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("network down"))
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    await commbitz_purchaser.fulfill(db, esim_order.id)
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None
    assert purchase.state == PurchaseState.SUBMISSION_UNKNOWN
    assert "network" in (purchase.last_error or "")
    # 钱货状态不明绝不自动退款：订单保持 paid，等人工核对后 /bind 或 /refund
    order_after = await db.get_order(esim_order.id)
    assert order_after is not None and order_after.status == OrderStatus.PAID


async def test_definite_rejection_auto_refunds_and_closes_order(
    *, db, user, esim_order, commbitz_purchaser, httpx_mock
):
    """建单前被上游明确拒绝：自动退款到买家余额并关单，不再允许重试（退款规则）。"""
    httpx_mock.add_response(status_code=404, json={"statusCode": 404, "message": "Plan not found with SKU: US-1"})
    purchaser = commbitz_purchaser
    await orders.mark_paid(db, purchaser, esim_order.id)
    order = await purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.REFUNDED
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.REJECTED
    # 全额退款到余额并写 refund 流水
    user_after = await db.get_user(user.id)
    assert user_after is not None and user_after.balance_cents == esim_order.amount_cents
    txs = await db.list_balance_transactions(user.id)
    assert [t.kind for t in txs] == ["refund"] and txs[0].amount_cents == esim_order.amount_cents
    # 已退款关闭的订单不能重试，也不会再发创建请求
    ok, detail = await purchaser.retry_rejected(db, esim_order.id)
    assert not ok and "已退款关闭" in detail
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths.count("/distributor-api/v1/request") == 1


async def test_retry_refuses_unknown_state(*, db, esim_order, commbitz_purchaser, httpx_mock):
    httpx_mock.add_response(status_code=503)
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    await commbitz_purchaser.fulfill(db, esim_order.id)
    ok, detail = await commbitz_purchaser.retry_rejected(db, esim_order.id)
    assert not ok
    assert "submission_unknown" in detail


async def test_missing_id_in_success_response_becomes_unknown(*, db, esim_order, commbitz_purchaser, httpx_mock):
    body = _create_ok()
    body["data"]["data"].pop("_id")
    httpx_mock.add_response(json=body)
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    await commbitz_purchaser.fulfill(db, esim_order.id)
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.SUBMISSION_UNKNOWN


async def test_upstream_failure_status_auto_refunds(*, db, user, esim_order, commbitz_purchaser, httpx_mock):
    """已建单但上游明确宣告失败：货不会发，自动退款到余额并关单（平台成本与上游对账另算）。"""
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock, status="failed")
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    order = await commbitz_purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.REFUNDED
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.REJECTED
    user_after = await db.get_user(user.id)
    assert user_after is not None and user_after.balance_cents == esim_order.amount_cents


async def test_partial_esims_keep_waiting(*, db, esim_order, commbitz_purchaser, httpx_mock):
    """数量 2 只返回 1 张：继续等待补齐，绝不只发一半货品。"""
    product = Product(1, "US 1GB", "", 4999, "CNY", sku="US-1", upstream_plan_id="id-US-1", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, esim_order.user_id, product, 2)
    await orders.mark_paid(db, commbitz_purchaser, order.id)
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock, count=1)
    order = await commbitz_purchaser.fulfill(db, order.id)
    assert order is not None and order.status == OrderStatus.PAID
    _details_mock(httpx_mock, count=2)
    order = await commbitz_purchaser.fulfill(db, order.id)
    assert order is not None and order.status == OrderStatus.DELIVERED
    assert order.payload is not None and order.payload.count("ICCID") == 2


async def test_concurrent_fulfill_submits_once(*, db, esim_order, commbitz_purchaser, httpx_mock):
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock)
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    results = await asyncio.gather(
        commbitz_purchaser.fulfill(db, esim_order.id),
        commbitz_purchaser.fulfill(db, esim_order.id),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) for r in results)
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths.count("/distributor-api/v1/request") == 1


async def test_restart_recovery_with_known_id_only_queries(*, tmp_path, httpx_mock):
    """已有上游 _id 时，重启恢复只查询详情，不重复创建。"""
    path = str(tmp_path / "known-id.db")
    db = Database(path)
    await db.connect()
    try:
        user = await db.upsert_user(42, "audit")
        product = Product(1, "test", "", 999, "CNY", sku="US-1", request_type="esim")
        await db.seed_products([product])
        order = await orders.create_order(db, user.id, product, 1)
        await orders.mark_paid(db, DemoPurchaser(), order.id, trade_no="T1")
        purchase = await db.get_purchase_by_order(order.id)
        assert purchase is not None
        # 模拟中断在「已保存 _id、尚未交付」之后
        await db.transition_purchase(
            purchase.id, PurchaseState.UPSTREAM_PENDING, from_state=PurchaseState.READY, upstream_request_id="up-9"
        )
    finally:
        await db.close()

    recovered = Database(path)
    await recovered.connect()
    try:
        gateway = FakeCommbitzGateway(details={
            "status": "Success", "requestType": "esim", "quantity": 1,
            "esims": [{"iccid": "89", "lpa": "LPA:1", "qrCode": "https://q.png"}],
        })
        purchaser = CommbitzPurchaser(gateway)
        await recover_once(recovered, purchaser, None)  # bot=None：通知失败由异常路径吞掉，不重复采购
        order = await recovered.get_order(order.id)
        assert order is not None and order.status == OrderStatus.DELIVERED
        assert order.upstream_ref == "up-9"
        # 只查询既有 ID，绝不重新购买
        assert gateway.create_calls == 0
        assert gateway.detail_calls == 1
    finally:
        await recovered.close()


async def test_bind_verifies_and_binds_unknown_purchase(*, db, esim_order, commbitz_purchaser, httpx_mock):
    httpx_mock.add_response(status_code=503)
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    await commbitz_purchaser.fulfill(db, esim_order.id)
    # 核对不匹配：数量不符 → 拒绝绑定
    httpx_mock.add_response(json=_details_with_esims(quantity=5))
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, esim_order.id, "up-x")
    assert not ok and "不匹配" in detail
    # 核对通过 → 绑定
    _details_mock(httpx_mock)
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, esim_order.id, "up-x")
    assert ok, detail
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.UPSTREAM_PENDING
    assert purchase.upstream_request_id == "up-x"
    async with db.connection() as conn:
        async with conn.execute("SELECT note FROM order_events WHERE note LIKE '%bound%'") as cur:
            assert await cur.fetchone() is not None  # 绑定写入审计


async def test_bind_refuses_when_gateway_says_not_found(*, db, esim_order, commbitz_purchaser, httpx_mock):
    httpx_mock.add_response(status_code=503)
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    await commbitz_purchaser.fulfill(db, esim_order.id)
    httpx_mock.add_response(status_code=404, json={"statusCode": 404, "message": "not found"})
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, esim_order.id, "bad-id")
    assert not ok and "查询失败" in detail


def test_classify_status_handles_upstream_case_chaos():
    assert classify_status("Success") == "success"
    assert classify_status("APPROVED") == "success"
    assert classify_status("active") == "success"
    assert classify_status("Failed") == "failure"
    assert classify_status("pending") == "pending"
    assert classify_status(None) == "pending"
    assert classify_status("") == "pending"


def test_split_payload_chunks_keeps_blocks_intact():
    blocks = [f"[{i}]\nICCID: {i}\nLPA: x" for i in range(300)]
    payload = "\n\n".join(blocks)
    chunks = split_payload_chunks(payload)
    assert all(len(c) <= 3500 for c in chunks)
    rejoined = "\n\n".join(chunks)
    assert rejoined == payload  # 内容无损
    # 单块超限也不会被丢弃
    single = "x" * 5000
    assert split_payload_chunks(single) == [single]


async def test_multi_esim_notification_sends_in_chunks(*, db, user, commbitz_purchaser, httpx_mock, bot):
    product = Product(1, "US bulk", "", 4999, "CNY", sku="US-100", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 100)
    await orders.mark_paid(db, commbitz_purchaser, order.id)
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock, count=100)
    order = await commbitz_purchaser.fulfill(db, order.id)
    assert order is not None and order.payload
    assert await notify_owner(db, bot, order.id)
    goods_messages = [m for m in bot.session.sent if "ICCID" in (m.text or "")]
    assert len(goods_messages) > 1  # 分条发送
    assert all(len(m.text) <= 4096 for m in bot.session.sent)


# ---- 审计修复回归（d2bbca7/21b4bdb/1950679 审计报告）----


async def _seed_order(db, user_id, sku, request_type):
    product = Product(1, "p", "", 999, "CNY", sku=sku, request_type=request_type)
    await db.seed_products([product])
    return await orders.create_order(db, user_id, product, 1)


async def test_recharge_days_included_in_payment_amount(db, user):
    """P1-1：充值天数计入收款金额（unitPrice × quantity × days）。"""
    product = Product(1, "1GB/day", "", 100, "CNY", sku="R-1", request_type="recharge")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1, msisdn="+919876543210", days=30)
    assert order.amount_cents == 100 * 1 * 30
    order_one_day = await orders.create_order(db, user.id, product, 2)
    assert order_one_day.amount_cents == 100 * 2  # 未填天数按 1 天


async def test_incomplete_esim_content_never_delivered(*, db, esim_order, commbitz_purchaser, httpx_mock):
    """P1-2：空数组 / 缺 LPA / 缺二维码都不得交付。"""
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    httpx_mock.add_response(json=_create_ok())
    cases = [
        [],  # 空数组
        [{"iccid": "89", "lpa": "", "qrCode": "https://q.png"}],  # 缺 LPA
        [{"iccid": "89", "lpa": "LPA:1", "qrCode": ""}],  # 缺二维码
    ]
    for esims in cases:
        httpx_mock.add_response(json=_details(status="Success", esims=esims))
        order = await commbitz_purchaser.fulfill(db, esim_order.id)
        assert order is not None and order.status == OrderStatus.PAID, f"误交付: {esims}"
    # 完整内容才交付
    _details_mock(httpx_mock)
    order = await commbitz_purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.DELIVERED


async def test_voucher_missing_voucher_code_never_delivered(*, db, user, commbitz_purchaser, httpx_mock):
    """P1-2：兑换券订单缺少券面不得交付。"""
    order = await _seed_order(db, user.id, "V-1", "voucher")
    await orders.mark_paid(db, commbitz_purchaser, order.id)
    httpx_mock.add_response(json=_create_ok())
    httpx_mock.add_response(json={"statusCode": 200, "data": {"success": True, "data": {
        "_id": "up-1", "status": "Success", "requestType": "voucher", "quantity": 1}}})
    order = await commbitz_purchaser.fulfill(db, order.id)
    assert order is not None and order.status == OrderStatus.PAID
    # 有券面才交付
    httpx_mock.add_response(json={"statusCode": 200, "data": {"success": True, "data": {
        "_id": "up-1", "status": "Success", "requestType": "voucher", "quantity": 1,
        "voucher": "VOUCHER123"}}})
    order = await commbitz_purchaser.fulfill(db, order.id)
    assert order is not None and order.status == OrderStatus.DELIVERED
    assert order.payload == "兑换券：VOUCHER123"


async def test_retry_refuses_when_upstream_order_exists(*, db, esim_order, commbitz_purchaser, httpx_mock):
    """P1-3：已建单的 rejected 不得重购（防重复扣款 + 保留原单关联）。"""
    httpx_mock.add_response(json=_create_ok(request_id="remote-1"))
    httpx_mock.add_response(json=_details(status="pending", esims=[]))
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    await commbitz_purchaser.fulfill(db, esim_order.id)
    httpx_mock.add_response(json=_details(status="failed", esims=[]))
    await commbitz_purchaser.fulfill(db, esim_order.id)  # 详情 failed → rejected → 自动退款关单
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.REJECTED
    assert purchase.upstream_request_id == "remote-1"

    ok, detail = await commbitz_purchaser.retry_rejected(db, esim_order.id)
    assert not ok and "已退款关闭" in detail
    # 状态不变，也不发新的创建请求
    assert (await db.get_purchase_by_order(esim_order.id)).state == PurchaseState.REJECTED
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths.count("/distributor-api/v1/request") == 1


async def test_bind_refuses_plan_mismatch_and_duplicate_upstream_id(
    *, db, user, esim_order, commbitz_purchaser, httpx_mock
):
    """P1-4：绑定必须核对套餐归属；同一上游单不能绑定两个订单。"""
    httpx_mock.add_response(status_code=503)
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    await commbitz_purchaser.fulfill(db, esim_order.id)
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.SUBMISSION_UNKNOWN

    # planId 与本地商品 upstream_plan_id 不一致 → 拒绝
    httpx_mock.add_response(json=_details_with_esims(plan_id="OTHER-PLAN"))
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, esim_order.id, "up-x")
    assert not ok and "套餐" in detail

    # 匹配 → 绑定成功
    _details_mock(httpx_mock)
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, esim_order.id, "up-x")
    assert ok, detail

    # 另一个买家试图绑定同一上游单 → 拒绝
    order2 = await _seed_order(db, user.id, "US-1", "esim")
    await orders.mark_paid(db, commbitz_purchaser, order2.id)
    httpx_mock.add_response(status_code=503)
    await commbitz_purchaser.fulfill(db, order2.id)
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, order2.id, "up-x")
    assert not ok and "已绑定到订单 #1" in detail


async def test_order_snapshot_survives_product_change(*, db, user, commbitz_purchaser, httpx_mock):
    """P1-5：下单后修改商品，采购仍使用下单时锁定的 SKU/业务类型。"""
    product = Product(1, "old", "", 999, "CNY", sku="OLD-SKU", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    # 下单后篡改商品
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE products SET sku = 'NEW-SKU', request_type = 'physical' WHERE id = 1"
        )
    await orders.mark_paid(db, commbitz_purchaser, order.id)
    httpx_mock.add_response(json=_create_ok())
    httpx_mock.add_response(json={"statusCode": 200, "data": {"success": True, "data": {
        "_id": "up-1", "status": "pending", "requestType": "esim", "quantity": 1, "esims": []}}})
    await commbitz_purchaser.fulfill(db, order.id)
    request = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/v1/request")][-1]
    body = request.read().decode()
    assert '"sku":"OLD-SKU"' in body
    assert '"requestType":"esim"' in body


async def test_catalog_sync_preserves_manual_request_type(db):
    """P1-5：目录同步不得覆盖管理员人工指定的业务类型。"""
    class FakeCommbitz:
        async def get_all_plans(self, **filters):
            return [{
                "_id": "id-US-1", "sku": "US-1", "name": "new name", "simCategory": "esim",
                "planIsFor": 3, "pricing": {"currency": {"code": "USD"}},
            }]

    await db.upsert_product_from_upstream(
        sku="US-1", name="n", description="d", upstream_plan_id="id-US-1", request_type="esim"
    )
    async with db.transaction() as conn:
        await conn.execute("UPDATE products SET request_type = 'recharge' WHERE sku = 'US-1'")
    await sync_catalog(db, FakeCommbitz())
    row = await db._one("SELECT request_type FROM products WHERE sku = 'US-1'")
    assert row is not None and row["request_type"] == "recharge"  # 人工配置保留


async def test_account_level_kyc_requires_documents_before_creation(
    *, db, esim_order, commbitz_purchaser, httpx_mock
):
    """P1-6：账户级强制 KYC 被拒后可补证件，重新创建时携带 kycDocuments。"""
    httpx_mock.add_response(status_code=400, json={"statusCode": 400, "message":
        "kycDocuments is mandatory for this distributor when creating activation or eSIM order"})
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    order = await commbitz_purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.PAID
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.AWAITING_KYC
    assert purchase.upstream_request_id is None

    # 买家补交链接证件 → 暂存并回到 ready
    ok, detail = await purchaser_submit_links(commbitz_purchaser, db, esim_order.id)
    assert ok, detail
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.READY
    assert purchase.kyc_documents is not None and "passportFront" in purchase.kyc_documents

    # 重新创建：请求体携带 kycDocuments；成功后 kycStatus=pending → awaiting_kyc（等审核）
    httpx_mock.add_response(json={"statusCode": 201, "data": {"success": True, "data": {
        "_id": "up-9", "status": "pending", "requestType": "esim", "quantity": 1,
        "kycStatus": "pending", "isKycRequired": True}}})
    httpx_mock.add_response(json={"statusCode": 200, "data": {"success": True, "data": {
        "_id": "up-9", "status": "pending", "kycStatus": "pending", "isKycVerified": False, "esims": []}}})
    await commbitz_purchaser.fulfill(db, esim_order.id)
    request = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/v1/request")][-1]
    assert "kycDocuments" in request.read().decode()
    # 已带证件建单，kycStatus=pending：保持等待审核，轮询到 submitted/verified 再推进
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.AWAITING_KYC
    assert purchase.upstream_request_id == "up-9"
    # 上游标记 submitted → kyc_submitted
    httpx_mock.add_response(json={"statusCode": 200, "data": {"success": True, "data": {
        "_id": "up-9", "status": "pending", "kycStatus": "submitted", "isKycVerified": False, "esims": []}}})
    await commbitz_purchaser.fulfill(db, esim_order.id)
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.KYC_SUBMITTED


async def purchaser_submit_links(purchaser, db, order_id):
    return await purchaser.submit_kyc(
        db, order_id, documents={"passportFront": "https://cdn.example/pf.jpg"}
    )


def test_kyc_release_requires_verification_when_required():
    """P1-6：isKycRequired=true 时缺失/未验证的 kycStatus 不得放行。"""
    assert not _kyc_released({"isKycRequired": True, "isKycVerified": False, "kycStatus": None})
    assert not _kyc_released({"isKycRequired": True, "isKycVerified": False, "kycStatus": "submitted"})
    assert _kyc_released({"isKycRequired": True, "isKycVerified": True, "kycStatus": "verified"})
    assert _kyc_released({"isKycRequired": True, "isKycVerified": True, "kycStatus": None})
    # 不要求 KYC 时正常放行
    assert _kyc_released({"isKycRequired": False, "isKycVerified": False, "kycStatus": None})
    assert _kyc_released({"kycStatus": None})


async def test_kyc_submit_before_creation_rejects_files(*, db, user, commbitz_purchaser, httpx_mock):
    """未建单（账户级 KYC 被拒）时不支持文件直传，需提供 HTTPS 链接。"""
    order = await _seed_order(db, user.id, "IN-9", "esim")
    await orders.mark_paid(db, commbitz_purchaser, order.id)
    httpx_mock.add_response(status_code=400, json={"statusCode": 400, "message":
        "kycDocuments is mandatory for this distributor when creating activation or eSIM order"})
    await commbitz_purchaser.fulfill(db, order.id)
    ok, detail = await commbitz_purchaser.submit_kyc(
        db, order.id, files=[("passportFront", "pf.jpg", b"x")]
    )
    assert not ok and "HTTPS 链接" in detail


# ---- 第二轮审计修复回归（7e5f8d5 审计报告）----


async def test_bind_uses_order_plan_snapshot_and_refuses_unverifiable(
    *, db, user, commbitz_purchaser, httpx_mock
):
    """P1-b：绑定用下单时套餐快照；商品被改后旧订单仍按原套餐核验；
    上游响应无套餐/SKU 标识时拒绝绑定而不是放行。"""
    product = Product(1, "US 1GB", "", 4999, "CNY", sku="US-1",
                      upstream_plan_id="id-US-1", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, commbitz_purchaser, order.id)
    httpx_mock.add_response(status_code=503)
    await commbitz_purchaser.fulfill(db, order.id)  # 提交结果不明
    # 下单后篡改商品套餐
    async with db.transaction() as conn:
        await conn.execute("UPDATE products SET upstream_plan_id = 'NEW-PLAN' WHERE id = 1")

    # 上游响应无 planId 也无 SKU → 订单有套餐快照，缺失即不匹配
    httpx_mock.add_response(json={"statusCode": 200, "data": {"success": True, "data": {
        "_id": "up-x", "status": "Success", "requestType": "esim", "quantity": 1, "esims": []}}})
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, order.id, "up-x")
    assert not ok and "套餐 缺失 != id-US-1" in detail

    # planId 与下单快照一致（而非当前商品）→ 通过
    httpx_mock.add_response(json={"statusCode": 200, "data": {"success": True, "data": {
        "_id": "up-x", "status": "Success", "requestType": "esim", "quantity": 1,
        "planId": "id-US-1",
        "esims": [{"iccid": "89", "lpa": "LPA:1", "qrCode": "https://q.png"}]}}})
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, order.id, "up-x")
    assert ok, detail
    purchase = await db.get_purchase_by_order(order.id)
    assert purchase is not None and purchase.upstream_request_id == "up-x"


async def test_server_error_with_kyc_message_stays_unknown_never_repurchases(
    *, db, esim_order, commbitz_purchaser, httpx_mock
):
    """P1-c：500 + kycDocuments 文案属于结果不明，必须保持 submission_unknown。"""
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    httpx_mock.add_response(status_code=500, json={"statusCode": 500, "message":
        "kycDocuments is mandatory for this distributor when creating activation or eSIM order"})
    await commbitz_purchaser.fulfill(db, esim_order.id)
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.SUBMISSION_UNKNOWN
    # 恢复不得再次发起创建
    await commbitz_purchaser.fulfill(db, esim_order.id)
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths.count("/distributor-api/v1/request") == 1


async def test_interrupt_between_order_and_purchase_finalizes_on_recovery(
    *, db, esim_order, commbitz_purchaser, httpx_mock, bot
):
    """P2-b：订单已 delivered 但采购仍 upstream_pending 时，恢复循环补齐采购终态。"""
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id, trade_no="T1")
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock)
    await commbitz_purchaser.fulfill(db, esim_order.id)
    assert (await db.get_order(esim_order.id)).status == OrderStatus.DELIVERED
    # 模拟历史中断窗口：采购被回拨到 upstream_pending
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None
    await db.transition_purchase(
        purchase.id, PurchaseState.UPSTREAM_PENDING,
        from_state=PurchaseState.FULFILLED, upstream_request_id=None,
    )
    await recover_once(db, commbitz_purchaser, bot)
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.FULFILLED
    final = await db.get_order(esim_order.id)
    assert final is not None and final.payload
    assert await notify_owner(db, bot, esim_order.id)


async def test_migration_survives_duplicate_upstream_ids(tmp_path, caplog):
    """P2-c：旧库存在重复上游单时跳过唯一索引并告警，不阻断启动。"""
    path = str(tmp_path / "dup.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, quantity INTEGER,
                amount_cents INTEGER, currency TEXT, status TEXT NOT NULL DEFAULT 'pending_payment',
                upstream_ref TEXT, trade_no TEXT, payload TEXT,
                notified_at TEXT, notification_pending INTEGER NOT NULL DEFAULT 0,
                input_iccid TEXT, input_msisdn TEXT, input_days INTEGER,
                input_sku TEXT, input_request_type TEXT, input_plan_id TEXT,
                created_at TEXT, updated_at TEXT);
            INSERT INTO orders VALUES (1, 1, 1, 1, 999, 'CNY', 'paid', NULL, 'T1', NULL,
                NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL, '', '');
            INSERT INTO orders VALUES (2, 1, 1, 1, 999, 'CNY', 'paid', NULL, 'T2', NULL,
                NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL, '', '');
            CREATE TABLE purchases (
                id INTEGER PRIMARY KEY, order_id INTEGER, state TEXT, request_type TEXT,
                sku TEXT, quantity INTEGER, upstream_request_id TEXT, upstream_order_no TEXT,
                attempts INTEGER DEFAULT 0, last_error TEXT, kyc_documents TEXT,
                created_at TEXT, updated_at TEXT);
            INSERT INTO purchases VALUES (1, 1, 'submission_unknown', 'esim', 'S', 1,
                'shared-id', NULL, 1, NULL, NULL, '', '');
            INSERT INTO purchases VALUES (2, 2, 'submission_unknown', 'esim', 'S', 1,
                'shared-id', NULL, 1, NULL, NULL, '', '');
        """)
    with caplog.at_level(logging.ERROR):
        db = Database(path)
        await db.connect()
    try:
        assert any("duplicate upstream request ids" in r.message for r in caplog.records)
        # 无唯一索引：重复记录保留，交由管理员人工处理
        row = await db._one("SELECT COUNT(*) AS c FROM purchases WHERE upstream_request_id = 'shared-id'")
        assert row is not None and row["c"] == 2
    finally:
        await db.close()


# ---- 第三轮审计修复回归（03d7a18 审计报告）----


async def test_duplicate_legacy_purchases_frozen_and_excluded_from_delivery(
    *, tmp_path, caplog, bot
):
    """P1-1：重复上游单冻结为 submission_unknown，恢复循环不给两个买家发同一货品。"""
    path = str(tmp_path / "dup2.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, quantity INTEGER,
                amount_cents INTEGER, currency TEXT, status TEXT NOT NULL DEFAULT 'pending_payment',
                upstream_ref TEXT, trade_no TEXT, payload TEXT,
                notified_at TEXT, notification_pending INTEGER NOT NULL DEFAULT 0,
                input_iccid TEXT, input_msisdn TEXT, input_days INTEGER,
                input_sku TEXT, input_request_type TEXT, input_plan_id TEXT,
                created_at TEXT, updated_at TEXT);
            INSERT INTO orders VALUES (1, 1, 1, 1, 999, 'CNY', 'paid', NULL, 'T1', NULL,
                NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL, '', '');
            INSERT INTO orders VALUES (2, 1, 1, 1, 999, 'CNY', 'paid', NULL, 'T2', NULL,
                NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL, '', '');
            CREATE TABLE purchases (
                id INTEGER PRIMARY KEY, order_id INTEGER, state TEXT, request_type TEXT,
                sku TEXT, quantity INTEGER, upstream_request_id TEXT, upstream_order_no TEXT,
                attempts INTEGER DEFAULT 0, last_error TEXT, kyc_documents TEXT,
                created_at TEXT, updated_at TEXT);
            INSERT INTO purchases VALUES (1, 1, 'upstream_pending', 'esim', 'S', 1,
                'shared-id', NULL, 1, NULL, NULL, '', '');
            INSERT INTO purchases VALUES (2, 2, 'upstream_pending', 'esim', 'S', 1,
                'shared-id', NULL, 1, NULL, NULL, '', '');
        """)
    with caplog.at_level(logging.ERROR):
        db = Database(path)
        await db.connect()
    try:
        frozen = await db.list_purchases_by_states((PurchaseState.SUBMISSION_UNKNOWN,))
        assert {p.order_id for p in frozen} == {1, 2}  # 两笔冲突记录都被冻结
        assert any("duplicate upstream request ids" in r.message for r in caplog.records)

        class NoTouchGateway(FakeCommbitzGateway):
            async def create_request(self, **kwargs):
                raise AssertionError("frozen purchase must not be re-purchased")

            async def get_order_details(self, request_id):
                raise AssertionError("frozen purchase must not be polled")

        await recover_once(db, CommbitzPurchaser(NoTouchGateway()), bot)
        # 冻结状态不翻转，两笔采购都等人处理
        for order_id in (1, 2):
            purchase = await db.get_purchase_by_order(order_id)
            assert purchase is not None and purchase.state == PurchaseState.SUBMISSION_UNKNOWN
    finally:
        await db.close()


async def test_bind_atomic_conflict_under_concurrency(*, db, user, commbitz_purchaser, httpx_mock):
    """P1-1：无唯一索引时，并发绑定同一上游单也只有一个成功（事务内复核）。"""
    product = Product(1, "US 1GB", "", 4999, "CNY", sku="US-1",
                      upstream_plan_id="id-US-1", request_type="esim")
    await db.seed_products([product])
    orders_list = [await orders.create_order(db, user.id, product, 1) for _ in range(2)]
    for order in orders_list:
        await orders.mark_paid(db, commbitz_purchaser, order.id)
        httpx_mock.add_response(status_code=503)
        await commbitz_purchaser.fulfill(db, order.id)
    _details_mock(httpx_mock)
    httpx_mock.add_response(json=_details_with_esims(plan_id="id-US-1"))
    results = await asyncio.gather(
        commbitz_purchaser.bind_unknown_purchase(db, orders_list[0].id, "shared-x"),
        commbitz_purchaser.bind_unknown_purchase(db, orders_list[1].id, "shared-x"),
    )
    successes = [ok for ok, _ in results if ok]
    assert len(successes) == 1  # 只有一个绑定成功
    owners = []
    for order in orders_list:
        purchase = await db.get_purchase_by_order(order.id)
        assert purchase is not None
        if purchase.upstream_request_id == "shared-x":
            owners.append(order.id)
    assert len(owners) == 1


async def test_bind_refused_when_local_snapshot_missing_and_upstream_plan_differs(
    *, db, user, commbitz_purchaser, httpx_mock
):
    """P1-2：旧订单无套餐快照、上游只返回 planId（无 SKU）→ 无法核对，拒绝绑定。"""
    product = Product(1, "legacy", "", 999, "CNY", sku="ORIGINAL-SKU", request_type="esim")
    await db.seed_products([product])
    order = await orders.create_order(db, user.id, product, 1)
    await orders.mark_paid(db, commbitz_purchaser, order.id)
    httpx_mock.add_response(status_code=503)
    await commbitz_purchaser.fulfill(db, order.id)
    # 上游只给 planId（无 SKU），与本地任何记录都无法匹配
    httpx_mock.add_response(json={"statusCode": 200, "data": {"success": True, "data": {
        "_id": "up-x", "status": "Success", "requestType": "esim", "quantity": 1,
        "planId": "OTHER-PLAN", "esims": []}}})
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, order.id, "up-x")
    assert not ok and "无法核对归属" in detail
    # 上游 SKU 与订单快照一致 → 交叉核对通过
    httpx_mock.add_response(json={"statusCode": 200, "data": {"success": True, "data": {
        "_id": "up-x", "status": "Success", "requestType": "esim", "quantity": 1,
        "sku": "ORIGINAL-SKU", "esims": []}}})
    ok, detail = await commbitz_purchaser.bind_unknown_purchase(db, order.id, "up-x")
    assert ok, detail


async def test_notified_delivered_order_with_pending_purchase_converges(
    *, db, esim_order, commbitz_purchaser, httpx_mock, bot
):
    """P2：已通知的历史订单若采购仍 upstream_pending，恢复循环必须收敛。"""
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id, trade_no="T1")
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock)
    await commbitz_purchaser.fulfill(db, esim_order.id)
    await notify_owner(db, bot, esim_order.id)
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None
    # 模拟历史中断窗口：采购停留在 upstream_pending，且订单已通知完毕
    await db.transition_purchase(
        purchase.id, PurchaseState.UPSTREAM_PENDING,
        from_state=PurchaseState.FULFILLED, upstream_request_id=None,
    )
    await recover_once(db, commbitz_purchaser, bot)
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.FULFILLED


# ---- 第四轮审计修复回归（6bf171c 审计报告）----


async def test_frozen_duplicate_purchases_block_notification_to_both_buyers(
    *, tmp_path, caplog, bot
):
    """P1：冻结的重复采购必须同时拦截自动通知与手动补发（同一货品两个买家）。"""
    path = str(tmp_path / "dup3.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, quantity INTEGER,
                amount_cents INTEGER, currency TEXT, status TEXT NOT NULL DEFAULT 'pending_payment',
                upstream_ref TEXT, trade_no TEXT, payload TEXT,
                notified_at TEXT, notification_pending INTEGER NOT NULL DEFAULT 0,
                input_iccid TEXT, input_msisdn TEXT, input_days INTEGER,
                input_sku TEXT, input_request_type TEXT, input_plan_id TEXT,
                created_at TEXT, updated_at TEXT);
            CREATE TABLE purchases (
                id INTEGER PRIMARY KEY, order_id INTEGER, state TEXT, request_type TEXT,
                sku TEXT, quantity INTEGER, upstream_request_id TEXT, upstream_order_no TEXT,
                attempts INTEGER DEFAULT 0, last_error TEXT, kyc_documents TEXT,
                created_at TEXT, updated_at TEXT);
            -- 场景：两份订单均已 delivered 且货品相同、通知都还没发出去
            INSERT INTO orders VALUES (1, 1, 1, 1, 999, 'CNY', 'delivered', 'shared-id', 'T1',
                'ICCID: 89', NULL, 1, NULL, NULL, NULL, NULL, NULL, NULL, '', '');
            INSERT INTO orders VALUES (2, 2, 1, 1, 999, 'CNY', 'delivered', 'shared-id', 'T2',
                'ICCID: 89', NULL, 1, NULL, NULL, NULL, NULL, NULL, NULL, '', '');
            -- 一笔已 fulfilled、一笔 upstream_pending：迁移后都必须冻结
            INSERT INTO purchases VALUES (1, 1, 'fulfilled', 'esim', 'S', 1,
                'shared-id', NULL, 1, NULL, NULL, '', '');
            INSERT INTO purchases VALUES (2, 2, 'upstream_pending', 'esim', 'S', 1,
                'shared-id', NULL, 1, NULL, NULL, '', '');
        """)

    db = Database(path)
    await db.connect()
    try:
        # 买家必须真实存在，避免因找不到收件人而让“未发送”的断言虚假通过。
        first_buyer = await db.upsert_user(42, "buyer-one")
        second_buyer = await db.upsert_user(43, "buyer-two")
        assert first_buyer.id == 1 and second_buyer.id == 2
        # 迁移把 fulfilled 记录也冻结（重复货品归属存疑，通知前必须人工确认）
        frozen = await db.list_purchases_by_states((PurchaseState.SUBMISSION_UNKNOWN,))
        assert {p.order_id for p in frozen} == {1, 2}

        await recover_once(db, CommbitzPurchaser(FakeCommbitzGateway()), bot)
        # 两个买家都没收到任何消息
        assert bot.session.sent == []
        # 通知保持待发状态，等管理员人工核对后处理
        for order_id in (1, 2):
            order = await db.get_order(order_id)
            assert order is not None and order.notification_pending == 1

        # 手动补发（resend=True）同样被拦截
        bot.session.fail_send = False
        assert not await notify_owner(db, bot, 1, resend=True)
        assert not await notify_owner(db, bot, 2, resend=True)
        assert bot.session.sent == []
    finally:
        await db.close()
