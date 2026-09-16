"""商城风主菜单测试：键盘布局、回复键盘路由、FSM 冲突防护。"""

from aiogram import Bot
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message

from shop_bot.db import FSMStorage
from shop_bot.handlers import start
from shop_bot.keyboards import (
    MENU_BUY,
    MENU_HELP,
    MENU_HISTORY,
    MENU_KYC,
    MENU_ORDERS,
    MENU_USAGE,
    main_menu,
    main_menu_reply,
)
from shop_bot.models import OrderStatus, Product


def menu_message(bot: Bot, text: str, chat_id: int = 42) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
            "text": text,
        },
        context={"bot": bot},
    )


def test_reply_keyboard_layout():
    kb = main_menu_reply()
    assert kb.resize_keyboard is True
    assert kb.input_field_placeholder == "选择功能或…"
    assert len(kb.keyboard) == 3 and all(len(row) == 2 for row in kb.keyboard)
    texts = [button.text for row in kb.keyboard for button in row]
    assert texts == [MENU_BUY, MENU_ORDERS, MENU_HISTORY, MENU_USAGE, MENU_KYC, MENU_HELP]


def test_inline_main_menu_two_columns():
    kb = main_menu()
    assert len(kb.inline_keyboard) == 2 and all(len(row) == 2 for row in kb.inline_keyboard)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callbacks == ["catalog", "myorders", "usage_hint", "help"]


def test_menu_text_handlers_are_guarded_by_idle_state():
    """FSM 进行中（如等待数量输入）点菜单按钮不会被误路由。"""
    expected = {
        "menu_buy": MENU_BUY,
        "menu_orders": MENU_ORDERS,
        "menu_history": MENU_HISTORY,
        "menu_usage": MENU_USAGE,
        "menu_kyc": MENU_KYC,
        "menu_help": MENU_HELP,
    }
    handlers = [h for h in start.router.message.handlers if getattr(h.callback, "__name__", "") in expected]
    assert len(handlers) == len(expected), "菜单处理器应全部注册"
    for handler in handlers:
        name = getattr(handler.callback, "__name__", "?")
        filters = handler.filters or []
        state_filters = [
            f.callback for f in filters if isinstance(f.callback, StateFilter)
        ]
        assert state_filters, f"{name} 缺少 StateFilter"
        assert all(f.states == (None,) for f in state_filters), f"{name} 应只在空闲状态触发"
        assert len(filters) >= 2, f"{name} 应同时匹配菜单文案"


async def test_cmd_start_clears_fsm_and_sets_keyboards(db, bot):
    context = FSMContext(storage=FSMStorage(db), key=StorageKey(bot_id=1, chat_id=42, user_id=42))
    await context.set_state("OrderFlow:quantity")
    await context.set_data({"days": 30})
    await start.cmd_start(menu_message(bot, "/start"), db, context)
    assert await context.get_state() is None
    assert await context.get_data() == {}
    texts = [m.text or "" for m in bot.session.sent]
    assert any("请选择功能" in t for t in texts)
    assert any("点击下方按钮快速使用" in t for t in texts)


async def test_menu_buy_shows_catalog(db, bot):
    await db.seed_products([Product(1, "美国 1GB", "", 999, "CNY")])
    await start.menu_buy(menu_message(bot, MENU_BUY), db)
    texts = [m.text or "" for m in bot.session.sent]
    assert any("商品目录" in t for t in texts)


async def test_menu_orders_empty_and_with_orders(db, user, bot):
    await start.menu_orders(menu_message(bot, MENU_ORDERS), db)
    assert any("你还没有订单" in (m.text or "") for m in bot.session.sent)

    product = Product(1, "美国 1GB", "", 999, "CNY")
    await db.seed_products([product])
    order = await db.create_order(user.id, product.id, 1, 999, "CNY")
    await db.transition_order(order.id, OrderStatus.DELIVERED)
    await start.menu_orders(menu_message(bot, MENU_ORDERS), db)
    texts = [m.text or "" for m in bot.session.sent]
    assert any(f"#{order.id}" in t and "delivered" in t for t in texts)
