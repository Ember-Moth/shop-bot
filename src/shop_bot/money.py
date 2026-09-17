"""本店金额以对应币种的分存储；不同币种从不隐式换算。"""

import re
from decimal import Decimal

SUPPORTED_CURRENCIES = frozenset({"USD", "CNY", "EUR", "GBP", "HKD", "SGD", "AUD", "CAD", "NZD", "CHF"})
DEFAULT_CURRENCY = "USD"
REQUEST_TYPES = frozenset({"esim", "physical", "activation", "recharge", "voucher"})


def parse_price(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", value):
        raise ValueError("价格需为正数，最多两位小数、七位整数")
    cents = int(Decimal(value) * 100)
    if cents <= 0:
        raise ValueError("价格必须大于零")
    return cents


def normalize_currency(value: str) -> str:
    currency = value.strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError("unsupported currency")
    return currency
