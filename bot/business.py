"""Режим Telegram Business.

Владелец подключает бота в Настройки → Telegram Business → Чат-боты, и Bot API
начинает присылать события из его личных чатов: новые сообщения, правки и —
главное — `deleted_business_messages`. Именно поэтому здесь не нужен юзербот.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import config
import db
from bot import dotcmd, parse, transcript
from core import fmt, mediastore, reporter, state

log = logging.getLogger("business")

# Права, которые бот получает при подключении, и что ломается без них.
REQUIRED_RIGHTS = (
    ("can_read_messages", "читать сообщения", "без этого бот не увидит ни одного чата"),
    ("can_reply", "отвечать", "без этого не будет уведомлений в самом чате"),
    ("can_delete_sent_messages", "удалять свои сообщения",
     "без этого команды `.` останутся висеть в переписке"),
    ("can_delete_all_messages", "удалять сообщения собеседника",
     "без этого не работает `.mute`"),
)

CONNECTED = (
    "✅ **Бот подключён к вашим личным чатам.**\n\n"
    "Теперь удалённые и изменённые сообщения приходят сюда, а команды `{p}` "
    "работают прямо в переписке.\n"
)
DISCONNECTED = (
    "🚫 **Бот отключён от личных чатов.**\n\n"
    "Отчёты приходить перестанут. Вернуть: Настройки → Telegram Business → "
    "Чат-боты."
)


def _rights_of(connection: dict) -> dict:
    rights = connection.get("rights")
    if isinstance(rights, dict) and rights:
        return rights
    # До Bot API 9.0 приходил только can_reply.
    return {"can_reply": bool(connection.get("can_reply")),
            "can_read_messages": True}


def rights_report(rights: dict) -> str:
    lines = []
    for key, label, consequence in REQUIRED_RIGHTS:
        if rights.get(key):
            lines.append(f"✅ {label}")
        else:
            lines.append(f"⚠️ {label} — {consequence}")
    return "\n".join(lines)


# ------------------------------------------------------------ подключение ---

async def on_business_connection(connection: dict) -> None:
    connection_id = connection["id"]
    user = connection.get("user") or {}
    user_id = user.get("id")
    user_chat_id = connection.get("user_chat_id") or user_id
    enabled = bool(connection.get("is_enabled", True))
    rights = _rights_of(connection)

    await db.save_business(connection_id, user_id, user_chat_id, enabled,
                           json.dumps(rights, ensure_ascii=False))
    state.business[connection_id] = {"user_id": user_id, "user_chat_id": user_chat_id,
                                     "is_enabled": enabled, "rights": rights}
    if not state.owner_id:
        state.owner_id = user_id
    if user_chat_id and (not state.owner_chat_id or state.owner_id == user_id):
        state.owner_chat_id = user_chat_id

    log.info("бизнес-подключение %s: пользователь %s, включено=%s",
             connection_id, user_id, enabled)

    if not enabled:
        await reporter.send_report(DISCONNECTED)
        return
    await reporter.send_report(
        CONNECTED.format(p=config.PREFIX) + "\n**Выданные права:**\n"
        + rights_report(rights)
        + f"\n\nПроверить: отправьте `{config.PREFIX}ping` в любой личный чат."
    )


async def load_connections() -> None:
    """Поднимает сохранённые подключения после перезапуска."""
    for row in await db.all_business():
        try:
            rights = json.loads(row["rights"] or "{}")
        except json.JSONDecodeError:
            rights = {}
        state.business[row["connection_id"]] = {
            "user_id": row["user_id"], "user_chat_id": row["user_chat_id"],
            "is_enabled": bool(row["is_enabled"]), "rights": rights,
        }
        if bool(row["is_enabled"]):
            state.owner_id = state.owner_id or row["user_id"]
            state.owner_chat_id = state.owner_chat_id or (row["user_chat_id"] or 0)
    log.info("подключений Business загружено: %s", len(state.business))


# --------------------------------------------------------------- сообщения --

async def _cache(message: dict, connection_id: str) -> None:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    media_type, file_id = parse.media_of(message)
    await db.cache_message(
        chat_id=chat.get("id"),
        msg_id=message["message_id"],
        user_id=sender.get("id"),
        is_private=chat.get("type") == "private",
        text=parse.text_of(message),
        media_type=media_type,
        media_ref=None,
        reply_to=(message.get("reply_to_message") or {}).get("message_id"),
        date=message.get("date") or db.now(),
        file_id=file_id,
        business_id=connection_id,
        user_name=parse.display_name(sender),
    )


async def _intercept(api, message: dict, connection_id: str) -> None:
    """Удаляет сообщение замученного и присылает его владельцу."""
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message["message_id"]
    try:
        await api.delete_business_messages(connection_id, [message_id])
        # В Business апдейт об удалении всегда несёт chat, а id
        # нумеруются внутри чата — алиас «без чата» глушил бы чужие.
        state.mark_own_deletion(chat_id, message_id)
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось удалить сообщение замученного: %r", e)
        if not state.rights_of(connection_id).get("can_delete_all_messages"):
            await reporter.send_report(
                "⚠️ У бота нет права удалять сообщения собеседника — `.mute` не "
                "работает. Настройки → Telegram Business → Чат-боты → включите "
                "удаление сообщений.")
        return

    if not config.MUTE_LOG:
        return
    sender = message.get("from") or {}
    media_type, file_id = parse.media_of(message)
    head = f"🔇 **Перехвачено у замученного** {parse.display_name(sender)}"
    if media_type:
        head += f"\n{mediastore.KIND_ICON.get(media_type, '📎')} {media_type}"
    body = fmt.truncate(parse.text_of(message), 2000)
    await reporter.send_report(head + (f"\n\n{body}" if body else ""),
                               file_id=file_id, media_type=media_type)


async def on_business_message(api, message: dict) -> None:
    connection_id = message.get("business_connection_id")
    info = state.business.get(connection_id) or {}
    owner_id = info.get("user_id")
    sender = message.get("from") or {}
    chat = message.get("chat") or {}

    if parse.is_service(message):
        return

    if sender.get("id") == owner_id:
        text = parse.text_of(message)
        if text.startswith(config.PREFIX):
            await dotcmd.handle(api, message, connection_id)
            return
        await _cache(message, connection_id)
        return

    if await state.is_muted(chat.get("id"), sender.get("id")):
        await _intercept(api, message, connection_id)
        return

    settings = await _chat_flags(chat.get("id"))
    if settings["ignored"] or not settings["antidelete"]:
        return
    await _cache(message, connection_id)


async def _chat_flags(chat_id: int) -> dict:
    from modules.antidelete import flags
    return await flags(chat_id, True)


# ---------------------------------------------------------------- удаления --

@dataclass
class _Batch:
    """Удаления по одному чату, собранные за короткое окно."""
    connection_id: str
    chat: dict
    ids: list[int] = field(default_factory=list)
    task: asyncio.Task | None = None


_pending: dict[int, _Batch] = {}


async def on_deleted_business_messages(update: dict) -> None:
    """Копит удаления и разбирает их пачкой.

    Очистка переписки прилетает как сотня id, иногда несколькими апдейтами.
    Карточка на каждое — это флуд-лимит и нечитаемая лента, поэтому решение
    принимается по всей пачке.
    """
    chat = update.get("chat") or {}
    chat_id = chat.get("id")
    connection_id = update.get("business_connection_id")
    ids = [mid for mid in (update.get("message_ids") or [])
           if not state.was_own_deletion(chat_id, mid)]
    if not ids:
        return

    batch = _pending.get(chat_id)
    if batch is None:
        batch = _Batch(connection_id=connection_id, chat=chat)
        _pending[chat_id] = batch
    batch.ids.extend(ids)
    batch.chat = chat or batch.chat

    if batch.task is not None and not batch.task.done():
        batch.task.cancel()
    if config.PURGE_DEBOUNCE_SEC > 0:
        batch.task = asyncio.create_task(_flush_after_pause(chat_id))
    else:
        await flush(chat_id)


async def _flush_after_pause(chat_id: int) -> None:
    try:
        await asyncio.sleep(config.PURGE_DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return                      # пришла ещё пачка — ждём дальше
    try:
        await flush(chat_id)
    except Exception:               # noqa: BLE001
        log.exception("не удалось разобрать удаления чата %s", chat_id)


async def flush_pending() -> None:
    """Разбирает всё накопленное — при остановке процесса и в тестах."""
    for chat_id in list(_pending):
        await flush(chat_id)


async def flush(chat_id: int) -> None:
    batch = _pending.pop(chat_id, None)
    if batch is None:
        return
    if batch.task is not None and not batch.task.done():
        batch.task.cancel()

    ids = list(dict.fromkeys(batch.ids))
    rows = await db.get_messages(chat_id, ids)
    where = parse.chat_title(batch.chat)

    if len(ids) < config.PURGE_THRESHOLD:
        for row in rows:
            await db.add_deleted(dict(row))
            await reporter.send_report(await _deleted_card(row, where),
                                       file_id=row["file_id"],
                                       media_type=row["media_type"])
        await db.drop_messages(chat_id, [row["msg_id"] for row in rows])
        return

    await _report_purge(chat_id, batch, rows, ids, where)


async def _report_purge(chat_id: int, batch: _Batch, rows, ids: list[int],
                        where: str) -> None:
    when = db.now()
    owner_id = (state.business.get(batch.connection_id) or {}).get("user_id")
    payload, stats = transcript.build(rows, chat_title=where, owner_id=owner_id,
                                      requested=len(ids), when=when)
    with_media = [row for row in rows if row["file_id"]][:config.PURGE_MEDIA_LIMIT]

    await reporter.send_document(
        payload, transcript.filename(chat_id, when),
        transcript.summary(where, stats, media_sent=len(with_media)))

    for row in rows:
        await db.add_deleted(dict(row))
    await db.drop_messages(chat_id, [row["msg_id"] for row in rows])

    for row in with_media:
        icon = mediastore.KIND_ICON.get(row["media_type"], "📎")
        await reporter.send_report(
            f"{icon} {row['media_type']} · {fmt.ts(row['date'])} · "
            f"{row['user_name'] or '—'}",
            file_id=row["file_id"], media_type=row["media_type"])


async def _deleted_card(row, where: str) -> str:
    lines = [
        "🗑 **Удалённое сообщение**",
        f"👤 {row['user_name'] or 'неизвестно'} (`{row['user_id']}`)",
        f"💬 {where}",
        f"🕒 отправлено {fmt.ts(row['date'])} · удалено {fmt.ts(db.now())}",
    ]
    if row["media_type"]:
        icon = mediastore.KIND_ICON.get(row["media_type"], "📎")
        lines.append(f"{icon} {row['media_type']}")
    if row["text"]:
        lines += ["", fmt.truncate(row["text"], 2500)]
    return "\n".join(lines)


async def on_edited_business_message(message: dict) -> None:
    connection_id = message.get("business_connection_id")
    info = state.business.get(connection_id) or {}
    chat = message.get("chat") or {}
    chat_id, message_id = chat.get("id"), message["message_id"]

    row = await db.get_message(chat_id, message_id)
    new_text = parse.text_of(message)
    if row is None:
        await _cache(message, connection_id)
        return
    old_text = row["text"] or ""
    if old_text == new_text:
        return

    await db.add_edit(chat_id, message_id, row["user_id"], old_text, new_text)
    await db.cache_message(
        chat_id=chat_id, msg_id=message_id, user_id=row["user_id"],
        is_private=bool(row["is_private"]), text=new_text,
        media_type=row["media_type"], media_ref=row["media_ref"],
        reply_to=row["reply_to"], date=row["date"], file_id=row["file_id"],
        business_id=connection_id, user_name=row["user_name"],
    )
    settings = await _chat_flags(chat_id)
    if not settings["log_edits"] or settings["ignored"]:
        return
    if row["user_id"] == info.get("user_id") and not config.LOG_OWN:
        return
    await reporter.send_report(
        "✏️ **Сообщение изменено**\n"
        f"👤 {row['user_name'] or 'неизвестно'} (`{row['user_id']}`)\n"
        f"💬 {parse.chat_title(chat)}\n\n"
        f"**Было:**\n{fmt.truncate(old_text, 1200)}\n\n"
        f"**Стало:**\n{fmt.truncate(new_text, 1200)}"
    )
