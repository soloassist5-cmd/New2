"""Пер-чатные настройки антиудаления с кэшем в памяти.

Общие для обоих режимов, поэтому живут в core, а не в модулях юзербота.
"""
from __future__ import annotations

import config
import db
from core import state

_cache: dict[tuple[int, int], dict] = {}


def invalidate(owner_id: int, chat_id: int) -> None:
    _cache.pop((owner_id, chat_id), None)


def invalidate_all() -> None:
    """Сбрасывает кэш — например, после восстановления базы."""
    _cache.clear()


async def flags(owner_id: int, chat_id: int, is_private: bool) -> dict:
    key = (owner_id, chat_id)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    row = await db.get_settings(owner_id, chat_id)
    default_on = config.DEFAULT_ANTIDELETE and (is_private or config.ANTIDELETE_GROUPS)
    resolved = {
        "antidelete": bool(row["antidelete"]) if row["antidelete"] is not None
        else default_on,
        "log_edits": bool(row["log_edits"]) if row["log_edits"] is not None
        else (config.DEFAULT_LOG_EDITS and default_on),
        "save_media": bool(row["save_media"]) if row["save_media"] is not None
        else config.DEFAULT_SAVE_MEDIA,
        "ignored": bool(row["ignored"]),
    }
    _cache[key] = resolved
    return resolved


async def toggle(owner_id: int, chat_id: int, key: str, value: bool, *,
                 title: str | None = None) -> None:
    await db.set_setting(owner_id, chat_id, key, int(value), title)
    invalidate(owner_id, chat_id)
    state._persist_soon()


async def reset(owner_id: int, chat_id: int) -> None:
    await db.reset_chat(owner_id, chat_id)
    invalidate(owner_id, chat_id)
    state._persist_soon()
