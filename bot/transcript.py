"""Сборка переписки в один текстовый файл.

Нужна, когда собеседник очищает чат целиком: отдельная карточка на каждое из
сотни удалённых сообщений — это флуд-лимит и нечитаемая лента.
"""
from __future__ import annotations

import io

from core import fmt, mediastore

SEPARATOR = "─" * 60


def _line(row, owner_id: int | None) -> str:
    who = "Вы" if owner_id and row["user_id"] == owner_id else (
        row["user_name"] or f"id {row['user_id']}")
    out = [f"[{fmt.ts(row['date'])}] {who}"]
    if row["media_type"]:
        icon = mediastore.KIND_ICON.get(row["media_type"], "📎")
        out.append(f"  {icon} <{row['media_type']}>")
    text = (row["text"] or "").strip()
    if text:
        out += [f"  {part}" for part in text.splitlines()]
    if not text and not row["media_type"]:
        out.append("  <пусто>")
    return "\n".join(out)


def build(rows, *, chat_title: str, owner_id: int | None, requested: int,
          when: int, reason: str = "Переписка очищена") -> tuple[bytes, dict]:
    """Возвращает (файл, статистика). rows — то, что удалось поднять из кэша."""
    media = [row for row in rows if row["media_type"]]
    stats = {
        "requested": requested,
        "recovered": len(rows),
        "missing": max(requested - len(rows), 0),
        "media": len(media),
        "first": rows[0]["date"] if rows else None,
        "last": rows[-1]["date"] if rows else None,
    }

    buf = io.StringIO()
    buf.write(f"{reason}\n")
    buf.write(f"Чат: {chat_title}\n")
    buf.write(f"Зафиксировано: {fmt.ts(when)}\n")
    buf.write(f"Удалено сообщений: {requested}\n")
    buf.write(f"Восстановлено из кэша: {stats['recovered']}\n")
    if stats["missing"]:
        buf.write(f"Не было в кэше: {stats['missing']}\n")
    if stats["media"]:
        buf.write(f"Вложений: {stats['media']}\n")
    if rows:
        buf.write(f"Период: {fmt.ts(stats['first'])} — {fmt.ts(stats['last'])}\n")
    buf.write(f"\n{SEPARATOR}\n\n")

    for row in rows:
        buf.write(_line(row, owner_id))
        buf.write("\n\n")
    if not rows:
        buf.write("Ни одного сообщения не сохранилось: они старше срока хранения "
                  "кэша или пришли до подключения бота.\n")

    return buf.getvalue().encode("utf-8"), stats


def summary(chat_title: str, stats: dict, *, reason: str = "🧹 **Переписка очищена**",
            media_sent: int = 0) -> str:
    lines = [reason, f"💬 {chat_title}", ""]
    lines.append(f"🗑 удалено сообщений: **{stats['requested']}**")
    lines.append(f"💾 восстановлено: **{stats['recovered']}**")
    if stats["missing"]:
        lines.append(f"❔ не было в кэше: {stats['missing']}")
    if stats["first"]:
        lines.append(f"🕒 период: {fmt.ts(stats['first'])} — {fmt.ts(stats['last'])}")
    if stats["media"]:
        tail = (f", присылаю {media_sent}" if media_sent < stats["media"]
                else ", присылаю следом")
        lines.append(f"📎 вложений: **{stats['media']}**{tail}")
    return "\n".join(lines)


def filename(chat_id: int, when: int) -> str:
    return f"chat_{chat_id}_{when}.txt"
