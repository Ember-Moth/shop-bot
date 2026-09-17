from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InaccessibleMessage

from ..db import Database
from ..keyboards import CB_PRODUCT_PREFIX, catalog, catalog_text, escape_markdown, product_detail

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


@router.callback_query(F.data == "catalog")
async def cb_catalog(callback: CallbackQuery, db: Database) -> None:
    products = await db.list_products()
    if not products:
        await callback.answer("暂时没有商品", show_alert=True)
        return
    await _safe_edit(callback, catalog_text(products), reply_markup=catalog(products))
    await callback.answer()


@router.callback_query(F.data.startswith(CB_PRODUCT_PREFIX))
async def cb_product(callback: CallbackQuery, db: Database) -> None:
    data = callback.data
    assert data is not None  # 过滤器已保证非空
    product = await db.get_product(int(data.removeprefix(CB_PRODUCT_PREFIX)))
    if product is None or not product.active:
        await callback.answer("商品不存在或已下架", show_alert=True)
        return
    text = (
        f"**{escape_markdown(product.name)}**\n\n{escape_markdown(product.description)}\n\n价格：{product.price_text}"
    )
    await _safe_edit(callback, text, reply_markup=product_detail(product.id), parse_mode="Markdown")
    await callback.answer()
