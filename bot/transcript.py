"""Сборка переписки в один текстовый файл.

Нужна, когда собеседник очищает чат целиком: отдельная карточка на каждое из
сотни удалённых сообщений — это флуд-лимит и нечитаемая лента.
"""
from __future__ import annotations

import io

from core import fmt, mediastore

SEPARATOR = "─" * 60
HEADING = "═" * 60
REASONS = {"mute": "мут", "dnd": "не беспокоить"}


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


# ----------------------------------------------------- перехваченное ---------

def _period(rows) -> str:
    return f"{fmt.ts(rows[0]['date'])} — {fmt.ts(rows[-1]['date'])}"


def _counts(rows) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        key = REASONS.get(row["reason"], row["reason"] or "—")
        out[key] = out.get(key, 0) + 1
    return out


def _grouped_line(row, *, who: bool, reason: bool) -> str:
    """Строка в разбивке по чатам: имя и причина — только там, где они разные."""
    extra = []
    if who:
        extra.append(row["user_name"] or f"id {row['user_id']}")
    if reason:
        extra.append(REASONS.get(row["reason"], row["reason"]))
    head = f"[{fmt.ts(row['date'])}]"
    out = [head + (" " + " · ".join(extra) if extra else "")]
    if row["media_type"]:
        icon = mediastore.KIND_ICON.get(row["media_type"], "📎")
        out.append(f"  {icon} <{row['media_type']}>")
    text = (row["text"] or "").strip()
    if text:
        out += [f"  {part}" for part in text.splitlines()]
    if not text and not row["media_type"]:
        out.append("  <пусто>")
    return "\n".join(out)


def _chat_header(index: int, total: int, chat_id, rows, title: str) -> str:
    media = sum(1 for row in rows if row["media_type"])
    counts = _counts(rows)
    parts = [f"сообщений: {len(rows)}"]
    if media:
        parts.append(f"вложений: {media}")
    parts += [f"{name}: {count}" for name, count in sorted(counts.items())]
    return "\n".join([
        HEADING,
        f"[{index}/{total}] {title or 'без названия'}",
        f"chat_id: {chat_id if chat_id is not None else 'неизвестен'}",
        " · ".join(parts),
        f"период: {_period(rows)}",
        SEPARATOR,
    ])


def build_grouped(groups, *, owner_id: int | None, when: int,
                  titles: dict, reason: str = "Перехваченные сообщения",
                  ) -> tuple[bytes, dict]:
    """Файл, разложенный по чатам: вперемешку такое читать невозможно.

    groups — [(chat_id, сообщения по возрастанию даты)], свежий чат первым.
    """
    flat = [row for _, rows in groups for row in rows]
    dates = sorted(row["date"] or 0 for row in flat)
    stats = {
        "total": len(flat),
        "chats": len(groups),
        "media": sum(1 for row in flat if row["media_type"]),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
    }

    buf = io.StringIO()
    buf.write(f"{reason}\n")
    buf.write(f"Собрано: {fmt.ts(when)}\n")
    buf.write(f"Всего сообщений: {stats['total']}\n")
    buf.write(f"Чатов: {stats['chats']}\n")
    if stats["media"]:
        buf.write(f"Вложений: {stats['media']}\n")
    if flat:
        buf.write(f"Период: {fmt.ts(stats['first'])} — {fmt.ts(stats['last'])}\n")
    for name, count in sorted(_counts(flat).items()):
        buf.write(f"Причина «{name}»: {count}\n")

    for index, (chat_id, rows) in enumerate(groups, 1):
        buf.write("\n")
        buf.write(_chat_header(index, len(groups), chat_id, rows,
                               titles.get(chat_id, "")))
        buf.write("\n\n")
        # Имя и причину подписываем только там, где они в чате не одинаковые:
        # в личке иначе одно и то же слово повторяется у каждой строки.
        who = len({row["user_name"] for row in rows}) > 1
        reason_differs = len({row["reason"] for row in rows}) > 1
        for row in rows:
            buf.write(_grouped_line(row, who=who, reason=reason_differs))
            buf.write("\n\n")

    if not flat:
        buf.write("\nПусто.\n")
    return buf.getvalue().encode("utf-8"), stats
