"""Commbitz 客户端离线测试：鉴权、令牌刷新、401 恢复、并发刷新、目录解析。"""

import asyncio
import time

import httpx
import pytest

from shop_bot.services.commbitz_api import (
    CommbitzClient,
    CommbitzError,
    Tokens,
    base_url_for,
    parse_plan,
)

UAT = "https://api-uat.commbitz.com/distributor-api"
LIVE = "https://api-cb.commbitz.com/distributor-api"
TOKEN_RESPONSE = {
    "message": "Tokens generated successfully",
    "statusCode": 201,
    "data": {"accessToken": "acc-1", "refreshToken": "ref-1", "expiresIn": 3600},
}
REFRESH_RESPONSE = {
    "message": "Token refreshed successfully",
    "statusCode": 200,
    "data": {"accessToken": "acc-2", "refreshToken": "ref-2", "expiresIn": 3600},
}
COUNTRIES_OK = {"statusCode": 200, "data": {"countries": [{"iso": "US"}], "count": 1}}


@pytest.fixture
async def client():
    instance = CommbitzClient(UAT, "key-123", "secret-456", timeout=5.0)
    yield instance
    await instance.close()


def test_base_url_for():
    assert base_url_for("uat") == UAT
    assert base_url_for("live") == LIVE
    with pytest.raises(CommbitzError):
        base_url_for("prod")


async def test_get_token_then_bearer(client, httpx_mock):
    httpx_mock.add_response(json=TOKEN_RESPONSE)
    httpx_mock.add_response(json=COUNTRIES_OK)
    countries = await client.get_countries()
    assert countries[0]["iso"] == "US"
    requests = httpx_mock.get_requests()
    assert requests[0].url.path == "/distributor-api/v1/get-token"
    assert b"secret-456" in requests[0].read()  # 凭据在请求体
    assert requests[1].headers["Authorization"] == "Bearer acc-1"


async def test_token_cached_between_requests(client, httpx_mock):
    httpx_mock.add_response(json=TOKEN_RESPONSE)
    httpx_mock.add_response(json=COUNTRIES_OK)
    httpx_mock.add_response(json=COUNTRIES_OK)
    await client.get_countries()
    await client.get_countries()
    token_requests = [r for r in httpx_mock.get_requests() if r.url.path.endswith("get-token")]
    assert len(token_requests) == 1


async def test_expired_token_triggers_refresh(client, httpx_mock):
    client._tokens = Tokens(access_token="old", refresh_token="ref-old", expires_at=0.0)
    httpx_mock.add_response(url=f"{UAT}/v1/refresh-token", json=REFRESH_RESPONSE)
    httpx_mock.add_response(json=COUNTRIES_OK)
    await client.get_countries()
    requests = httpx_mock.get_requests()
    assert requests[0].url.path == "/distributor-api/v1/refresh-token"
    assert requests[1].headers["Authorization"] == "Bearer acc-2"


async def test_401_refreshes_and_retries_once(client, httpx_mock):
    client._tokens = Tokens(access_token="stale", refresh_token="ref-old", expires_at=time.monotonic() + 3600)
    httpx_mock.add_response(url=f"{UAT}/v1/countries", status_code=401, json={"statusCode": 401, "message": "expired"})
    httpx_mock.add_response(url=f"{UAT}/v1/refresh-token", json=REFRESH_RESPONSE)
    httpx_mock.add_response(json=COUNTRIES_OK)
    countries = await client.get_countries()
    assert countries[0]["iso"] == "US"
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths.count("/distributor-api/v1/refresh-token") == 1


async def test_concurrent_refresh_single_request(client, httpx_mock):
    client._tokens = Tokens(access_token="old", refresh_token="ref-old", expires_at=0.0)
    httpx_mock.add_response(url=f"{UAT}/v1/refresh-token", json=REFRESH_RESPONSE)
    for _ in range(5):
        httpx_mock.add_response(json=COUNTRIES_OK)
    await asyncio.gather(*(client.get_countries() for _ in range(5)))
    refresh_requests = [r for r in httpx_mock.get_requests() if r.url.path.endswith("refresh-token")]
    assert len(refresh_requests) == 1


async def test_error_message_no_secrets(client, httpx_mock):
    httpx_mock.add_response(status_code=401, json={"statusCode": 401, "message": "Invalid API credentials"})
    with pytest.raises(CommbitzError) as exc_info:
        await client.get_countries()
    text = str(exc_info.value)
    assert "Invalid API credentials" in text
    assert "secret-456" not in text
    assert "key-123" not in text


def _plans_body(skus: list[str], next_page: bool) -> dict:
    plans = [
        {
            "_id": f"id-{sku}",
            "sku": sku,
            "name": f"plan {sku}",
            "simCategory": "esim",
            "planIsFor": 3,
            "pricing": {"currency": {"code": "USD"}},
        }
        for sku in skus
    ]
    return {
        "statusCode": 200,
        "data": {"plans": plans, "pagination": {"hasNextPage": next_page}},
    }


async def test_get_all_plans_pagination(client, httpx_mock):
    httpx_mock.add_response(json=TOKEN_RESPONSE)
    httpx_mock.add_response(json=_plans_body(["A", "B"], True))
    httpx_mock.add_response(json=_plans_body(["C"], False))
    plans = await client.get_all_plans()
    assert [p["sku"] for p in plans] == ["A", "B", "C"]


async def test_plan_detail_unwraps_data(client, httpx_mock):
    # 该接口没有 statusCode 外层，直接 {success, message, data}
    httpx_mock.add_response(json=TOKEN_RESPONSE)
    httpx_mock.add_response(json={"success": True, "message": "ok", "data": {"_id": "id-A", "sku": "A"}})
    detail = await client.get_plan_detail("id-A")
    assert detail["sku"] == "A"


async def test_order_details_unwraps_data_data(client, httpx_mock):
    httpx_mock.add_response(json=TOKEN_RESPONSE)
    httpx_mock.add_response(
        json={"statusCode": 200, "data": {"success": True, "data": {"_id": "req-1", "esims": [{"iccid": "89"}]}}}
    )
    detail = await client.get_order_details("req-1")
    assert detail["esims"][0]["iccid"] == "89"


def test_parse_plan():
    plan = parse_plan(
        {
            "_id": "p1",
            "sku": "US-1",
            "name": "US 1GB",
            "simCategory": "esim",
            "planIsFor": 3,
            "pricing": {"retailPrice": 6.0, "overridePrice": 5.0, "currency": {"code": "USD"}},
        }
    )
    assert plan.plan_id == "p1"
    assert plan.sku == "US-1"
    assert plan.currency == "USD"
    assert plan.retail_price == 6.0
    assert plan.override_price == 5.0


async def test_concurrent_401_responses_share_one_refresh(client, httpx_mock):
    client._tokens = Tokens(access_token="stale", refresh_token="ref-old", expires_at=time.monotonic() + 3600)
    arrivals = 0
    all_arrived = asyncio.Event()

    async def countries(request):
        nonlocal arrivals
        if request.headers["Authorization"] == "Bearer stale":
            arrivals += 1
            if arrivals == 5:
                all_arrived.set()
            await asyncio.wait_for(all_arrived.wait(), 1)
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json=COUNTRIES_OK)

    httpx_mock.add_callback(countries, url=f"{UAT}/v1/countries", is_reusable=True)
    httpx_mock.add_response(url=f"{UAT}/v1/refresh-token", json=REFRESH_RESPONSE)
    results = await asyncio.gather(*(client.get_countries() for _ in range(5)))
    assert all(result == [{"iso": "US"}] for result in results)
    assert len([r for r in httpx_mock.get_requests() if r.url.path.endswith("refresh-token")]) == 1
