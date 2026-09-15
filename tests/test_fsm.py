import pytest
from aiogram.fsm.storage.base import StorageKey

from shop_bot.db import Database, FSMStorage


@pytest.fixture
async def storage(db: Database):
    return FSMStorage(db)


@pytest.fixture
def key():
    return StorageKey(bot_id=1, chat_id=123, user_id=456)


async def test_set_and_get_state(storage, key):
    await storage.set_state(key, "OrderFlow:quantity")
    assert await storage.get_state(key) == "OrderFlow:quantity"


async def test_set_state_none_clears(storage, key):
    await storage.set_state(key, "OrderFlow:quantity")
    await storage.set_state(key, None)
    assert await storage.get_state(key) is None


async def test_set_and_get_data(storage, key):
    await storage.set_data(key, {"product_id": 1, "quantity": 3})
    assert await storage.get_data(key) == {"product_id": 1, "quantity": 3}


async def test_get_data_empty(storage, key):
    assert await storage.get_data(key) == {}


async def test_update_data(storage, key):
    await storage.set_data(key, {"product_id": 1})
    await storage.update_data(key, {"quantity": 3})
    assert await storage.get_data(key) == {"product_id": 1, "quantity": 3}


async def test_state_and_data_independent(storage, key):
    await storage.set_state(key, "OrderFlow:quantity")
    await storage.set_data(key, {"product_id": 1})
    assert await storage.get_state(key) == "OrderFlow:quantity"
    assert await storage.get_data(key) == {"product_id": 1}


async def test_different_keys_isolated(storage):
    key1 = StorageKey(bot_id=1, chat_id=123, user_id=456)
    key2 = StorageKey(bot_id=1, chat_id=123, user_id=789)
    await storage.set_state(key1, "OrderFlow:quantity")
    await storage.set_data(key1, {"product_id": 1})
    assert await storage.get_state(key2) is None
    assert await storage.get_data(key2) == {}


async def test_restart_recovery(tmp_path):
    """模拟 bot 重启：关掉数据库重开，状态应该还在。"""
    db_path = str(tmp_path / "test.db")
    db1 = Database(db_path)
    await db1.connect()
    storage1 = FSMStorage(db1)
    key = StorageKey(bot_id=1, chat_id=123, user_id=456)
    await storage1.set_state(key, "OrderFlow:quantity")
    await storage1.set_data(key, {"product_id": 1, "quantity": 3})
    await db1.close()

    # 重启后重开同一个数据库文件
    db2 = Database(db_path)
    await db2.connect()
    storage2 = FSMStorage(db2)
    assert await storage2.get_state(key) == "OrderFlow:quantity"
    assert await storage2.get_data(key) == {"product_id": 1, "quantity": 3}
    await db2.close()
