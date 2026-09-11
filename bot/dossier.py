"""Досье на собеседника: всё, что бот успел записать про человека.

Это не разведка: наружу бот не ходит и ничего не ищет. В карточке только своя
база — переписка владельца с этим человеком — и то, что Telegram сам приложил к
его сообщению. Поэтому карточка и уходит в личку с ботом, а не в переписку.
"""
from __future__ import annotations

import logging
import time

import db
from bot import parse
from core import fmt

log = logging.getLogger("dossier")

REASONS = {"mute": "мут", "dnd": "не беспокоить"}
BIO_LIMIT = 200
EMPTY = ("🕵️ **{name}**\n🆔 `{user_id}`\n\n"
         "Больше про него у меня ничего нет: ни сохранённых сообщений, ни "
         "удалений, ни перехваченного.")


def _since(first: int | None, last: int | None) -> str:
    if not first:
        return ""
    if last and last - first > 86400:
        return f" _({fmt.hm(first)} — {fmt.hm(last)})_"
    return f" _(с {fmt.hm(first)})_"


def _same_person(known: str, name: str) -> bool:
    a, b = known.casefold().strip(), name.casefold().strip()
    return a == b or a in b or b in a


def _birthday(raw: dict | None) -> str:
    if not raw or not raw.get("day"):
        return ""
    day = f"{raw['day']:02d}.{raw.get('month', 0):02d}"
    return f"{day}.{raw['year']}" if raw.get("year") else day


def _identity(profile: dict, known: str | None, user_id: int) -> list[str]:
    """Кто это: сообщение, getChat и база — в таком порядке доверия."""
    name = parse.display_name(profile) if profile else (known or f"id {user_id}")
    lines = [f"🕵️ **{name}**", f"🆔 `{user_id}`"]

    # «Раньше звали иначе» — только про настоящую смену имени. В базе часто
    # лежит укороченный вариант («Вася» против «Вася Пупкин»), и сообщать о
    # нём как о переименовании — врать.
    if known and not _same_person(known, name):
        lines.append(f"🏷 раньше был(а): {known}")
    if profile.get("username"):
        lines.append(f"🔗 @{profile['username']}")

    marks = []
    if profile.get("is_premium"):
        marks.append("⭐️ Premium")
    if profile.get("language_code"):
        marks.append(f"🌐 {profile['language_code']}")
    if profile.get("is_bot"):
        marks.append("🤖 бот")
    born = _birthday(profile.get("birthdate"))
    if born:
        marks.append(f"🎂 {born}")
    if marks:
        lines.append(" · ".join(marks))

    if profile.get("bio"):
        lines.append(f"📝 {fmt.truncate(profile['bio'], BIO_LIMIT)}")
    return lines


def _history(data: dict) -> list[str]:
    """Что накопилось в базе. Пустые строки не пишем — это просто шум."""
    lines = []
    msgs, dels = data["messages"], data["deleted"]
    if msgs["total"]:
        tail = f", вложений {msgs['media']}" if msgs["media"] else ""
        lines.append(f"💾 сохранено сообщений: **{msgs['total']}**{tail}"
                     + _since(msgs["first"], msgs["last"]))
    if dels["total"]:
        lines.append(f"🗑 удалял(а) у всех: **{dels['total']}** · "
                     f"последний раз {fmt.hm(dels['last'])}")
    if data["edits"]["total"]:
        lines.append(f"✏️ правил(а) сообщения: **{data['edits']['total']}** · "
                     f"последний раз {fmt.hm(data['edits']['last'])}")

    kept = data["intercepted"]
    if kept:
        parts = " · ".join(f"{REASONS.get(key, key)} {value}"
                           for key, value in sorted(kept.items()))
        lines.append(f"🔇 перехвачено: **{sum(kept.values())}** _({parts})_")
    if data["urgent"]["total"]:
        lines.append(f"🚨 срочных вызовов: **{data['urgent']['total']}** · "
                     f"последний {fmt.hm(data['urgent']['last'])}")
    return lines


def _status(data: dict, chat_id: int | None) -> list[str]:
    lines = []
    now = int(time.time())
    for row in data["mutes"]:
        where = "во всех чатах" if row["chat_id"] == 0 else "в этом чате"
        if row["until"] and row["until"] <= now:
            continue
        when = f"до {fmt.ts(row['until'])}" if row["until"] else "бессрочно"
        why = f" · {row['reason']}" if row["reason"] else ""
        lines.append(f"🔇 замучен(а) {where}, {when}{why}")
    if data["allowed"]:
        lines.append("✅ в белом списке — «не беспокоить» его пропускает")

    chat = data["chat"]
    if chat and chat["ignored"]:
        lines.append("🙈 чат в игноре: ничего не сохраняю и не сообщаю")
    elif chat and chat["antidelete"] == 0:
        lines.append("🔕 антиудаление в этом чате выключено")
    if chat_id is not None and chat_id != data["user_id"]:
        lines.append(f"💬 чат: `{chat_id}`")
    return lines


async def extras(api, user_id: int) -> dict:
    """Био и прочее из getChat. Может не ответить — это не повод падать."""
    try:
        return await api.get_chat(user_id) or {}
    except Exception as e:                                   # noqa: BLE001
        log.debug("getChat(%s) не ответил: %r", user_id, e)
        return {}


async def card(api, owner_id: int, user_id: int, *, chat_id: int | None = None,
               user: dict | None = None) -> str:
    data = await db.dossier(owner_id, user_id, chat_id)
    # getChat знает био и день рождения, сообщение — Premium и язык. Живое
    # сообщение свежее, поэтому кладётся сверху.
    profile = {**(await extras(api, user_id) if api is not None else {}),
               **(user or {})}

    history, status = _history(data), _status(data, chat_id)
    if not history and not status and len(_identity(profile, data["name"],
                                                    user_id)) <= 2:
        return EMPTY.format(name=data["name"] or (parse.display_name(profile)
                                                  if profile else f"id {user_id}"),
                            user_id=user_id)

    blocks = [_identity(profile, data["name"], user_id)]
    if history:
        blocks.append(["📊 **Что у меня записано**", *history])
    if status:
        blocks.append(["⚙️ **Сейчас**", *status])
    return fmt.truncate("\n\n".join("\n".join(block) for block in blocks), 3500)
