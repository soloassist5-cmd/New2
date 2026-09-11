"""Досье на собеседника: всё, что бот успел записать про человека.

Это не разведка: наружу бот не ходит и ничего не ищет. В карточке только своя
база — переписка владельца с этим человеком — и то, что Telegram сам приложил к
его сообщению. Поэтому карточка и уходит в личку с ботом, а не в переписку.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time

import db
from bot import parse, regdate
from core import fmt

log = logging.getLogger("dossier")

REASONS = {"mute": "мут", "dnd": "не беспокоить"}
BIO_LIMIT = 200
ALIAS_LIMIT = 5          # длиннее — это уже не досье, а лента
NIGHT = range(0, 6)
QUIET = 3 * 86400        # молчание короче трёх дней ни о чём не говорит
ENOUGH = 15              # меньше сообщений — «привычка» будет выдумкой
EMPTY = ("🕵️ **{name}**\n🆔 `{user_id}`\n\n"
         "Больше про него у меня ничего нет: ни сохранённых сообщений, ни "
         "удалений, ни перехваченного.")
# Поля, по которым видно, что человек нам хоть сколько-то знаком. Номер сюда не
# входит: по нему считается дата регистрации, а её знает и совсем чужой id.
FACTS = ("username", "bio", "birthdate", "is_premium", "language_code")


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


def _identity(profile: dict, known: str | None, user_id: int, *,
              renamed: bool = False) -> list[str]:
    """Кто это: сообщение, getChat и база — в таком порядке доверия."""
    name = parse.display_name(profile) if profile else (known or f"id {user_id}")
    lines = [f"🕵️ **{name}**", f"🆔 `{user_id}`"]

    # Запасной вариант для старых записей, у которых истории имён ещё нет.
    # «Раньше звали иначе» — только про настоящую смену: в базе часто лежит
    # укороченный вариант («Вася» против «Вася Пупкин»), и выдавать его за
    # переименование — врать.
    if known and not renamed and not _same_person(known, name):
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

    born_account = regdate.words(user_id)
    if born_account:
        lines.append(f"🗓 {born_account}")
    return lines


def _portrait(times: list[int]) -> list[str]:
    """Когда человек пишет — по нашим же сохранённым сообщениям, без гаданий."""
    if len(times) < ENOUGH:
        return []

    hours = [_dt.datetime.fromtimestamp(at).hour for at in times]
    # Трёхчасовое окно, а не один час: на одном часе это шум, а не привычка.
    window = max(range(24), key=lambda h: sum(hours.count((h + i) % 24)
                                              for i in range(3)))
    share = sum(hours.count((window + i) % 24) for i in range(3)) / len(hours)
    lines = [f"🕰 чаще пишет с {window}:00 до {(window + 3) % 24}:00 "
             f"_({share:.0%} сообщений)_"]

    night = sum(1 for hour in hours if hour in NIGHT)
    if night and night / len(hours) >= 0.2:
        lines.append(f"🌃 ночью, с 00 до 06: **{night / len(hours):.0%}**")

    gaps = [(later - earlier, earlier)
            for earlier, later in zip(times, times[1:], strict=False)]
    longest, since = max(gaps, default=(0, 0))
    if longest >= QUIET:
        lines.append(f"🤐 дольше всего молчал(а) {fmt.human_delta(longest)} "
                     f"_(с {fmt.hm(since)})_")

    days = (times[-1] - times[0]) / 86400
    if days >= 2:
        lines.append(f"📈 в среднем {len(times) / days:.1f} сообщений в день")
    return lines


def _aliases(rows) -> list[str]:
    """История имён — только то, что бот видел сам. Свежее сверху."""
    if len(rows) < 2:
        return []
    lines = []
    for row in rows[1:ALIAS_LIMIT + 1]:
        handle = f" (@{row['username']})" if row["username"] else ""
        lines.append(f"• до {fmt.hm(row['last_seen'])} — "
                     f"{row['name'] or '—'}{handle}")
    return ["🏷 **Раньше звали иначе**", *lines]


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

    names = _aliases(await db.aliases(owner_id, user_id, ALIAS_LIMIT + 1))
    head = _identity(profile, data["name"], user_id, renamed=bool(names))
    history = _history(data)
    portrait = _portrait(await db.message_times(owner_id, user_id))
    status = _status(data, chat_id)

    if not any((history, status, names, portrait)) and not any(
            profile.get(key) for key in FACTS):
        blank = EMPTY.format(name=data["name"] or (parse.display_name(profile)
                                                   if profile
                                                   else f"id {user_id}"),
                             user_id=user_id)
        born = regdate.words(user_id)
        # Дату регистрации считает сам номер — это единственное, что можно
        # сказать о человеке, которого бот никогда не видел.
        return f"{blank}\n\nСудя по номеру, аккаунт {born}." if born else blank

    blocks = [head]
    if names:
        blocks.append(names)
    if history:
        blocks.append(["📊 **Что у меня записано**", *history])
    if portrait:
        blocks.append(["🧭 **Как пишет**", *portrait])
    if status:
        blocks.append(["⚙️ **Сейчас**", *status])
    return fmt.truncate("\n\n".join("\n".join(block) for block in blocks), 3500)
