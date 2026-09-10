"""Кнопочное управление ботом.

Команды помнить не обязательно: всё состояние видно на одном экране, и любой
режим выключается той же кнопкой, которой включён. Экраны перерисовываются
в том же сообщении, так что лента не засоряется.
"""
from __future__ import annotations

import time

import config
import db
from core import fmt, state

PAGE = 8                     # записей журнала на экран
BACK = {"text": "◀️ Назад", "callback_data": "m:home"}


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def markup(*rows: list[dict]) -> dict:
    return {"inline_keyboard": [row for row in rows if row]}


async def _title(owner_id: int, chat_id: int) -> str:
    return await db.chat_title(owner_id, chat_id) or f"чат {chat_id}"


async def _person(owner_id: int, user_id: int) -> str:
    return await db.name_of(owner_id, user_id) or f"id {user_id}"


# ------------------------------------------------------------- главный -----

async def home(owner_id: int) -> tuple[str, dict]:
    stats = await db.stats(owner_id)
    ignored = [row for row in await db.tuned_chats(owner_id) if row["ignored"]]
    dnd = state.dnd_active(owner_id)

    lines = ["🛡 **Guard**", ""]
    lines.append(f"🌙 Не беспокоить: **{'включён' if dnd else 'выключен'}**")
    lines.append("🔗 Личные чаты: "
                 + ("**подключены**" if state.business_of(owner_id)
                    else "**не подключены** — /connect"))
    lines.append(f"🗑 Сохранено удалённых: **{stats['deleted']}**")
    if stats["mutes"]:
        lines.append(f"🔇 Замучено: **{stats['mutes']}**")
    if ignored:
        lines.append(f"🙈 Чатов в игноре: **{len(ignored)}**")

    keyboard = markup(
        [button(f"🌙 Не беспокоить: {'ВКЛ' if dnd else 'выкл'}", "m:dnd")],
        [button("🗑 Журнал", "m:log:0"), button("🔇 Муты", "m:mutes")],
        [button("🙈 Чаты", "m:chats"), button("✅ Белый список", "m:allow")],
        [button("❓ Что тут как работает", "m:help")],
    )
    return "\n".join(lines), keyboard


# ------------------------------------------------------- не беспокоить -----

async def dnd(owner_id: int) -> tuple[str, dict]:
    active = state.dnd_active(owner_id)
    quoted = "\n".join(f"> {line}" for line in config.DND_TEXT.splitlines())

    lines = ["🌙 **Режим «Не беспокоить»**", ""]
    if active:
        since = state.dnd_since(owner_id)
        lines.append(f"Сейчас: **включён**, уже {fmt.uptime(time.time() - since)}.")
    else:
        lines.append("Сейчас: **выключен**.")
    lines += [
        "",
        "Пока включён, сообщения **от всех** удаляются сразу после отправки, "
        "а отправитель получает от вашего имени:",
        "",
        quoted,
        "",
        "Всё удалённое сохраняется — посмотреть можно в журнале. "
        "Исключения задаются белым списком.",
    ]
    toggle = (button("☀️ Выключить", "dnd:off") if active
              else button("🌙 Включить", "dnd:on"))
    return "\n".join(lines), markup([toggle], [button("✅ Белый список", "m:allow")],
                                    [BACK])


# ------------------------------------------------------------- журнал -----

async def log(owner_id: int, offset: int = 0) -> tuple[str, dict]:
    rows = await db.last_deleted(owner_id, None, offset + PAGE + 1)
    page = rows[offset:offset + PAGE]
    if not rows:
        return ("🗑 **Журнал пуст.**\n\nСюда попадают сообщения, которые удалили "
                "в ваших личных чатах.", markup([BACK]))

    lines = [f"🗑 **Удалённые сообщения** ({offset + 1}–{offset + len(page)})", ""]
    for row in page:
        preview = fmt.truncate(row["text"] or "", 140) or (
            f"_{row['media_type']}_" if row["media_type"] else "_пусто_")
        lines.append(f"• `{fmt.ts(row['deleted_at'])}` **{row['user_name'] or '—'}**\n"
                     f"  {preview}")

    nav = []
    if offset:
        nav.append(button("⬅️", f"m:log:{max(offset - PAGE, 0)}"))
    if len(rows) > offset + PAGE:
        nav.append(button("➡️ Ещё", f"m:log:{offset + PAGE}"))
    return fmt.truncate("\n".join(lines), 3500), markup(nav, [BACK])


# --------------------------------------------------------------- муты -----

async def mutes(owner_id: int) -> tuple[str, dict]:
    now = int(time.time())
    rows = [row for row in await db.all_mutes(owner_id)
            if not row["until"] or row["until"] > now]
    if not rows:
        return ("🔇 **Никто не замучен.**\n\nЗамутить: команда `.mute` в нужной "
                "переписке. Его сообщения будут удаляться сразу, но приходить "
                "вам в журнал.", markup([BACK]))

    lines = ["🔇 **Замученные**", ""]
    buttons = []
    for row in rows[:PAGE]:
        who = await _person(owner_id, row["user_id"])
        scope = "везде" if row["chat_id"] == 0 else "в одном чате"
        left = ("бессрочно" if not row["until"]
                else f"ещё {fmt.human_delta(row['until'] - now)}")
        lines.append(f"• **{who}** — {scope}, {left}")
        buttons.append([button(f"🔊 Снять: {fmt.truncate(who, 24)}",
                               f"mu:{row['chat_id']}:{row['user_id']}")])
    return "\n".join(lines), markup(*buttons, [BACK])


# ------------------------------------------------------- белый список -----

async def allow(owner_id: int) -> tuple[str, dict]:
    rows = await db.allowed_users(owner_id)
    if not rows:
        return ("✅ **Белый список пуст.**\n\nЭто исключения для режима "
                "«Не беспокоить»: их сообщения будут доходить как обычно.\n"
                "Добавить: `.allow` реплаем в нужной переписке.", markup([BACK]))

    lines = ["✅ **Белый список**", "",
             "Их сообщения доходят даже в режиме «Не беспокоить».", ""]
    buttons = []
    for row in rows[:PAGE]:
        name = row["name"] or await _person(owner_id, row["user_id"])
        lines.append(f"• **{name}**")
        buttons.append([button(f"🚫 Убрать: {fmt.truncate(name, 24)}",
                               f"al:{row['user_id']}")])
    return "\n".join(lines), markup(*buttons, [BACK])


# ------------------------------------------------------------- чаты ------

async def chats(owner_id: int) -> tuple[str, dict]:
    rows = await db.tuned_chats(owner_id)
    if not rows:
        return ("⚙️ **Особых настроек нет.**\n\nВо всех личных чатах я сохраняю "
                "удалённые сообщения. Чтобы для какого-то чата этого не делать, "
                "отправьте там `.ignore` — он появится здесь, и его можно будет "
                "вернуть кнопкой.", markup([BACK]))

    lines = ["⚙️ **Настройки чатов**", "",
             "Здесь только чаты, где вы что-то меняли — остальные работают "
             "по умолчанию.", ""]
    buttons = []
    for row in rows[:PAGE]:
        title = row["title"] or await _title(owner_id, row["chat_id"])
        if row["ignored"]:
            lines.append(f"🙈 **{title}** — игнорирую полностью")
        elif row["antidelete"] == 0:
            lines.append(f"🔕 **{title}** — не сохраняю удалённые")
        else:
            lines.append(f"⚙️ **{title}** — настройки изменены")
        buttons.append([button(f"↩️ Вернуть как было: {fmt.truncate(title, 20)}",
                               f"ch:{row['chat_id']}")])
    return "\n".join(lines), markup(*buttons, [BACK])


# ------------------------------------------------------------ справка -----

HELP_TEXT = (
    "❓ **Три разные вещи, которые легко перепутать**\n\n"
    "🔇 **Мут** — про одного человека.\n"
    "`{p}mute` в переписке с ним: его новые сообщения удаляются сразу после "
    "отправки, но приходят вам в журнал. Снять — `{p}unmute` или кнопкой "
    "в разделе «Муты».\n\n"
    "🌙 **Не беспокоить** — про всех сразу.\n"
    "Сообщения от кого угодно удаляются, отправитель получает вежливый ответ "
    "от вашего имени. Включается и выключается одной кнопкой на своём экране. "
    "Исключения — белый список.\n\n"
    "🙈 **Игнор чата** — про один чат.\n"
    "`{p}ignore` в переписке: я перестаю там что-либо сохранять и вообще "
    "вмешиваться. Ничего не удаляется, просто вас не беспокою отчётами. "
    "Вернуть — кнопкой в разделе «Чаты» или `{p}ignore off` там же.\n\n"
    "_Разница коротко: мут удаляет чужие сообщения, «не беспокоить» делает то "
    "же со всеми, а игнор ничего не удаляет — он про то, что я молчу._"
)


async def help_screen(_owner_id: int) -> tuple[str, dict]:
    return HELP_TEXT.format(p=config.PREFIX), markup(
        [button("🌙 Не беспокоить", "m:dnd"), button("🙈 Чаты", "m:chats")], [BACK])


SCREENS = {
    "home": home,
    "dnd": dnd,
    "mutes": mutes,
    "allow": allow,
    "chats": chats,
    "help": help_screen,
}


async def render(owner_id: int, name: str, arg: str = "") -> tuple[str, dict]:
    if name == "log":
        return await log(owner_id, int(arg) if arg.isdigit() else 0)
    builder = SCREENS.get(name, home)
    return await builder(owner_id)


# ------------------------------------------- кнопки под карточкой отчёта ---

def report_actions(chat_id: int, user_id: int | None) -> dict:
    """Быстрые действия прямо под отчётом об удалённом сообщении."""
    row = []
    if user_id:
        row.append(button("🔇 Замутить", f"q:{chat_id}:{user_id}"))
    row.append(button("🙈 Игнорировать чат", f"qi:{chat_id}"))
    return markup(row)
