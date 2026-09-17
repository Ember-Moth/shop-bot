from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..config import get_settings
from ..db import Database
from ..logging_config import get_logger
from ..models import OrderStatus, PurchaseState
from ..services import orders
from ..services.balance import format_cents, parse_signed_amount
from ..services.fulfillment import notify_owner
from ..services.orders import OrderError
from ..services.purchasing import CommbitzPurchaser, Purchaser

router = Router()
logger = get_logger(__name__)
router.message.filter(F.from_user.id.in_(get_settings().admin_ids))
router.callback_query.filter(F.from_user.id.in_(get_settings().admin_ids))


def _fmt(o) -> str:
    return f"#{o.id} · user_db_id={o.user_id} · x{o.quantity} · {o.amount_text} · {o.status}"


@router.message(Command("orders"))
async def cmd_orders(message: Message, db: Database) -> None:
    text = message.text
    assert text is not None  # 只有文本消息会进入命令处理器
    parts = text.split(maxsplit=1)
    if len(parts) == 2:
        try:
            status = OrderStatus(parts[1].strip())
        except ValueError:
            valid = ", ".join(s.value for s in OrderStatus)
            await message.answer(f"未知状态。可用：{valid}")
            return
        orders_ = await db.list_orders(status)
        title = f"状态为 {status.value} 的订单"
    else:
        orders_ = await db.list_orders()
        title = "最近订单"
    if not orders_:
        await message.answer("没有订单。")
        return
    await message.answer(f"📦 {title}\n\n" + "\n".join(_fmt(o) for o in orders_))


@router.message(Command("paid"))
async def cmd_paid(message: Message, db: Database, purchaser: Purchaser, bot: Bot) -> None:
    """手动确认付款并立即推进一次履约；通知失败留给恢复循环重试。

    实体卡订单受理成功后停在 awaiting_dispatch，发货确认走独立的 /dispatch（审计 P2：
    确认付款不等于确认发货）。
    """
    order_id = _parse_order_id(message)
    if order_id is None:
        await message.answer("用法：/paid <订单号>")
        return
    try:
        order = await orders.mark_paid(db, purchaser, order_id, retry_failed=True)
        order = await purchaser.fulfill(db, order.id)
    except OrderError as exc:
        await message.answer(f"❌ {exc}")
        return
    assert order is not None
    if order.status != OrderStatus.DELIVERED:
        hint = ""
        if isinstance(purchaser, CommbitzPurchaser):
            purchase = await db.get_purchase_by_order(order.id)
            if purchase is not None and purchase.state == PurchaseState.AWAITING_DISPATCH:
                hint = "；实体卡已受理，确认发货请用 /dispatch <订单号>"
        await message.answer(f"⏳ 订单 #{order.id} 已确认付款，履约状态：{order.status}{hint}")
        return
    notified = await notify_owner(db, bot, order.id, resend=True)
    detail = "货品已私信发送给买家" if notified else "货品已保存，私信发送失败，系统会重试"
    await message.answer(f"✅ 订单 #{order.id} 已发货；{detail}")


@router.message(Command("dispatch"))
async def cmd_dispatch(message: Message, db: Database, purchaser: Purchaser, bot: Bot) -> None:
    """独立确认实体卡已发货：必须管理员显式操作，不随 /paid 自动触发。"""
    if not isinstance(purchaser, CommbitzPurchaser):
        await message.answer("当前为模拟采购模式，无需确认发货")
        return
    order_id = _parse_order_id(message)
    if order_id is None:
        await message.answer("用法：/dispatch <订单号>（实体卡实际发出后确认）")
        return
    ok, detail = await purchaser.confirm_dispatch(db, order_id)
    if not ok:
        await message.answer(f"❌ {detail}")
        return
    order = await db.get_order(order_id)
    if order is not None:
        await notify_owner(db, bot, order.id, resend=True)
    await message.answer(f"✅ {detail}")


@router.message(Command("purchases"))
async def cmd_purchases(message: Message, db: Database) -> None:
    """人工核对入口：列出需要人工处理的采购任务（结果不明/被拒绝）。"""
    manual = await db.list_purchases_by_states((PurchaseState.SUBMISSION_UNKNOWN, PurchaseState.REJECTED))
    if not manual:
        await message.answer("没有需要人工处理的采购任务。")
        return
    lines = [
        f"#{p.order_id} · {p.state.value} · {p.request_type} {p.sku} x{p.quantity}"
        f" · 尝试 {p.attempts} 次 · 上游单 {p.upstream_request_id or '无'}"
        f"{(' · ' + (p.last_error or '')) if p.last_error else ''}"
        for p in manual
    ]
    await message.answer(
        "🛠 需要人工处理\n\n"
        + "\n".join(lines)
        + "\n\n/bind <订单号> <上游请求ID> 核对绑定；/retry <订单号> 重试被拒采购"
    )


@router.message(Command("retry"))
async def cmd_retry(message: Message, db: Database, purchaser: Purchaser) -> None:
    if not isinstance(purchaser, CommbitzPurchaser):
        await message.answer("当前为模拟采购模式，无需重试")
        return
    order_id = _parse_order_id(message)
    if order_id is None:
        await message.answer("用法：/retry <订单号>（仅 rejected 采购可重试）")
        return
    ok, detail = await purchaser.retry_rejected(db, order_id)
    await message.answer(("✅ " if ok else "❌ ") + detail)


@router.message(Command("bind"))
async def cmd_bind(message: Message, db: Database, purchaser: Purchaser) -> None:
    if not isinstance(purchaser, CommbitzPurchaser):
        await message.answer("当前为模拟采购模式，无需人工绑定")
        return
    text = message.text
    if text is None:
        return
    parts = text.split(maxsplit=2)
    if len(parts) != 3 or not parts[2].strip() or not parts[1].strip().isdecimal():
        await message.answer("用法：/bind <订单号> <上游请求ID>")
        return
    order_id, upstream_request_id = int(parts[1].strip()), parts[2].strip()
    ok, detail = await purchaser.bind_unknown_purchase(db, order_id, upstream_request_id)
    await message.answer(("✅ " if ok else "❌ ") + detail)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db: Database) -> None:
    order_id = _parse_order_id(message)
    if order_id is None:
        await message.answer("用法：/cancel <订单号>")
        return
    try:
        order = await orders.cancel_order(db, order_id)
    except OrderError as exc:
        await message.answer(f"❌ {exc}")
        return
    await message.answer(f"🚫 订单 #{order.id} 已取消")


@router.message(Command("adjust"))
async def cmd_adjust(message: Message, db: Database, bot: Bot) -> None:
    """人工调账：余额支付订单履约失败退款等场景；扣成负余额拒绝，流水可溯。"""
    text = message.text
    assert text is not None  # 只有文本消息会进入命令处理器
    parts = text.split(maxsplit=3)
    if len(parts) < 3 or not parts[1].isdecimal():
        await message.answer("用法：/adjust <用户ID> <±金额元> [备注]，例如 /adjust 3 -10.00 订单#42退款")
        return
    delta_cents = parse_signed_amount(parts[2])
    if delta_cents is None or delta_cents == 0:
        await message.answer("金额无效：需非零、最多两位小数，例如 +5 或 -10.50")
        return
    user_id = int(parts[1])
    note = parts[3].strip() if len(parts) == 4 else ""
    new_balance = await db.adjust_balance(user_id, delta_cents, note or "admin adjust")
    if new_balance is None:
        await message.answer("❌ 调账失败：用户不存在，或负向调整超出当前余额")
        return
    await message.answer(
        f"✅ 已调账 {delta_cents / 100:+.2f} 元，用户 #{user_id} 当前余额 {format_cents(new_balance)} 元"
    )
    user = await db.get_user(user_id)
    if user is not None:
        try:
            await bot.send_message(
                user.telegram_id,
                f"💳 余额调整 {delta_cents / 100:+.2f} 元"
                + (f"（{note}）" if note else "")
                + f"\n当前余额：{format_cents(new_balance)} 元",
            )
        except Exception as exc:
            # 私信失败不影响已落库的调账事实
            logger.warning(
                "adjust notification failed", extra={"user_id": user_id, "error": type(exc).__name__}
            )


def _parse_order_id(message: Message) -> int | None:
    text = message.text
    if text is None:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[1].strip())
    except ValueError:
        return None
