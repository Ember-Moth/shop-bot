"""商城风主菜单测试：键盘布局、回复键盘路由、FSM 冲突防护。"""

from aiogram import Bot
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message

from shop_bot.config import Settings
from shop_bot.db import FSMStorage
from shop_bot.handlers import kyc as kyc_module
from shop_bot.handlers import start
from shop_bot.keyboards import (
    MENU_BUY,
    MENU_HELP,
    MENU_HISTORY,
    MENU_KYC,
    MENU_ORDERS,
    MENU_USAGE,
    escape_markdown,
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


def test_reply_keyboard_layout_with_kyc_enabled():
    kb = main_menu_reply(kyc_enabled=True)
    assert kb.resize_keyboard is True
    assert kb.input_field_placeholder == "选择功能或…"
    assert len(kb.keyboard) == 3 and all(len(row) == 2 for row in kb.keyboard)
    texts = [button.text for row in kb.keyboard for button in row]
    assert texts == [MENU_BUY, MENU_ORDERS, MENU_HISTORY, MENU_USAGE, MENU_KYC, MENU_HELP]


def test_reply_keyboard_layout_hides_kyc_when_disabled():
    kb = main_menu_reply(kyc_enabled=False)
    texts = [button.text for row in kb.keyboard for button in row]
    assert MENU_KYC not in texts
    assert texts == [MENU_BUY, MENU_ORDERS, MENU_HISTORY, MENU_USAGE, MENU_HELP]


def test_kyc_disabled_feature_flag():
    """features.kyc=False 时 Settings 正确读取。"""
    assert Settings(features={"kyc": True}).features.kyc is True
    assert Settings(features={"kyc": False}).features.kyc is False
    assert Settings().features.kyc is True  # 默认开放


def test_inline_main_menu_two_columns():
    kb = main_menu()
    assert len(kb.inline_keyboard) == 2 and all(len(row) == 2 for row in kb.inline_keyboard)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callbacks == ["catalog", "myorders", "usage_hint", "help"]


def test_menu_text_handlers_are_guarded_by_idle_state():
    """FSM 进行中（如等待数量输入）点菜单按钮不会被误路由。"""
    handler = next(
        h for h in start.router.message.handlers if getattr(h.callback, "__name__", "") == "menu_router"
    )
    filters = handler.filters or []
    state_filters = [f.callback for f in filters if isinstance(f.callback, StateFilter)]
    assert state_filters and all(f.states == (None,) for f in state_filters), "菜单路由应只在空闲状态触发"
    assert len(filters) >= 2, "菜单路由应同时匹配菜单文案"


def test_escape_markdown_neutralizes_markers():
    raw = "US [1GB] *7天* _special_ `code`"
    escaped = escape_markdown(raw)
    assert escaped.count("\\") == 7, "7 个标记字符（[ ×1、* ×2、_ ×2、` ×2）都应转义"
    # 正常名字不受影响
    assert escape_markdown("US 1GB 7 Days") == "US 1GB 7 Days"


async def test_menu_router_debounces_repeated_taps(db, user, bot):
    """连点同一菜单按钮 3 秒内只处理第一次。"""
    start._menu_last_seen.clear()
    product = Product(1, "美国 1GB", "", 999, "CNY")
    await db.seed_products([product])
    for _ in range(5):
        await start.menu_router(menu_message(bot, MENU_BUY), db)
    catalog_msgs = [m.text for m in bot.session.sent if m.text and "商品目录" in m.text]
    assert len(catalog_msgs) == 1, "防抖后连点只应产生一条目录"


async def test_menu_router_dispatches_each_entry(db, user, bot):
    """防抖窗口外，各菜单入口都能正确分发。"""
    start._menu_last_seen.clear()
    product = Product(1, "美国 1GB", "", 999, "CNY")
    await db.seed_products([product])
    await start.menu_router(menu_message(bot, MENU_BUY), db)
    # 手动清除防抖时间戳，模拟 3 秒后
    start._menu_last_seen.clear()
    await start.menu_router(menu_message(bot, MENU_ORDERS), db)
    start._menu_last_seen.clear()
    await start.menu_router(menu_message(bot, MENU_USAGE), db)
    start._menu_last_seen.clear()
    await start.menu_router(menu_message(bot, MENU_HELP), db)
    texts = [m.text or "" for m in bot.session.sent]
    assert any("商品目录" in t for t in texts)
    assert any("我的订单" in t for t in texts)
    assert any("用量" in t and "/usage" in t for t in texts)
    assert any("使用帮助" in t for t in texts)


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


async def test_menu_router_buy_and_orders(db, user, bot):
    start._menu_last_seen.clear()
    await db.seed_products([Product(1, "美国 1GB", "", 999, "CNY")])
    await start.menu_router(menu_message(bot, MENU_BUY), db)
    assert any("商品目录" in (m.text or "") for m in bot.session.sent)

    start._menu_last_seen.clear()
    await start.menu_router(menu_message(bot, MENU_ORDERS), db)
    assert any("你还没有订单" in (m.text or "") for m in bot.session.sent)

    product2 = Product(2, "美国 1GB", "", 999, "CNY")
    await db.seed_products([product2])
    order = await db.create_order(user.id, product2.id, 1, 999, "CNY")
    await db.transition_order(order.id, OrderStatus.DELIVERED)
    start._menu_last_seen.clear()
    await start.menu_router(menu_message(bot, MENU_ORDERS), db)
    texts = [m.text or "" for m in bot.session.sent]
    assert any(f"#{order.id}" in t and "delivered" in t for t in texts)


# ---- KYC 功能开关 ----


async def test_menu_kyc_refused_when_disabled(db, bot, monkeypatch):
    """开关关闭：点「🪪 证件补交」按钮得到未开放提示，而非引导提交。"""
    start._menu_last_seen.clear()
    monkeypatch.setattr(start, "get_settings", lambda: Settings(features={"kyc": False}))
    await start.menu_router(menu_message(bot, MENU_KYC), db)
    texts = [m.text or "" for m in bot.session.sent]
    assert any("未开放" in t for t in texts)
    assert not any("/kyc" in t for t in texts)  # 不再引导使用被禁用的命令


async def test_menu_kyc_guides_when_enabled(db, bot, monkeypatch):
    start._menu_last_seen.clear()
    monkeypatch.setattr(start, "get_settings", lambda: Settings(features={"kyc": True}))
    await start.menu_router(menu_message(bot, MENU_KYC), db)
    texts = [m.text or "" for m in bot.session.sent]
    assert any("/kyc" in t for t in texts)


async def test_menu_help_omits_kyc_line_when_disabled(db, bot, monkeypatch):
    start._menu_last_seen.clear()
    monkeypatch.setattr(start, "get_settings", lambda: Settings(features={"kyc": False}))
    start._menu_last_seen.clear()
    await start.menu_router(menu_message(bot, MENU_HELP), db)
    texts = [m.text or "" for m in bot.session.sent]
    assert all("/kyc" not in t for t in texts)


async def test_cmd_kyc_refused_when_disabled(db, bot, monkeypatch):
    monkeypatch.setattr(kyc_module, "get_settings", lambda: Settings(features={"kyc": False}))
    await kyc_module.cmd_kyc(
        menu_message(bot, "/kyc 1"), db, None, None  # 开关先于依赖使用，传 None 不触达
    )
    texts = [m.text or "" for m in bot.session.sent]
    assert any("未开放" in t for t in texts)
