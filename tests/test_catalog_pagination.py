from dataclasses import dataclass

import pytest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from shop_bot.handlers.catalog import cb_catalog, cb_product
from shop_bot.handlers.catalog import router as catalog_router
from shop_bot.handlers.start import _send_catalog
from shop_bot.keyboards import CATALOG_PAGE_SIZE, CB_CATALOG_PAGE, CB_PRODUCT_PREFIX
from shop_bot.models import Product
from shop_bot.telegram_text import text_units
from tests.test_balance import _balance_callback, menu_message_of


def callback(bot, data) -> CallbackQuery:
    return _balance_callback(bot, 1).model_copy(update={"data": data})


@dataclass
class CatalogView:
    text: str
    reply_markup: InlineKeyboardMarkup
    parse_mode: str | None


def last_catalog(bot) -> CatalogView:
    method = next(m for m in reversed(bot.session.sent) if isinstance(m, (SendMessage, EditMessageText)))
    assert isinstance(method.text, str) and isinstance(method.reply_markup, InlineKeyboardMarkup)
    assert method.parse_mode is None or isinstance(method.parse_mode, str)
    return CatalogView(method.text, method.reply_markup, method.parse_mode)


def product_buttons(message: CatalogView) -> list[InlineKeyboardButton]:
    return [
        b
        for row in message.reply_markup.inline_keyboard
        for b in row
        if b.callback_data is not None and b.callback_data.startswith(CB_PRODUCT_PREFIX)
    ]


@pytest.mark.parametrize("character", ["D", "😀"])
async def test_both_catalog_entrypoints_are_bounded_and_all_products_reachable(db, bot, character):
    products = [Product(i, f"Plan {i} " + "😀" * 90, character * 500, 999) for i in range(1, 13)]
    await db.seed_products(products)
    await _send_catalog(menu_message_of(bot), db)
    first = last_catalog(bot)
    assert text_units(first.text) <= 4096 and first.parse_mode is None
    assert len(product_buttons(first)) == CATALOG_PAGE_SIZE
    seen = []
    for page in range(3):
        await cb_catalog(callback(bot, "catalog" if page == 0 else f"catalog:{page}"), db)
        message = last_catalog(bot)
        assert text_units(message.text) <= 4096 and message.parse_mode is None
        buttons = product_buttons(message)
        assert len(buttons) <= CATALOG_PAGE_SIZE
        for button in buttons:
            assert button.callback_data is not None
            seen.append(int(button.callback_data.split(":")[1]))
        nav = [b.callback_data for row in message.reply_markup.inline_keyboard for b in row]
        assert (f"{CB_CATALOG_PAGE}{page + 1}" in nav) == (page < 2)
        assert (f"{CB_CATALOG_PAGE}{page - 1}" in nav) == (page > 0)
    assert seen == list(range(1, 13))


async def test_product_details_return_to_same_page_and_keep_full_description(db, bot):
    products = [Product(i, f"Plan[{i}]", "😀" * 500, 999) for i in range(1, 9)]
    await db.seed_products(products)
    await cb_catalog(callback(bot, "catalog:1"), db)
    catalog = last_catalog(bot)
    selected = product_buttons(catalog)[0]
    assert selected.callback_data == "p:6:1"
    await cb_product(callback(bot, selected.callback_data), db)
    detail = last_catalog(bot)
    assert "😀" * 500 in detail.text
    back = detail.reply_markup.inline_keyboard[-1][0].callback_data
    assert back == "catalog:1"
    await cb_catalog(callback(bot, back), db)
    assert "第 2/2 页" in last_catalog(bot).text
    await cb_product(callback(bot, "p:6"), db)  # 历史按钮仍可用
    assert last_catalog(bot).reply_markup.inline_keyboard[-1][0].callback_data == "catalog:0"


async def test_pagination_clamps_page_after_unpublish_and_handles_empty(db, bot):
    await db.seed_products([Product(i, f"Plan {i}", "", 999) for i in range(1, 9)])
    for product_id in (6, 7, 8):
        await db.configure_product(product_id, active=False)
    await cb_catalog(callback(bot, "catalog:1"), db)
    assert "第 1/1 页" in last_catalog(bot).text
    assert [b.callback_data for b in product_buttons(last_catalog(bot))] == [f"p:{i}:0" for i in range(1, 6)]
    for product_id in range(1, 6):
        await db.configure_product(product_id, active=False)
    await cb_catalog(callback(bot, "catalog:1"), db)
    assert last_catalog(bot).text == "暂时没有商品"


@pytest.mark.parametrize("data", ["catalog:-1", "catalog:1.2", "catalog:abc", "catalog:" + "1" * 30])
async def test_invalid_page_callback_is_rejected(db, bot, data):
    await cb_catalog(callback(bot, data), db)
    assert bot.session.sent[-1].text == "目录页码无效"
    assert not any(isinstance(m, EditMessageText) for m in bot.session.sent)


async def test_real_router_dispatches_pagination_and_product_callbacks(db, bot):
    await db.seed_products([Product(i, f"Plan {i}", "detail", 999) for i in range(1, 9)])
    await catalog_router.propagate_event(update_type="callback_query", event=callback(bot, "catalog:1"), db=db)
    assert "第 2/2 页" in last_catalog(bot).text
    await catalog_router.propagate_event(update_type="callback_query", event=callback(bot, "p:6:1"), db=db)
    detail = last_catalog(bot)
    assert "Plan 6" in detail.text and detail.reply_markup.inline_keyboard[-1][0].callback_data == "catalog:1"
