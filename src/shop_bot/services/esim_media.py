"""从已持久化的 LPA 生成安装二维码；不访问外部图片服务，不把安装码写入日志或临时文件。"""

import io
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import segno

MAX_LPA_BYTES = 2000
MAX_ESIMS_PER_ARCHIVE = 100
MAX_ARCHIVE_BYTES = 48_000_000  # Telegram sendDocument 的 50 MB 上限内预留余量


@dataclass(frozen=True)
class EsimMedia:
    iccid: str
    lpa: str
    msisdn: str | None = None
    msisdn_recorded: bool = True

    @property
    def number_text(self) -> str:
        if self.msisdn:
            return self.msisdn
        return "上游未提供号码" if self.msisdn_recorded else "历史订单未保存号码"


def normalize_msisdn(value: object) -> str | None:
    # 可选信息不影响安装交付；保留 + 和前导零，不猜测国家码或从 ICCID 推导。
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if re.fullmatch(r"\+?[0-9][0-9 ()-]{0,63}", value) else None


def serialize_esims(esims: list[dict]) -> str:
    return json.dumps(
        [
            {"iccid": esim["iccid"], "lpa": esim["lpa"], "msisdn": normalize_msisdn(esim.get("msisdn"))}
            for esim in esims
        ],
        ensure_ascii=False,
    )


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
            match = re.fullmatch(
                r"\[\d+\]\nICCID: ([^\r\n]+)(?:\nMSISDN: ([^\r\n]+))?\nLPA: ([^\r\n]+)"
                r"(?:\n二维码: [^\r\n]+)?",
                block,
            )
            if match:
                record = {"iccid": match[1], "lpa": match[3]}
                if match[2] is not None:
                    record["msisdn"] = match[2]
                records.append(record)
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
        items.append(
            EsimMedia(
                iccid=iccid,
                lpa=lpa,
                msisdn=normalize_msisdn(record.get("msisdn")),
                msisdn_recorded="msisdn" in record,
            )
        )
    return items


def qr_png(lpa: str) -> bytes:
    buffer = io.BytesIO()
    # 标准 QR（非 Micro QR），保留四模块白边，避免图片压缩后不易识别。
    segno.make_qr(lpa, error="m", encoding="utf-8").save(buffer, kind="png", scale=8, border=4)
    return buffer.getvalue()


def esim_zip(
    order_id: int,
    esims: Sequence[EsimMedia],
    start_index: int = 0,
    progress: Callable[[], None] | None = None,
) -> bytes:
    """在内存中生成一个独立可解压的 ZIP；编号是订单内序号，不使用上游字符串作为路径。"""
    if not esims or len(esims) > MAX_ESIMS_PER_ARCHIVE or start_index < 0:
        raise ValueError("invalid esim archive batch")
    buffer = io.BytesIO()

    def write(archive: ZipFile, name: str, data: bytes | str) -> None:
        # 固定 ZIP 元数据，让同一份货品重试/补发时生成相同文件；所有路径由本地编号构造。
        info = ZipInfo(name)
        info.compress_type = ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o100600 << 16
        archive.writestr(info, data)
        if buffer.tell() > MAX_ARCHIVE_BYTES:
            raise ValueError("esim archive exceeds document size limit")

    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        write(
            archive,
            "README.txt",
            f"订单 #{order_id}\n本包包含第 {start_index + 1}–{start_index + len(esims)} 张 eSIM。\n"
            "请先解压。每个编号目录包含：\n"
            "qrcode.png：安装二维码\ninstallation.txt：号码、ICCID 与完整 LPA 安装资料\n"
            "lpa.txt：仅含完整 LPA，便于复制\n"
            "esims.json 是本包的卡片汇总清单，编号与二维码目录对应。\n",
        )
        records = []
        for offset, esim in enumerate(esims):
            index = start_index + offset + 1
            directory = f"{index:04d}"
            write(archive, f"{directory}/qrcode.png", qr_png(esim.lpa))
            write(
                archive,
                f"{directory}/installation.txt",
                f"订单 #{order_id} · eSIM {index}\n号码: {esim.number_text}\nICCID: {esim.iccid}\nLPA: {esim.lpa}\n",
            )
            write(archive, f"{directory}/lpa.txt", esim.lpa)
            records.append(
                {
                    "index": index,
                    "iccid": esim.iccid,
                    "msisdn": esim.msisdn,
                    "msisdn_recorded": esim.msisdn_recorded,
                    "lpa": esim.lpa,
                    "qrcode": f"{directory}/qrcode.png",
                }
            )
            if progress is not None:
                progress()
        write(archive, "esims.json", json.dumps({"order_id": order_id, "esims": records}, ensure_ascii=False, indent=2))
    if buffer.tell() > MAX_ARCHIVE_BYTES:
        raise ValueError("esim archive exceeds document size limit")
    return buffer.getvalue()
