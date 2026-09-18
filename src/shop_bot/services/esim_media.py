"""从已持久化的 LPA 生成安装二维码；不访问外部图片服务，不把安装码写入日志或临时文件。"""

import io
import json
import re
from dataclasses import dataclass

import segno

MAX_LPA_BYTES = 2000


@dataclass(frozen=True)
class EsimMedia:
    iccid: str
    lpa: str


def serialize_esims(esims: list[dict]) -> str:
    return json.dumps([{"iccid": esim["iccid"], "lpa": esim["lpa"]} for esim in esims], ensure_ascii=False)


def delivery_esims(serialized: str | None, payload: str | None, quantity: int) -> list[EsimMedia]:
    if serialized is not None:
        records = json.loads(serialized)
        if not isinstance(records, list) or len(records) != quantity:
            raise ValueError("invalid stored esim count")
    else:
        # 兼容旧订单：只识别本项目原有格式，其他商品/模拟货品保持文本交付。
        # 二维码 URL 行在早期版本存在、现已移除，故设为可选以兼容新旧 payload。
        records = []
        for block in (payload or "").split("\n\n"):
            match = re.fullmatch(r"\[\d+\]\nICCID: ([^\r\n]+)\nLPA: ([^\r\n]+)(?:\n二维码: [^\r\n]+)?", block)
            if match:
                records.append({"iccid": match[1], "lpa": match[2]})
        if not records:
            return []
        if len(records) != quantity:
            raise ValueError("incomplete legacy esim records")
    items = []
    for record in records:
        if not isinstance(record, dict):
            raise TypeError("invalid stored esim record")
        iccid, lpa = record.get("iccid"), record.get("lpa")
        if not isinstance(iccid, str) or not iccid or not isinstance(lpa, str) or not lpa.startswith("LPA:"):
            raise ValueError("invalid stored esim installation data")
        if len(lpa.encode("utf-8")) > MAX_LPA_BYTES or any(c in lpa for c in "\r\n\x00"):
            raise ValueError("invalid stored esim installation data")
        items.append(EsimMedia(iccid=iccid, lpa=lpa))
    return items


def qr_png(lpa: str) -> bytes:
    buffer = io.BytesIO()
    # 标准 QR（非 Micro QR），保留四模块白边，避免图片压缩后不易识别。
    segno.make_qr(lpa, error="m", encoding="utf-8").save(buffer, kind="png", scale=8, border=4)
    return buffer.getvalue()
