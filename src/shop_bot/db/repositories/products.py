"""商品仓储：目录、上游同步、定价与上下架审计。"""

from __future__ import annotations

import json

from ...models import Product
from ...money import REQUEST_TYPES, normalize_currency
from ..mappers import row_to_product
from .base import Repository


class ProductRepository(Repository):
    async def list_products(self) -> list[Product]:
        return [
            row_to_product(r) for r in await self._db.fetch_all("SELECT * FROM products WHERE active = 1 ORDER BY id")
        ]

    async def list_products_page(self, page: int, page_size: int) -> tuple[list[Product], int, int]:
        if page < 0 or not 1 <= page_size <= 20:
            raise ValueError("invalid catalog page")
        async with self._db.connection() as conn:
            async with conn.execute("SELECT COUNT(*) FROM products WHERE active = 1") as cur:
                row = await cur.fetchone()
            assert row is not None
            page_count = max(1, (row[0] + page_size - 1) // page_size)
            current_page = min(page, page_count - 1)
            async with conn.execute(
                "SELECT * FROM products WHERE active = 1 ORDER BY id LIMIT ? OFFSET ?",
                (page_size, current_page * page_size),
            ) as cur:
                products = [row_to_product(row) for row in await cur.fetchall()]
        return products, current_page, page_count

    async def get_product(self, product_id: int) -> Product | None:
        row = await self._db.fetch_one("SELECT * FROM products WHERE id = ?", (product_id,))
        return row_to_product(row) if row else None

    async def seed_products(self, products: list[Product]) -> None:
        async with self._db.transaction() as conn:
            await conn.executemany(
                """INSERT OR IGNORE INTO products (id, name, description, price_cents, currency,
                sku, upstream_plan_id, request_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (p.id, p.name, p.description, p.price_cents, p.currency, p.sku, p.upstream_plan_id, p.request_type)
                    for p in products
                ],
            )

    async def upsert_product_from_upstream(
        self, *, sku: str, name: str, description: str, upstream_plan_id: str, request_type: str | None
    ) -> bool:
        """按 SKU 同步上游商品；只更新名称/描述/上游 ID，不动本店价格与上架状态。

        返回 True 表示新建。新商品 0 价且下架（目录可见不等于可售，开发方案 7.3），
        需管理员定价并上架后才对用户可见。
        """
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT id FROM products WHERE sku = ?", (sku,)) as cur:
                row = await cur.fetchone()
            if row is None:
                await conn.execute(
                    """INSERT INTO products
                        (name, description, price_cents, currency, active, sku, upstream_plan_id, request_type)
                    VALUES (?, ?, 0, 'USD', 0, ?, ?, ?)""",
                    (name, description, sku, upstream_plan_id, request_type),
                )
                return True
            # 名称/描述只在新建时写入：人工改名（/rename）不被目录同步覆盖；
            # request_type 保留人工配置（COALESCE）：管理员指定的业务类型不被目录同步覆盖
            await conn.execute(
                """UPDATE products SET upstream_plan_id = ?,
                request_type = COALESCE(request_type, ?)
                WHERE id = ?""",
                (upstream_plan_id, request_type, row["id"]),
            )
            return False

    async def list_all_products(self) -> list[Product]:
        return [row_to_product(r) for r in await self._db.fetch_all("SELECT * FROM products ORDER BY id")]

    async def configure_product(
        self,
        product_id: int,
        *,
        actor_id: int | None = None,
        price_cents: int | None = None,
        currency: str | None = None,
        name: str | None = None,
        description: str | None = None,
        active: bool | None = None,
        require_upstream: bool = False,
    ) -> Product | None:
        if price_cents is not None and not 0 < price_cents <= 999999999:
            raise ValueError("价格需大于零且不超过 9999999.99")
        if currency is not None:
            currency = normalize_currency(currency)
        if name is not None:
            name = name.strip()
            if not 0 < len(name) <= 100:
                raise ValueError("名称需为 1–100 个字符")
            if any(ord(c) < 0x20 or c == "\x7f" for c in name):
                # 名称会进入买家可见的目录按钮与详情，换行等控制字符会破坏排版
                raise ValueError("名称不能包含换行等控制字符")
        if description is not None:
            description = description.strip()
            if len(description) > 500:
                raise ValueError("描述最长 500 个字符")
            if any(ord(c) < 0x20 or c == "\x7f" for c in description):
                # 描述显示在目录正文与详情页，控制字符会破坏排版
                raise ValueError("描述不能包含换行等控制字符")
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            before = {key: row[key] for key in ("name", "description", "price_cents", "currency", "active")}
            after = {
                "name": name if name is not None else row["name"],
                "description": description if description is not None else row["description"],
                "price_cents": price_cents if price_cents is not None else row["price_cents"],
                "currency": currency if currency is not None else row["currency"],
                "active": int(active) if active is not None else row["active"],
            }
            if active:
                if after["price_cents"] <= 0:
                    raise ValueError("请先用 /price 设置有效售价")
                normalize_currency(after["currency"])
                if require_upstream and (
                    not row["sku"] or not row["upstream_plan_id"] or row["request_type"] not in REQUEST_TYPES
                ):
                    raise ValueError("真实商品缺少 SKU、上游套餐或明确业务类型，暂不能上架")
            async with conn.execute(
                """UPDATE products SET name = ?, description = ?, price_cents = ?, currency = ?, active = ?
                WHERE id = ? RETURNING *""",
                (
                    after["name"],
                    after["description"],
                    after["price_cents"],
                    after["currency"],
                    after["active"],
                    product_id,
                ),
            ) as cur:
                updated = await cur.fetchone()
            if before != after:
                await conn.execute(
                    "INSERT INTO product_events (product_id, actor_id, before_json, after_json) VALUES (?, ?, ?, ?)",
                    (product_id, actor_id, json.dumps(before), json.dumps(after)),
                )
        return row_to_product(updated) if updated else None

    async def set_product_currency(
        self,
        product_id: int,
        currency: str,
        *,
        actor_id: int | None = None,
    ) -> Product | None:
        return await self.configure_product(product_id, currency=currency, actor_id=actor_id)
