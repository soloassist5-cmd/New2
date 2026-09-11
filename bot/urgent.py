"""Срочный вызов сквозь режим «Не беспокоить».

Режим удаляет всё подряд, и у человека не остаётся способа сообщить, что дело
не терпит. Поэтому в автоответе есть команда: одна на сутки, чтобы ею не
злоупотребляли.
"""
from __future__ import annotations

import logging
import re
import time

import config
import db
from bot import parse
from core import fmt, reporter

log = logging.getLogger("urgent")

ACCEPTED = (
    "🚨 Принято: отметил как срочное и отправил уведомление. "
    "Отвечу, как только смогу."
)
ALREADY = (
    "🚨 Срочный вызов от вас уже отправлен — следующий можно будет "
    "отправить через {left}."
)


# Отказ — тоже исходящее сообщение от имени владельца: на каждую попытку
# отвечать нельзя, иначе настойчивый человек устроит флуд его руками.
REFUSAL_COOLDOWN = 600
_refused: dict[tuple[int, int], float] = {}


def forget_refusals() -> None:
    _refused.clear()


def _pattern() -> re.Pattern:
    words = "|".join(re.escape(word) for word in config.URGENT_WORDS)
    return re.compile(rf"^\s*[/!](?:{words})\b[\s,.!—-]*([\s\S]*)$", re.IGNORECASE)


def match(text: str) -> str | None:
    """Возвращает причину вызова (может быть пустой) или None, если это не вызов."""
    if not config.URGENT_ENABLED or not config.URGENT_WORDS:
        return None
    found = _pattern().match(text or "")
    return found.group(1).strip() if found else None


def hint() -> str:
    """Строка про срочный вызов для автоответа «Не беспокоить»."""
    if not config.URGENT_ENABLED or not config.URGENT_WORDS:
        return ""
    word = config.URGENT_WORDS[0]
    return (f"\n\nЕсли дело срочное — отправьте `/{word}` и в том же сообщении "
            f"пару слов, в чём дело. Я получу отдельное уведомление. "
            f"Так можно раз в сутки.")


async def _tell_sender(api, chat_id: int, connection_id: str, text: str) -> None:
    try:
        await api.send_message(chat_id, text, business_connection_id=connection_id)
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось ответить на срочный вызов: %r", e)


async def handle(api, owner_id: int, message: dict, connection_id: str,
                 reason: str) -> None:
    """Регистрирует вызов и уведомляет владельца. Квота — одна на сутки."""
    sender = message.get("from") or {}
    user_id = sender.get("id")
    chat_id = (message.get("chat") or {}).get("id")
    who = parse.display_name(sender)

    cooldown = config.URGENT_COOLDOWN_HOURS * 3600
    last = await db.last_urgent_call(owner_id, user_id) or 0
    passed = db.now() - last
    if last and passed < cooldown:
        key = (owner_id, user_id)
        if time.time() - _refused.get(key, 0) >= REFUSAL_COOLDOWN:
            _refused[key] = time.time()
            await _tell_sender(api, chat_id, connection_id,
                               ALREADY.format(left=fmt.human_delta(cooldown - passed)))
        return

    _refused.pop((owner_id, user_id), None)
    await db.add_urgent_call(owner_id, user_id, who, chat_id, reason)
    await _tell_sender(api, chat_id, connection_id, ACCEPTED)

    card = [
        "🚨 **Срочный вызов**",
        f"👤 {who} (`{user_id}`)",
        f"🕒 {fmt.ts(db.now())}",
    ]
    if reason:
        card += ["", f"**{fmt.truncate(reason, 1500)}**"]
    else:
        card += ["", "_Причину не указали._"]
    card.append(f"\n_Человек написал вам, пока включено «Не беспокоить». "
                f"Следующий вызов от него — не раньше чем через "
                f"{fmt.human_delta(cooldown)}._")
    await reporter.send_report(owner_id, "\n".join(card))
    log.info("срочный вызов владельцу %s от %s", owner_id, user_id)


async def recent_text(owner_id: int, limit: int = 10) -> str:
    rows = await db.urgent_calls(owner_id, limit)
    if not rows:
        return ("🚨 Срочных вызовов не было.\n\n"
                "Так люди могут достучаться до вас, пока включено "
                "«Не беспокоить» — по одному разу в сутки каждый.")
    lines = [f"🚨 **Срочные вызовы** ({len(rows)})", ""]
    for row in rows:
        reason = fmt.truncate(row["reason"] or "", 200) or "_без причины_"
        lines.append(f"• `{fmt.ts(row['at'])}` **{row['user_name'] or '—'}** "
                     f"(`{row['user_id']}`)\n  {reason}")
    return fmt.truncate("\n".join(lines), 3500)
