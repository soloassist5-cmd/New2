"""Доставка отчётов владельцу.

Основной путь — бот из @BotFather: отчёты приходят в отдельную личку с ним.
Если бот недоступен (не задан токен, владелец не открывал чат), отчёт уходит
запасным путём в LOG_CHAT от юзербота, чтобы ничего не потерялось.
"""
from __future__ import annotations

import logging

from core import mediastore, state

log = logging.getLogger("reporter")

NO_START_HINT = (
    "🤖 Бот не смог написать вам первым. Откройте с ним чат и нажмите **Start** — "
    "после этого отчёты пойдут туда."
)


async def _warn_once() -> None:
    if state.bot_blocked:
        return
    state.bot_blocked = True
    log.warning("бот не может писать владельцу — нужен /start в чате с ботом")
    await mediastore.send_log(NO_START_HINT)


async def _via_bot(text: str, file_id: str | None, media_type: str | None) -> bool:
    try:
        if file_id:
            await state.api.send_media(state.owner_chat_id, file_id, media_type,
                                       caption=text)
        else:
            await state.api.send_message(state.owner_chat_id, text)
        state.bot_blocked = False
        return True
    except Exception as e:                                   # noqa: BLE001
        log.warning("бот не смог доставить отчёт: %r", e)
        # Медиа могло протухнуть или быть недоступным — текст важнее вложения.
        if file_id:
            try:
                await state.api.send_message(state.owner_chat_id, text)
                state.bot_blocked = False
                return True
            except Exception:                                # noqa: BLE001
                pass
        return False


async def send_report(text: str, *, file_id: str | None = None,
                      media_type: str | None = None,
                      media_ref: int | None = None) -> bool:
    """Отправляет отчёт владельцу. Возвращает True, если доставлено."""
    if state.api is not None and state.owner_chat_id:
        if await _via_bot(text, file_id, media_type):
            return True
        await _warn_once()

    # Запасной путь: копия медиа уже лежит в LOG_CHAT, отчёт цепляем к ней ответом.
    return await mediastore.send_log(text, media_ref=media_ref) is not None
