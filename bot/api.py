"""Минимальный асинхронный клиент Bot API поверх aiohttp."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from bot import markup

log = logging.getLogger("botapi")

BASE = "https://api.telegram.org"
MAX_RETRIES = 3
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

# Метод отправки для каждого типа медиа и имя поля с файлом.
MEDIA_METHODS = {
    "фото": ("sendPhoto", "photo"),
    "видео": ("sendVideo", "video"),
    "видеосообщение": ("sendVideoNote", "video_note"),
    "гифка": ("sendAnimation", "animation"),
    "голосовое": ("sendVoice", "voice"),
    "аудио": ("sendAudio", "audio"),
    "стикер": ("sendSticker", "sticker"),
    "документ": ("sendDocument", "document"),
}


class BotAPIError(Exception):
    def __init__(self, method: str, code: int, description: str,
                 parameters: dict | None = None):
        super().__init__(f"{method}: {code} {description}")
        self.method, self.code = method, code
        self.description, self.parameters = description, parameters or {}

    @property
    def retry_after(self) -> int:
        return int(self.parameters.get("retry_after", 0))


class BotAPI:
    def __init__(self, token: str, session: aiohttp.ClientSession | None = None):
        self.token = token
        self._session = session
        self._own_session = session is None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=90))

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------ транспорт --
    async def call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        for key, value in list(payload.items()):
            if isinstance(value, (dict, list)):
                payload[key] = json.dumps(value)

        url = f"{BASE}/bot{self.token}/{method}"
        for attempt in range(MAX_RETRIES):
            async with self._session.post(url, data=payload) as response:
                body = await response.json(content_type=None)
            if body.get("ok"):
                return body.get("result")
            error = BotAPIError(method, body.get("error_code", 0),
                                body.get("description", ""), body.get("parameters"))
            if error.code == 429 and attempt < MAX_RETRIES - 1:
                delay = error.retry_after or 1
                log.warning("флуд-лимит на %s, жду %s с", method, delay)
                await asyncio.sleep(delay)
                continue
            raise error
        raise error

    async def upload(self, method: str, field: str, data: bytes, filename: str,
                     **params: Any) -> Any:
        form = aiohttp.FormData()
        for key, value in params.items():
            if value is not None:
                form.add_field(key, str(value))
        form.add_field(field, data, filename=filename)
        url = f"{BASE}/bot{self.token}/{method}"
        async with self._session.post(url, data=form) as response:
            body = await response.json(content_type=None)
        if not body.get("ok"):
            raise BotAPIError(method, body.get("error_code", 0),
                              body.get("description", ""), body.get("parameters"))
        return body.get("result")

    # -------------------------------------------------------------- методы --
    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_updates(self, offset: int | None, timeout: int,
                          allowed_updates: list[str]) -> list[dict]:
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=allowed_updates)

    async def send_message(self, chat_id: int, text: str, *,
                           business_connection_id: str | None = None,
                           reply_to: int | None = None,
                           reply_markup: dict | None = None) -> dict:
        return await self.call(
            "sendMessage", chat_id=chat_id,
            text=markup.trim(markup.to_html(text), TEXT_LIMIT),
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            business_connection_id=business_connection_id,
            reply_parameters={"message_id": reply_to} if reply_to else None,
            reply_markup=reply_markup,
        )

    async def answer_callback(self, callback_query_id: str, text: str = "",
                              show_alert: bool = False) -> bool:
        return await self.call("answerCallbackQuery",
                               callback_query_id=callback_query_id,
                               text=text or None, show_alert=show_alert or None)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, *,
                                business_connection_id: str | None = None,
                                reply_markup: dict | None = None) -> Any:
        return await self.call(
            "editMessageText", chat_id=chat_id, message_id=message_id,
            text=markup.trim(markup.to_html(text), TEXT_LIMIT), parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            business_connection_id=business_connection_id,
            reply_markup=reply_markup,
        )

    async def edit_message_reply_markup(self, chat_id: int, message_id: int,
                                        reply_markup: dict | None = None) -> Any:
        return await self.call("editMessageReplyMarkup", chat_id=chat_id,
                               message_id=message_id, reply_markup=reply_markup)

    async def send_sticker(self, chat_id: int, sticker: str, *,
                           business_connection_id: str | None = None,
                           disable_notification: bool = False) -> dict:
        return await self.call("sendSticker", chat_id=chat_id, sticker=sticker,
                               business_connection_id=business_connection_id,
                               disable_notification=disable_notification or None)

    async def upload_sticker(self, chat_id: int, data: bytes, filename: str, *,
                             business_connection_id: str | None = None,
                             disable_notification: bool = False) -> dict:
        return await self.upload("sendSticker", "sticker", data, filename,
                                 chat_id=chat_id,
                                 business_connection_id=business_connection_id,
                                 disable_notification=(
                                     "true" if disable_notification else None))

    async def send_dice(self, chat_id: int, emoji: str, *,
                        business_connection_id: str | None = None) -> dict:
        return await self.call("sendDice", chat_id=chat_id, emoji=emoji,
                               business_connection_id=business_connection_id)

    async def send_media(self, chat_id: int, file_id: str, media_type: str | None, *,
                         caption: str | None = None,
                         business_connection_id: str | None = None,
                         reply_markup: dict | None = None) -> dict:
        method, field = MEDIA_METHODS.get(media_type or "", ("sendDocument", "document"))
        params: dict[str, Any] = {
            "chat_id": chat_id, field: file_id,
            "business_connection_id": business_connection_id,
            "reply_markup": reply_markup,
        }
        if caption and method != "sendSticker":
            params["caption"] = markup.trim(markup.to_html(caption), CAPTION_LIMIT)
            params["parse_mode"] = "HTML"
        return await self.call(method, **params)

    async def send_file(self, chat_id: int, data: bytes, filename: str, *,
                        caption: str | None = None) -> dict:      # noqa: D401
        return await self.upload(
            "sendDocument", "document", data, filename, chat_id=chat_id,
            caption=markup.trim(markup.to_html(caption), CAPTION_LIMIT) if caption else None,
            parse_mode="HTML" if caption else None,
        )

    async def get_chat(self, chat_id: int) -> dict:
        return await self.call("getChat", chat_id=chat_id)

    async def pin_message(self, chat_id: int, message_id: int) -> bool:
        return await self.call("pinChatMessage", chat_id=chat_id,
                               message_id=message_id, disable_notification=True)

    async def unpin_message(self, chat_id: int, message_id: int) -> bool:
        return await self.call("unpinChatMessage", chat_id=chat_id,
                               message_id=message_id)

    async def get_business_connection(self, business_connection_id: str) -> dict:
        return await self.call("getBusinessConnection",
                               business_connection_id=business_connection_id)

    async def delete_business_messages(self, business_connection_id: str,
                                       message_ids: list[int]) -> bool:
        return await self.call("deleteBusinessMessages",
                               business_connection_id=business_connection_id,
                               message_ids=message_ids)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    async def set_chat_menu_button(self, menu_button: dict) -> bool:
        return await self.call("setChatMenuButton", menu_button=menu_button)

    async def set_my_commands(self, commands: list[dict]) -> bool:
        return await self.call("setMyCommands", commands=commands)

    async def set_webhook(self, url: str, secret_token: str,
                          allowed_updates: list[str]) -> bool:
        return await self.call("setWebhook", url=url, secret_token=secret_token,
                               allowed_updates=allowed_updates,
                               drop_pending_updates=False,
                               max_connections=20)

    async def delete_webhook(self) -> bool:
        return await self.call("deleteWebhook", drop_pending_updates=False)

    async def get_webhook_info(self) -> dict:
        return await self.call("getWebhookInfo")

    async def get_file(self, file_id: str) -> dict:
        return await self.call("getFile", file_id=file_id)

    async def download(self, file_path: str) -> bytes:
        """Скачивает файл бота. Bot API отдаёт не больше 20 МБ."""
        url = f"{BASE}/file/bot{self.token}/{file_path}"
        async with self._session.get(url) as response:
            response.raise_for_status()
            return await response.read()
