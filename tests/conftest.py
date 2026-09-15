import pytest
from aiogram import Bot

from shop_bot.db import Database
from shop_bot.models import Product
from shop_bot.services.epay import EPayClient, EPayConfig

from .fakes import FakeSession


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
    await db.seed_products([Product(id=1, name="demo", description="d", price_cents=999, currency="USD")])
    return (await db.list_products())[0]


@pytest.fixture
async def epay():
    client = EPayClient(EPayConfig(pid="1000", key="audit-secret", url="https://pay.example.com"))
    yield client
    await client.close()


@pytest.fixture
async def bot():
    instance = Bot("123456:FAKE_TOKEN_FOR_OFFLINE_TEST", session=FakeSession())
    yield instance
    await instance.session.close()
