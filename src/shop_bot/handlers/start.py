from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from ..db import Database
from ..keyboards import main_menu

router = Router()


async def _safe_edit(callback: CallbackQuery, text: str, **kwargs) -> None:
    msg = callback.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        return
    try:
        await msg.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    from_user = message.from_user
    assert from_user is not None  # aiogram 在私聊场景下保证非空
    await db.upsert_user(from_user.id, from_user.username)
    await message.answer(
        "你好！我是商店机器人 🤖\n从下方菜单浏览商品并下单。",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery) -> None:
    await _safe_edit(callback, "主菜单", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "myorders")
async def cb_my_orders(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer("你还没有下过单", show_alert=True)
        return
    orders = await db.list_orders_for_user(user.id)
    if not orders:
        await _safe_edit(
            callback, "你还没有订单。\n去商品目录看看吧！", reply_markup=main_menu()
        )
        await callback.answer()
        return
    lines = [f"#{o.id} · 数量 x{o.quantity} · {o.amount_text} · {o.status}" for o in orders]
    await _safe_edit(
        callback, "📦 我的订单\n\n" + "\n".join(lines), reply_markup=main_menu()
    )
    await callback.answer()
