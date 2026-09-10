"""Команды в личке с ботом. До подключения Business бот выдаёт инструкцию."""
from __future__ import annotations

import logging
import re
import time

import config
import db
from bot import dotcmd, parse
from core import backup, fmt, state

log = logging.getLogger("botcmd")

CONNECT_STEPS = (
    "🔌 **Как подключить бота к личным чатам**\n\n"
    "Понадобится **Telegram Premium** — без подписки раздела Business в настройках нет.\n\n"
    "**Шаг 1. Разрешить режим Business — в @BotFather**\n"
    "`/mybots` → выберите {bot} → **Bot Settings** → **Business Mode** → **Turn on**\n"
    "Тумблер по умолчанию выключен. Пока он выключен, Telegram отвечает "
    "«этот бот пока не поддерживает режим секретаря».\n\n"
    "**Шаг 2. Подключить бота к чатам**\n"
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
    "Как только подключите, я пришлю сюда подтверждение и список выданных прав. "
    "После этого заработают отчёты об удалённых сообщениях и команды `{p}` "
    "прямо в переписке."
)

WELCOME = (
    "🛡 **Guard**\n\n"
    "Я сохраняю удалённые и изменённые сообщения из ваших личных чатов и "
    "присылаю их сюда — вместе с фото, голосовыми и файлами.\n\n"
)

NOT_CONNECTED = (
    "Сначала подключите меня к личным чатам — команда /connect.\n"
    "До этого мне не видно ни одного чата."
)

HELP = (
    "**Здесь:**\n"
    "/status — состояние и права\n"
    "/deleted [N] — последние удалённые\n"
    "/mutes — активные муты\n"
    "/unmute <id> — снять мут\n"
    "/backup — прислать базу файлом\n"
    "/connect — инструкция по подключению\n\n"
    "**В самих переписках** (пишете вы, от своего имени):\n"
    "`{p}mute` · `{p}unmute` · `{p}del` · `{p}purge` · `{p}deleted` · `{p}help`"
)

MENU = (
    ("status", "состояние и права"),
    ("deleted", "последние удалённые сообщения"),
    ("mutes", "активные муты"),
    ("unmute", "снять мут: /unmute <id>"),
    ("backup", "прислать базу файлом"),
    ("connect", "как подключить к личным чатам"),
    ("help", "справка"),
)


def bot_mention() -> str:
    username = (state.bot_user or {}).get("username")
    return f"@{username}" if username else "юзернейм бота"


def owner_known() -> bool:
    return bool(state.owner_id)


def is_owner(user_id: int) -> bool:
    return bool(state.owner_id) and user_id == state.owner_id


def connected() -> bool:
    return any(info.get("is_enabled") for info in state.business.values())


# ----------------------------------------------------------------- команды --

async def cmd_start(api, message: dict, _args: str) -> None:
    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id")
    if is_owner(user_id):
        state.owner_chat_id = chat_id

    if connected() and is_owner(user_id):
        await api.send_message(chat_id, WELCOME + HELP.format(p=config.PREFIX))
        return
    await api.send_message(
        chat_id,
        WELCOME + CONNECT_STEPS.format(bot=bot_mention(), p=config.PREFIX))


async def cmd_connect(api, message: dict, _args: str) -> None:
    await api.send_message(
        message["chat"]["id"],
        CONNECT_STEPS.format(bot=bot_mention(), p=config.PREFIX))


async def cmd_help(api, message: dict, _args: str) -> None:
    chat_id = message["chat"]["id"]
    if not connected():
        await api.send_message(chat_id, NOT_CONNECTED)
        return
    await api.send_message(chat_id, HELP.format(p=config.PREFIX) + "\n\n"
                           + dotcmd.help_text())


async def cmd_status(api, message: dict, _args: str) -> None:
    from bot.business import rights_report

    chat_id = message["chat"]["id"]
    stats = await db.stats()
    lines = [
        "📊 **Состояние**",
        f"⏱ аптайм: {fmt.uptime(time.time() - state.start_time)}",
        f"🗂 в кэше: {stats['cached']}",
        f"🗑 удалённых сохранено: {stats['deleted']}",
        f"✏️ правок: {stats['edits']}",
        f"🔇 мутов: {stats['mutes']}",
        f"💾 база: {fmt.size(stats['size'])}",
        "",
    ]
    if connected():
        lines.append("🔗 **Подключено к личным чатам**")
        for info in state.business.values():
            if info.get("is_enabled"):
                lines.append(rights_report(info.get("rights") or {}))
                break
    else:
        lines.append("🔌 **Не подключено.** Инструкция — /connect")
    if state.client is not None:
        lines.append("\n👤 Юзербот-режим активен: работают и групповые чаты.")
    await api.send_message(chat_id, "\n".join(lines))


async def cmd_deleted(api, message: dict, args: str) -> None:
    chat_id = message["chat"]["id"]
    limit = int(args) if args.strip().isdigit() else 10
    rows = await db.last_deleted(None, max(1, min(limit, 30)))
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
    chat_id = message["chat"]["id"]
    rows = await db.all_mutes()
    now = int(time.time())
    lines = ["🔇 **Активные муты**", ""]
    for row in rows:
        if row["until"] and row["until"] <= now:
            continue
        scope = "везде" if row["chat_id"] == 0 else f"чат `{row['chat_id']}`"
        left = ("бессрочно" if not row["until"]
                else f"ещё {fmt.human_delta(row['until'] - now)}")
        lines.append(f"• `{row['user_id']}` — {scope}, {left}")
    if len(lines) == 2:
        await api.send_message(chat_id, "🔇 Список мутов пуст.")
        return
    lines.append("\n_Снять:_ `/unmute <id>`")
    await api.send_message(chat_id, fmt.truncate("\n".join(lines), 3500))


async def cmd_unmute(api, message: dict, args: str) -> None:
    chat_id = message["chat"]["id"]
    raw = args.strip()
    if not re.fullmatch(r"-?\d+", raw):
        await api.send_message(chat_id, "Использование: `/unmute 123456789`")
        return
    user_id = int(raw)
    removed = [key for key in list(state.mutes) if key[1] == user_id]
    for scope, _ in removed:
        await state.unmute_user(scope, user_id)
    await api.send_message(chat_id, f"🔊 Мутов снято: **{len(removed)}**." if removed
                           else "Этот пользователь не был замучен.")


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


# Команды, доступные до подключения: они ничего не раскрывают.
PUBLIC = {"start", "connect", "help"}

HANDLERS = {
    "start": cmd_start,
    "connect": cmd_connect,
    "help": cmd_help,
    "status": cmd_status,
    "stats": cmd_status,
    "deleted": cmd_deleted,
    "mutes": cmd_mutes,
    "unmute": cmd_unmute,
    "backup": cmd_backup,
}

COMMAND_RE = re.compile(r"^/(\w+)(?:@[\w_]+)?(?:\s+([\s\S]*))?$")
BACKUP_LIMIT_MB = 20          # предел getFile в Bot API


async def restore_from_document(api, message: dict) -> None:
    """Владелец прислал файл базы — поднимаем её вместо текущей."""
    from modules.antidelete import invalidate_all

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
    except Exception as e:                                   # noqa: BLE001
        log.exception("скачивание бэкапа не удалось")
        incoming.unlink(missing_ok=True)
        await api.send_message(chat_id, f"⚠️ Не вышло скачать: `{type(e).__name__}`")
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
        await state.load_mutes()
        invalidate_all()
    except Exception as e:                                   # noqa: BLE001
        log.exception("восстановление не удалось")
        if previous.exists():
            previous.replace(config.DB_PATH)
        await db.init()
        await state.load_mutes()
        await api.send_message(chat_id, f"⚠️ Не вышло: `{type(e).__name__}`. "
                                        "Прежняя база на месте.")
        return
    finally:
        incoming.unlink(missing_ok=True)
        previous.unlink(missing_ok=True)

    stats = await db.stats()
    await api.send_message(
        chat_id, f"✅ **База восстановлена.**\n🗂 в кэше: {stats['cached']} · "
                 f"🗑 сохранено: {stats['deleted']} · 🔇 мутов: {stats['mutes']}")


async def handle(api, message: dict) -> None:
    if (message.get("chat") or {}).get("type") != "private":
        return

    user_id = (message.get("from") or {}).get("id")
    if message.get("document") and owner_known() and is_owner(user_id):
        await restore_from_document(api, message)
        return

    match = COMMAND_RE.match(parse.text_of(message))
    if match is None:
        return
    name = match.group(1).lower()
    handler = HANDLERS.get(name)
    if handler is None:
        return

    if name not in PUBLIC:
        if not owner_known():
            await api.send_message(message["chat"]["id"], NOT_CONNECTED)
            return
        if not is_owner(user_id):
            return                      # чужому отвечать нечего

    try:
        await handler(api, message, match.group(2) or "")
    except Exception as e:                                   # noqa: BLE001
        log.exception("ошибка команды /%s", name)
        try:
            await api.send_message(message["chat"]["id"],
                                   f"⚠️ `{type(e).__name__}: {e}`")
        except Exception:
            pass


async def publish_menu(api) -> None:
    try:
        await api.set_my_commands([{"command": name, "description": desc}
                                   for name, desc in MENU])
    except Exception as e:                                   # noqa: BLE001
        log.debug("меню команд не обновилось: %r", e)
