"""Снимок медиа в чат-хранилище.

После удаления сообщения его файл уже не скачать, поэтому копия делается в
момент получения. В БД хранится только id сообщения-копии, так что диск хоста
не растёт — файлы лежат в Telegram. Когда сообщение действительно удалят,
копия скачивается обратно и уходит владельцу в отчёте.
"""
from __future__ import annotations

import io
import logging

from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
)

import config
from core import state

log = logging.getLogger("mediastore")

KIND_ICON = {
    "фото": "🖼", "видео": "🎬", "видеосообщение": "📹", "гифка": "🎞",
    "голосовое": "🎙", "аудио": "🎵", "стикер": "🩹", "документ": "📎",
    "геолокация": "📍", "контакт": "👤", "опрос": "📊",
}


def media_kind(message) -> str | None:
    media = getattr(message, "media", None)
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        return "фото"
    if isinstance(media, MessageMediaGeo):
        return "геолокация"
    if isinstance(media, MessageMediaContact):
        return "контакт"
    if isinstance(media, MessageMediaPoll):
        return "опрос"
    if isinstance(media, MessageMediaDocument):
        attrs = getattr(media.document, "attributes", []) or []
        if any(isinstance(a, DocumentAttributeSticker) for a in attrs):
            return "стикер"
        if any(isinstance(a, DocumentAttributeAnimated) for a in attrs):
            return "гифка"
        for a in attrs:
            if isinstance(a, DocumentAttributeVideo):
                return "видеосообщение" if getattr(a, "round_message", False) else "видео"
            if isinstance(a, DocumentAttributeAudio):
                return "голосовое" if getattr(a, "voice", False) else "аудио"
        return "документ"
    return "документ"


def too_big(message) -> bool:
    size = getattr(getattr(message, "file", None), "size", None)
    if not size:
        return False
    return size > config.MAX_MEDIA_MB * 1024 * 1024


async def snapshot(message) -> int | None:
    """Копирует медиа в лог-чат. Возвращает id сообщения-копии."""
    if state.log_entity is None or getattr(message, "media", None) is None:
        return None
    if isinstance(message.media, (MessageMediaGeo, MessageMediaContact, MessageMediaPoll)):
        return None
    if too_big(message):
        log.debug("медиа больше лимита, пропуск")
        return None

    try:
        copies = await state.client.forward_messages(state.log_entity, message)
        copy = copies[0] if isinstance(copies, list) else copies
        if copy is not None:
            return copy.id
    except Exception as e:                                   # noqa: BLE001
        log.debug("forward не прошёл (%r), пробую перезалить", e)

    # Чат с запретом пересылки — скачиваем в память и заливаем заново.
    try:
        buf = io.BytesIO()
        await state.client.download_media(message, file=buf)
        buf.seek(0)
        name = getattr(getattr(message, "file", None), "name", None) or "media"
        copy = await state.client.send_file(state.log_entity, buf, file_name=name,
                                            force_document=False)
        return copy.id
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось сохранить медиа: %r", e)
        return None


async def send_log(text: str, *, media_ref: int | None = None, file=None):
    """Отправляет карточку в лог-чат, при наличии — с прикреплённой копией медиа."""
    if state.log_entity is None:
        return None
    try:
        if file is not None:
            return await state.client.send_message(state.log_entity, text, file=file,
                                                   link_preview=False)
        return await state.client.send_message(
            state.log_entity, text, link_preview=False,
            reply_to=media_ref if media_ref else None)
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось записать в лог: %r", e)
        try:
            return await state.client.send_message(state.log_entity, text,
                                                   link_preview=False)
        except Exception:
            return None
