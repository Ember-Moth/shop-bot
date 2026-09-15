import pytest

from shop_bot.services.epay import (
    EPayClient,
    EPayConfig,
    EPayOrder,
    _create_sign,
    _format_money,
    _md5,
    _verify_sign,
)


@pytest.fixture
async def epay_client():
    client = EPayClient(EPayConfig(pid="1000", key="test-key", url="https://pay.example.com", type="alipay"))

    yield client
    await client.close()


def test_md5():
    assert _md5("hello") == "5d41402abc4b2a76b9719d911017c592"


def test_format_money():
    assert _format_money(9.99) == "9.99"
    assert _format_money(0.1) == "0.10"
    assert _format_money(100) == "100.00"


def test_create_sign():
    params = {"money": "9.99", "name": "test", "out_trade_no": "123", "pid": "1000"}
    sign = _create_sign(params, "test-key")
    assert len(sign) == 32  # MD5 hex
    # 空值和 sign 字段不参与签名
    params_with_empty = {**params, "empty": "", "sign": "abc"}
    assert _create_sign(params_with_empty, "test-key") == sign


def test_verify_sign(epay_client):
    params = {"money": "9.99", "name": "test", "out_trade_no": "123", "pid": "1000"}
    params["sign"] = _create_sign(params, "test-key")
    assert _verify_sign(params, "test-key")
    assert not _verify_sign(params, "wrong-key")
    assert not _verify_sign({**params, "sign": ""}, "test-key")


def test_create_pay_url(epay_client):
    order = EPayOrder(
        name="测试商品",
        order_no="123",
        amount=9.99,
        notify_url="https://bot.example.com/payment/callback",
        return_url="https://bot.example.com/return",
    )
    url = epay_client.create_pay_url(order)
    assert url.startswith("https://pay.example.com/submit.php?")
    assert "out_trade_no=123" in url
    assert "money=9.99" in url
    assert "sign=" in url
    assert "sign_type=MD5" in url


def test_parse_callback_paid(epay_client):
    params = {
        "out_trade_no": "123",
        "trade_no": "2024010123456789",
        "money": "9.99",
        "trade_status": "TRADE_SUCCESS",
        "type": "alipay",
    }
    parsed = epay_client.parse_callback(params)
    assert parsed.order_no == "123"
    assert parsed.trade_no == "2024010123456789"
    assert parsed.money == "9.99"
    assert parsed.paid is True


def test_parse_callback_not_paid(epay_client):
    params = {
        "out_trade_no": "123",
        "trade_no": "",
        "money": "9.99",
        "trade_status": "TRADE_CLOSED",
        "type": "alipay",
    }
    parsed = epay_client.parse_callback(params)
    assert parsed.paid is False


async def test_query_order(epay_client, httpx_mock):
    httpx_mock.add_response(
        url="https://pay.example.com/api.php?act=order&pid=1000&key=test-key&out_trade_no=123",
        json={
            "code": 1,
            "trade_no": "2024010123456789",
            "out_trade_no": "123",
            "money": "9.99",
            "status": 1,
            "msg": "success",
        },
    )
    result = await epay_client.query_order("123")
    assert result.paid is True
    assert result.trade_no == "2024010123456789"
    assert result.money == "9.99"
    await epay_client.close()
