"""Telegram 文本边界工具；按 UTF-16 单位保守计数，不拆开 Unicode 字符。"""


def text_units(text: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def truncate_text(text: str, limit: int) -> str:
    if text_units(text) <= limit:
        return text
    if limit <= 0:
        return ""
    result = []
    remaining = limit - 1  # 预留省略号
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if width > remaining:
            break
        result.append(char)
        remaining -= width
    return "".join(result) + "…"
