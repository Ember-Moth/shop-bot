import json
import sqlite3

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from shop_bot.config import Settings, UpstreamSettings
from shop_bot.db import FSMStorage
from shop_bot.handlers import admin
from shop_bot.handlers.order import cb_confirm, product_quote
from shop_bot.money import parse_price
from shop_bot.services import orders
from tests.test_balance import _balance_callback, menu_message_of


@pytest.mark.parametrize("value", ["0", "-1", "+1", "1e2", "NaN", "1.001", "10000000", "１.０", ""])
def test_price_rejects_invalid_amount(value):
    with pytest.raises(ValueError):
        parse_price(value)


async def test_admin_price_publish_unpublish_and_audit(db, bot, monkeypatch):
    monkeypatch.setattr(admin, "get_settings", lambda: Settings(upstream=UpstreamSettings(provider="commbitz")))
    await db.upsert_product_from_upstream(
        sku="S", name="test", description="", upstream_plan_id="P", request_type="esim"
    )
    msg = menu_message_of(bot)
    await admin.cmd_publish(msg.model_copy(update={"text": "/publish 1"}), db)
    assert not (await db.get_product(1)).active
    await admin.cmd_price(msg.model_copy(update={"text": "/price 1 19.95"}), db)
    product = await db.get_product(1)
    assert product.price_cents == 1995 and product.currency == "USD" and not product.active
    await admin.cmd_publish(msg.model_copy(update={"text": "/publish 1"}), db)
    assert len(await db.list_products()) == 1
    await admin.cmd_unpublish(msg.model_copy(update={"text": "/unpublish 1"}), db)
    assert await db.list_products() == []
    events = await db._all("SELECT * FROM product_events ORDER BY id")
    assert len(events) == 3
    assert all(event["actor_id"] == 42 for event in events)
    assert json.loads(events[0]["before_json"])["price_cents"] == 0
    assert json.loads(events[0]["after_json"])["price_cents"] == 1995
    await db.upsert_product_from_upstream(
        sku="S", name="changed", description="", upstream_plan_id="P", request_type="esim"
    )
    assert not (await db.get_product(1)).active
    assert (await db.get_product(1)).price_cents == 1995
    assert (await db.get_product(1)).name == "test"  # 名称不被目录同步覆盖


async def test_admin_rename_and_audit(db, bot):
    await db.upsert_product_from_upstream(
        sku="S", name="old", description="", upstream_plan_id="P", request_type="esim"
    )
    msg = menu_message_of(bot)
    await admin.cmd_rename(msg.model_copy(update={"text": "/rename 1 美国1GB·7天"}), db)
    assert (await db.get_product(1)).name == "美国1GB·7天"
    events = await db._all("SELECT * FROM product_events")
    assert len(events) == 1
    assert json.loads(events[0]["before_json"])["name"] == "old"
    assert json.loads(events[0]["after_json"])["name"] == "美国1GB·7天"
    # 目录同步不覆盖人工名称
    await db.upsert_product_from_upstream(
        sku="S", name="upstream name", description="", upstream_plan_id="P", request_type="esim"
    )
    assert (await db.get_product(1)).name == "美国1GB·7天"
    # 用法错误与非法名称
    await admin.cmd_rename(msg.model_copy(update={"text": "/rename 1"}), db)
    assert any("用法" in (m.text or "") for m in bot.session.sent)
    with pytest.raises(ValueError, match="名称"):
        await db.configure_product(1, name="   ")
    # 名称进入买家可见目录，多行消息注入的换行必须拒绝（审计 P2-1）
    with pytest.raises(ValueError, match="控制字符"):
        await db.configure_product(1, name="美国\n1GB")
    await admin.cmd_rename(msg.model_copy(update={"text": "/rename 1 美国\n1GB"}), db)
    assert any("控制字符" in (m.text or "") for m in bot.session.sent)


async def test_admin_describe_and_audit(db, bot):
    await db.upsert_product_from_upstream(
        sku="S", name="plan", description="esim · planIsFor=6", upstream_plan_id="P", request_type="esim"
    )
    msg = menu_message_of(bot)
    await admin.cmd_describe(msg.model_copy(update={"text": "/describe 1 美国TMO 100条短信+50分钟通话-30天"}), db)
    assert (await db.get_product(1)).description == "美国TMO 100条短信+50分钟通话-30天"
    events = await db._all("SELECT * FROM product_events")
    assert len(events) == 1
    assert json.loads(events[0]["before_json"])["description"] == "esim · planIsFor=6"
    assert json.loads(events[0]["after_json"])["description"] == "美国TMO 100条短信+50分钟通话-30天"
    # 目录同步不覆盖人工描述
    await db.upsert_product_from_upstream(
        sku="S", name="plan", description="esim · planIsFor=9", upstream_plan_id="P", request_type="esim"
    )
    assert (await db.get_product(1)).description == "美国TMO 100条短信+50分钟通话-30天"
    # 非法描述
    await admin.cmd_describe(msg.model_copy(update={"text": "/describe 1"}), db)
    assert any("用法" in (m.text or "") for m in bot.session.sent)
    with pytest.raises(ValueError, match="描述"):
        await db.configure_product(1, description="x" * 501)
    with pytest.raises(ValueError, match="控制字符"):
        await db.configure_product(1, description="多行\n描述")


async def test_live_publish_requires_mapping(db, product):
    await db.configure_product(product.id, active=False)
    with pytest.raises(ValueError, match="SKU"):
        await db.configure_product(product.id, active=True, require_upstream=True)
    assert not (await db.get_product(product.id)).active


async def test_reprice_requires_fresh_confirmation_and_old_orders_keep_snapshot(db, user, product, bot):
    original = await orders.create_order(db, user.id, product, 1)
    context = FSMContext(storage=FSMStorage(db), key=StorageKey(bot_id=1, chat_id=42, user_id=42))
    await context.set_data({"product_id": product.id, "quantity": 1, "product_quote": product_quote(product)})
    await db.configure_product(product.id, price_cents=1234, currency="USD", actor_id=700)
    callback = _balance_callback(bot, original.id).model_copy(update={"data": "order:confirm"})
    await cb_confirm(callback, context, db, None)
    assert len(await db.list_orders()) == 1
    assert any("12.34 USD" in (getattr(m, "text", None) or "") for m in bot.session.sent)
    await cb_confirm(callback, context, db, None)
    current = (await db.list_orders())[0]
    assert current.amount_cents == 1234 and current.currency == "USD"
    saved_original = await db.get_order(original.id)
    assert saved_original.amount_cents == original.amount_cents and saved_original.currency == "CNY"


async def test_atomic_order_creation_rejects_stale_price_and_unpublish(db, user, product):
    await db.configure_product(product.id, price_cents=1500)
    with pytest.raises(ValueError, match="报价"):
        await orders.create_order(db, user.id, product, 1)
    latest = await db.get_product(product.id)
    await db.configure_product(product.id, active=False)
    with pytest.raises(ValueError, match="下架"):
        await orders.create_order(db, user.id, latest, 1)
    assert await db.list_orders() == []


async def test_product_audit_and_update_roll_back_together(db, product):
    async with db.transaction() as conn:
        await conn.execute("""CREATE TRIGGER fail_product_event BEFORE INSERT ON product_events
            BEGIN SELECT RAISE(ABORT, 'audit failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="audit failure"):
        await db.configure_product(product.id, price_cents=1000, actor_id=700)
    assert (await db.get_product(product.id)).price_cents == product.price_cents


async def test_whois_searches_by_telegram_id_username_and_display_name(db, bot):
    await db.upsert_user(111, "alice", "爱丽丝")
    await db.upsert_user(222, "bob", None)
    msg = menu_message_of(bot)
    # (查询词, 期望命中的 TG ID 子串, 是否应命中)
    for query, hit, found in (
        ("111", "TG 111", True),
        ("alice", "@alice", True),
        ("丽丝", "爱丽丝", True),
        ("bob", "TG 222", True),
        ("nobody", "", False),
    ):
        await admin.cmd_whois(msg.model_copy(update={"text": f"/whois {query}"}), db)
        text = bot.session.sent[-1].text or ""
        if found:
            assert hit in text
        else:
            assert "没有匹配" in text


async def test_whois_usage_and_empty(db, bot):
    msg = menu_message_of(bot)
    await admin.cmd_whois(msg.model_copy(update={"text": "/whois"}), db)
    assert "用法" in (bot.session.sent[-1].text or "")
