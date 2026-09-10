"""Доставка отчётов владельцу.

Основной путь — бот из @BotFather: отчёты приходят каждому владельцу в его
личку с ботом. Запасной — LOG_CHAT юзербота, если бот недоступен.
"""
from __future__ import annotations

import io
import logging

from core import mediastore, state

log = logging.getLogger("reporter")

NO_START_HINT = (
    "🤖 Бот не смог написать вам первым. Откройте с ним чат и нажмите **Start** — "
    "после этого отчёты пойдут туда."
)


def _userbot_owner(owner_id: int) -> bool:
    """Владелец юзербота — единственный, кому есть куда падать в запасной путь."""
    return state.me is not None and owner_id == state.me.id


async def _warn_once(owner_id: int) -> None:
    if state.bot_blocked:
        return
    state.bot_blocked = True
    log.warning("бот не может писать владельцу %s — нужен /start", owner_id)
    if _userbot_owner(owner_id):
        await mediastore.send_log(NO_START_HINT)


async def _via_bot(owner_id: int, text: str, file_id: str | None,
                   media_type: str | None, reply_markup: dict | None) -> bool:
    chat_id = state.chat_of(owner_id)
    if not chat_id:
        return False
    try:
        if file_id:
            await state.api.send_media(chat_id, file_id, media_type, caption=text,
                                       reply_markup=reply_markup)
        else:
            await state.api.send_message(chat_id, text, reply_markup=reply_markup)
        state.bot_blocked = False
        return True
    except Exception as e:                                   # noqa: BLE001
        log.warning("бот не смог доставить отчёт владельцу %s: %r", owner_id, e)
        # Медиа могло протухнуть или быть недоступным — текст важнее вложения.
        if file_id:
            try:
                await state.api.send_message(chat_id, text)
                state.bot_blocked = False
                return True
            except Exception:                                # noqa: BLE001
                pass
        return False


async def send_report(owner_id: int, text: str, *, file_id: str | None = None,
                      media_type: str | None = None,
                      media_ref: int | None = None,
                      reply_markup: dict | None = None) -> bool:
    """Отправляет отчёт владельцу. Возвращает True, если доставлено."""
    if state.api is not None:
        if await _via_bot(owner_id, text, file_id, media_type, reply_markup):
            return True
        await _warn_once(owner_id)

    if not _userbot_owner(owner_id):
        return False
    # Запасной путь: копия медиа уже лежит в LOG_CHAT, отчёт цепляем к ней ответом.
    return await mediastore.send_log(text, media_ref=media_ref) is not None


async def send_document(owner_id: int, payload: bytes, filename: str,
                        caption: str) -> bool:
    """Файл владельцу тем же маршрутом, что и обычные отчёты."""
    if state.api is not None:
        chat_id = state.chat_of(owner_id)
        if chat_id:
            try:
                await state.api.send_file(chat_id, payload, filename, caption=caption)
                state.bot_blocked = False
                return True
            except Exception as e:                           # noqa: BLE001
                log.warning("бот не смог отправить файл: %r", e)
                await _warn_once(owner_id)

    if not _userbot_owner(owner_id) or state.log_entity is None:
        return False
    try:
        buf = io.BytesIO(payload)
        buf.name = filename
        return await mediastore.send_log(caption, file=buf) is not None
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось отправить файл в лог-чат: %r", e)
        return False
