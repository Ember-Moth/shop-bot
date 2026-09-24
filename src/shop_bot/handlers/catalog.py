from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InaccessibleMessage

from ..db import Database
from ..keyboards import (
    CATALOG_PAGE_SIZE,
    CB_CATALOG_PAGE,
    CB_PRODUCT_PREFIX,
    catalog,
    catalog_text,
    escape_markdown,
    main_menu,
    product_detail,
)

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


def _callback_number(value: str) -> int | None:
    if not value.isascii() or not value.isdecimal() or len(value) > 18:
        return None
    return int(value)


@router.callback_query((F.data == "catalog") | F.data.startswith(CB_CATALOG_PAGE))
async def cb_catalog(callback: CallbackQuery, db: Database) -> None:
    data = callback.data or ""
    requested_page = 0 if data == "catalog" else _callback_number(data.removeprefix(CB_CATALOG_PAGE))
    if requested_page is None:
        await callback.answer("目录页码无效", show_alert=True)
        return
    products, page, page_count = await db.products.list_products_page(requested_page, CATALOG_PAGE_SIZE)
    if not products:
        await _safe_edit(callback, "暂时没有商品", reply_markup=main_menu(), parse_mode=None)
        await callback.answer("暂时没有商品", show_alert=True)
        return
    await _safe_edit(
        callback,
        catalog_text(products, page, page_count),
        reply_markup=catalog(products, page, page_count),
        parse_mode=None,
    )
    await callback.answer()


@router.callback_query(F.data.startswith(CB_PRODUCT_PREFIX))
async def cb_product(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").removeprefix(CB_PRODUCT_PREFIX).split(":")
    if len(parts) not in (1, 2):
        await callback.answer("商品参数无效", show_alert=True)
        return
    product_id = _callback_number(parts[0])
    page = _callback_number(parts[1]) if len(parts) == 2 else 0  # 兼容旧 p:<id> 按钮
    if product_id is None or page is None:
        await callback.answer("商品参数无效", show_alert=True)
        return
    product = await db.products.get_product(product_id)
    if product is None or not product.active:
        await callback.answer("商品不存在或已下架", show_alert=True)
        return
    text = (
        f"**{escape_markdown(product.name)}**\n\n{escape_markdown(product.description)}\n\n价格：{product.price_text}"
    )
    await _safe_edit(callback, text, reply_markup=product_detail(product.id, page), parse_mode="Markdown")
    await callback.answer()
