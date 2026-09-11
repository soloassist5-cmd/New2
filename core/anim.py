"""Анимация редактирования собственного сообщения.

Telegram ограничивает частоту правок, поэтому:
  * число кадров фиксировано и невелико;
  * FloodWait длиннее FLOOD_LIMIT прерывает анимацию и сразу ставит финальный текст;
  * MessageNotModified игнорируется.
"""
from __future__ import annotations

import asyncio
import logging
import re

from telethon.errors import (
    FloodWaitError,
    MessageIdInvalidError,
    MessageNotModifiedError,
)

import config

log = logging.getLogger("anim")

FLOOD_LIMIT = 5          # сек: дольше — не ждём, обрываем анимацию
MAX_TYPE_FRAMES = 10     # больше правок — выше риск словить флуд-лимит
FILLED, EMPTY = "▰", "▱"
CURSOR = "▌"

# Незакрытая разметка в промежуточном кадре видна как мусор, поэтому её убираем:
# сначала ссылки [текст](url) -> текст, затем сами маркеры.
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD = re.compile(r"\*\*|~~|`|(?<!\w)_+|_+(?!\w)")


def strip_md(text: str) -> str:
    return _MD.sub("", _MD_LINK.sub(r"\1", text))


def _chunks(words: list[str], limit: int) -> list[list[str]]:
    """Режет слова на не более чем limit групп, чтобы не упереться в лимит правок."""
    if len(words) <= limit:
        return [[w] for w in words]
    size = -(-len(words) // limit)
    return [words[i:i + size] for i in range(0, len(words), size)]


class Aborted(Exception):
    """Анимацию продолжать нельзя (флуд-лимит или сообщение исчезло)."""


async def safe_edit(msg, text: str, *, link_preview: bool = False):
    """Правка с проглатыванием штатных ошибок. Возвращает сообщение или None."""
    try:
        return await msg.edit(text, link_preview=link_preview)
    except MessageNotModifiedError:
        return msg
    except FloodWaitError as e:
        if e.seconds > FLOOD_LIMIT:
            raise Aborted from e
        await asyncio.sleep(e.seconds + 0.5)
        try:
            return await msg.edit(text, link_preview=link_preview)
        except Exception as retry_error:
            raise Aborted from retry_error
    except MessageIdInvalidError as e:
        raise Aborted from e
    except Exception as e:              # noqa: BLE001 — анимация не должна ронять команду
        log.warning("edit failed: %r", e)
        raise Aborted from e


async def safe_edit_quiet(msg, text: str) -> None:
    """Последняя попытка поставить текст: результат важнее спецэффекта."""
    try:
        await msg.edit(text, link_preview=False)
    except Exception:                                        # noqa: BLE001
        pass


def bar(step: int, steps: int, width: int = 8) -> str:
    filled = round(width * step / steps)
    return FILLED * filled + EMPTY * (width - filled)


async def loading(msg, final: str, *, icon: str = "🔇", steps: int = 4,
                  delay: float | None = None) -> None:
    """Полоса загрузки, затем финальный текст."""
    delay = config.ANIM_DELAY if delay is None else delay
    for step in range(steps + 1):
        await safe_edit(msg, f"{icon} {bar(step, steps)}")
        await asyncio.sleep(delay)
    await safe_edit(msg, final)


async def type_out(msg, final: str, *, prefix: str = "", delay: float | None = None,
                   cursor: bool = True) -> None:
    """Пословное «печатание» текста; разметка появляется только на финальном кадре."""
    delay = config.ANIM_DELAY if delay is None else delay
    groups = _chunks(strip_md(final).split(" "), MAX_TYPE_FRAMES)
    shown = ""
    for i, group in enumerate(groups):
        shown = f"{shown} {' '.join(group)}".strip()
        tail = f" {CURSOR}" if cursor and i < len(groups) - 1 else ""
        await safe_edit(msg, f"{prefix}{shown}{tail}")
        await asyncio.sleep(delay)
    await safe_edit(msg, f"{prefix}{final}")


async def play(msg, final: str, *, icon: str = "🔇", style: str | None = None,
               delay: float | None = None) -> None:
    """Проигрывает анимацию выбранного стиля и гарантированно ставит финальный текст.

    style: 'type' — полоса + пословное печатание, 'bar' — только полоса, 'off' — без анимации.
    """
    style = (style or config.MUTE_ANIM).lower()
    delay = config.ANIM_DELAY if delay is None else delay
    try:
        if style == "off":
            await safe_edit(msg, final)
            return
        if style == "bar":
            await loading(msg, final, icon=icon, delay=delay)
            return
        # style == "type"
        for step in range(3):
            await safe_edit(msg, f"{icon} {bar(step, 3, width=6)}")
            await asyncio.sleep(delay)
        await type_out(msg, final, delay=delay)
    except Aborted:
        # Анимация сорвалась — важен результат, а не спецэффект.
        try:
            await msg.edit(final, link_preview=False)
        except Exception:
            pass
