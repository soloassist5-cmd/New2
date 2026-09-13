"""Ручная чистка всего, что копится само.

Само оно чистится по сроку (`DELETED_TTL_DAYS`, `CACHE_TTL_HOURS`), но ждать
месяц не всегда хочется. Удаление необратимо, поэтому команда ничего не делает
сразу: сначала показывает, сколько попадёт под нож, и ждёт нажатия кнопки.

Объём чистки целиком лежит в callback_data кнопки — никакого «ожидающего
подтверждения» в памяти процесса, которое пропало бы после передеплоя и
оставило кнопку, делающую непонятно что.
"""
from __future__ import annotations

import logging
import re

import config
import db
from core import backup, fmt

log = logging.getLogger("cleanup")

# Порядок тот же, в каком идут кнопки и строки обзора.
KINDS = (
    ("intercepted", "🔇", "перехваченное", "мут и «не беспокоить»"),
    ("deleted", "🗑", "удалённые", "журнал антиудаления"),
    ("edits", "✏️", "правки", "«было → стало»"),
    ("urgent", "🚨", "срочные вызовы", "кто дёргал сквозь «не беспокоить»"),
    ("names", "🏷", "история имён", "как собеседников звали раньше"),
    ("cache", "💾", "кэш переписки", "из него восстанавливаются удалённые"),
)
LABEL = {key: (icon, name) for key, icon, name, _ in KINDS}
CODES = {key: key[0] for key, *_ in KINDS}          # i d e u n c
BY_CODE = {code: key for key, code in CODES.items()}
ALL = "*"

# cl:x — отмена · cl:<код>:<a|d|c>:<значение>:<владелец>
CALLBACK_RE = re.compile(r"^cl:(?:x|([ideunc*]):([adc]):(-?\d+):(\d+))$")
OFFER_DAYS = (7, 30)
MAX_DAYS = 3650

EMPTY = "🧹 Чистить нечего — всё и так пусто."
CANCELLED = "Отменено"
NOT_YOURS = "Это не ваша кнопка"


def _plural(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return "записей"
    return {1: "запись", 2: "записи", 3: "записи",
            4: "записи"}.get(count % 10, "записей")


def cutoff(days: int) -> int:
    return db.now() - days * 86400


def _kinds_of(code: str) -> list[str]:
    return [key for key, *_ in KINDS] if code == ALL else [BY_CODE[code]]


def where_words(code: str, scope: str, value: int) -> str:
    what = "всё" if code == ALL else LABEL[BY_CODE[code]][1]
    if scope == "d":
        return f"{what} старше {value} дней"
    if scope == "c":
        return f"{what} по чату `{value}`"
    return "всё сразу" if code == ALL else what


async def _count(code: str, owner_id: int, scope: str, value: int) -> int:
    chat_id = value if scope == "c" else None
    before = cutoff(value) if scope == "d" else None
    return sum([await db.count_kind(kind, owner_id, chat_id=chat_id, before=before)
                for kind in _kinds_of(code)])


def _button(code: str, scope: str, value: int, owner_id: int, text: str) -> dict:
    return {"text": text, "callback_data": f"cl:{code}:{scope}:{value}:{owner_id}"}


CANCEL = [{"text": "✖️ Отмена", "callback_data": "cl:x"}]


async def overview(owner_id: int) -> tuple[str, dict | None]:
    """Что сейчас накопилось и что из этого можно почистить."""
    counts = await db.counts_by_kind(owner_id)
    if not any(counts.values()):
        return EMPTY, None

    lines = ["🧹 **Что у меня накопилось**", ""]
    buttons = []
    for key, icon, name, hint in KINDS:
        total = counts.get(key, 0)
        if not total:
            continue
        oldest = await db.oldest_of(key, owner_id)
        since = f" _(с {fmt.hm(oldest)})_" if oldest else ""
        lines.append(f"{icon} {name}: **{total}** _{hint}_{since}")
        buttons.append([_button(CODES[key], "a", 0, owner_id,
                                f"{icon} {name} — {total}")])

    total = sum(counts.values())
    buttons.append([_button(ALL, "a", 0, owner_id, f"🧨 Всё сразу — {total}")])
    buttons.append(CANCEL)
    lines += ["", f"_Само по себе это удаляется через {config.DELETED_TTL_DAYS} "
                  f"дней. Отменить чистку нельзя._"]
    return "\n".join(lines), {"inline_keyboard": buttons}


async def offer(owner_id: int, code: str, scope: str = "a",
                value: int = 0) -> tuple[str, dict | None]:
    """Подтверждение для одной области: сколько уйдёт и кнопка."""
    total = await _count(code, owner_id, scope, value)
    where = where_words(code, scope, value)
    if not total:
        return f"🧹 {where.capitalize()} — чистить нечего.", None

    buttons = [[_button(code, scope, value, owner_id, f"🗑 Очистить — {total}")]]
    # Крупную чистку предлагаем сузить по сроку: обычно нужно именно старое.
    if scope == "a":
        for days in OFFER_DAYS:
            older = await _count(code, owner_id, "d", days)
            if older and older < total:
                buttons.append([_button(code, "d", days, owner_id,
                                        f"🧹 Только старше {days} дней — {older}")])
    buttons.append(CANCEL)
    return (f"🧹 **Чистка**\n\nПод чистку попадёт: **{total}** {_plural(total)}"
            f" — {where}.\n\n_Отменить нельзя._",
            {"inline_keyboard": buttons})


def parse_args(raw: str) -> tuple[str, int]:
    """`7` → старше 7 дней, `chat -100500` → один чат, пусто → всё."""
    parts = (raw or "").split()
    if not parts:
        return "a", 0
    if parts[0].lower() in {"chat", "чат"} and len(parts) > 1:
        if re.fullmatch(r"-?\d+", parts[1]):
            return "c", int(parts[1])
        return "a", 0
    if parts[0].isdigit() and 1 <= int(parts[0]) <= MAX_DAYS:
        return "d", int(parts[0])
    return "a", 0


async def apply(owner_id: int, code: str, scope: str, value: int) -> int:
    """Выполняет чистку и сохраняет результат: иначе он вернётся из бэкапа."""
    chat_id = value if scope == "c" else None
    before = cutoff(value) if scope == "d" else None
    removed = 0
    for kind in _kinds_of(code):
        removed += await db.clear_kind(kind, owner_id, chat_id=chat_id,
                                       before=before)
    if removed:
        # Именно сразу: отложенная копия не переживёт остановки процесса, и
        # после передеплоя почищенное вернулось бы из старого бэкапа.
        await backup.save_now()
    log.info("владелец %s почистил %s (%s=%s): -%s", owner_id, code, scope,
             value, removed)
    return removed


def done_text(code: str, scope: str, value: int, removed: int) -> str:
    if not removed:
        return "🧹 Под чистку ничего не попало — уже пусто."
    return (f"🧹 **Почищено.**\n\nУдалено: **{removed}** {_plural(removed)} — "
            f"{where_words(code, scope, value)}.")


async def handle_callback(api, query: dict) -> bool:
    """Обрабатывает нажатие. False — кнопка не наша."""
    match = CALLBACK_RE.match(query.get("data") or "")
    if match is None:
        return False

    origin = query.get("message") or {}
    if match.group(1) is None:                                   # cl:x
        await api.answer_callback(query["id"], CANCELLED)
        await _replace(api, origin, "✖️ Чистка отменена.")
        return True

    code, scope, value, owner_id = (match.group(1), match.group(2),
                                    int(match.group(3)), int(match.group(4)))
    if (query.get("from") or {}).get("id") != owner_id:
        await api.answer_callback(query["id"], NOT_YOURS, show_alert=True)
        return True

    removed = await apply(owner_id, code, scope, value)
    await api.answer_callback(query["id"], f"Удалено: {removed}")
    await _replace(api, origin, done_text(code, scope, value, removed))
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
