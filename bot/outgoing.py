"""Отправка в саму переписку от имени владельца — единственная точка.

Свои сообщения бот обратно апдейтом не получает: Telegram присылает
`business_message` на то, что написал владелец руками или его собеседник, но не
на то, что отправил сам бот. Значит, в кэш такие сообщения сами не попадают.

А удаляют их наравне со всем остальным — и тогда владельцу прилетает карточка
«этих сообщений нет в моей памяти» на ровном месте: автоответчик, стикер или
уведомление о муте бот отправил сам, а вспомнить не может. Поэтому всё, что
уходит в переписку, здесь же и запоминается.
"""
from __future__ import annotations

import logging

import db

log = logging.getLogger("outgoing")


async def remember(owner_id: int, chat_id: int, sent: dict | None,
                   connection_id: str | None, *, text: str = "",
                   media_type: str | None = None,
                   file_id: str | None = None) -> None:
    """Кладёт отправленное в кэш так же, как входящее. Молча — это не задача."""
    message_id = (sent or {}).get("message_id")
    if not (owner_id and chat_id and message_id):
        return
    try:
        await db.cache_message(
            owner_id=owner_id, chat_id=chat_id, msg_id=message_id,
            user_id=owner_id, is_private=True, text=text,
            media_type=media_type, media_ref=None, reply_to=None,
            date=(sent or {}).get("date") or db.now(),
            file_id=file_id, business_id=connection_id)
    except Exception as e:                                   # noqa: BLE001
        log.warning("своё сообщение %s не записалось в кэш: %r", message_id, e)


async def retext(owner_id: int, chat_id: int, message_id: int, text: str) -> None:
    """Бот переписал своё сообщение (анимация) — в кэше должен лежать итог."""
    if not (owner_id and chat_id and message_id):
        return
    try:
        await db.set_text(owner_id, chat_id, message_id, text)
    except Exception as e:                                   # noqa: BLE001
        log.debug("текст %s в кэше не обновился: %r", message_id, e)


async def message(api, owner_id: int, chat_id: int, text: str,
                  connection_id: str | None, *, reply_to: int | None = None) -> dict:
    sent = await api.send_message(chat_id, text,
                                  business_connection_id=connection_id,
                                  reply_to=reply_to)
    await remember(owner_id, chat_id, sent, connection_id, text=text)
    return sent


async def dice(api, owner_id: int, chat_id: int, emoji: str,
               connection_id: str | None) -> dict:
    sent = await api.send_dice(chat_id, emoji,
                               business_connection_id=connection_id)
    # Без media_type: file_id у кубика нет, и переслать его обратно нечем —
    # в карточке удаления честнее показать сам символ.
    await remember(owner_id, chat_id, sent, connection_id, text=emoji)
    return sent


async def sticker(api, owner_id: int, chat_id: int, file_id: str,
                  connection_id: str | None, *, emoji: str = "",
                  disable_notification: bool = False) -> dict:
    sent = await api.send_sticker(chat_id, file_id,
                                  business_connection_id=connection_id,
                                  disable_notification=disable_notification)
    await remember(owner_id, chat_id, sent, connection_id, text=emoji,
                   media_type="стикер", file_id=file_id)
    return sent


async def upload(api, owner_id: int, chat_id: int, payload: bytes, filename: str,
                 connection_id: str | None, *, emoji: str = "",
                 disable_notification: bool = False) -> dict:
    sent = await api.upload_sticker(chat_id, payload, filename,
                                    business_connection_id=connection_id,
                                    disable_notification=disable_notification)
    await remember(owner_id, chat_id, sent, connection_id, text=emoji,
                   media_type="стикер",
                   file_id=((sent or {}).get("sticker") or {}).get("file_id"))
    return sent
