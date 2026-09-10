"""Перевод разметки Telethon (`**жирный**`) в HTML для Bot API.

Тексты во всём проекте написаны в одном стиле, а Bot API не понимает такой
Markdown: legacy-режим ждёт `*жирный*`, MarkdownV2 требует экранировать почти
всю пунктуацию. HTML — единственный вариант без сюрпризов.
"""
from __future__ import annotations

import html
import re

_FENCED = re.compile(r"```(?:\w+\n)?([\s\S]*?)```")
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")
_STRIKE = re.compile(r"~~([^~]+)~~")

_SLOT = "\x00{}\x00"
_SLOT_RE = re.compile(r"\x00(\d+)\x00")


def to_html(text: str) -> str:
    """Готовый HTML для parse_mode=HTML."""
    if not text:
        return ""

    parked: list[str] = []

    def park(fragment: str) -> str:
        parked.append(fragment)
        return _SLOT.format(len(parked) - 1)

    # Код и ссылки прячем до экранирования: внутри них разметку искать не нужно.
    text = _FENCED.sub(lambda m: park(f"<pre>{html.escape(m.group(1))}</pre>"), text)
    text = _CODE.sub(lambda m: park(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK.sub(
        lambda m: park(f'<a href="{html.escape(m.group(2), quote=True)}">'
                       f"{html.escape(m.group(1))}</a>"), text)

    text = html.escape(text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)

    return _SLOT_RE.sub(lambda m: parked[int(m.group(1))], text)


_TAG = re.compile(r"<(/?)(\w+)[^>]*>")


def trim(text: str, limit: int) -> str:
    """Обрезает по длине и закрывает повисшие теги — иначе Telegram отклонит текст."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]

    if cut.rfind("<") > cut.rfind(">"):          # не рвать тег посередине
        cut = cut[: cut.rfind("<")]
    amp = cut.rfind("&")
    if amp > cut.rfind(";") and len(cut) - amp <= 8:   # не рвать &amp; и подобные
        cut = cut[:amp]

    stack: list[str] = []
    for match in _TAG.finditer(cut):
        closing, name = match.group(1), match.group(2)
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)
    return cut + "…" + "".join(f"</{name}>" for name in reversed(stack))
