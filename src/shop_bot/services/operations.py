"""运行状态与管理员告警：公共检查不泄露凭据，告警按收件人持久化去重。"""

import asyncio
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import ErrorEvent

from ..config import OperationsSettings
from ..db import Database
from ..logging_config import get_logger
from .backup import BackupManager

logger = get_logger(__name__)
MONITORED_KEYS = (
    "manual_purchases",
    "stalled_orders",
    "pending_notifications",
    "wallet_notifications",
    "business_notifications",
    "database",
    "recovery",
    "worker_recovery",
    "worker_monitor",
    "worker_backup",
    "backup",
    "catalog_sync",
    "telegram_handler",
)


@dataclass
class RuntimeState:
    webhook_ready: bool = False
    stopping: bool = False
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    heartbeats: dict[str, float] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    def beat(self, name: str) -> None:
        self.heartbeats[name] = time.monotonic()

    def worker_ok(self, name: str, stale_after: float) -> bool:
        task = self.tasks.get(name)
        return bool(
            task
            and not task.done()
            and (name == "backup" or time.monotonic() - self.heartbeats.get(name, 0) <= stale_after)
        )


class Operations:
    def __init__(
        self,
        db: Database,
        bot: Bot,
        admin_ids: list[int],
        settings: OperationsSettings,
        *,
        runtime: RuntimeState,
        backups: BackupManager,
    ) -> None:
        self.db, self.bot, self.settings = db, bot, settings
        self.admin_ids = list(dict.fromkeys(admin_ids))
        self.runtime, self.backups = runtime, backups
        self._fallback_sent: dict[int, float] = {}
        self._delivery_lock = asyncio.Lock()

    async def readiness(self) -> dict[str, bool]:
        checks = {"webhook": self.runtime.webhook_ready and not self.runtime.stopping}
        stale = max(self.settings.worker_stale_seconds, self.settings.check_interval_seconds * 3)
        checks.update({name: self.runtime.worker_ok(name, stale) for name in ("recovery", "monitor")})
        if self.backups.settings.enabled:
            checks["backup_worker"] = self.runtime.worker_ok("backup", stale)
        try:
            async with asyncio.timeout(self.settings.health_timeout_seconds):
                await self.db.ping()
            checks["database"] = "database" not in self.runtime.failures
        except Exception:
            checks["database"] = False
        return checks

    async def deliver_alerts(self) -> None:
        async with self._delivery_lock:
            await self._deliver_alerts_locked()

    async def _deliver_alerts_locked(self) -> None:
        if not self.settings.alerts_enabled:
            return
        for admin_id in self.admin_ids:
            self.runtime.beat("monitor")
            for alert in await self.db.pending_alerts(admin_id, time.time(), self.settings.alert_cooldown_seconds):
                heading = "⚠️ 运维告警" if alert["active"] else "✅ 告警恢复"
                hint = "/status 查看运行状态"
                if alert["key"] == "stalled_orders" and alert["active"]:
                    hint = "请核对上游订单；该提醒不代表采购已失败。\n相同待办不重复提醒，/orders paid 查看订单。"
                try:
                    await self.bot.send_message(
                        admin_id,
                        f"{heading} [{alert['key']}]\n{alert['summary']}\n{hint}",
                        parse_mode=None,
                        request_timeout=5,
                    )
                except Exception as exc:
                    logger.warning("operator alert delivery failed", extra={"error": type(exc).__name__})
                    break  # 同一管理员暂不可达，留到下一轮重试，其他管理员照常通知。
                await self.db.mark_alert_sent(alert["key"], admin_id, alert["revision"], time.time())

    async def monitor_once(self) -> None:
        self.runtime.beat("monitor")
        try:
            async with asyncio.timeout(self.settings.health_timeout_seconds):
                issues = await self.db.operational_issues(
                    self.settings.stale_order_seconds,
                    self.settings.notification_stale_seconds,
                )
            self.runtime.failures.pop("database", None)
            issues.update(self.runtime.failures)
            stale = max(self.settings.worker_stale_seconds, self.settings.check_interval_seconds * 3)
            for name in self.runtime.tasks:
                if not self.runtime.worker_ok(name, stale):
                    issues[f"worker_{name}"] = f"后台任务 {name} 已退出或长时间没有进度，请检查服务日志"
            if self.backups.error:
                issues["backup"] = f"数据库备份失败（{self.backups.error}），现有备份保留，稍后自动重试"
            for key in MONITORED_KEYS:
                await self.db.set_alert(key, issues.get(key))
            await self.deliver_alerts()
            self.runtime.beat("monitor")
        except Exception as exc:
            self.runtime.failures["database"] = "运维检查或告警存储暂不可用，请检查数据库和服务日志"
            logger.error("operations scan failed", extra={"error": type(exc).__name__})  # noqa: TRY400 - 异常原文可能含敏感信息
            # 数据库故障时不能依赖同一个数据库写告警；有限频率直接通知管理员。
            await self._database_fallback()

    async def _database_fallback(self) -> None:
        if not self.settings.alerts_enabled:
            return
        for admin_id in self.admin_ids:
            now = time.monotonic()
            if now - self._fallback_sent.get(admin_id, float("-inf")) < self.settings.alert_cooldown_seconds:
                continue
            try:
                await self.bot.send_message(
                    admin_id,
                    "⚠️ 运维检查无法访问数据库或告警记录，请检查 /readyz 和服务日志。",
                    request_timeout=5,
                )
            except Exception:
                logger.warning("database outage alert delivery failed")
                continue
            self._fallback_sent[admin_id] = now

    async def on_handler_error(self, event: ErrorEvent) -> bool:
        # 不把异常原文、用户输入、请求 URL、证件内容发给管理员或写入日志。
        self.runtime.failures["telegram_handler"] = (
            "机器人消息处理出现异常，请检查日志；处理完成后 /ackalert telegram_handler"
        )
        logger.error("telegram handler failed", extra={"error": type(event.exception).__name__})
        return True

    async def run(self) -> None:
        while True:
            await self.monitor_once()
            await asyncio.sleep(self.settings.check_interval_seconds)
