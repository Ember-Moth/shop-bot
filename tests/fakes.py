from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendDocument, SendMessage, SendPhoto
from aiogram.types import Message, User


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.fail_send = False
        self.fail_photo = False
        self.fail_document = False
        self.fail_commands_for = set()  # 模拟 Telegram 拒绝为这些 chat scope 注册菜单

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 - aiogram session API
        self.sent.append(method)
        if method.__api_method__ == "getMe":
            return User(id=123456, is_bot=True, first_name="Audit", username="audit_bot")
        if method.__api_method__ in ("sendMessage", "editMessageText"):
            if self.fail_send:
                raise RuntimeError("simulated Telegram unavailable")
            assert isinstance(method, (SendMessage, EditMessageText)) and method.chat_id is not None
            chat_id = int(method.chat_id)
            return Message.model_validate(
                {
                    "message_id": 99,
                    "date": 0,
                    "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                    "text": method.text,
                }
            )
        if method.__api_method__ == "sendPhoto":
            if self.fail_send or self.fail_photo:
                raise RuntimeError("simulated Telegram image unavailable")
            assert isinstance(method, SendPhoto)
            return Message.model_validate(
                {
                    "message_id": 100,
                    "date": 0,
                    "chat": {"id": int(method.chat_id), "type": "private"},
                    "caption": method.caption,
                    "photo": [{"file_id": "photo", "file_unique_id": "photo-id", "width": 512, "height": 512}],
                }
            )
        if method.__api_method__ == "sendDocument":
            if self.fail_send or self.fail_document:
                raise RuntimeError("simulated Telegram document unavailable")
            assert isinstance(method, SendDocument)
            return Message.model_validate(
                {
                    "message_id": 101,
                    "date": 0,
                    "chat": {"id": int(method.chat_id), "type": "private"},
                    "caption": method.caption,
                    "document": {"file_id": "document", "file_unique_id": "document-id", "file_name": "esims.zip"},
                }
            )
        if method.__api_method__ == "setMyCommands":
            scope = getattr(method, "scope", None)
            if scope is not None and getattr(scope, "chat_id", None) in self.fail_commands_for:
                raise TelegramBadRequest(method=method, message="chat not found")
        return True

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""


class FakeCommbitzGateway:
    """满足 PurchaseGateway 协议的离线假上游；记录调用供断言。"""

    def __init__(self, created=None, details=None):
        self.created = created or {"_id": "up-fake", "status": "pending"}
        self.details = details or {}
        self.create_calls = 0
        self.detail_calls = 0

    async def create_request(self, **kwargs):
        self.create_calls += 1
        return dict(self.created)

    async def get_order_details(self, request_id):
        self.detail_calls += 1
        return dict(self.details)

    async def submit_kyc_documents_json(self, request_id, documents):
        return {"kycStatus": "submitted"}

    async def submit_kyc_documents_files(self, request_id, files):
        return {"kycStatus": "submitted"}

    async def get_esim_usage(self, **kwargs):
        return {}
