"""超时未付款订单自动关闭：正常关闭、正在付款时暂缓、关单前核对、迟到款项进余额与界面提示。"""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shop_bot.handlers import start
from shop_bot.models import OrderStatus
from shop_bot.services import orders
from shop_bot.services.fulfillment import recover_once
from shop_bot.services.order_expiry import close_expired_orders
from shop_bot.services.orders import OrderError
from shop_bot.services.payment_prompts import HEADER_PENDING, order_prompt
from tests.conftest import GM_URL
from tests.test_gmpay import callback_payload, locked_order, mock_create, mock_status
from tests.test_payment_prompts import calls, text_message

TIMEOUT = 1800
LATER = TIMEOUT + 900  # 已超过订单超时，且默认十分钟的收款信息加宽限也已过去


def far_future():
    return time.time() + LATER


async def status_of(db, order):
    current = await db.orders.get_order(order.id)
    assert current is not None
    return current.status


async def test_unpaid_order_closes_and_its_prompt_updates(*, db, user, product, bot, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    await db.prompts.record(42, 5, order_id=order.id)
    assert await close_expired_orders(db, None, timeout_seconds=TIMEOUT, now=time.time() + 60) == (0, 0)
    assert await close_expired_orders(db, None, timeout_seconds=TIMEOUT, now=far_future()) == (1, 0)
    assert await status_of(db, order) == OrderStatus.EXPIRED
    events = await db.fetch_all("SELECT to_status, note FROM order_events WHERE order_id = ?", (order.id,))
    assert [tuple(e) for e in events] == [("expired", "auto-closed: payment timeout")]
    await recover_once(db, purchaser, bot)
    edit = calls(bot, "editMessageText")[-1]
    assert edit.message_id == 5 and "超时未付款，已自动关闭" in edit.text
    assert [b.callback_data for row in edit.reply_markup.inline_keyboard for b in row] == ["myorders"]


async def test_active_payment_info_defers_closing(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1", expiration_time=int(time.time()) + LATER + 600)
    await gmpay.order_checkout(bot, db, order)
    assert await close_expired_orders(db, gmpay, timeout_seconds=TIMEOUT, now=far_future()) == (0, 0)
    assert await status_of(db, order) == OrderStatus.PENDING_PAYMENT
    # 收款信息截止并过了宽限期：先向 epusdt 核对，确认未付款才关闭
    mock_status(httpx_mock, "T100", 3)
    later = time.time() + LATER + 600 + 300
    assert await close_expired_orders(db, gmpay, timeout_seconds=TIMEOUT, now=later) == (1, 0)
    assert await status_of(db, order) == OrderStatus.EXPIRED


async def test_gap_topup_in_progress_defers_closing(*, db, user, product, bot, gmpay, httpx_mock):
    await db.wallet.adjust_balance(user.id, 300, "seed", "CNY")
    order = await orders.create_order(db, user.id, product, 1)
    topup = await db.wallet.gap_topup(user.id, order.id, 699, "CNY")
    mock_create(httpx_mock, f"T{topup.id}-1", amount=6.99, expiration_time=int(time.time()) + LATER + 600)
    await gmpay.topup_checkout(bot, db, topup, "补差价")
    assert await close_expired_orders(db, gmpay, timeout_seconds=TIMEOUT, now=far_future()) == (0, 0)
    assert await status_of(db, order) == OrderStatus.PENDING_PAYMENT


async def test_payment_found_before_closing_is_kept(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    await gmpay.order_checkout(bot, db, order)
    mock_status(httpx_mock, "T100", 2)  # 回调丢失，但买家其实已付款
    assert await close_expired_orders(db, gmpay, timeout_seconds=TIMEOUT, now=far_future()) == (0, 0)
    assert await status_of(db, order) == OrderStatus.PAID


async def test_gateway_failure_skips_closing_this_round(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    await gmpay.order_checkout(bot, db, order)
    httpx_mock.add_response(method="GET", url=f"{GM_URL}/pay/check-status/T100", status_code=502, text="down")
    assert await close_expired_orders(db, gmpay, timeout_seconds=TIMEOUT, now=far_future()) == (0, 1)
    assert await status_of(db, order) == OrderStatus.PENDING_PAYMENT


async def test_late_payment_after_closing_goes_to_wallet(*, db, user, product, bot, gmpay, httpx_mock):
    order = await locked_order(db, user, product)
    mock_create(httpx_mock, f"{order.id}-1")
    await gmpay.order_checkout(bot, db, order)
    mock_status(httpx_mock, "T100", 3)
    assert await close_expired_orders(db, gmpay, timeout_seconds=TIMEOUT, now=far_future()) == (1, 0)
    # 例如 TRON 上超时才到账，管理员在 epusdt 后台标记已付后收到回调
    await gmpay.apply_callback(db, callback_payload(f"{order.id}-1"))
    assert await status_of(db, order) == OrderStatus.EXPIRED
    assert await db.wallet.get_balance(user.id, "CNY") == 999
    receipts = await db.fetch_all("SELECT disposition FROM payment_receipts WHERE trade_no = 'T100'")
    assert [row["disposition"] for row in receipts] == ["wallet_credit"]


async def test_sweep_pages_past_orders_it_must_skip(db, user, product):
    now = far_future()
    blocked, *others = [await orders.create_order(db, user.id, product, 1) for _ in range(3)]
    ref, _ = await db.gmpay.begin(order_id=blocked.id, amount_cents=999, currency="CNY")
    await db.gmpay.created(
        ref,
        trade_id="T1",
        token="USDT",
        network="tron",
        receive_address="TAddr",
        actual_amount="1.41",
        payment_url="",
        expires_at=now + 3600,
        check_at=now + 3620,
    )
    assert await close_expired_orders(db, None, timeout_seconds=TIMEOUT, now=now, batch=2) == (2, 0)
    assert [await status_of(db, o) for o in (blocked, *others)] == [
        OrderStatus.PENDING_PAYMENT,
        OrderStatus.EXPIRED,
        OrderStatus.EXPIRED,
    ]


async def test_closed_orders_reject_admin_confirmation_and_explain_in_query(*, db, user, product, bot, purchaser):
    order = await orders.create_order(db, user.id, product, 1)
    await close_expired_orders(db, None, timeout_seconds=TIMEOUT, now=far_future())
    with pytest.raises(OrderError, match="expired"):
        await orders.mark_paid(db, purchaser, order.id)
    await start.cmd_query(text_message(bot, f"/query {order.id}"), db, None, purchaser, bot)
    assert "超时未付款，已自动关闭" in calls(bot, "sendMessage")[-1].text


@pytest.mark.parametrize("minutes", [30, 0])
async def test_prompt_states_the_payment_deadline(*, db, user, product, bot, monkeypatch, minutes):
    monkeypatch.setattr(
        "shop_bot.services.payment_prompts.get_settings",
        lambda: SimpleNamespace(payment=SimpleNamespace(order_timeout_minutes=minutes)),
    )
    order = await orders.create_order(db, user.id, product, 1)
    view = await order_prompt(bot, db, None, order, HEADER_PENDING)
    if minutes:
        created = datetime.strptime(str(order.created_at), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        deadline = f"{created + timedelta(minutes=minutes):%H:%M}"
        assert f"请在 {deadline} UTC 前付款，逾期订单自动关闭。" in view.text
    else:
        assert "逾期订单自动关闭" not in view.text
