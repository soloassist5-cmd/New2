"""Сводка о перехваченном — всегда txt-файлом.

Файл, а не лента в переписке с ботом: журнал хочется хранить и искать по нему,
а лента тонет среди отчётов об удалениях. Короткая сводка при этом дублируется
в подписи к файлу, чтобы не открывать его ради двух строк.

Внутри всё разложено по чатам. Вперемешку это нечитаемо уже на втором
собеседнике: строки идут по времени, а глазами нужен ответ на вопрос «что мне
писал вот этот».
"""
from __future__ import annotations

import datetime as _dt

import config
import db
from bot import transcript
from core import fmt, mediastore, reporter

# Сколько сообщений ещё показать прямо в подписи к файлу. 0 — только файл.
LIST_LIMIT = config.DIGEST_LIST_LIMIT
CAPTION_ROOM = 900       # у подписи Telegram предел 1024, остальное — на разметку
DEFAULT_TITLE = "перехваченное"
REASON_ICON = {"mute": "🔇", "dnd": "🌙"}


def group(rows) -> list[tuple[int | None, list]]:
    """Раскладывает перехваченное по чатам; чат со свежим сообщением — первым."""
    chats: dict[int | None, list] = {}
    for row in rows:
        chats.setdefault(row["chat_id"], []).append(row)
    for messages in chats.values():
        messages.sort(key=lambda row: (row["date"] or 0, row["id"]))
    return sorted(chats.items(), key=lambda item: item[1][-1]["date"] or 0,
                  reverse=True)


async def titles(owner_id: int, groups) -> dict:
    """Названия чатов: id в отчёте нужен, но искать по нему человека неудобно."""
    out: dict[int | None, str] = {}
    for chat_id, rows in groups:
        if chat_id is None:
            continue
        named = await db.chat_title(owner_id, chat_id)
        # Имя отправителя — запасной вариант: в личке чат и есть собеседник.
        out[chat_id] = named or next(
            (row["user_name"] for row in rows if row["user_name"]), "")
    return out


def _preview(row) -> str:
    text = fmt.truncate(row["text"] or "", 160)
    if not text and row["media_type"]:
        icon = mediastore.KIND_ICON.get(row["media_type"], "📎")
        return f"_{icon} {row['media_type']}_"
    return text or "_пусто_"


def _line(row, *, senders: bool, reasons: bool) -> str:
    head = f"• `{fmt.hm(row['date'])}`"
    if reasons:
        head += f" {REASON_ICON.get(row['reason'], '•')}"
    if senders:
        head += f" **{row['user_name'] or '—'}**"
    return f"{head}\n  {_preview(row)}"


def _chat_block(chat_id, rows, title: str) -> str:
    """Заголовок чата и его сообщения, свежие снизу — как в самой переписке."""
    senders = len({row["user_name"] for row in rows}) > 1
    reasons = len({row["reason"] for row in rows}) > 1
    where = title or "без названия"
    head = (f"💬 **{where}** · `{chat_id}` — {len(rows)}" if chat_id is not None
            else f"💬 **{where}** — {len(rows)}")
    return "\n".join([head] + [_line(row, senders=senders, reasons=reasons)
                               for row in rows])


def _caption(title: str, stats: dict, groups, names: dict) -> str:
    """Подпись к файлу: короткую сводку показываем прямо в ней."""
    if 0 < stats["total"] <= LIST_LIMIT:
        preview = "\n\n".join(_chat_block(chat_id, messages, names.get(chat_id, ""))
                              for chat_id, messages in groups)
        tail = (f"\n\n📎 вложений: **{stats['media']}**" if stats["media"] else "")
        caption = f"{title}\n\n{preview}{tail}"
        # Подпись Telegram обрежет по 1024 символа, а обрезанная сводка хуже,
        # чем её отсутствие: файл-то с полным текстом всё равно приложен.
        if len(caption) <= CAPTION_ROOM:
            return caption

    lines = [title, "", f"сообщений: **{stats['total']}**"]
    if stats["chats"] > 1:
        lines.append(f"чатов: **{stats['chats']}**")
    if stats["media"]:
        lines.append(f"вложений: **{stats['media']}**")
    return "\n".join(lines)


def filename(when: int) -> str:
    """Дата в имени: журнал копится, и потом его надо как-то находить."""
    return f"intercepted_{_dt.datetime.fromtimestamp(when):%Y-%m-%d_%H%M}.txt"


async def deliver(owner_id: int, rows, *, title: str, empty: str,
                  chat_title: str = DEFAULT_TITLE) -> bool:
    """Отдаёт владельцу журнал файлом. False — отдавать было нечего.

    Файл приходит всегда, даже на одно сообщение: его удобно хранить и искать
    по нему, а лента в переписке с ботом теряется среди отчётов. Короткая
    сводка при этом дублируется в подписи — открывать файл ради двух строк
    незачем.
    """
    if not rows:
        if empty:
            await reporter.send_report(owner_id, empty)
        return False

    groups = group(rows)
    names = await titles(owner_id, groups)
    when = db.now()
    # «Перехваченные сообщения — перехваченное» в шапке файла выглядит глупо:
    # уточнение дописываем, только когда оно и правда что-то уточняет.
    heading = "Перехваченные сообщения"
    if chat_title and chat_title != DEFAULT_TITLE:
        heading += f" — {chat_title}"
    payload, stats = transcript.build_grouped(
        groups, owner_id=owner_id, when=when, titles=names, reason=heading)
    await reporter.send_document(owner_id, payload, filename(when),
                                 _caption(title, stats, groups, names))
    return True
