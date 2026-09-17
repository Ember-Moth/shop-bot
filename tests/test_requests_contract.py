"""阶段 C 合同测试：各请求类型 payload、KYC 流、用量解析、实体卡物流边界。"""

import sqlite3
import time

import pytest

from shop_bot.db import Database
from shop_bot.models import OrderStatus, Product, PurchaseState
from shop_bot.services import orders
from shop_bot.services.commbitz_api import CommbitzClient, CommbitzError, Tokens
from shop_bot.services.fulfillment import notify_owner, recover_once
from shop_bot.services.purchasing import CommbitzPurchaser, format_usage

UAT = "https://api-uat.commbitz.com/distributor-api"
TOKEN_RESPONSE = {"statusCode": 201, "data": {"accessToken": "a", "refreshToken": "r", "expiresIn": 3600}}


@pytest.fixture
async def purchaser():
    client = CommbitzClient(UAT, "k", "s", timeout=5.0)
    client._tokens = Tokens(access_token="acc", refresh_token="ref", expires_at=time.monotonic() + 3600)
    yield CommbitzPurchaser(client)
    await client.close()


async def _seed_order(db, user_id, sku, request_type, **order_kwargs):
    product = Product(1, "p", "", 999, "CNY", sku=sku, request_type=request_type)
    await db.seed_products([product])
    return await orders.create_order(db, user_id, product, 1, **order_kwargs)


@pytest.mark.parametrize(
    "request_type,order_kwargs,expected_fields",
    [
        ("activation", {"iccid": "8901260123456789012"}, {"iccid": "8901260123456789012"}),
        ("recharge", {"msisdn": "+919876543210"}, {"mobile_number": "+919876543210"}),
        ("recharge", {"msisdn": "+919876543210", "days": 7}, {"mobile_number": "+919876543210", "days": 7}),
        ("voucher", {}, {}),
        ("esim", {}, {}),
        ("physical", {}, {}),
    ],
)
async def test_request_type_payload_contract(
    *, db, user, purchaser, httpx_mock, request_type, order_kwargs, expected_fields
):
    """各业务类型的 /v1/request 请求体合同：字段名、大小写、条件必填。"""
    order = await _seed_order(db, user.id, f"SKU-{request_type}", request_type, **order_kwargs)
    await orders.mark_paid(db, purchaser, order.id)
    httpx_mock.add_response(
        json={
            "statusCode": 201,
            "data": {
                "success": True,
                "data": {"_id": "up-1", "status": "pending", "requestType": request_type, "quantity": 1},
            },
        }
    )
    httpx_mock.add_response(
        json={
            "statusCode": 200,
            "data": {
                "success": True,
                "data": {"_id": "up-1", "status": "pending", "requestType": request_type, "quantity": 1, "esims": []},
            },
        }
    )
    await purchaser.fulfill(db, order.id)
    requests = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/v1/request")]
    assert len(requests) == 1
    body = requests[0].read().decode()
    assert f'"requestType":"{request_type}"' in body
    assert '"sku":"SKU-' + request_type + '"' in body
    for key, value in expected_fields.items():
        assert f'"{key}":' in body
        assert str(value) in body


async def test_physical_sim_stays_awaiting_dispatch_until_admin_confirms(*, db, user, purchaser, httpx_mock, bot):
    """实体卡边界：上游受理成功不自动记为已发货；管理员 /paid 确认后才交付。"""
    order = await _seed_order(db, user.id, "SKU-physical", "physical")
    await orders.mark_paid(db, purchaser, order.id, trade_no="T1")
    httpx_mock.add_response(
        json={
            "statusCode": 201,
            "data": {
                "success": True,
                "data": {"_id": "up-p", "status": "pending", "requestType": "physical", "quantity": 1},
            },
        }
    )
    httpx_mock.add_response(
        json={
            "statusCode": 200,
            "data": {
                "success": True,
                "data": {"_id": "up-p", "status": "Success", "requestType": "physical", "quantity": 1, "esims": []},
            },
        }
    )
    order = await purchaser.fulfill(db, order.id)
    assert order is not None and order.status == OrderStatus.PAID  # 受理成功 ≠ 已发货
    purchase = await db.get_purchase_by_order(order.id)
    assert purchase is not None and purchase.state == PurchaseState.AWAITING_DISPATCH
    # 管理员确认发货 → delivered + 通知买家
    ok, detail = await purchaser.confirm_dispatch(db, order.id)
    assert ok, detail
    final = await db.get_order(order.id)
    assert final is not None and final.status == OrderStatus.DELIVERED
    assert final.payload is not None and "实体" in final.payload
    assert await notify_owner(db, bot, order.id)
    purchase = await db.get_purchase_by_order(order.id)
    assert purchase is not None and purchase.state == PurchaseState.FULFILLED
    # 重复确认被拒绝
    ok, _detail = await purchaser.confirm_dispatch(db, order.id)
    assert not ok


async def test_kyc_pending_requires_documents_then_releases(*, db, user, purchaser, httpx_mock):
    """INR 订单 KYC 流：建单 kycStatus=pending → 买家补交 → 审核释放后才交付。"""
    order = await _seed_order(db, user.id, "IN-1", "esim")
    await orders.mark_paid(db, purchaser, order.id, trade_no="T1")
    # 建单响应 kycStatus=pending
    httpx_mock.add_response(
        json={
            "statusCode": 201,
            "data": {
                "success": True,
                "data": {
                    "_id": "up-in",
                    "status": "pending",
                    "requestType": "esim",
                    "quantity": 1,
                    "kycStatus": "pending",
                    "isKycRequired": True,
                },
            },
        }
    )
    # 轮询：kyc 未过，esims 空
    httpx_mock.add_response(
        json={
            "statusCode": 200,
            "data": {
                "success": True,
                "data": {
                    "_id": "up-in",
                    "status": "pending",
                    "kycStatus": "pending",
                    "isKycVerified": False,
                    "esims": [],
                },
            },
        }
    )
    order = await purchaser.fulfill(db, order.id)
    assert order is not None and order.status == OrderStatus.PAID
    purchase = await db.get_purchase_by_order(order.id)
    assert purchase is not None and purchase.state == PurchaseState.AWAITING_KYC

    # 买家补交证件（JSON URL 模式）
    httpx_mock.add_response(
        json={
            "success": True,
            "data": {"kycStatus": "submitted", "isKycRequired": True, "isKycVerified": False, "esims": []},
        }
    )
    ok, detail = await purchaser.submit_kyc(db, order.id, documents={"passportFront": "https://cdn.example/pf.jpg"})
    assert ok, detail
    purchase = await db.get_purchase_by_order(order.id)
    assert purchase is not None and purchase.state == PurchaseState.KYC_SUBMITTED

    # 轮询：已提交但未审核（即使上游返回 eSIM 安装信息也不交付，开发方案 7.2）
    httpx_mock.add_response(
        json={
            "statusCode": 200,
            "data": {
                "success": True,
                "data": {
                    "_id": "up-in",
                    "status": "Success",
                    "kycStatus": "submitted",
                    "isKycVerified": False,
                    "esims": [{"iccid": "89", "lpa": "LPA:1", "qrCode": "https://q.png"}],
                },
            },
        }
    )
    order = await purchaser.fulfill(db, order.id)
    assert order is not None and order.status == OrderStatus.PAID  # 不提前交付

    # 审核通过 → 自动交付
    httpx_mock.add_response(
        json={
            "statusCode": 200,
            "data": {
                "success": True,
                "data": {
                    "_id": "up-in",
                    "status": "Success",
                    "kycStatus": "verified",
                    "isKycVerified": True,
                    "esims": [{"iccid": "89", "lpa": "LPA:1", "qrCode": "https://q.png"}],
                },
            },
        }
    )
    order = await purchaser.fulfill(db, order.id)
    assert order is not None and order.status == OrderStatus.DELIVERED


async def test_kyc_submit_already_verified_recovers(*, db, user, purchaser, httpx_mock):
    """规则 7：证件已审核过时（上传报 already verified），状态继续推进而不卡死。"""
    order = await _seed_order(db, user.id, "IN-1", "esim")
    await orders.mark_paid(db, purchaser, order.id)
    httpx_mock.add_response(
        json={
            "statusCode": 201,
            "data": {
                "success": True,
                "data": {"_id": "up-in", "status": "pending", "kycStatus": "pending", "isKycRequired": True},
            },
        }
    )
    httpx_mock.add_response(
        json={
            "statusCode": 200,
            "data": {
                "success": True,
                "data": {
                    "_id": "up-in",
                    "status": "pending",
                    "kycStatus": "pending",
                    "isKycVerified": False,
                    "esims": [],
                },
            },
        }
    )
    await purchaser.fulfill(db, order.id)
    httpx_mock.add_response(
        status_code=400, json={"statusCode": 400, "message": "KYC is already verified for this order"}
    )
    ok, _detail = await purchaser.submit_kyc(db, order.id, documents={"passportFront": "https://x/1.jpg"})
    assert ok
    purchase = await db.get_purchase_by_order(order.id)
    assert purchase is not None and purchase.state == PurchaseState.KYC_SUBMITTED


async def test_kyc_submit_rejects_wrong_states(*, db, user, purchaser):
    order = await _seed_order(db, user.id, "IN-1", "esim")
    await orders.mark_paid(db, purchaser, order.id)
    ok, detail = await purchaser.submit_kyc(db, order.id, documents={"passportFront": "https://x/1.jpg"})
    assert not ok and "ready" in detail
    ok, detail = await purchaser.submit_kyc(db, 999, documents={"passportFront": "https://x/1.jpg"})
    assert not ok and "不可履约" in detail


async def test_kyc_requires_at_least_one_document(purchaser):
    with pytest.raises(CommbitzError, match="at least one"):
        await purchaser.client.submit_kyc_documents_json("up-1", {})
    with pytest.raises(CommbitzError, match="at least one"):
        await purchaser.client.submit_kyc_documents_files("up-1", [("bogus", "f", b"x")])


async def test_kyc_multipart_upload_contract(*, purchaser, httpx_mock):
    httpx_mock.add_response(json={"success": True, "data": {"kycStatus": "submitted"}})
    await purchaser.client.submit_kyc_documents_files("up-9", [("passportFront", "pf.jpg", b"image-bytes")])
    request = httpx_mock.get_requests()[-1]
    assert request.url.path == "/distributor-api/v1/orders/up-9/kyc-documents"
    body = request.read()
    assert b'name="passportFront"' in body
    assert b"image-bytes" in body


async def test_usage_query_and_formatting(*, db, user, purchaser, httpx_mock):
    order = await _seed_order(db, user.id, "US-1", "esim")
    await orders.mark_paid(db, purchaser, order.id)
    await db.transition_order(order.id, OrderStatus.DELIVERED, upstream_ref="up-1", payload="goods")
    httpx_mock.add_response(
        json={
            "statusCode": 200,
            "code": "000",
            "data": {
                "data": {
                    "effectiveTime": "2026-06-01",
                    "expiryTime": "2026-06-08",
                    "totalUsageFormatted": "1.2 GB",
                    "summary": {"totalDays": 3, "averageDailyUsageFormatted": "400 MB"},
                },
                "totalDataFormatted": "5 GB",
            },
        }
    )
    usage = await purchaser.client.get_esim_usage(order_id="up-1")
    text = format_usage(usage)
    assert "2026-06-01" in text and "1.2 GB" in text and "5 GB" in text and "400 MB" in text
    request = httpx_mock.get_requests()[-1]
    assert "orderId=up-1" in str(request.url)


async def test_usage_format_missing_fields_shows_unknown():
    text = format_usage({})
    assert "未知" in text
    assert "有效期" not in text  # 无任何时间信息时不显示该行
    assert format_usage({"totalUsageFormatted": "2 GB"}).count("未知") == 1


async def test_recovery_loop_drives_kyc_orders(*, db, user, purchaser, httpx_mock, bot):
    """恢复循环同样推进 KYC 订单（通知补发与采购恢复统一）。"""
    order = await _seed_order(db, user.id, "IN-1", "esim")
    await orders.mark_paid(db, purchaser, order.id)
    httpx_mock.add_response(
        json={
            "statusCode": 201,
            "data": {
                "success": True,
                "data": {"_id": "up-in", "status": "pending", "kycStatus": "pending", "isKycRequired": True},
            },
        }
    )
    httpx_mock.add_response(
        json={
            "statusCode": 200,
            "data": {
                "success": True,
                "data": {
                    "_id": "up-in",
                    "status": "Success",
                    "kycStatus": "verified",
                    "isKycVerified": True,
                    "esims": [{"iccid": "89", "lpa": "LPA:1", "qrCode": "https://q.png"}],
                },
            },
        }
    )
    await recover_once(db, purchaser, bot)
    final = await db.get_order(order.id)
    assert final is not None and final.status == OrderStatus.DELIVERED
    assert await notify_owner(db, bot, order.id)


async def test_legacy_orders_table_gains_input_columns(tmp_path):
    """旧库 orders 表迁移后补 input_iccid/input_msisdn/input_days。"""
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, quantity INTEGER,
                amount_cents INTEGER, currency TEXT, status TEXT NOT NULL DEFAULT 'pending_payment',
                upstream_ref TEXT, created_at TEXT, updated_at TEXT);
        """)
    db = Database(path)
    await db.connect()
    try:
        order = await db.create_order(1, 1, 1, 999, "CNY", iccid="89", msisdn="+8613800138000", days=7)
        assert order.input_iccid == "89"
        assert order.input_msisdn == "+8613800138000"
        assert order.input_days == 7
    finally:
        await db.close()
