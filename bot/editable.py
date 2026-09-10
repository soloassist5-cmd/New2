"""Обёртки над сообщениями Bot API под интерфейс core.anim.

Анимация умеет работать с чем угодно, у чего есть `edit(text)`, поэтому один и
тот же движок крутит кадры и в переписке (от имени владельца), и в личке с ботом.
"""
from __future__ import annotations

NOT_MODIFIED = "message is not modified"


class _Editable:
    def __init__(self, api, chat_id: int, message_id: int):
        self.api, self.chat_id, self.id = api, chat_id, message_id

    async def _apply(self, text: str) -> None:
        raise NotImplementedError

    async def edit(self, text: str, link_preview: bool = False):
        try:
            await self._apply(text)
        except Exception as e:                               # noqa: BLE001
            if NOT_MODIFIED in str(e):
                return self                                  # кадр совпал с прошлым
            raise
        return self


class BizMessage(_Editable):
    """Сообщение, отправленное ботом от имени владельца в его переписке."""

    def __init__(self, api, chat_id: int, message_id: int, connection_id: str):
        super().__init__(api, chat_id, message_id)
        self.connection_id = connection_id

    async def _apply(self, text: str) -> None:
        await self.api.edit_message_text(
            self.chat_id, self.id, text,
            business_connection_id=self.connection_id)


class BotMessage(_Editable):
    """Собственное сообщение бота — например, в личке с владельцем."""

    async def _apply(self, text: str) -> None:
        await self.api.edit_message_text(self.chat_id, self.id, text)


async def animated(api, chat_id: int, final: str, *, icon: str,
                   connection_id: str | None = None, style: str | None = None):
    """Отправляет первый кадр и доигрывает анимацию до финального текста."""
    from core import anim

    first = f"{icon} {anim.bar(0, 3, width=6)}"
    sent = await api.send_message(chat_id, first,
                                  business_connection_id=connection_id)
    target = (BizMessage(api, chat_id, sent["message_id"], connection_id)
              if connection_id else BotMessage(api, chat_id, sent["message_id"]))
    await anim.play(target, final, icon=icon, style=style)
    return target
