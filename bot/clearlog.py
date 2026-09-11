"""Ручная чистка журнала перехваченного.

Сам журнал чистится по сроку (`DELETED_TTL_DAYS`), но ждать месяц не всегда
хочется. Удаление необратимо, поэтому команда ничего не делает сразу: сначала
показывает, сколько попадёт под нож, и ждёт нажатия кнопки.

Объём чистки целиком лежит в callback_data кнопки — никакого «ожидающего
подтверждения» в памяти процесса, который пропал бы после передеплоя и оставил
кнопку, делающую непонятно что.
"""
from __future__ import annotations

import logging
import re

import config
import db
from core import backup, fmt

log = logging.getLogger("clearlog")

# cl:a:<owner> — всё · cl:d:<дней>:<owner> — старше N · cl:c:<chat>:<owner> — чат
CALLBACK_RE = re.compile(r"^cl:(?:x|(a):(\d+)|([dc]):(-?\d+):(\d+))$")
OFFER_DAYS = (7, 30)
MAX_DAYS = 3650

EMPTY = "🧹 Журнал перехваченного пуст — чистить нечего."
CANCELLED = "Отменено"
NOT_YOURS = "Это не ваша кнопка"


def _plural(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return "сообщений"
    return {1: "сообщение", 2: "сообщения", 3: "сообщения",
            4: "сообщения"}.get(count % 10, "сообщений")


def cutoff(days: int) -> int:
    return db.now() - days * 86400


async def offer(owner_id: int) -> tuple[str, dict | None]:
    """Что сейчас в журнале и какие есть варианты чистки."""
    total = await db.intercepted_summary(owner_id)
    if not total["total"]:
        return EMPTY, None

    lines = ["🧹 **Чистка журнала перехваченного**", "",
             f"Сейчас хранится: **{total['total']}** {_plural(total['total'])}"
             + (f" из **{total['chats']}** чатов" if total["chats"] > 1 else ""),
             f"Самое старое: {fmt.ts(total['oldest'])}"]

    buttons = []
    for days in OFFER_DAYS:
        old = await db.intercepted_in_scope(owner_id, before=cutoff(days))
        if old:
            buttons.append([{"text": f"🧹 Старше {days} дней — {old}",
                             "callback_data": f"cl:d:{days}:{owner_id}"}])
    buttons.append([{"text": f"🗑 Всё — {total['total']}",
                     "callback_data": f"cl:a:{owner_id}"}])
    buttons.append([{"text": "✖️ Отмена", "callback_data": "cl:x"}])

    lines += ["", f"_Само по себе перехваченное удаляется через "
                  f"{config.DELETED_TTL_DAYS} дней. Отменить чистку нельзя._"]
    return "\n".join(lines), {"inline_keyboard": buttons}


async def offer_chat(owner_id: int, chat_id: int,
                     title: str = "") -> tuple[str, dict | None]:
    """То же самое, но для одного чата — вызывается из самой переписки."""
    count = await db.intercepted_in_scope(owner_id, chat_id=chat_id)
    where = f"**{title}**" if title else f"чата `{chat_id}`"
    if not count:
        return f"🧹 Для {where} в журнале ничего нет.", None
    return (f"🧹 **Чистка журнала перехваченного**\n\n"
            f"Чат: {where} · `{chat_id}`\n"
            f"Под чистку попадёт: **{count}** {_plural(count)}\n\n"
            f"_Отменить нельзя._",
            {"inline_keyboard": [
                [{"text": f"🗑 Очистить — {count}",
                  "callback_data": f"cl:c:{chat_id}:{owner_id}"}],
                [{"text": "✖️ Отмена", "callback_data": "cl:x"}],
            ]})


async def offer_days(owner_id: int, days: int) -> tuple[str, dict | None]:
    """Чистка по сроку: `/clearlog 7`."""
    count = await db.intercepted_in_scope(owner_id, before=cutoff(days))
    if not count:
        return f"🧹 Старше {days} дней в журнале ничего нет.", None
    return (f"🧹 **Чистка журнала перехваченного**\n\n"
            f"Под чистку попадёт: **{count}** {_plural(count)} — "
            f"всё старше {days} дней.\n\n_Отменить нельзя._",
            {"inline_keyboard": [
                [{"text": f"🧹 Очистить — {count}",
                  "callback_data": f"cl:d:{days}:{owner_id}"}],
                [{"text": "✖️ Отмена", "callback_data": "cl:x"}],
            ]})


def parse_args(raw: str) -> tuple[str, int] | None:
    """`7` → старше 7 дней, `chat -100500` → один чат. None — показать меню."""
    parts = (raw or "").split()
    if not parts:
        return None
    if parts[0].lower() in {"chat", "чат"} and len(parts) > 1:
        if re.fullmatch(r"-?\d+", parts[1]):
            return "chat", int(parts[1])
        return None
    if parts[0].isdigit() and 1 <= int(parts[0]) <= MAX_DAYS:
        return "days", int(parts[0])
    return None


async def apply(owner_id: int, kind: str, value: int) -> int:
    """Выполняет чистку и сохраняет результат: иначе он вернётся из бэкапа."""
    if kind == "a":
        removed = await db.clear_intercepted(owner_id)
    elif kind == "d":
        removed = await db.clear_intercepted(owner_id, before=cutoff(value))
    else:
        removed = await db.clear_intercepted(owner_id, chat_id=value)
    if removed:
        backup.request_soon()
    log.info("владелец %s почистил журнал (%s=%s): -%s", owner_id, kind,
             value, removed)
    return removed


def done_text(kind: str, value: int, removed: int) -> str:
    if not removed:
        return "🧹 Под чистку ничего не попало — журнал уже пуст."
    where = {"a": "весь журнал",
             "d": f"всё старше {value} дней",
             "c": f"журнал чата `{value}`"}[kind]
    return (f"🧹 **Журнал почищен.**\n\nУдалено: **{removed}** "
            f"{_plural(removed)} — {where}.")


async def handle_callback(api, query: dict) -> bool:
    """Обрабатывает нажатие. False — кнопка не наша."""
    match = CALLBACK_RE.match(query.get("data") or "")
    if match is None:
        return False

    who = (query.get("from") or {}).get("id")
    origin = query.get("message") or {}

    if match.group(1) is None and match.group(3) is None:      # cl:x
        await api.answer_callback(query["id"], CANCELLED)
        await _replace(api, origin, "✖️ Чистка отменена.")
        return True

    if match.group(1):
        kind, value, owner_id = "a", 0, int(match.group(2))
    else:
        kind, value, owner_id = (match.group(3), int(match.group(4)),
                                 int(match.group(5)))

    if who != owner_id:
        await api.answer_callback(query["id"], NOT_YOURS, show_alert=True)
        return True

    removed = await apply(owner_id, kind, value)
    await api.answer_callback(query["id"], f"Удалено: {removed}")
    await _replace(api, origin, done_text(kind, value, removed))
    return True


async def _replace(api, origin: dict, text: str) -> None:
    """Вместо карточки с кнопками — итог: повторно нажать уже нечего."""
    chat = (origin.get("chat") or {}).get("id")
    if not chat:
        return
    try:
        await api.edit_message_text(chat, origin["message_id"], text)
    except Exception as e:                                   # noqa: BLE001
        log.debug("карточку чистки не удалось обновить: %r", e)
