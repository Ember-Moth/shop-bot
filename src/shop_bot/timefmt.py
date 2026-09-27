"""显示时区：所有给买家和管理员看的时间统一按 ``timezone`` 配置显示，默认北京时间。

数据库时间戳、SQL 中的时间比较和备份文件名等内部记录仍用 UTC，
排序和比较不受显示时区影响；这里只负责换算与格式化。
"""

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
_LABELS = {"Asia/Shanghai": "北京时间", "Asia/Chongqing": "北京时间", "UTC": "UTC", "Etc/UTC": "UTC"}
_FALLBACKS: dict[str, tzinfo] = {
    # 系统缺少时区数据库时仍能用默认值启动；北京时间没有夏令时，固定 UTC+8 即可。
    "Asia/Shanghai": timezone(timedelta(hours=8), "Asia/Shanghai"),
    "UTC": UTC,
    "Etc/UTC": UTC,
}


class _Display:
    """启动时按配置设置一次；默认北京时间。"""

    name = DEFAULT_TIMEZONE


@lru_cache
def resolve(name: str) -> tzinfo:
    """把 IANA 时区名解析为 tzinfo；未知名称抛 ValueError。"""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        if name in _FALLBACKS:
            return _FALLBACKS[name]
        raise ValueError(f"unknown timezone: {name}") from None


def set_display_timezone(name: str) -> None:
    resolve(name)
    _Display.name = name


def display_zone() -> tzinfo:
    return resolve(_Display.name)


def zone_label() -> str:
    """消息里标注的时区名，北京时间显示为中文。"""
    return _LABELS.get(_Display.name, _Display.name)


def now() -> datetime:
    return datetime.now(display_zone())


def from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, display_zone())


def from_db(text: object) -> datetime | None:
    """解析 SQLite 的 UTC 文本（YYYY-MM-DD HH:MM:SS），换算到显示时区；无法解析返回 None。"""
    if isinstance(text, datetime):
        moment = text if text.tzinfo else text.replace(tzinfo=UTC)
        return moment.astimezone(display_zone())
    try:
        return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).astimezone(display_zone())
    except ValueError:
        return None


def format_db(text: object, fmt: str = "%m-%d %H:%M") -> str:
    moment = from_db(text)
    return moment.strftime(fmt) if moment is not None else str(text)


def format_iso(text: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """把带时区的 ISO 时间（如备份完成时间）换算到显示时区。"""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"{moment.astimezone(display_zone()).strftime(fmt)}（{zone_label()}）"
