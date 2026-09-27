"""GMPay 链上付款的买家操作：「我已转账」查询到账，没查到时提交交易哈希由 epusdt 到链上核验。"""

import re
from collections.abc import Mapping
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from .. import keyboards
from ..db import Database
from ..logging_config import get_logger
from ..services.gateway import GatewayError, PaymentGateway
from ..services.gmpay import STATUS_EXPIRED, STATUS_PAID, GMPayError, GMPayGateway

router = Router()
logger = get_logger(__name__)

TX_HASH = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")
_SUBMIT_ERRORS = {
    10038: "未能核验这笔转账。请确认它向上面的地址转入了完全一致的金额，并且已在链上确认。",
    10013: "这笔付款信息已不在等待付款状态，可能已过期或已付款。",
    10007: "这笔交易已经用于其他付款。",
    10009: "交易哈希格式不正确，请检查后重新提交。",
}


class TxHashFlow(StatesGroup):
    hash = State()


async def _owned_trade(db: Database, gateway: GMPayGateway, telegram_id: int, trade_ref: int) -> dict[str, Any] | None:
    user = await db.users.get_user_by_telegram_id(telegram_id)
    trade = await db.gmpay.get(trade_ref)
    if user is None or trade is None or trade["trade_id"] is None:
        return None
    return trade if await gateway.trade_owner_ok(db, trade, user.id) else None


def _settled_text(trade: Mapping[str, Any]) -> str:
    target = f"订单 #{trade['order_id']}" if trade["order_id"] is not None else f"充值单 {trade['topup_id']}"
    return f"✅ 已确认到账，{target}会自动处理"


@router.callback_query(F.data.startswith(keyboards.CB_GM_CHECK))
async def cb_check_transfer(
    callback: CallbackQuery, db: Database, gateway: PaymentGateway | None, state: FSMContext
) -> None:
    raw = (callback.data or "").removeprefix(keyboards.CB_GM_CHECK)
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 18:
        await callback.answer("参数无效", show_alert=True)
        return
    if not isinstance(gateway, GMPayGateway):
        await callback.answer("在线收款暂不可用，请联系管理员", show_alert=True)
        return
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await callback.answer("消息已过期，请用 /query <订单号> 查询", show_alert=True)
        return
    trade = await _owned_trade(db, gateway, callback.from_user.id, int(raw))
    if trade is None:
        await callback.answer("付款信息不存在", show_alert=True)
        return
    if trade["state"] == "paid":
        await callback.answer(_settled_text(trade), show_alert=True)
        return
    try:
        status = await gateway.refresh(db, trade)
    except GatewayError:
        logger.warning("gmpay status check failed", extra={"order_id": trade["order_id"] or trade["topup_id"]})
        await callback.answer("暂时查不到付款状态，请稍后再试", show_alert=True)
        return
    if status == STATUS_PAID:
        await callback.answer(_settled_text(trade), show_alert=True)
        return
    if status == STATUS_EXPIRED:
        await callback.answer("这笔付款信息已过期，请勿再向该地址转账。可点「重新获取付款信息」。", show_alert=True)
        return
    await state.set_state(TxHashFlow.hash)
    await state.update_data(trade_ref=trade["id"])
    await msg.answer(
        "暂未查到到账，链上确认通常需要一两分钟。\n"
        "如果已经转账，请回复这笔转账的交易哈希（TXID），我们会到链上核验。\n发送 /start 取消。"
    )
    await callback.answer()


@router.message(TxHashFlow.hash, F.text)
async def tx_hash_input(message: Message, db: Database, gateway: PaymentGateway | None, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("已取消核验。")
        return
    if not TX_HASH.fullmatch(text):
        await message.answer("这不是有效的交易哈希，请复制钱包或区块浏览器里的 TXID 重新回复，或发送 /start 取消。")
        return
    trade_ref = (await state.get_data()).get("trade_ref")
    await state.clear()
    from_user = message.from_user
    if not isinstance(gateway, GMPayGateway) or not isinstance(trade_ref, int) or from_user is None:
        await message.answer("付款信息不存在，请用 /query <订单号> 重新获取。")
        return
    trade = await _owned_trade(db, gateway, from_user.id, trade_ref)
    if trade is None:
        await message.answer("付款信息不存在，请用 /query <订单号> 重新获取。")
        return
    try:
        status = await gateway.submit_tx_hash(db, trade, text)
    except GMPayError as exc:
        await message.answer(_SUBMIT_ERRORS.get(exc.code or 0, "核验暂时失败，请稍后再试或联系管理员。"))
        return
    except GatewayError:
        await message.answer("核验暂时失败，请稍后再试或联系管理员。")
        return
    if status == STATUS_PAID:
        await message.answer(_settled_text(trade))
    else:
        await message.answer("已提交核验，到账确认后会自动通知。")


@router.message(TxHashFlow.hash)
async def tx_hash_non_text(message: Message) -> None:
    await message.answer("请回复文本形式的交易哈希（TXID），或发送 /start 取消。")
