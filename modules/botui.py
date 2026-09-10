"""Интерфейс бота из @BotFather: отчёты приходят в личку с ним, там же команды.

Все обработчики висят на клиенте бота и отвечают только владельцу — чужие
сообщения игнорируются молча.
"""
from __future__ import annotations

import logging
import re
import time

from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

import config
import db
from core import backup, fmt, state

log = logging.getLogger("botui")

HELP = (
    "🛡 **Guard**\n\n"
    "Отчёты об удалённых и изменённых сообщениях приходят сюда автоматически.\n"
    "Управление ботом — командами `{p}` в самом Telegram, от вашего аккаунта.\n\n"
    "**Здесь доступно:**\n"
    "/status — состояние и статистика\n"
    "/deleted [N] — последние удалённые сообщения\n"
    "/mutes — список активных мутов\n"
    "/unmute <id> — снять мут с пользователя\n"
    "/backup — прислать базу файлом\n"
    "/help — эта справка"
)


def _pattern(name: str) -> re.Pattern:
    return re.compile(rf"^/{name}(?:@\w+)?(?:\s+([\s\S]*))?$", re.IGNORECASE)


def owner_only(handler):
    async def wrapper(event):
        if event.sender_id != state.owner_id:
            return
        try:
            await handler(event)
        except Exception as e:                               # noqa: BLE001
            log.exception("ошибка команды бота")
            await event.respond(f"⚠️ `{type(e).__name__}: {e}`")
    return wrapper


@owner_only
async def cmd_start(event) -> None:
    state.bot_blocked = False
    from core import reporter
    await reporter.resolve_owner()          # теперь бот знает, как до вас достучаться
    await event.respond(
        "🛡 **Guard на связи.**\n\n"
        "Сюда будут приходить удалённые и изменённые сообщения из ваших чатов, "
        "вместе с вложениями.\n\n" + HELP.format(p=config.PREFIX)
    )


@owner_only
async def cmd_help(event) -> None:
    await event.respond(HELP.format(p=config.PREFIX))


@owner_only
async def cmd_status(event) -> None:
    s = await db.stats()
    await event.respond(
        "📊 **Состояние**\n"
        f"⏱ аптайм: {fmt.uptime(time.time() - state.start_time)}\n"
        f"🗂 в кэше: {s['cached']}\n"
        f"🗑 удалённых сохранено: {s['deleted']}\n"
        f"✏️ правок: {s['edits']}\n"
        f"🔇 мутов: {s['mutes']}\n"
        f"💾 база: {fmt.size(s['size'])}\n"
        f"👤 аккаунт: {fmt.name_of(state.me)} (`{state.owner_id}`)"
    )


@owner_only
async def cmd_deleted(event) -> None:
    from modules.antidelete import _name

    raw = event.pattern_match.group(1)
    limit = int(raw) if raw and raw.strip().isdigit() else 10
    rows = await db.last_deleted(None, max(1, min(limit, 30)))
    if not rows:
        await event.respond("🗑 Пока ничего не удаляли.")
        return
    out = [f"🗑 **Последние удалённые** ({len(rows)})", ""]
    for row in rows:
        who = await _name(row["user_id"])
        where = await _name(row["chat_id"])
        preview = fmt.truncate(row["text"] or "", 160) or (
            f"_{row['media_type']}_" if row["media_type"] else "_пусто_")
        out.append(f"• `{fmt.ts(row['deleted_at'])}` **{who}** → {where}\n  {preview}")
    await event.respond(fmt.truncate("\n".join(out), 4000))


@owner_only
async def cmd_mutes(event) -> None:
    from modules.antidelete import _name

    rows = await db.all_mutes()
    now = int(time.time())
    lines = ["🔇 **Активные муты**", ""]
    for row in rows:
        if row["until"] and row["until"] <= now:
            continue
        who = await _name(row["user_id"])
        scope = "везде" if row["chat_id"] == 0 else await _name(row["chat_id"])
        left = ("бессрочно" if not row["until"]
                else f"ещё {fmt.human_delta(row['until'] - now)}")
        lines.append(f"• **{who}** (`{row['user_id']}`) — {scope}, {left}")
    if len(lines) == 2:
        await event.respond("🔇 Список мутов пуст.")
        return
    lines.append("\n_Снять:_ `/unmute <id>`")
    await event.respond(fmt.truncate("\n".join(lines), 4000))


@owner_only
async def cmd_unmute(event) -> None:
    raw = (event.pattern_match.group(1) or "").strip()
    if not re.fullmatch(r"-?\d+", raw):
        await event.respond("Использование: `/unmute 123456789`")
        return
    user_id = int(raw)
    removed = [key for key in list(state.mutes) if key[1] == user_id]
    for chat_id, _ in removed:
        await state.unmute_user(chat_id, user_id)
    await event.respond(f"🔊 Мутов снято: **{len(removed)}**." if removed
                        else "Этот пользователь не был замучен.")


@owner_only
async def cmd_backup(event) -> None:
    path = await backup.make_backup()
    if path is None:
        await event.respond("⚠️ Не удалось собрать бэкап.")
        return
    try:
        await event.respond(file=str(path), message=f"🗄 База · {fmt.ts(db.now())}")
    finally:
        path.unlink(missing_ok=True)


HANDLERS = (
    ("start", cmd_start),
    ("help", cmd_help),
    ("status", cmd_status),
    ("stats", cmd_status),
    ("deleted", cmd_deleted),
    ("mutes", cmd_mutes),
    ("unmute", cmd_unmute),
    ("backup", cmd_backup),
)


MENU = (
    ("status", "состояние и статистика"),
    ("deleted", "последние удалённые сообщения"),
    ("mutes", "активные муты"),
    ("unmute", "снять мут: /unmute <id>"),
    ("backup", "прислать базу файлом"),
    ("help", "справка"),
)


def setup_bot(bot) -> None:
    for name, handler in HANDLERS:
        bot.add_event_handler(handler, events.NewMessage(pattern=_pattern(name)))


async def publish_menu(bot) -> None:
    """Список команд в меню бота — чтобы не вспоминать их наизусть."""
    try:
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="",
            commands=[BotCommand(name, desc) for name, desc in MENU]))
    except Exception as e:                                   # noqa: BLE001
        log.debug("меню команд не обновилось: %r", e)
