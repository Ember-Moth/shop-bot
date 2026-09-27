"""显示时区：默认北京时间，数据库仍存 UTC；可配置，时区数据库缺失时回退。"""

import logging
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

import pytest
from pydantic import ValidationError

from shop_bot import timefmt
from shop_bot.config import Settings
from shop_bot.handlers import start
from shop_bot.keyboards import MENU_HISTORY
from shop_bot.logging_config import JSONFormatter
from shop_bot.services.daily_report import format_daily_report, yesterday_window
from shop_bot.services.gateway import Checkout
from shop_bot.services.payment_prompts import checkout_lines
from tests.test_payment_prompts import calls, text_message

BEIJING = timedelta(hours=8)


def test_stored_utc_is_shown_in_beijing_time():
    assert timefmt.zone_label() == "北京时间"
    assert timefmt.format_db("2026-09-27 06:35:00") == "09-27 14:35"
    assert timefmt.format_db("2026-09-27 20:10:00", "%Y-%m-%d %H:%M") == "2026-09-28 04:10"  # 跨日
    assert timefmt.format_iso("2026-09-27T06:35:00.5+00:00") == "2026-09-27 14:35:00（北京时间）"
    assert timefmt.format_db("not a time") == "not a time"


def test_timezone_setting_defaults_to_beijing_and_rejects_unknown_names():
    assert Settings().timezone == "Asia/Shanghai"
    assert Settings(timezone=" UTC ").timezone == "UTC"
    with pytest.raises(ValidationError):
        Settings(timezone="Mars/Olympus")


def test_default_zone_survives_missing_tz_database(monkeypatch):
    def missing(name):
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(timefmt, "ZoneInfo", missing)
    timefmt.resolve.cache_clear()
    try:
        assert timefmt.resolve("Asia/Shanghai").utcoffset(None) == BEIJING
        with pytest.raises(ValueError, match="unknown timezone"):
            timefmt.resolve("Europe/Paris")
    finally:
        timefmt.resolve.cache_clear()


def test_display_timezone_is_configurable():
    timefmt.set_display_timezone("UTC")
    try:
        assert timefmt.zone_label() == "UTC"
        assert timefmt.format_db("2026-09-27 06:35:00") == "09-27 06:35"
    finally:
        timefmt.set_display_timezone(timefmt.DEFAULT_TIMEZONE)


def test_gmpay_deadline_is_labelled_beijing_time():
    expires = time.time() + 600
    lines = checkout_lines(
        Checkout(web_url="", address="TAddr", amount="1.41", token="USDT", network="tron", expires_at=expires)
    )
    deadline = datetime.fromtimestamp(expires, UTC) + BEIJING
    assert f"截止 {deadline:%H:%M}（北京时间）" in lines[0]


async def test_history_lists_beijing_times(db, user, bot):
    topup = await db.wallet.create_topup(user.id, 1000, "CNY")
    await db.wallet.complete_topup(topup.id, trade_no="TOPUP-1")
    async with db.transaction() as conn:
        await conn.execute("UPDATE balance_transactions SET created_at = '2026-09-27 06:35:00'")
    start._menu_last_seen.clear()
    await start.menu_router(text_message(bot, MENU_HISTORY), db, None, None)
    history = calls(bot, "sendMessage")[-1].text
    assert history.startswith("🧾 交易记录（北京时间）") and "09-27 14:35 · 充值到余额" in history


def test_json_log_timestamps_carry_beijing_offset():
    record = logging.LogRecord("shop_bot", logging.INFO, __file__, 1, "hello", None, None)
    record.created = datetime(2026, 9, 27, 6, 35, tzinfo=UTC).timestamp()
    assert '"ts": "2026-09-27T14:35:00+08:00"' in JSONFormatter().format(record)


def test_daily_report_uses_beijing_midnight_by_default():
    label, start_utc, end_utc = yesterday_window()
    assert start_utc.endswith("16:00:00") and end_utc.endswith("16:00:00")  # 北京时间 0 点
    assert format_daily_report(
        {"orders": [], "receipts": [], "transactions": [], "wallet_total": []}, label
    ).startswith(f"📊 每日流水 · {label}（北京时间）")
