"""Команды с префиксом «.» внутри личных чатов в режиме Business.

Владелец пишет их сам в переписке. Бот не может редактировать чужое сообщение
(даже своего владельца), поэтому команда удаляется, а на её место бот от имени
владельца отправляет своё — его уже можно править, и анимация работает как в
режиме юзербота.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import config
import db
from bot import parse, transcript
from core import anim, fmt, state

log = logging.getLogger("dotcmd")

NOT_MODIFIED = "message is not modified"


@dataclass
class BizCommand:
    name: str
    handler: Callable
    args: str = ""
    desc: str = ""
    visible: bool = False        # ответ виден собеседнику или уходит в личку с ботом
    aliases: tuple[str, ...] = ()


REGISTRY: dict[str, BizCommand] = {}
COMMANDS: list[BizCommand] = []


def bizcmd(name: str, *, args: str = "", desc: str = "", visible: bool = False,
           aliases: tuple[str, ...] | list[str] = ()):
    def wrapper(func: Callable) -> Callable:
        cmd = BizCommand(name, func, args, desc, visible, tuple(aliases))
        COMMANDS.append(cmd)
        for key in (name, *cmd.aliases):
            REGISTRY[key] = cmd
        return func
    return wrapper


class BizMessage:
    """Адаптер под core.anim: сообщение бота, отправленное от имени владельца."""

    def __init__(self, api, chat_id: int, message_id: int, connection_id: str):
        self.api, self.chat_id = api, chat_id
        self.id, self.connection_id = message_id, connection_id

    async def edit(self, text: str, link_preview: bool = False):
        try:
            await self.api.edit_message_text(
                self.chat_id, self.id, text,
                business_connection_id=self.connection_id)
        except Exception as e:                               # noqa: BLE001
            if NOT_MODIFIED in str(e):
                return self                                  # кадр совпал с прошлым
            raise
        return self


@dataclass
class BizCtx:
    api: object
    message: dict
    connection_id: str
    args: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    raw: str = ""

    @property
    def chat_id(self) -> int:
        return (self.message.get("chat") or {}).get("id")

    @property
    def message_id(self) -> int:
        return self.message["message_id"]

    @property
    def owner_id(self) -> int:
        return (state.business.get(self.connection_id) or {}).get("user_id", 0)

    @property
    def reply(self) -> dict | None:
        return self.message.get("reply_to_message")

    def can(self, right: str) -> bool:
        return bool(state.rights_of(self.connection_id).get(right))

    async def drop_command(self) -> bool:
        try:
            await self.api.delete_business_messages(self.connection_id,
                                                    [self.message_id])
            state.mark_own_deletion(self.chat_id, self.message_id)
            return True
        except Exception as e:                               # noqa: BLE001
            log.warning("не удалось убрать команду из переписки: %r", e)
            return False

    async def notice(self, final: str, *, icon: str = "🔇") -> None:
        """Заменяет команду уведомлением в самой переписке, с анимацией."""
        removed = await self.drop_command()
        first = f"{icon} {anim.bar(0, 3, width=6)}"
        try:
            sent = await self.api.send_message(
                self.chat_id, first, business_connection_id=self.connection_id,
                reply_to=None if removed else self.message_id)
        except Exception as e:                               # noqa: BLE001
            log.warning("не удалось отправить уведомление: %r", e)
            await self.private(final)
            return
        style = "off" if {"q", "quiet"} & self.flags else None
        await anim.play(BizMessage(self.api, self.chat_id, sent["message_id"],
                                   self.connection_id), final, icon=icon, style=style)

    async def private(self, text: str) -> None:
        """Отвечает в личку с ботом — собеседник ничего не видит."""
        await self.drop_command()
        target = state.owner_chat_id or self.owner_id
        if not target:
            return
        try:
            await self.api.send_message(target, text)
        except Exception as e:                               # noqa: BLE001
            log.warning("не удалось ответить владельцу: %r", e)

    async def fail(self, text: str) -> None:
        await self.private(f"⚠️ {text}")

    async def target(self) -> tuple[int | None, str, list[str]]:
        """(user_id, имя, оставшиеся аргументы) — реплай, аргумент или собеседник."""
        rest = list(self.args)
        if self.reply:
            sender = self.reply.get("from") or {}
            return sender.get("id"), parse.display_name(sender), rest
        if rest and re.fullmatch(r"-?\d+", rest[0]):
            return int(rest[0]), f"`{rest[0]}`", rest[1:]
        chat = self.message.get("chat") or {}
        if chat.get("type") == "private":
            return chat.get("id"), parse.display_name(chat), rest
        return None, "", rest


# ------------------------------------------------------------------ муты ----

MUTED_SELF = "🔇 **Вы были замучены на {period}.**"
MUTED_FOREVER_SELF = "🔇 **Вы были замучены на неопределённый срок.**"
MUTED_OTHER = "🔇 **{who} замучен(а) на {period}.**"
MUTED_FOREVER_OTHER = "🔇 **{who} замучен(а) на неопределённый срок.**"


async def _mute(ctx: BizCtx, *, global_: bool) -> None:
    user_id, who, rest = await ctx.target()
    if user_id is None:
        await ctx.fail("Кого мутим? Ответьте на сообщение или укажите id.")
        return
    if user_id == ctx.owner_id:
        await ctx.fail("Себя мутить бессмысленно.")
        return
    if not ctx.can("can_delete_all_messages"):
        await ctx.fail("У бота нет права удалять сообщения собеседника. "
                       "Настройки → Telegram Business → Чат-боты → включите его.")
        return

    seconds = fmt.parse_duration(rest[0]) if rest else None
    reason = " ".join(rest[1:] if (rest and seconds is not None) else rest)
    seconds = seconds or 0
    until = int(time.time()) + seconds if seconds else 0
    await state.mute_user(0 if global_ else ctx.chat_id, user_id, until, reason)

    is_peer = user_id == ctx.chat_id
    if seconds:
        period = fmt.human_delta(seconds)
        text = (MUTED_SELF if is_peer else MUTED_OTHER).format(who=who, period=period)
    else:
        text = (MUTED_FOREVER_SELF if is_peer
                else MUTED_FOREVER_OTHER).format(who=who)
    if global_:
        text += "\n_(во всех чатах)_"
    if reason:
        text += f"\n_Причина: {reason}_"
    await ctx.notice(text)


@bizcmd("mute", args="[reply|id] [10m|2h|1d] [причина]", visible=True,
        desc="замутить собеседника: его новые сообщения удаляются у всех")
async def cmd_mute(ctx: BizCtx) -> None:
    await _mute(ctx, global_=False)


@bizcmd("gmute", args="[reply|id] [срок]", visible=True,
        desc="замутить во всех личных чатах сразу")
async def cmd_gmute(ctx: BizCtx) -> None:
    await _mute(ctx, global_=True)


async def _unmute(ctx: BizCtx, *, global_: bool) -> None:
    user_id, who, _ = await ctx.target()
    if user_id is None:
        await ctx.fail("Кого размутить?")
        return
    existed = await state.unmute_user(0 if global_ else ctx.chat_id, user_id)
    if not existed and not global_:
        existed = await state.unmute_user(0, user_id)
    if not existed:
        await ctx.fail("Этот пользователь и не был замучен.")
        return
    text = ("🔊 **С вас снят мут. Можете писать.**" if user_id == ctx.chat_id
            else f"🔊 **С {who} снят мут.**")
    await ctx.notice(text, icon="🔊")


@bizcmd("unmute", args="[reply|id]", visible=True, desc="снять мут в этом чате")
async def cmd_unmute(ctx: BizCtx) -> None:
    await _unmute(ctx, global_=False)


@bizcmd("ungmute", args="[reply|id]", visible=True, desc="снять глобальный мут")
async def cmd_ungmute(ctx: BizCtx) -> None:
    await _unmute(ctx, global_=True)


@bizcmd("mutelist", desc="список активных мутов", aliases=["mutes"])
async def cmd_mutelist(ctx: BizCtx) -> None:
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
    await ctx.private("🔇 Список мутов пуст." if len(lines) == 2
                      else fmt.truncate("\n".join(lines), 3500))


# ---------------------------------------------------------------- чистка ----

@bizcmd("del", args="(реплаем)", desc="удалить сообщение у всех", aliases=["d"])
async def cmd_del(ctx: BizCtx) -> None:
    if not ctx.reply:
        await ctx.fail("Ответьте на сообщение, которое нужно удалить.")
        return
    ids = [ctx.reply["message_id"], ctx.message_id]
    try:
        await ctx.api.delete_business_messages(ctx.connection_id, ids)
        state.mark_own_deletion(ctx.chat_id, *ids)
    except Exception as e:                                   # noqa: BLE001
        await ctx.fail(f"Не удалось удалить: `{type(e).__name__}`")


@bizcmd("purge", args="(реплаем)", desc="удалить всё от реплая до текущего сообщения")
async def cmd_purge(ctx: BizCtx) -> None:
    if not ctx.reply:
        await ctx.fail("Ответьте на сообщение, с которого начинать чистку.")
        return
    ids = list(range(ctx.reply["message_id"], ctx.message_id + 1))
    removed = 0
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        try:
            await ctx.api.delete_business_messages(ctx.connection_id, batch)
            state.mark_own_deletion(ctx.chat_id, *batch)
            removed += len(batch)
        except Exception as e:                               # noqa: BLE001
            log.warning("пачка не удалилась: %r", e)
    target = state.owner_chat_id or ctx.owner_id
    if target:
        await ctx.api.send_message(target, f"🧹 Удалено сообщений: **{removed}**")


# ------------------------------------------------------------ антиудаление --

async def _toggle(ctx: BizCtx, key: str, label: str) -> None:
    from modules.antidelete import flags, invalidate
    current = (await flags(ctx.chat_id, True))[key]
    value = (not current) if not ctx.args else ctx.args[0].lower() in {
        "on", "вкл", "1", "да", "yes", "true"}
    await db.set_setting(ctx.chat_id, key, int(value))
    invalidate(ctx.chat_id)
    await ctx.private(f"{'✅' if value else '🚫'} {label}: "
                      f"**{'включено' if value else 'выключено'}** для этого чата.")


@bizcmd("antidelete", args="[on|off]", desc="логировать удалённые в этом чате",
        aliases=["ad"])
async def cmd_antidelete(ctx: BizCtx) -> None:
    await _toggle(ctx, "antidelete", "Антиудаление")


@bizcmd("ignore", args="[on|off]", desc="полностью игнорировать этот чат")
async def cmd_ignore(ctx: BizCtx) -> None:
    await _toggle(ctx, "ignored", "Игнорирование чата")


@bizcmd("deleted", args="[N]", desc="последние удалённые в этом чате", aliases=["dels"])
async def cmd_deleted(ctx: BizCtx) -> None:
    limit = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    rows = await db.last_deleted(ctx.chat_id, max(1, min(limit, 30)))
    if not rows:
        await ctx.private("🗑 Удалённых сообщений в этом чате нет.")
        return
    out = [f"🗑 **Последние удалённые** ({len(rows)})", ""]
    for row in rows:
        preview = fmt.truncate(row["text"] or "", 160) or (
            f"_{row['media_type']}_" if row["media_type"] else "_пусто_")
        out.append(f"• `{fmt.ts(row['deleted_at'])}` **{row['user_name'] or '—'}**\n"
                   f"  {preview}")
    await ctx.private(fmt.truncate("\n".join(out), 3500))


@bizcmd("export", desc="выгрузить журнал удалённых этого чата файлом")
async def cmd_export(ctx: BizCtx) -> None:
    from core import reporter

    rows = await db.last_deleted(ctx.chat_id, 5000)
    await ctx.drop_command()
    if not rows:
        await ctx.private("🗑 В этом чате нечего выгружать.")
        return
    ordered = sorted(rows, key=lambda row: (row["date"], row["msg_id"]))
    when = db.now()
    payload, stats = transcript.build(
        ordered, chat_title=parse.display_name(ctx.message.get("chat")),
        owner_id=ctx.owner_id, requested=len(ordered), when=when,
        reason="Журнал удалённых сообщений")
    await reporter.send_document(
        payload, transcript.filename(ctx.chat_id, when),
        f"🗑 **Журнал удалённых**\n💬 {parse.display_name(ctx.message.get('chat'))}\n"
        f"записей: **{len(ordered)}**")


# ------------------------------------------------------------------ инфо ----

@bizcmd("id", desc="id чата и собеседника")
async def cmd_id(ctx: BizCtx) -> None:
    lines = [f"💬 Чат: `{ctx.chat_id}`"]
    if ctx.reply:
        sender = ctx.reply.get("from") or {}
        lines.append(f"👤 Отправитель: `{sender.get('id')}`")
        lines.append(f"✉️ Сообщение: `{ctx.reply['message_id']}`")
    await ctx.private("\n".join(lines))


@bizcmd("ping", desc="проверить, что бот жив")
async def cmd_ping(ctx: BizCtx) -> None:
    started = time.perf_counter()
    await ctx.drop_command()
    delay = (time.perf_counter() - started) * 1000
    target = state.owner_chat_id or ctx.owner_id
    if target:
        await ctx.api.send_message(
            target, f"🏓 **{delay:.0f} мс**\n"
                    f"⏱ аптайм: {fmt.uptime(time.time() - state.start_time)}")


@bizcmd("stats", desc="статистика базы")
async def cmd_stats(ctx: BizCtx) -> None:
    s = await db.stats()
    await ctx.private(
        "📊 **Статистика**\n"
        f"🗂 в кэше: **{s['cached']}**\n"
        f"🗑 удалённых сохранено: **{s['deleted']}**\n"
        f"✏️ правок: **{s['edits']}**\n"
        f"🔇 мутов: **{s['mutes']}**\n"
        f"🔗 подключений Business: **{s['business']}**\n"
        f"💾 база: **{fmt.size(s['size'])}**"
    )


@bizcmd("help", args="[команда]", desc="список команд")
async def cmd_help(ctx: BizCtx) -> None:
    await ctx.private(help_text(ctx.args[0] if ctx.args else None))


def help_text(name: str | None = None) -> str:
    if name:
        cmd = REGISTRY.get(name.lstrip(config.PREFIX).lower())
        if cmd is None:
            return f"⚠️ Команда `{name}` не найдена."
        where = "видна собеседнику" if cmd.visible else "ответ только вам"
        return (f"**{config.PREFIX}{cmd.name}**\n"
                f"`{config.PREFIX}{cmd.name} {cmd.args}`\n\n{cmd.desc}\n_{where}_")
    lines = [f"🛡 **Команды в личных чатах** (префикс `{config.PREFIX}`)", ""]
    for cmd in COMMANDS:
        mark = "💬" if cmd.visible else "🔒"
        lines.append(f"{mark} `{config.PREFIX}{cmd.name}` — {cmd.desc}")
    lines += ["", "💬 — уведомление появляется в переписке, 🔒 — ответ приходит "
              "сюда, в личку с ботом."]
    return "\n".join(lines)


# ------------------------------------------------------------- диспетчер ----

PATTERN = re.compile(rf"^{re.escape(config.PREFIX)}(\w+)(?:\s+([\s\S]*))?$")
FLAG_RE = re.compile(r"^-[^\W\d_]")


async def handle(api, message: dict, connection_id: str) -> None:
    match = PATTERN.match(parse.text_of(message))
    if match is None:
        return
    cmd = REGISTRY.get(match.group(1).lower())
    if cmd is None:
        return

    raw = (match.group(2) or "").strip()
    tokens = raw.split()
    ctx = BizCtx(
        api=api, message=message, connection_id=connection_id,
        args=[t for t in tokens if not FLAG_RE.match(t)],
        flags={t.lstrip("-").lower() for t in tokens if FLAG_RE.match(t)},
        raw=raw,
    )
    try:
        await cmd.handler(ctx)
    except Exception as e:                                   # noqa: BLE001
        log.exception("ошибка команды %s", cmd.name)
        await ctx.fail(f"`{type(e).__name__}: {e}`")
