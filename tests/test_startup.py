import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiohttp import ClientSession
from aiohttp.test_utils import unused_port

import shop_bot
from shop_bot.config import Settings
from shop_bot.db import Database, FSMStorage


@pytest.mark.parametrize("registration_fails", [False, True])
async def test_actual_startup_authentication_and_resource_cleanup(tmp_path, monkeypatch, bot, registration_fails):
    settings = Settings(
        bot_token=bot.token,
        database_path=str(tmp_path / "startup.db"),
        webhook={"url": "https://bot.example.com", "port": unused_port(), "secret_token": "startup-secret"},
    )
    database = Database(settings.database_path)
    ready = asyncio.Event()

    async def register(*args, **kwargs):
        if registration_fails:
            raise RuntimeError("simulated registration failure")
        ready.set()

    registration = AsyncMock(side_effect=register)
    monkeypatch.setattr(shop_bot, "get_settings", lambda: settings)
    monkeypatch.setattr(shop_bot, "Database", lambda path: database)
    monkeypatch.setattr(shop_bot, "Bot", lambda token: bot)
    def fake_dispatcher(db, purchaser, epay, commbitz):
        return Dispatcher(storage=FSMStorage(db))

    monkeypatch.setattr(shop_bot, "build_dispatcher", fake_dispatcher)
    monkeypatch.setattr(bot, "set_webhook", registration)
    task = asyncio.create_task(shop_bot.amain())
    if registration_fails:
        with pytest.raises(RuntimeError, match="simulated registration failure"):
            await asyncio.wait_for(task, timeout=2)
    else:
        try:
            await asyncio.wait_for(ready.wait(), timeout=2)
            registration.assert_awaited_once_with("https://bot.example.com/webhook", secret_token="startup-secret")
            async with ClientSession() as client:
                response = await client.post(f"http://127.0.0.1:{settings.webhook.port}/webhook", json={"update_id": 1})
                assert response.status == 401
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    # 启动失败和正常退出都必须释放连接，不能留下 aiosqlite 后台线程。
    with pytest.raises(RuntimeError, match="must be called first"):
        await database.get_user(1)
