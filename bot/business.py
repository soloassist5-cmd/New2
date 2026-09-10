"""Режим Telegram Business.

Владелец подключает бота в Настройки → Telegram для бизнеса → Чат-боты, и Bot
API начинает присылать события из его личных чатов: новые сообщения, правки и —
главное — `deleted_business_messages`. Именно поэтому здесь не нужен юзербот.

Владельцев может быть несколько, поэтому owner_id тащится в каждый вызов: id
личного чата в Bot API совпадает с id собеседника и одинаков для всех.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import config
import db
from bot import access, dotcmd, parse, transcript
from core import chatprefs, fmt, mediastore, reporter, state

log = logging.getLogger("business")

# Права, которые бот получает при подключении, и что ломается без них.
REQUIRED_RIGHTS = (
    ("can_read_messages", "читать сообщения", "без этого бот не увидит ни одного чата"),
    ("can_reply", "отвечать", "без этого не будет уведомлений в самом чате"),
    ("can_delete_sent_messages", "удалять свои сообщения",
     "без этого команды `.` останутся висеть в переписке"),
    ("can_delete_all_messages", "удалять сообщения собеседника",
     "без этого не работают `.mute` и «не беспокоить»"),
)

CONNECTED = (
    "✅ **Бот подключён к вашим личным чатам.**\n\n"
    "Теперь удалённые и изменённые сообщения приходят сюда, а команды `{p}` "
    "работают прямо в переписке.\n"
)
DISCONNECTED = (
    "🚫 **Бот отключён от личных чатов.**\n\n"
    "Отчёты приходить перестанут. Вернуть: Настройки → Telegram для бизнеса → "
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

async def on_business_connection(api, connection: dict, *,
                                 announce: bool = True) -> None:
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
    log.info("бизнес-подключение %s: пользователь %s, включено=%s",
             connection_id, user_id, enabled)

    if not state.is_approved(user_id):
        # Бот хранит переписку — обслуживать чужого без разрешения нельзя.
        # Пользователя записывает сама заявка: она же решает, не дубль ли это.
        reply = await access.request(api, user, chat_id=user_chat_id,
                                     source="подключил бота к личным чатам")
        try:
            await api.send_message(user_chat_id, reply)
        except Exception as e:                               # noqa: BLE001
            log.info("не удалось ответить на заявку %s: %r", user_id, e)
        return

    await state.remember_user(user_id, name=parse.display_name(user),
                              chat_id=user_chat_id)
    if not announce:
        return
    if not enabled:
        await reporter.send_report(user_id, DISCONNECTED)
        return
    await reporter.send_report(
        user_id,
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
    log.info("подключений Business загружено: %s", len(state.business))


# ------------------------------------------------- восстановление связки ---
# Подключение хранится в базе, а на бесплатных хостах диск эфемерный. Telegram
# присылает business_connection только при изменении подключения, поэтому после
# передеплоя бот сам себя считает неподключённым. Лечится запросом по id,
# который приходит с каждым событием из личных чатов.

_relearn_attempt: dict[str, float] = {}
_relearn_announced: set[int] = set()
RELEARN_RETRY_SEC = 60


async def ensure_connection(api, connection_id: str | None) -> dict | None:
    if not connection_id or api is None:
        return None
    info = state.business.get(connection_id)
    if info:
        return info

    last = _relearn_attempt.get(connection_id, 0)
    if time.time() - last < RELEARN_RETRY_SEC:
        return None
    _relearn_attempt[connection_id] = time.time()

    try:
        payload = await api.get_business_connection(connection_id)
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось перечитать подключение %s: %r", connection_id, e)
        return None

    await on_business_connection(api, payload, announce=False)
    log.info("подключение %s восстановлено после перезапуска", connection_id)

    info = state.business.get(connection_id) or {}
    owner_id = info.get("user_id")
    if owner_id and state.is_approved(owner_id) and owner_id not in _relearn_announced:
        _relearn_announced.add(owner_id)
        await reporter.send_report(
            owner_id,
            "🔄 **Связь с вашими чатами восстановлена после обновления.**\n\n"
            "Всё снова работает. Сообщения, сохранённые до обновления, могли "
            "не уцелеть — новые сохраняются как обычно."
        )
    return info or None


# --------------------------------------------------------------- сообщения --

async def _cache(owner_id: int, message: dict, connection_id: str) -> None:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    media_type, file_id = parse.media_of(message)
    await db.cache_message(
        owner_id=owner_id,
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


async def _intercept(api, owner_id: int, message: dict, connection_id: str, *,
                     reason: str = "mute") -> bool:
    """Удаляет сообщение и кладёт его в журнал перехваченного."""
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message["message_id"]
    try:
        await api.delete_business_messages(connection_id, [message_id])
        # В Business апдейт об удалении всегда несёт chat, а id нумеруются
        # внутри чата — алиас «без чата» глушил бы чужие удаления.
        state.mark_own_deletion(owner_id, chat_id, message_id)
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось удалить перехваченное сообщение: %r", e)
        if not state.rights_of(connection_id).get("can_delete_all_messages"):
            await reporter.send_report(
                owner_id,
                "⚠️ У бота нет права удалять сообщения собеседника — `.mute` и "
                "режим «не беспокоить» не работают. Настройки → Telegram для "
                "бизнеса → Чат-боты → включите удаление сообщений.")
        return False

    sender = message.get("from") or {}
    media_type, file_id = parse.media_of(message)
    await db.add_intercepted({
        "owner_id": owner_id, "chat_id": chat_id, "msg_id": message_id,
        "user_id": sender.get("id"), "user_name": parse.display_name(sender),
        "text": parse.text_of(message), "media_type": media_type,
        "file_id": file_id, "date": message.get("date") or db.now(),
    }, reason)

    # По умолчанию молчим: иначе замученный собеседник превращает личку с ботом
    # в ту же переписку. Всё лежит в журнале — .muted, /intercepted, сводка
    # при снятии мута.
    if not config.MUTE_LOG:
        return True

    head = ("🔇 **Перехвачено у замученного** " if reason == "mute"
            else "🌙 **Не беспокоить** ") + parse.display_name(sender)
    if media_type:
        head += f"\n{mediastore.KIND_ICON.get(media_type, '📎')} {media_type}"
    body = fmt.truncate(parse.text_of(message), 2000)
    await reporter.send_report(owner_id, head + (f"\n\n{body}" if body else ""),
                               file_id=file_id, media_type=media_type)
    return True


async def _dnd_notice(owner_id: int, who: str) -> None:
    """Напоминание, что режим работает и съедает входящие.

    Без него «не беспокоить» выглядит как поломка: сообщения не приходят,
    отчётов нет, и понять, что это включённый режим, неоткуда.
    """
    last = state.dnd_notified.get(owner_id, 0)
    if time.time() - last < config.DND_NOTICE_EVERY:
        return
    state.dnd_notified[owner_id] = time.time()
    total = await db.count_intercepted(owner_id, since=state.dnd_since(owner_id))
    await reporter.send_report(
        owner_id,
        f"🌙 **Режим «Не беспокоить» работает.**\n\n"
        f"Сообщение от {who} удалено, отправителю отправлен ответ. "
        f"Перехвачено с момента включения: **{total}** — всё сохранено.\n\n"
        f"Посмотреть: /intercepted · выключить: /ungmute")


async def _dnd_reply(api, owner_id: int, chat_id: int, user_id: int,
                     connection_id: str) -> None:
    """Отвечает отправителю от имени владельца — не чаще раза в час на человека."""
    key = (owner_id, user_id)
    if time.time() - state.dnd_replied.get(key, 0) < config.DND_REPLY_COOLDOWN:
        return
    state.dnd_replied[key] = time.time()
    try:
        await api.send_message(chat_id, config.DND_TEXT,
                               business_connection_id=connection_id)
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось ответить в режиме «не беспокоить»: %r", e)


async def on_business_message(api, message: dict) -> None:
    connection_id = message.get("business_connection_id")
    info = (state.business.get(connection_id)
            or await ensure_connection(api, connection_id) or {})
    owner_id = info.get("user_id")
    sender = message.get("from") or {}
    chat = message.get("chat") or {}

    if parse.is_service(message):
        return
    if owner_id and not state.is_approved(owner_id):
        return                       # доступ ещё не подтверждён администратором

    if sender.get("id") == owner_id:
        text = parse.text_of(message)
        if text.startswith(config.PREFIX):
            await dotcmd.handle(api, message, connection_id, owner_id)
            return
        await _cache(owner_id, message, connection_id)
        return

    if not owner_id:
        # Кто владелец — неизвестно, а значит нельзя отличить его сообщения от
        # чужих. Удалять вслепую опаснее, чем пропустить: только кэшируем.
        log.warning("подключение %s не опознано — перехват отключён", connection_id)
        return

    if await state.is_muted(owner_id, chat.get("id"), sender.get("id")):
        await _intercept(api, owner_id, message, connection_id, reason="mute")
        return

    if state.dnd_active(owner_id) and not state.is_allowed(owner_id, sender.get("id")):
        if await _intercept(api, owner_id, message, connection_id, reason="dnd"):
            await _dnd_reply(api, owner_id, chat.get("id"), sender.get("id"),
                             connection_id)
            await _dnd_notice(owner_id, parse.display_name(sender))
        return

    settings = await chatprefs.flags(owner_id, chat.get("id"), True)
    if settings["ignored"] or not settings["antidelete"]:
        return
    await _cache(owner_id, message, connection_id)


# ---------------------------------------------------------------- удаления --

@dataclass
class _Batch:
    """Удаления по одному чату, собранные за короткое окно."""
    connection_id: str
    owner_id: int
    chat: dict
    ids: list[int] = field(default_factory=list)
    task: asyncio.Task | None = None


_pending: dict[tuple[int, int], _Batch] = {}


async def on_deleted_business_messages(api, update: dict) -> None:
    """Копит удаления и разбирает их пачкой.

    Очистка переписки прилетает как сотня id, иногда несколькими апдейтами.
    Карточка на каждое — это флуд-лимит и нечитаемая лента, поэтому решение
    принимается по всей пачке.
    """
    chat = update.get("chat") or {}
    chat_id = chat.get("id")
    connection_id = update.get("business_connection_id")
    info = (state.business.get(connection_id)
            or await ensure_connection(api, connection_id) or {})
    owner_id = info.get("user_id")
    if not owner_id or not state.is_approved(owner_id):
        return

    ids = [mid for mid in (update.get("message_ids") or [])
           if not state.was_own_deletion(owner_id, chat_id, mid)]
    if not ids:
        return

    key = (owner_id, chat_id)
    batch = _pending.get(key)
    if batch is None:
        batch = _Batch(connection_id=connection_id, owner_id=owner_id, chat=chat)
        _pending[key] = batch
    batch.ids.extend(ids)
    batch.chat = chat or batch.chat

    if batch.task is not None and not batch.task.done():
        batch.task.cancel()
    if config.PURGE_DEBOUNCE_SEC > 0:
        batch.task = asyncio.create_task(_flush_after_pause(key))
    else:
        await flush(key)


async def _flush_after_pause(key: tuple[int, int]) -> None:
    try:
        await asyncio.sleep(config.PURGE_DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return                      # пришла ещё пачка — ждём дальше
    try:
        await flush(key)
    except Exception:               # noqa: BLE001
        log.exception("не удалось разобрать удаления %s", key)


async def flush_pending() -> None:
    """Разбирает всё накопленное — при остановке процесса и в тестах."""
    for key in list(_pending):
        await flush(key)


async def flush(key: tuple[int, int]) -> None:
    batch = _pending.pop(key, None)
    if batch is None:
        return
    # Сюда приходят и из задачи ожидания: отменить её отсюда — значит бросить
    # CancelledError в самого себя и молча оборвать разбор на первом же await.
    task = batch.task
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()

    owner_id, chat_id = key
    ids = list(dict.fromkeys(batch.ids))
    rows = await db.get_messages(owner_id, chat_id, ids)
    where = parse.chat_title(batch.chat)

    if len(ids) < config.PURGE_THRESHOLD:
        for row in rows:
            await db.add_deleted(dict(row))
            await reporter.send_report(
                owner_id, await _deleted_card(row, where),
                file_id=row["file_id"], media_type=row["media_type"])
        await db.drop_messages(owner_id, chat_id, [row["msg_id"] for row in rows])
        return

    await _report_purge(owner_id, chat_id, rows, ids, where)


async def _report_purge(owner_id: int, chat_id: int, rows, ids: list[int],
                        where: str) -> None:
    when = db.now()
    payload, stats = transcript.build(rows, chat_title=where, owner_id=owner_id,
                                      requested=len(ids), when=when)
    with_media = [row for row in rows if row["file_id"]][:config.PURGE_MEDIA_LIMIT]

    await reporter.send_document(
        owner_id, payload, transcript.filename(chat_id, when),
        transcript.summary(where, stats, media_sent=len(with_media)))

    for row in rows:
        await db.add_deleted(dict(row))
    await db.drop_messages(owner_id, chat_id, [row["msg_id"] for row in rows])

    for row in with_media:
        icon = mediastore.KIND_ICON.get(row["media_type"], "📎")
        await reporter.send_report(
            owner_id,
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


async def on_edited_business_message(api, message: dict) -> None:
    connection_id = message.get("business_connection_id")
    info = (state.business.get(connection_id)
            or await ensure_connection(api, connection_id) or {})
    owner_id = info.get("user_id")
    if not owner_id or not state.is_approved(owner_id):
        return

    chat = message.get("chat") or {}
    chat_id, message_id = chat.get("id"), message["message_id"]
    row = await db.get_message(owner_id, chat_id, message_id)
    new_text = parse.text_of(message)
    if row is None:
        await _cache(owner_id, message, connection_id)
        return
    old_text = row["text"] or ""
    if old_text == new_text:
        return

    await db.add_edit(owner_id, chat_id, message_id, row["user_id"], old_text,
                      new_text)
    await db.cache_message(
        owner_id=owner_id, chat_id=chat_id, msg_id=message_id,
        user_id=row["user_id"], is_private=bool(row["is_private"]), text=new_text,
        media_type=row["media_type"], media_ref=row["media_ref"],
        reply_to=row["reply_to"], date=row["date"], file_id=row["file_id"],
        business_id=connection_id, user_name=row["user_name"],
    )
    settings = await chatprefs.flags(owner_id, chat_id, True)
    if not settings["log_edits"] or settings["ignored"]:
        return
    if row["user_id"] == owner_id and not config.LOG_OWN:
        return
    await reporter.send_report(
        owner_id,
        "✏️ **Сообщение изменено**\n"
        f"👤 {row['user_name'] or 'неизвестно'} (`{row['user_id']}`)\n"
        f"💬 {parse.chat_title(chat)}\n\n"
        f"**Было:**\n{fmt.truncate(old_text, 1200)}\n\n"
        f"**Стало:**\n{fmt.truncate(new_text, 1200)}"
    )
