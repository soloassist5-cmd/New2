"""Сводка о перехваченном: списком, если мало, файлом — если много."""
from __future__ import annotations

import db
from bot import transcript
from core import fmt, mediastore, reporter

LIST_LIMIT = 10          # больше — уже нечитаемо, отправляем файлом


def _line(row) -> str:
    preview = fmt.truncate(row["text"] or "", 160)
    if not preview and row["media_type"]:
        icon = mediastore.KIND_ICON.get(row["media_type"], "📎")
        preview = f"_{icon} {row['media_type']}_"
    return (f"• `{fmt.ts(row['at'])}` **{row['user_name'] or '—'}**\n"
            f"  {preview or '_пусто_'}")


async def deliver(rows, *, title: str, empty: str, owner_id: int | None = None,
                  chat_title: str = "перехваченное") -> bool:
    """Отдаёт владельцу список или файл. False — отдавать было нечего."""
    if not rows:
        await reporter.send_report(empty)
        return False

    ordered = sorted(rows, key=lambda row: (row["date"] or 0, row["id"]))
    if len(ordered) <= LIST_LIMIT:
        body = "\n".join(_line(row) for row in reversed(ordered))
        await reporter.send_report(f"{title}\n\n{fmt.truncate(body, 3500)}")
        return True

    when = db.now()
    payload, stats = transcript.build(
        ordered, chat_title=chat_title, owner_id=owner_id,
        requested=len(ordered), when=when, reason="Перехваченные сообщения")
    await reporter.send_document(
        payload, f"intercepted_{when}.txt",
        f"{title}\n\nсообщений: **{stats['recovered']}**"
        + (f" · вложений: **{stats['media']}**" if stats["media"] else ""))
    return True
