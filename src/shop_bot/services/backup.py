"""SQLite 在线快照：独立只读连接、校验、原子发布、按数据库隔离的保留策略。"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from ..config import BackupSettings, get_settings
from ..logging_config import get_logger

logger = get_logger(__name__)


def backup_database(source: Path, settings: BackupSettings) -> Path:
    source = source.resolve(strict=True)
    directory = Path(settings.directory)
    if not directory.is_absolute():
        directory = source.parent / directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    prefix = "shop-bot-" + hashlib.sha256(str(source).encode()).hexdigest()[:12] + "-"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    fd, name = tempfile.mkstemp(prefix=prefix + stamp + "-", suffix=".partial", dir=directory)
    os.close(fd)  # mkstemp 的权限为 0600；不会把包含证件/余额的快照公开给其他用户。
    temporary = Path(name)
    final = temporary.with_suffix(".sqlite3")
    started = time.monotonic()

    def check_deadline(*_args):
        if time.monotonic() - started > settings.timeout_seconds:
            raise TimeoutError("backup deadline exceeded")

    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(temporary)) as writer:
                reader.backup(writer, pages=256, progress=check_deadline, sleep=0.05)
                writer.set_progress_handler(lambda: int(time.monotonic() - started > settings.timeout_seconds), 1000)
                result = writer.execute("PRAGMA integrity_check").fetchall()
                if result != [("ok",)]:
                    raise ValueError("backup integrity check failed")
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        temporary.replace(final)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    # 仅处理本数据库生成的完整快照；其他文件、符号链接、失败临时文件都不清理。
    pattern = re.compile(re.escape(prefix) + r"[0-9]{8}T[0-9]{12}Z-[a-z0-9_]+\.sqlite3")
    snapshots = sorted(
        (p for p in directory.iterdir() if pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink()),
        key=lambda p: p.name,
        reverse=True,
    )
    for expired in snapshots[settings.keep :]:
        expired.unlink()
    return final


class BackupManager:
    def __init__(self, database_path: str, settings: BackupSettings) -> None:
        self.source = Path(database_path)
        self.settings = settings
        self.last_success: str | None = None
        self.last_file: str | None = None
        self.error: str | None = None
        self._lock = asyncio.Lock()

    async def run_once(self) -> Path:
        async with self._lock:
            work = asyncio.create_task(asyncio.to_thread(backup_database, self.source, self.settings))
            try:
                # 关闭时等待这个有截止时间的快照完成，避免遗留仍写文件的线程。
                result = await asyncio.shield(work)
            except asyncio.CancelledError:
                await asyncio.gather(work, return_exceptions=True)
                raise
            except Exception as exc:
                self.error = type(exc).__name__
                raise
            self.last_success = datetime.now(UTC).isoformat()
            self.last_file = result.name
            self.error = None
            return result

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("database backup failed", extra={"error": type(exc).__name__})  # noqa: TRY400 - 异常原文可能含敏感信息
            # 失败后一分钟重试；健康备份按配置周期执行。
            await asyncio.sleep(
                min(60, self.settings.interval_seconds) if self.error else self.settings.interval_seconds
            )


def main() -> None:
    """一次性备份工具；不启动 Bot，不修改或迁移源数据库。"""
    settings = get_settings()
    try:
        result = backup_database(Path(settings.database_path), settings.backup)
    except Exception as exc:
        raise SystemExit(f"backup failed: {type(exc).__name__}") from None
    print(json.dumps({"backup": str(result), "integrity_check": "ok"}))
