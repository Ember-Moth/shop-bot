"""买家 KYC 补交与 eSIM 用量查询。

- /kyc <订单号>：只能补交自己订单的证件；仅私聊接收材料（开发方案 4）。
  支持直接发送照片/文件（multipart 上传，1–3 份按护照正面/背面/签证顺序映射），
  或发送 1–3 个 HTTPS 链接（JSON 模式）。
- /usage <订单号>：查询已交付 eSIM 订单的用量；只允许订单买家和管理员。
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from ..config import get_settings
from ..db import Database
from ..logging_config import get_logger
from ..services.purchasing import CommbitzPurchaser, format_usage

router = Router()
logger = get_logger(__name__)

KYC_FIELDS = ("passportFront", "passportBack", "visaFront")
MAX_FILE_BYTES = 10 * 1024 * 1024  # 文档未给出上限；10MB 为保守值


class KycFlow(StatesGroup):
    waiting_documents = State()


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_ids


async def _owned_order_or_reply(db: Database, message: Message, order_id: int | None):
    """校验订单存在且请求者是买家或管理员；不通过时回复提示并返回 None。"""
    if order_id is None:
        await message.answer(f"用法：/{message.text.split()[0].lstrip('/')} <订单号>" if message.text else "用法错误")
        return None
    order = await db.get_order(order_id)
    if order is None:
        await message.answer("订单不存在")
        return None
    from_user = message.from_user
    assert from_user is not None
    user = await db.get_user_by_telegram_id(from_user.id)
    if not _is_admin(from_user.id) and (user is None or order.user_id != user.id):
        await message.answer("只能操作自己的订单")
        return None
    return order


def _parse_order_arg(text: str | None) -> int | None:
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[1].strip())
    except ValueError:
        return None


async def _require_private(message: Message) -> bool:
    if message.chat.type != "private":
        await message.answer("为保护证件隐私，材料只能在私聊中提交。")
        return False
    return True


@router.message(Command("kyc"))
async def cmd_kyc(
    message: Message, db: Database, purchaser: CommbitzPurchaser, state: FSMContext
) -> None:
    order_id = _parse_order_arg(message.text)
    order = await _owned_order_or_reply(db, message, order_id)
    if order is None:
        return
    if not isinstance(purchaser, CommbitzPurchaser):
        await message.answer("当前为模拟采购模式，无需提交证件")
        return
    if not await _require_private(message):
        return
    purchase = await db.get_purchase_by_order(order.id)
    if purchase is None:
        await message.answer("该订单没有采购记录")
        return
    if purchase.state.value not in ("awaiting_kyc", "kyc_submitted"):
        await message.answer(f"订单 #{order.id} 当前采购状态为 {purchase.state.value}，无需提交证件")
        return
    await state.set_state(KycFlow.waiting_documents)
    await state.update_data(order_id=order.id, pending_files=[])
    await message.answer(
        f"请直接发送订单 #{order.id} 的证件材料（照片或 PDF 文件，1–3 份，"
        "将按顺序作为护照正面/护照背面/签证正面提交）。\n"
        "Telegram 相册会逐张送达，请逐条发送后用 /done 提交；"
        "也可以发送 1–3 个证件图片的 HTTPS 链接（每行一个）。\n发送 /cancel 取消。"
    )


@router.message(KycFlow.waiting_documents, F.text)
async def msg_kyc_text(
    message: Message, db: Database, purchaser: CommbitzPurchaser, state: FSMContext
) -> None:
    text = message.text
    assert text is not None
    command = text.split()[0].lower() if text.split() else ""
    if command in ("/done", "/submit@audit_bot"):
        await _submit_collected_files(message, db, purchaser, state)
        return
    if command == "/cancel":
        await state.clear()
        await message.answer("已取消证件提交。")
        return
    if not await _require_private(message):
        await state.clear()
        return
    urls = [line.strip() for line in text.splitlines() if line.strip()]
    if not urls or len(urls) > 3 or any(not u.lower().startswith(("http://", "https://")) for u in urls):
        await message.answer("请发送 1–3 个 https:// 开头的证件链接，或直接发送照片/文件后用 /done 提交。")
        return
    data = await state.get_data()
    order_id = data["order_id"]
    documents = dict(zip(KYC_FIELDS, urls, strict=False))
    ok, detail = await purchaser.submit_kyc(db, order_id, documents=documents)
    await state.clear()
    await message.answer(("✅ " if ok else "❌ ") + detail)


@router.message(KycFlow.waiting_documents)
async def msg_kyc_files(
    message: Message, db: Database, purchaser: CommbitzPurchaser, state: FSMContext
) -> None:
    """接收照片/文件：先逐张收集（Telegram 相册按多条消息送达），/done 统一提交。"""
    if not await _require_private(message):
        await state.clear()
        return
    data = await state.get_data()
    pending: list[dict[str, Any]] = list(data.get("pending_files") or [])
    if len(pending) >= 3:
        await message.answer("最多 3 份材料，已收集完毕，请用 /done 提交。")
        return
    photos = message.photo or []
    item = photos[-1] if photos else message.document
    if item is None:
        await message.answer("请发送照片或文件材料。")
        return
    pending.append({
        "file_id": item.file_id,
        "name": getattr(item, "file_name", None) or f"document{len(pending) + 1}.jpg",
    })
    await state.update_data(pending_files=pending)
    remaining = 3 - len(pending)
    hint = "已收集满 3 份，" if remaining == 0 else f"还可发送 {remaining} 份，"
    await message.answer(f"已收到第 {len(pending)} 份材料；{hint}发送 /done 提交。")


async def _submit_collected_files(
    message: Message, db: Database, purchaser: CommbitzPurchaser, state: FSMContext
) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    pending: list[dict[str, Any]] = list(data.get("pending_files") or [])
    if order_id is None or not pending:
        await message.answer("还没有收集到材料，请先发送照片/文件。")
        return
    bot = message.bot
    assert bot is not None
    files: list[tuple[str, str, bytes]] = []
    for index, entry in enumerate(pending):
        file = await bot.get_file(entry["file_id"])
        if (file.file_size or 0) > MAX_FILE_BYTES:
            await message.answer("存在过大文件，请压缩后重新发送材料。")
            return
        assert file.file_path is not None  # Telegram 对已上传文件必返回路径
        content = await bot.download_file(file.file_path)
        if not isinstance(content, bytes):
            # aiogram 可能返回 BinaryIO
            content = b"" if content is None else content.read()
        files.append((KYC_FIELDS[index], entry["name"], content))
    ok, detail = await purchaser.submit_kyc(db, order_id, files=files)
    await state.clear()
    await message.answer(("✅ " if ok else "❌ ") + detail)


@router.message(Command("usage"))
async def cmd_usage(message: Message, db: Database, purchaser: CommbitzPurchaser) -> None:
    order_id = _parse_order_arg(message.text)
    order = await _owned_order_or_reply(db, message, order_id)
    if order is None:
        return
    if not isinstance(purchaser, CommbitzPurchaser):
        await message.answer("当前为模拟采购模式，无法查询用量")
        return
    if order.status.value != "delivered" or not order.upstream_ref:
        await message.answer("订单尚未交付，暂无用量数据")
        return
    try:
        usage = await purchaser.client.get_esim_usage(order_id=order.upstream_ref)
    except Exception:
        logger.warning("usage query failed", extra={"order_id": order.id})
        await message.answer("用量查询失败，请稍后再试")
        return
    await message.answer(f"📊 订单 #{order.id} 用量\n\n{format_usage(usage)}")
