"""Commbitz 采购状态机测试：提交一次、已知 ID 只查询、未知转人工、并发与恢复。"""

import asyncio
import time

import httpx
import pytest

from shop_bot.db import Database
from shop_bot.models import OrderStatus, Product, PurchaseState
from shop_bot.services import orders
from shop_bot.services.commbitz_api import CommbitzClient, Tokens
from shop_bot.services.fulfillment import notify_owner, recover_once
from shop_bot.services.purchasing import (
    CommbitzPurchaser,
    DemoPurchaser,
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


async def test_definite_rejection_marks_rejected_and_admin_retry_resubmits(
    *, db, esim_order, commbitz_purchaser, httpx_mock
):
    httpx_mock.add_response(status_code=404, json={"statusCode": 404, "message": "Plan not found with SKU: US-1"})
    purchaser = commbitz_purchaser
    await orders.mark_paid(db, purchaser, esim_order.id)
    order = await purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.PAID  # 付款事实保留
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.REJECTED
    # 管理员受控重试 → ready → 重新提交成功
    ok, detail = await purchaser.retry_rejected(db, esim_order.id)
    assert ok, detail
    httpx_mock.add_response(json=_create_ok(request_id="up-2"))
    _details_mock(httpx_mock)
    order = await purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.DELIVERED
    assert order.upstream_ref == "up-2"


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


async def test_upstream_failure_status_rejects_but_payment_stays(
    *, db, esim_order, commbitz_purchaser, httpx_mock
):
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock, status="failed")
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    order = await commbitz_purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.status == OrderStatus.PAID
    purchase = await db.get_purchase_by_order(esim_order.id)
    assert purchase is not None and purchase.state == PurchaseState.REJECTED


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


async def test_multi_esim_notification_sends_in_chunks(*, db, esim_order, commbitz_purchaser, httpx_mock, bot):
    httpx_mock.add_response(json=_create_ok())
    _details_mock(httpx_mock, count=100)
    await orders.mark_paid(db, commbitz_purchaser, esim_order.id)
    order = await commbitz_purchaser.fulfill(db, esim_order.id)
    assert order is not None and order.payload
    assert await notify_owner(db, bot, esim_order.id)
    goods_messages = [m for m in bot.session.sent if "ICCID" in (m.text or "")]
    assert len(goods_messages) > 1  # 分条发送
    assert all(len(m.text) <= 4096 for m in bot.session.sent)
