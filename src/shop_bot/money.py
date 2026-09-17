"""本店金额以对应币种的分存储；不同币种从不隐式换算。"""

SUPPORTED_CURRENCIES = frozenset({"USD", "CNY", "EUR", "GBP", "HKD", "SGD", "AUD", "CAD", "NZD", "CHF"})
DEFAULT_CURRENCY = "USD"


def normalize_currency(value: str) -> str:
    currency = value.strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError("unsupported currency")
    return currency
