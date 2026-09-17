"""钱包服务：余额充值（EPay）与余额支付。

金额约定：对用户展示/输入单位为元（最多两位小数），存储一律为分（整数）。
入账与扣款都在数据库事务内完成并写 balance_transactions 流水（只增不改）。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ..db import Database
from ..logging_config import get_logger
from ..models import Topup

logger = get_logger(__name__)

MIN_TOPUP_CENTS = 100  # 单笔最低 1 元
MAX_TOPUP_CENTS = 10000_00  # 单笔最高 10000 元

_AMOUNT_RE = re.compile(r"^\d{1,5}([.]\d{1,2})?$")
# 调账金额：可带正负号，整数部分限 7 位（百万级）防手滑多敲零
_SIGNED_AMOUNT_RE = re.compile(r"^[+-]?\d{1,7}([.]\d{1,2})?$")


def parse_signed_amount(text: str) -> int | None:
    """把管理员调账金额（元，可带正负号）解析为分；非法返回 None。"""
    raw = text.strip().replace("元", "")
    if not _SIGNED_AMOUNT_RE.fullmatch(raw):
        return None
    try:
        return int((Decimal(raw) * 100).to_integral_value())
    except InvalidOperation:
        return None


def parse_topup_amount(text: str) -> int | None:
    """把用户输入的金额（元）解析为分；非法或超范围返回 None。"""
    raw = text.strip().replace("元", "")
    if not _AMOUNT_RE.fullmatch(raw):
        return None
    try:
        cents = int((Decimal(raw) * 100).to_integral_value())
    except InvalidOperation:
        return None
    if cents < MIN_TOPUP_CENTS or cents > MAX_TOPUP_CENTS:
        return None
    return cents


def format_cents(cents: int) -> str:
    return f"{cents / 100:.2f}"


async def create_topup(db: Database, user_id: int, amount_cents: int) -> Topup:
    topup = await db.create_topup(user_id, amount_cents)
    logger.info(
        "topup created",
        extra={"order_id": topup.id, "user_id": user_id, "error": f"{amount_cents}"},
    )
    return topup
