from aiogram.client.session.base import BaseSession
from aiogram.types import Message, User


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.fail_send = False

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 - aiogram session API
        self.sent.append(method)
        if method.__api_method__ == "getMe":
            return User(id=123456, is_bot=True, first_name="Audit", username="audit_bot")
        if method.__api_method__ in ("sendMessage", "editMessageText"):
            if self.fail_send:
                raise RuntimeError("simulated Telegram unavailable")
            chat_id = int(method.chat_id)
            return Message.model_validate(
                {
                    "message_id": 99,
                    "date": 0,
                    "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                    "text": method.text,
                }
            )
        return True

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""
