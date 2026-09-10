"""Доставка отчётов владельцу.

Если задан BOT_TOKEN, отчёты присылает бот из @BotFather — так они приходят
в отдельную личку, а не смешиваются с вашими сообщениями. Без токена всё
работает по-старому: юзербот пишет в LOG_CHAT.
"""
from __future__ import annotations

import io
import logging

from telethon.tl.types import InputPeerUser

from core import mediastore, state

log = logging.getLogger("reporter")

CAPTION_LIMIT = 1024      # предел подписи к медиа в Telegram
NO_START_HINT = (
    "🤖 Бот не может написать первым. Откройте с ним чат и нажмите **Start** — "
    "после этого отчёты пойдут туда."
)


async def resolve_owner() -> bool:
    """Готовит peer владельца для бота. False — владелец ещё не нажал /start."""
    if state.bot is None or not state.owner_id:
        return False
    try:
        state.owner_peer = await state.bot.get_input_entity(state.owner_id)
        return True
    except (ValueError, TypeError):
        # Бот ещё не видел этого пользователя. Для тех, кто уже нажал /start,
        # Telegram принимает peer с нулевым access_hash.
        state.owner_peer = InputPeerUser(state.owner_id, 0)
        return False


async def _warn_once() -> None:
    if state.bot_blocked:
        return
    state.bot_blocked = True
    await mediastore.send_log(NO_START_HINT)


async def _fetch_media(media_ref: int) -> io.BytesIO | None:
    """Скачивает копию медиа из хранилища, чтобы бот мог переслать её владельцу."""
    if state.log_entity is None:
        return None
    try:
        staged = await state.client.get_messages(state.log_entity, ids=media_ref)
        if staged is None or staged.media is None:
            return None
        buf = io.BytesIO()
        await state.client.download_media(staged, file=buf)
        buf.seek(0)
        buf.name = getattr(getattr(staged, "file", None), "name", None) or "media"
        return buf
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось забрать медиа из хранилища: %r", e)
        return None


async def _send_via_bot(text: str, media_ref: int | None) -> bool:
    payload = await _fetch_media(media_ref) if media_ref else None
    try:
        if payload is not None:
            if len(text) > CAPTION_LIMIT:
                await state.bot.send_message(state.owner_peer, text, link_preview=False)
                await state.bot.send_file(state.owner_peer, payload)
            else:
                await state.bot.send_file(state.owner_peer, payload, caption=text)
        else:
            await state.bot.send_message(state.owner_peer, text, link_preview=False)
        state.bot_blocked = False
        return True
    except Exception as e:                                   # noqa: BLE001
        log.warning("бот не смог доставить отчёт: %r", e)
        return False


async def send_report(text: str, *, media_ref: int | None = None) -> bool:
    """Отправляет отчёт владельцу. Возвращает True, если доставлено."""
    if state.bot is not None and state.owner_peer is not None:
        if await _send_via_bot(text, media_ref):
            return True
        await _warn_once()          # падаем обратно в лог-чат, чтобы не потерять отчёт

    return await mediastore.send_log(text, media_ref=media_ref) is not None
