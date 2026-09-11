"""Сводка о перехваченном: списком, если мало, файлом — если много.

Перехваченное копится из всех чатов сразу, поэтому и список, и файл разложены
по чатам. Вперемешку это нечитаемо уже на втором собеседнике: строки идут по
времени, а глазами нужен ответ на вопрос «что мне писал вот этот».
"""
from __future__ import annotations

import config
import db
from bot import transcript
from core import fmt, mediastore, reporter

# Больше — уже нечитаемо, отправляем файлом. 0 — файлом всегда.
LIST_LIMIT = config.DIGEST_LIST_LIMIT
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


async def deliver(owner_id: int, rows, *, title: str, empty: str,
                  chat_title: str = "перехваченное") -> bool:
    """Отдаёт владельцу список или файл. False — отдавать было нечего."""
    if not rows:
        if empty:
            await reporter.send_report(owner_id, empty)
        return False

    groups = group(rows)
    names = await titles(owner_id, groups)

    if len(rows) <= LIST_LIMIT:
        blocks = [_chat_block(chat_id, messages, names.get(chat_id, ""))
                  for chat_id, messages in groups]
        body = fmt.truncate("\n\n".join(blocks), 3500)
        await reporter.send_report(owner_id, f"{title}\n\n{body}")
        return True

    when = db.now()
    payload, stats = transcript.build_grouped(
        groups, owner_id=owner_id, when=when, titles=names,
        reason=f"Перехваченные сообщения — {chat_title}")
    caption = [title, "", f"сообщений: **{stats['total']}**",
               f"чатов: **{stats['chats']}**"]
    if stats["media"]:
        caption.append(f"вложений: **{stats['media']}**")
    await reporter.send_document(owner_id, payload, f"intercepted_{when}.txt",
                                 "\n".join(caption))
    return True
