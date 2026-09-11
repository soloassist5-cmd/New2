"""Карточка правки: что именно изменилось.

Два абзаца «Было» и «Стало» заставляют владельца сличать их глазами — а правка
чаще всего в одну букву. Поэтому здесь тексты сливаются в один: убранное
зачёркнуто, добавленное выделено. Разбирать нечего, разница видна сразу.

Когда от старого текста не осталось почти ничего, слитый вид превращается в
кашу — тогда честнее показать два абзаца, как раньше.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from bot import markup, parse
from core import fmt

# Короткие строки сравниваем посимвольно: правка «првет» → «привет» так и
# читается — «пр(и)вет». На длинных это превратилось бы в рябь из букв.
CHARWISE = 80
# Ниже этой схожести общего почти нет, и слитый вид только мешает.
LOW = 0.35
PIECE = 1500             # столько текста влезает в карточку без простыни
QUICK = 120              # правка в первые две минуты — это опечатка, не подмена

_WORDS = re.compile(r"\s+|\S+")


def _split(text: str, charwise: bool) -> list[str]:
    return list(text) if charwise else _WORDS.findall(text)


def _mark(piece: str, wrapper: str) -> str:
    """Пробел под зачёркиванием не виден, а разметку ломает — его не трогаем."""
    if not piece or not piece.strip():
        return markup.raw(piece)
    return f"{wrapper}{markup.raw(piece)}{wrapper}"


def merge(old: str, new: str) -> str | None:
    """Слитый текст правки. None — тексты слишком разные, сливать нечего."""
    if not old or not new:
        return None

    charwise = max(len(old), len(new)) <= CHARWISE
    before, after = _split(old, charwise), _split(new, charwise)
    matcher = SequenceMatcher(None, before, after, autojunk=False)
    if matcher.ratio() < LOW:
        return None

    out = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        gone, added = "".join(before[i1:i2]), "".join(after[j1:j2])
        if tag == "equal":
            out.append(markup.raw(gone))
            continue
        if tag in {"delete", "replace"}:
            out.append(_mark(gone, "~~"))
        if tag in {"insert", "replace"}:
            out.append(_mark(added, "**"))
    return "".join(out)


def _quote(text: str) -> str:
    """Цитатой: сообщение собеседника отделяется от служебных строк."""
    return "\n".join(f"> {line}" for line in text.split("\n"))


def _when(row, now: int) -> str:
    sent = row["date"] or 0
    if not sent:
        return ""
    passed = max(now - sent, 0)
    if passed <= QUICK:
        return f"🕒 поправил(а) через {fmt.human_delta(passed)}"
    return f"🕒 отправлено {fmt.hm(sent)}, поправлено спустя {fmt.human_delta(passed)}"


def _nth(count: int) -> str:
    return f"правка №{count}" if count > 1 else ""


def card(row, chat: dict, old: str, new: str, *, now: int,
         count: int = 1, owner_id: int | None = None) -> str:
    """Готовый текст уведомления о правке."""
    who = ("Вы" if owner_id and row["user_id"] == owner_id
           else row["user_name"] or "неизвестно")
    where = parse.chat_title(chat)
    head = [f"✏️ **Правка** · {who}"]
    if where and where != who:
        head.append(f"💬 {where}")

    marks = [mark for mark in (_when(row, now), _nth(count)) if mark]
    if marks:
        head.append(" · ".join(marks))

    old, new = fmt.truncate(old, PIECE), fmt.truncate(new, PIECE)
    if not old:
        body = ["📝 **Подпись добавлена**", "", _quote(markup.raw(new))]
    elif not new:
        body = ["🧹 **Текст убран.** Было:", "", _quote(markup.raw(old))]
    else:
        merged = merge(old, new)
        if merged is not None:
            body = ["", _quote(merged)]
        else:
            # Общего почти нет — сливать бессмысленно, показываем как есть.
            body = ["", "**Было:**", _quote(markup.raw(old)),
                    "", "**Стало:**", _quote(markup.raw(new))]
    return "\n".join(head + body)
