"""Заглушка Bot API: пишет всё, что бот попытался отправить."""
from __future__ import annotations

import itertools


class FakeBotAPI:
    def __init__(self, *, delete_ok: bool = True):
        self.sent: list[tuple[int, str, str | None]] = []      # (chat_id, text, biz)
        self.edits: list[tuple[int, int, str]] = []
        self.media: list[tuple[int, str, str | None, str | None]] = []
        self.files: list[tuple[int, str]] = []
        self.deleted: list[tuple[str, list[int]]] = []
        self.commands: list[dict] | None = None
        self.delete_ok = delete_ok
        self.downloads: dict[str, bytes] = {}
        self.uploads: list[bytes] = []
        self.connection = None            # что вернёт getBusinessConnection
        self.chat: dict | None = {}       # что вернёт getChat
        self.pinned: list[int] = []
        self.unpinned: list[int] = []
        self.connection_lookups = 0
        self.captions: list[str | None] = []
        self.markups: list[dict | None] = []
        self.callbacks: list[tuple[str, str]] = []
        self.markup_edits: list[tuple] = []
        self.edit_markups: list[dict] = []
        self._ids = itertools.count(1000)

    async def send_message(self, chat_id, text, *, business_connection_id=None,
                           reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, business_connection_id))
        self.markups.append(reply_markup)
        return {"message_id": next(self._ids)}

    async def answer_callback(self, callback_query_id, text="", show_alert=False):
        self.callbacks.append((callback_query_id, text))
        return True

    async def edit_message_text(self, chat_id, message_id, text, *,
                                business_connection_id=None, reply_markup=None):
        self.edits.append((chat_id, message_id, text))
        self.edit_markups.append(reply_markup or {"inline_keyboard": []})
        return {"message_id": message_id}

    async def send_media(self, chat_id, file_id, media_type, *, caption=None,
                         business_connection_id=None, reply_markup=None):
        self.media.append((chat_id, file_id, media_type, caption))
        self.markups.append(reply_markup)
        return {"message_id": next(self._ids)}

    async def edit_message_reply_markup(self, chat_id, message_id,
                                        reply_markup=None):
        self.markup_edits.append((chat_id, message_id, reply_markup))
        return True

    async def send_file(self, chat_id, data, filename, *, caption=None):
        self.files.append((chat_id, filename))
        self.uploads.append(data)
        self.captions.append(caption)
        return {"message_id": next(self._ids)}

    async def delete_business_messages(self, business_connection_id, message_ids):
        if not self.delete_ok:
            raise RuntimeError("Bad Request: not enough rights")
        self.deleted.append((business_connection_id, list(message_ids)))
        return True

    async def set_my_commands(self, commands):
        self.commands = commands
        return True

    async def get_chat(self, chat_id):
        if self.chat is None:
            raise RuntimeError("Bad Request: chat not found")
        return self.chat

    async def pin_message(self, chat_id, message_id):
        self.pinned.append(message_id)
        return True

    async def unpin_message(self, chat_id, message_id):
        self.unpinned.append(message_id)
        return True

    async def get_business_connection(self, business_connection_id):
        self.connection_lookups += 1
        if self.connection is None:
            raise RuntimeError("Bad Request: business connection not found")
        return self.connection

    async def get_file(self, file_id):
        return {"file_id": file_id, "file_path": f"documents/{file_id}"}

    async def download(self, file_path):
        return self.downloads.get(file_path, b"")

    # ------------------------------------------------------------ помощь --
    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]

    def texts_to(self, chat_id: int) -> list[str]:
        return [text for cid, text, _ in self.sent if cid == chat_id]

    @property
    def all_deleted_ids(self) -> list[int]:
        return [mid for _, ids in self.deleted for mid in ids]


ALL_RIGHTS = {
    "can_read_messages": True,
    "can_reply": True,
    "can_delete_sent_messages": True,
    "can_delete_all_messages": True,
}


def business_message(text="", *, message_id=10, chat_id=777, from_id=777,
                     connection_id="biz1", first_name="Собеседник", reply_to=None,
                     photo=None, date=1700000000):
    """Апдейт business_message в формате Bot API."""
    message = {
        "message_id": message_id,
        "date": date,
        "business_connection_id": connection_id,
        "chat": {"id": chat_id, "type": "private", "first_name": "Чат"},
        "from": {"id": from_id, "first_name": first_name},
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {
            "message_id": reply_to["message_id"],
            "from": reply_to.get("from", {"id": from_id, "first_name": first_name}),
        }
    if photo is not None:
        message.pop("text", None)
        message["caption"] = text
        message["photo"] = [{"file_id": photo, "width": 90},
                            {"file_id": photo, "width": 1280}]
    return message


def approve(user_id: int, chat_id: int | None = None, *, name: str = "Владелец",
            status: str = "approved") -> None:
    """Кладёт пользователя в кэш state.users, минуя базу."""
    from core import state

    state.users[user_id] = {"name": name, "status": status,
                            "chat_id": chat_id if chat_id is not None else user_id,
                            "dnd_since": 0}
