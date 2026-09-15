"""目录同步测试：同步只维护名称/SKU/上游 ID，不动本店价格与上架状态。"""

import aiosqlite

from shop_bot import sync_upstream_catalog
from shop_bot.config import Settings
from shop_bot.db import Database
from shop_bot.services.catalog_sync import sync_catalog


class FakeCommbitz:
    """只实现 sync_catalog 需要的接口。"""

    def __init__(self, plans):
        self._plans = plans

    async def get_all_plans(self, **filters):
        return self._plans


def _plan(sku: str, name: str = "plan") -> dict:
    return {
        "_id": f"id-{sku}",
        "sku": sku,
        "name": name,
        "simCategory": "esim",
        "planIsFor": 3,
        "pricing": {"currency": {"code": "USD"}},
    }


async def test_sync_creates_inactive_products(db):
    result = await sync_catalog(db, FakeCommbitz([_plan("US-1", "US 1GB")]))
    assert (result.total, result.created, result.updated, result.skipped) == (1, 1, 0, 0)
    # 新商品下架，对用户不可见
    assert await db.list_products() == []
    row = await db._one("SELECT * FROM products WHERE sku = 'US-1'")
    assert row is not None
    assert row["active"] == 0
    assert row["price_cents"] == 0
    assert row["currency"] == "CNY"
    assert row["upstream_plan_id"] == "id-US-1"
    assert row["name"] == "US 1GB"


async def test_resync_updates_without_touching_price(db):
    await sync_catalog(db, FakeCommbitz([_plan("US-1", "old name")]))
    async with db.transaction() as conn:
        await conn.execute("UPDATE products SET price_cents = 49900, active = 1 WHERE sku = 'US-1'")
    result = await sync_catalog(db, FakeCommbitz([_plan("US-1", "new name")]))
    assert (result.created, result.updated) == (0, 1)
    row = await db._one("SELECT * FROM products WHERE sku = 'US-1'")
    # 本店定价和上架状态保留，只更新名称
    assert row["price_cents"] == 49900
    assert row["active"] == 1
    assert row["name"] == "new name"


async def test_sync_skips_missing_sku(db):
    raw = _plan("X")
    raw["sku"] = ""
    result = await sync_catalog(db, FakeCommbitz([raw]))
    assert result.skipped == 1
    assert result.created == 0


async def test_sync_skips_malformed_plan(db):
    result = await sync_catalog(db, FakeCommbitz([{"sku": "NO-ID"}]))
    assert result.skipped == 1
    assert result.created == 0


async def test_legacy_products_table_migrates(tmp_path):
    """旧库 products 表没有 sku 列时，connect 迁移后同步可用。"""
    path = str(tmp_path / "legacy.db")
    conn = await aiosqlite.connect(path)
    await conn.execute(
        """CREATE TABLE products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        price_cents INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD',
        active INTEGER NOT NULL DEFAULT 1)"""
    )
    await conn.commit()
    await conn.close()

    db = Database(path)
    await db.connect()
    try:
        created = await db.upsert_product_from_upstream(
            sku="S1", name="n", description="d", upstream_plan_id="p1"
        )
        assert created is True
        row = await db._one("SELECT * FROM products WHERE sku = 'S1'")
        assert row is not None and row["upstream_plan_id"] == "p1"
    finally:
        await db.close()


# ---- 启动接线 sync_upstream_catalog ----

TOKEN_RESPONSE = {
    "message": "ok",
    "statusCode": 201,
    "data": {"accessToken": "acc-1", "refreshToken": "ref-1", "expiresIn": 3600},
}


def _wired_settings(**upstream) -> Settings:
    return Settings(upstream=upstream)


async def test_wiring_noop_without_provider(db, httpx_mock):
    await sync_upstream_catalog(db, _wired_settings())
    # 未发任何 HTTP 请求（httpx_mock 对未匹配请求会报错）


async def test_wiring_requires_credentials(db, httpx_mock):
    await sync_upstream_catalog(db, _wired_settings(provider="commbitz"))
    # 缺密钥时不发请求，只记警告


async def test_wiring_syncs_catalog(db, httpx_mock):
    httpx_mock.add_response(json=TOKEN_RESPONSE)
    httpx_mock.add_response(
        json={
            "statusCode": 200,
            "data": {
                "plans": [
                    {
                        "_id": "p1",
                        "sku": "US-1",
                        "name": "US 1GB",
                        "simCategory": "esim",
                        "planIsFor": 3,
                        "pricing": {"currency": {"code": "USD"}},
                    }
                ],
                "pagination": {"hasNextPage": False},
            },
        }
    )
    await sync_upstream_catalog(db, _wired_settings(provider="commbitz", api_key="k", secret_key="s"))
    row = await db._one("SELECT * FROM products WHERE sku = 'US-1'")
    assert row is not None
    assert row["upstream_plan_id"] == "p1"
    assert row["active"] == 0
