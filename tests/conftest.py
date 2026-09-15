import pytest

from shop_bot.db import Database
from shop_bot.models import Product


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def user(db):
    return await db.upsert_user(telegram_id=42, username="tester")


@pytest.fixture
async def product(db):
    await db.seed_products(
        [Product(id=1, name="demo", description="d", price_cents=999, currency="USD")]
    )
    return (await db.list_products())[0]
