"""后台通知共享限速；不持有数据库连接或跨买家的网络锁。"""

import asyncio
import time


class NotificationThrottle:
    def __init__(self) -> None:
        self._global_next = 0.0
        self._chat_next: dict[int, float] = {}

    async def wait(self, chat_id: int) -> None:
        while True:
            now = time.monotonic()
            delay = max(self._global_next, self._chat_next.get(chat_id, 0)) - now
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            # 同一 event loop 内，此处到赋值之间没有 await。
            self._global_next = now + 0.05
            self._chat_next[chat_id] = now + 1
            if len(self._chat_next) > 1024:
                self._chat_next = {key: value for key, value in self._chat_next.items() if value > now}
            return
