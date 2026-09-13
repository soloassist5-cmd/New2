"""Команды в личке с ботом.

Бот многопользовательский: каждый одобренный пользователь — владелец своих
чатов и видит только свои данные. Администратор (OWNER_ID) решает, кого пускать,
и распоряжается базой целиком.
"""
from __future__ import annotations

import logging
import re
import time

import config
import db
from bot import access, cleanup, digest, dossier, dotcmd, parse, transcript, urgent
from core import backup, chatprefs, fmt, state

log = logging.getLogger("botcmd")

# Включение Business Mode — разовое действие того, кто создавал бота.
# Обычному пользователю про @BotFather знать незачем.
ADMIN_PREREQUISITE = (
    "**Шаг 0 (только для вас, один раз).** В @BotFather: `/mybots` → {bot} → "
    "**Bot Settings** → **Business Mode** → **Turn on**. Пока он выключен, "
    "Telegram отвечает «этот бот пока не поддерживает режим секретаря».\n\n"
)

CONNECT_STEPS = (
    "🔌 **Как подключить бота к личным чатам**\n\n"
    "{prerequisite}"
    "Понадобится **Telegram Premium** — без подписки раздела Business в "
    "настройках нет.\n\n"
    "1. Настройки → **Telegram для бизнеса**\n"
    "2. **Чат-боты**\n"
    "3. Вставьте {bot}\n"
    "4. Выберите, к каким чатам подключить — проще всего «Все чаты»\n"
    "5. Выдайте разрешения:\n"
    "   • читать сообщения\n"
    "   • отвечать на сообщения\n"
    "   • удалять сообщения — **и свои, и собеседника**\n\n"
    "Названия пунктов немного отличаются между версиями приложения — "
    "ориентируйтесь по смыслу.\n\n"
    "Как только подключите, я пришлю сюда подтверждение и список выданных прав."
)

WELCOME = (
    "🛡 **Guard**\n\n"
    "Я сохраняю удалённые и изменённые сообщения из ваших личных чатов и "
    "присылаю их сюда — вместе с фото, голосовыми и файлами.\n\n"
)

NOT_CONNECTED = (
    "🔌 **Пока не вижу подключения к вашим личным чатам.**\n\n"
    "**Если ещё не подключали** — /connect, там пошаговая инструкция.\n\n"
    "**Если подключали, а я забыл** — так бывает после обновления бота. "
    "Ничего переподключать не нужно: напишите что-нибудь в любом личном чате, "
    "и я перечитаю связку у Telegram сам."
)

ALREADY_LINKED_HINT = (
    "\n\n_Уже подключали, а я не вижу? Напишите что-нибудь в любом личном чате — "
    "связку я подхвачу сам._"
)

OWNER_ID_HINT = (
    "\n\n⚙️ _Администратору: добавьте в переменные окружения_ "
    "`OWNER_ID={user_id}` _— тогда бот будет поднимать базу из закреплённого "
    "здесь бэкапа после обновлений._"
)

HELP = (
    "**Здесь:**\n"
    "/gmute — «не беспокоить»: удалять сообщения всех подряд\n"
    "/ungmute — выключить\n"
    "/allow, /deny, /allowed — белый список для «не беспокоить»\n"
    "/intercepted [N] — что перехвачено мутом и «не беспокоить»\n"
    "/clear — что накопилось и что из этого почистить\n"
    "  _по областям:_ /clearurgent, /clearlog, /cleardeleted, /clearedits, "
    "/clearnames, /clearcache\n"
    "/deleted [N] — последние удалённые\n"
    "/dox <id> — досье: всё, что я записал про человека\n"
    "/chats — чаты, где вы меняли настройки\n"
    "/urgent — срочные вызовы от собеседников\n"
    "/find <текст> — поиск по всему сохранённому\n"
    "/mutes, /unmute <id> — муты\n"
    "/export — ваш журнал файлом\n"
    "/status — состояние и права\n"
    "/connect — инструкция по подключению"
)

IN_CHAT_HINT = (
    "\n\n**В самих переписках** (пишете вы, от своего имени):\n"
    "`{p}mute` · `{p}unmute` · `{p}gmute` · `{p}del` · `{p}purge` · `{p}deleted` · "
    "`{p}id` · `{p}dox` · `{p}help`"
)

ADMIN_HELP = (
    "\n\n**Только для администратора:**\n"
    "/users — кто пользуется ботом и заявки\n"
    "/revoke <id> — закрыть доступ\n"
    "/backup — база целиком файлом"
)

MENU = (
    ("gmute", "не беспокоить: удалять сообщения всех"),
    ("ungmute", "выключить «не беспокоить»"),
    ("intercepted", "что перехвачено"),
    ("clear", "что накопилось и что почистить"),
    ("deleted", "последние удалённые сообщения"),
    ("dox", "досье на человека: /dox <id>"),
    ("chats", "чаты с изменёнными настройками"),
    ("urgent", "срочные вызовы от собеседников"),
    ("find", "поиск по сохранённому: /find текст"),
    ("allowed", "белый список"),
    ("mutes", "активные муты"),
    ("unmute", "снять мут: /unmute <id>"),
    ("export", "ваш журнал файлом"),
    ("status", "состояние и права"),
    ("connect", "как подключить к личным чатам"),
    ("help", "справка"),
)


def bot_mention() -> str:
    username = (state.bot_user or {}).get("username")
    return f"@{username}" if username else "юзернейм бота"


def connect_steps(user_id: int) -> str:
    prerequisite = ADMIN_PREREQUISITE.format(bot=bot_mention()) \
        if state.is_admin(user_id) else ""
    return CONNECT_STEPS.format(bot=bot_mention(), prerequisite=prerequisite)


def connected(user_id: int) -> bool:
    return state.business_of(user_id) is not None


def owner_id_hint(user_id: int) -> str:
    """Техническая подсказка: показывается, только пока администратор не назначен."""
    return "" if config.OWNER_ID else OWNER_ID_HINT.format(user_id=user_id)


# ----------------------------------------------------------------- команды --

async def cmd_start(api, message: dict, _args: str) -> None:
    chat_id = message["chat"]["id"]
    user = message.get("from") or {}
    user_id = user.get("id")
    await state.remember_user(user_id, name=parse.display_name(user),
                              chat_id=chat_id)

    if connected(user_id):
        await api.send_message(chat_id, WELCOME + help_text(user_id)
                               + owner_id_hint(user_id))
        return
    await api.send_message(chat_id, WELCOME + connect_steps(user_id)
                           + ALREADY_LINKED_HINT + owner_id_hint(user_id))


async def cmd_connect(api, message: dict, _args: str) -> None:
    user_id = (message.get("from") or {}).get("id")
    await api.send_message(message["chat"]["id"], connect_steps(user_id))


def help_text(user_id: int, *, in_chat_hint: bool = True) -> str:
    text = HELP.format(p=config.PREFIX)
    if in_chat_hint:
        text += IN_CHAT_HINT.format(p=config.PREFIX)
    return text + ADMIN_HELP if state.is_admin(user_id) else text


async def cmd_help(api, message: dict, _args: str) -> None:
    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id")
    if not connected(user_id):
        await api.send_message(chat_id, NOT_CONNECTED)
        return
    await api.send_message(chat_id, help_text(user_id, in_chat_hint=False)
                           + "\n\n" + dotcmd.help_text())


async def cmd_status(api, message: dict, _args: str) -> None:
    from bot.business import rights_report

    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id")
    stats = await db.stats(user_id)
    activity = await db.activity(user_id)
    lines = ["📊 **Состояние**", ""]

    connection_id = state.business_of(user_id)
    if connection_id:
        lines.append("🔗 Личные чаты: **подключены**")
        lines.append(rights_report(state.rights_of(connection_id)))
    else:
        lines.append("🔌 Личные чаты: **не подключены** — /connect")
        lines.append("_Если подключение есть, напишите что-нибудь в любой "
                     "переписке: я его подхвачу._")
    lines.append("")

    if state.dnd_active(user_id):
        spent = fmt.uptime(time.time() - state.dnd_since(user_id))
        lines.append(f"🌙 Не беспокоить: **включён** (уже {spent}) — /ungmute")
        lines.append("_Пока включён, входящие удаляются сразу, поэтому отчётов "
                     "об удалении не будет._")
        if config.URGENT_ENABLED and config.URGENT_WORDS:
            calls = len(await db.urgent_calls(user_id, 50))
            lines.append(f"🚨 Срочный вызов собеседникам доступен "
                         f"(`/{config.URGENT_WORDS[0]}`, раз в сутки)"
                         + (f" · вызовов: **{calls}** — /urgent" if calls else ""))
    else:
        lines.append("🌙 Не беспокоить: выключен")

    ignored = [row for row in await db.tuned_chats(user_id) if row["ignored"]]
    if ignored:
        lines.append(f"🙈 Чатов в игноре: **{len(ignored)}** — /chats")
    if stats["mutes"]:
        lines.append(f"🔇 Замучено: **{stats['mutes']}** — /mutes")

    lines.append("")
    lines.append(f"🗑 Сохранено удалённых: **{stats['deleted']}**")
    if activity["last_report"]:
        lines.append(f"   последнее — {fmt.ts(activity['last_report'])}")
    if stats["intercepted"]:
        lines.append(f"🔒 Перехвачено: **{stats['intercepted']}** — /intercepted")

    if state.is_admin(user_id):
        lines += ["", "— — —",
                  f"👥 пользователей: {stats['users']}",
                  f"💾 база: {fmt.size(stats['size'])}",
                  f"⏱ бот на связи: {fmt.uptime(time.time() - state.start_time)}"]
        if state.client is not None:
            lines.append("👤 юзербот-режим активен: работают и групповые чаты")
    await api.send_message(chat_id, "\n".join(lines))


async def cmd_urgent(api, message: dict, args: str) -> None:
    owner_id = (message.get("from") or {}).get("id")
    limit = int(args) if args.strip().isdigit() else 10
    await api.send_message(message["chat"]["id"],
                           await urgent.recent_text(owner_id, max(1, min(limit, 50))))


async def cmd_chats(api, message: dict, _args: str) -> None:
    user_id = (message.get("from") or {}).get("id")
    await api.send_message(message["chat"]["id"], await dotcmd.chats_text(user_id))


async def cmd_deleted(api, message: dict, args: str) -> None:
    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id")
    limit = int(args) if args.strip().isdigit() else 10
    rows = await db.last_deleted(user_id, None, max(1, min(limit, 30)))
    if not rows:
        await api.send_message(chat_id, "🗑 Пока ничего не удаляли.")
        return
    out = [f"🗑 **Последние удалённые** ({len(rows)})", ""]
    for row in rows:
        preview = fmt.truncate(row["text"] or "", 160) or (
            f"_{row['media_type']}_" if row["media_type"] else "_пусто_")
        out.append(f"• `{fmt.ts(row['deleted_at'])}` **{row['user_name'] or '—'}**\n"
                   f"  {preview}")
    await api.send_message(chat_id, fmt.truncate("\n".join(out), 3500))


async def cmd_mutes(api, message: dict, _args: str) -> None:
    user_id = (message.get("from") or {}).get("id")
    await api.send_message(message["chat"]["id"],
                           await dotcmd.mute_list_text(user_id))


async def cmd_unmute(api, message: dict, args: str) -> None:
    chat_id = message["chat"]["id"]
    owner_id = (message.get("from") or {}).get("id")
    raw = args.strip()
    if not re.fullmatch(r"-?\d+", raw):
        await api.send_message(chat_id, "Использование: `/unmute 123456789`")
        return
    target = int(raw)
    removed = [key for key in list(state.mutes)
               if key[0] == owner_id and key[2] == target]
    for _, scope, _ in removed:
        await state.unmute_user(owner_id, scope, target)
    await api.send_message(chat_id, f"🔊 Мутов снято: **{len(removed)}**." if removed
                           else "Этот пользователь не был замучен.")


async def cmd_gmute(api, message: dict, _args: str) -> None:
    from bot import editable

    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id")
    if state.dnd_active(user_id):
        await api.send_message(chat_id, dotcmd.dnd_enabled_text())
        return
    await state.set_dnd(user_id)
    await editable.animated(api, chat_id, dotcmd.dnd_enabled_text(), icon="🌙")


async def cmd_ungmute(api, message: dict, _args: str) -> None:
    from bot import editable

    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id")
    if not state.dnd_active(user_id):
        await api.send_message(chat_id, "Режим «не беспокоить» и так выключен.")
        return
    since = state.dnd_since(user_id)
    await state.clear_dnd(user_id)
    await editable.animated(api, chat_id, dotcmd.dnd_disabled_text(since), icon="☀️")
    await dotcmd.dnd_digest(user_id, since)


async def cmd_allow(api, message: dict, args: str) -> None:
    chat_id = message["chat"]["id"]
    owner_id = (message.get("from") or {}).get("id")
    raw = args.strip()
    if not re.fullmatch(r"-?\d+", raw):
        await api.send_message(chat_id, "Использование: `/allow 123456789`\n"
                                        "id можно взять из `.id` в нужном чате.")
        return
    await state.allow_user(owner_id, int(raw), raw)
    await api.send_message(chat_id, f"✅ `{raw}` проходит сквозь «не беспокоить».")


async def cmd_deny(api, message: dict, args: str) -> None:
    chat_id = message["chat"]["id"]
    owner_id = (message.get("from") or {}).get("id")
    raw = args.strip()
    if not re.fullmatch(r"-?\d+", raw):
        await api.send_message(chat_id, "Использование: `/deny 123456789`")
        return
    removed = await state.deny_user(owner_id, int(raw))
    await api.send_message(chat_id, f"🚫 `{raw}` убран из белого списка." if removed
                           else "Его и не было в белом списке.")


async def cmd_allowed(api, message: dict, _args: str) -> None:
    owner_id = (message.get("from") or {}).get("id")
    await api.send_message(message["chat"]["id"],
                           await dotcmd.allowed_text(owner_id))


async def cmd_intercepted(api, message: dict, args: str) -> None:
    owner_id = (message.get("from") or {}).get("id")
    limit = int(args) if args.strip().isdigit() else 20
    rows = await db.intercepted(owner_id, limit=max(1, min(limit, 200)))
    await digest.deliver(owner_id, rows, title="🔇 **Перехваченные сообщения**",
                         empty="🔇 Пока ничего не перехвачено.")


async def cmd_clear(api, message: dict, args: str) -> None:
    """Обзор накопленного с кнопкой на каждую область."""
    owner_id = (message.get("from") or {}).get("id")
    text, keyboard = await cleanup.overview(owner_id)
    await api.send_message(message["chat"]["id"], text, reply_markup=keyboard)


def _cleaner(kind: str):
    """Команда на одну область: `/clearurgent`, `/clearlog` и остальные."""

    async def handler(api, message: dict, args: str) -> None:
        owner_id = (message.get("from") or {}).get("id")
        scope, value = cleanup.parse_args(args)
        text, keyboard = await cleanup.offer(owner_id, cleanup.CODES[kind],
                                             scope, value)
        await api.send_message(message["chat"]["id"], text, reply_markup=keyboard)

    return handler


async def cmd_dox(api, message: dict, args: str) -> None:
    """Досье по id: в переписке удобнее `.dox` реплаем, здесь — по номеру."""
    chat_id = message["chat"]["id"]
    owner_id = (message.get("from") or {}).get("id")
    raw = args.strip()
    if not re.fullmatch(r"-?\d+", raw):
        await api.send_message(chat_id, "Использование: `/dox <id>`\n\n"
                               "_id можно взять из `.id` в переписке или из "
                               "`/intercepted`._")
        return
    await api.send_message(chat_id, await dossier.card(api, owner_id, int(raw)))


async def cmd_find(api, message: dict, args: str) -> None:
    chat_id = message["chat"]["id"]
    owner_id = (message.get("from") or {}).get("id")
    query = args.strip()
    if len(query) < 2:
        await api.send_message(chat_id, "Использование: `/find слово`")
        return
    rows = await db.search_deleted(owner_id, query, limit=20)
    if not rows:
        await api.send_message(chat_id, f"🔍 По запросу «{query}» ничего не нашлось.")
        return
    kinds = {"deleted": "🗑", "intercepted": "🔇"}
    out = [f"🔍 **Найдено: {len(rows)}**", ""]
    for row in rows:
        out.append(f"{kinds.get(row['kind'], '•')} `{fmt.ts(row['at'])}` "
                   f"**{row['user_name'] or '—'}**\n"
                   f"  {fmt.truncate(row['text'] or '', 200)}")
    await api.send_message(chat_id, fmt.truncate("\n".join(out), 3500))


async def cmd_export(api, message: dict, _args: str) -> None:
    chat_id = message["chat"]["id"]
    owner_id = (message.get("from") or {}).get("id")
    rows = await db.last_deleted(owner_id, None, 5000)
    if not rows:
        await api.send_message(chat_id, "🗑 Журнал пуст.")
        return
    ordered = sorted(rows, key=lambda row: (row["date"] or 0, row["msg_id"] or 0))
    when = db.now()
    payload, stats = transcript.build(
        ordered, chat_title="все чаты", owner_id=owner_id,
        requested=len(ordered), when=when, reason="Журнал удалённых сообщений")
    await api.send_file(chat_id, payload, f"journal_{when}.txt",
                        caption=f"🗑 **Журнал удалённых**\n"
                                f"записей: **{stats['recovered']}**")


# ------------------------------------------------------- администратор -----

async def cmd_users(api, message: dict, _args: str) -> None:
    chat_id = message["chat"]["id"]
    rows = await db.all_users()
    pending = [row for row in rows if row["status"] == db.PENDING]
    approved = [row for row in rows if row["status"] == db.APPROVED]
    denied = [row for row in rows if row["status"] == db.DENIED]

    lines = ["👥 **Пользователи бота**", ""]
    lines.append(f"✅ доступ открыт: **{len(approved)}**")
    for row in approved:
        mark = " (это вы)" if state.is_admin(row["user_id"]) else ""
        connection = "🔗" if state.business_of(row["user_id"]) else "🔌"
        lines.append(f"  {connection} {row['name'] or '—'} (`{row['user_id']}`){mark}")
    if denied:
        lines.append(f"\n🚫 отклонены: {len(denied)}")
    if pending:
        lines.append(f"\n⏳ **ждут решения: {len(pending)}**")
    lines.append("\n_Закрыть доступ:_ `/revoke <id>`")
    await api.send_message(chat_id, fmt.truncate("\n".join(lines), 3500))

    for row in pending:                       # заявки — с кнопками, по одной
        await api.send_message(
            chat_id,
            access.request_card({"id": row["user_id"], "first_name": row["name"]},
                                "ожидает решения"),
            reply_markup=access.keyboard(row["user_id"]))


async def cmd_revoke(api, message: dict, args: str) -> None:
    chat_id = message["chat"]["id"]
    raw = args.strip()
    if not re.fullmatch(r"-?\d+", raw):
        await api.send_message(chat_id, "Использование: `/revoke 123456789`")
        return
    if await access.revoke(int(raw)):
        await api.send_message(chat_id, f"🚫 Доступ для `{raw}` закрыт.")
    else:
        await api.send_message(chat_id, "У этого пользователя и не было доступа.")


async def cmd_backup(api, message: dict, _args: str) -> None:
    chat_id = message["chat"]["id"]
    path = await backup.make_backup()
    if path is None:
        await api.send_message(chat_id, "⚠️ Не удалось собрать бэкап.")
        return
    try:
        await api.send_file(chat_id, path.read_bytes(), "guard.sqlite3",
                            caption=f"🗄 База · {fmt.ts(db.now())}")
    finally:
        path.unlink(missing_ok=True)


# Доступны без одобрения: ничего чужого они не раскрывают.
PUBLIC = {"start", "connect", "help"}
# Только администратору: касаются всех пользователей или базы целиком.
ADMIN_ONLY = {"users", "revoke", "backup"}

HANDLERS = {
    "start": cmd_start,
    "connect": cmd_connect,
    "help": cmd_help,
    "status": cmd_status,
    "stats": cmd_status,
    "chats": cmd_chats,
    "urgent": cmd_urgent,
    "calls": cmd_urgent,
    "gmute": cmd_gmute,
    "dnd": cmd_gmute,
    "ungmute": cmd_ungmute,
    "undnd": cmd_ungmute,
    "allow": cmd_allow,
    "deny": cmd_deny,
    "allowed": cmd_allowed,
    "intercepted": cmd_intercepted,
    "muted": cmd_intercepted,
    "clear": cmd_clear,
    "cleanup": cmd_clear,
    "clearlog": _cleaner("intercepted"),
    "clearurgent": _cleaner("urgent"),
    "cleardeleted": _cleaner("deleted"),
    "clearedits": _cleaner("edits"),
    "clearnames": _cleaner("names"),
    "clearcache": _cleaner("cache"),
    "dox": cmd_dox,
    "whois": cmd_dox,
    "deleted": cmd_deleted,
    "find": cmd_find,
    "export": cmd_export,
    "mutes": cmd_mutes,
    "unmute": cmd_unmute,
    "users": cmd_users,
    "revoke": cmd_revoke,
    "backup": cmd_backup,
}

COMMAND_RE = re.compile(r"^/(\w+)(?:@[\w_]+)?(?:\s+([\s\S]*))?$")
BACKUP_LIMIT_MB = 20          # предел getFile в Bot API


async def restore_from_document(api, message: dict) -> None:
    """Администратор прислал файл базы — поднимаем её вместо текущей."""
    document = message["document"]
    chat_id = message["chat"]["id"]
    name = document.get("file_name") or ""
    if not name.endswith((".sqlite3", ".db")):
        return
    if (document.get("file_size") or 0) > BACKUP_LIMIT_MB * 1024 * 1024:
        await api.send_message(chat_id, f"⚠️ Файл больше {BACKUP_LIMIT_MB} МБ — "
                                        "Bot API такие не отдаёт.")
        return

    await api.send_message(chat_id, "🗄 Восстанавливаю базу…")
    incoming = config.DB_PATH.with_suffix(".incoming")
    previous = config.DB_PATH.with_suffix(".prev")
    try:
        info = await api.get_file(document["file_id"])
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_bytes(await api.download(info["file_path"]))
    except Exception:                                        # noqa: BLE001
        log.exception("скачивание бэкапа не удалось")
        incoming.unlink(missing_ok=True)
        await api.send_message(chat_id, "⚠️ Не вышло скачать файл.")
        return

    # Подменяем базу только после проверки: иначе чужой файл уничтожит рабочую.
    if not await db.is_valid_database(incoming):
        incoming.unlink(missing_ok=True)
        await api.send_message(
            chat_id, "⚠️ Это не похоже на базу Guard — ничего не менял.")
        return

    try:
        await db.close()
        if config.DB_PATH.exists():
            config.DB_PATH.replace(previous)
        incoming.replace(config.DB_PATH)
        await db.init()
        await reload_state()
    except Exception:                                        # noqa: BLE001
        log.exception("восстановление не удалось")
        if previous.exists():
            previous.replace(config.DB_PATH)
        await db.init()
        await reload_state()
        await api.send_message(chat_id, "⚠️ Не вышло восстановить. "
                                        "Прежняя база на месте.")
        return
    finally:
        incoming.unlink(missing_ok=True)
        previous.unlink(missing_ok=True)

    stats = await db.stats()
    await api.send_message(
        chat_id, f"✅ **База восстановлена.**\n👥 пользователей: {stats['users']} · "
                 f"🗑 сохранено: {stats['deleted']} · 🔇 мутов: {stats['mutes']}")


async def reload_state() -> None:
    await state.load_users()
    await state.load_mutes()
    await state.load_allowlist()
    chatprefs.invalidate_all()
    # База другая — значит и «как его звали в прошлый раз» другое.
    db.forget_aliases()


async def handle(api, message: dict) -> None:
    if (message.get("chat") or {}).get("type") != "private":
        return

    user = message.get("from") or {}
    user_id = user.get("id")
    if message.get("document") and state.is_admin(user_id):
        await restore_from_document(api, message)
        return

    match = COMMAND_RE.match(parse.text_of(message))
    if match is None:
        return
    name = match.group(1).lower()
    handler = HANDLERS.get(name)
    if handler is None:
        return

    if name in ADMIN_ONLY and not state.is_admin(user_id):
        return
    if name not in PUBLIC and not state.is_approved(user_id):
        reply = await access.request(api, user, source=f"команда /{name}",
                                     chat_id=message["chat"]["id"])
        await api.send_message(message["chat"]["id"], reply)
        return
    if name == "start" and not state.is_approved(user_id):
        reply = await access.request(api, user, source="открыл чат с ботом",
                                     chat_id=message["chat"]["id"])
        await api.send_message(message["chat"]["id"], reply)
        return

    try:
        await handler(api, message, match.group(2) or "")
    except Exception:                                        # noqa: BLE001
        log.exception("ошибка команды /%s", name)
        try:
            await api.send_message(message["chat"]["id"],
                                   f"⚠️ Не получилось выполнить /{name}. "
                                   "Попробуйте ещё раз.")
        except Exception:
            pass


ACCESS_RE = re.compile(r"^(ok|no):(-?\d+)$")


async def _access_callback(api, query: dict, match: re.Match) -> None:
    admin = (query.get("from") or {}).get("id")
    card, toast = await access.decide(api, admin, match.group(1),
                                      int(match.group(2)))
    await api.answer_callback(query["id"], toast, show_alert=not card)
    if not card:
        return
    origin = query.get("message") or {}
    try:
        await api.edit_message_text(origin["chat"]["id"], origin["message_id"], card)
    except Exception as e:                                   # noqa: BLE001
        log.debug("карточку заявки не удалось обновить: %r", e)


async def handle_callback(api, query: dict) -> None:
    """Кнопки — только у заявок на доступ и у подтверждения чистки."""
    if await cleanup.handle_callback(api, query):
        return
    match = ACCESS_RE.match(query.get("data") or "")
    if match is None:
        await api.answer_callback(query["id"])
        return
    await _access_callback(api, query, match)


async def publish_menu(api) -> None:
    try:
        await api.set_my_commands([{"command": name, "description": desc}
                                   for name, desc in MENU])
    except Exception as e:                                   # noqa: BLE001
        log.debug("меню команд не обновилось: %r", e)

    # Синяя кнопка слева от поля ввода: либо открывает приложение, либо, если
    # открывать нечего, возвращается к обычному списку команд — иначе на ней
    # осталась бы ссылка от прошлой версии.
    url = config.webapp_url()
    button = ({"type": "web_app", "text": "Guard", "web_app": {"url": url}}
              if url else {"type": "commands"})
    try:
        await api.set_chat_menu_button(button)
        log.info("кнопка меню: %s", url or "список команд")
    except Exception as e:                                   # noqa: BLE001
        log.warning("кнопку меню не удалось выставить: %r", e)
