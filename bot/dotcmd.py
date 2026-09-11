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
from bot import digest, outgoing, parse, transcript, urgent
from bot.editable import BizMessage
from core import anim, chatprefs, fmt, state

log = logging.getLogger("dotcmd")

@dataclass
class BizCommand:
    name: str
    handler: Callable
    args: str = ""
    desc: str = ""
    visible: bool = False        # ответ виден собеседнику или уходит в личку с ботом
    aliases: tuple[str, ...] = ()
    cat: str = "Прочее"


# Порядок разделов в справке — фиксированный, а не как импортировались модули.
CAT_ORDER = ("Мут", "Не беспокоить", "Чистка", "Чат", "Развлечения", "Инфо")

REGISTRY: dict[str, BizCommand] = {}
COMMANDS: list[BizCommand] = []


def bizcmd(name: str, *, args: str = "", desc: str = "", visible: bool = False,
           aliases: tuple[str, ...] | list[str] = (), cat: str = "Прочее"):
    def wrapper(func: Callable) -> Callable:
        cmd = BizCommand(name, func, args, desc, visible, tuple(aliases), cat)
        COMMANDS.append(cmd)
        for key in (name, *cmd.aliases):
            REGISTRY[key] = cmd
        return func
    return wrapper


@dataclass
class BizCtx:
    api: object
    message: dict
    connection_id: str
    owner_id: int
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
    def reply(self) -> dict | None:
        return self.message.get("reply_to_message")

    def can(self, right: str) -> bool:
        return bool(state.rights_of(self.connection_id).get(right))

    async def drop_command(self) -> bool:
        try:
            await self.api.delete_business_messages(self.connection_id,
                                                    [self.message_id])
            state.mark_own_deletion(self.owner_id, self.chat_id, self.message_id)
            return True
        except Exception as e:                               # noqa: BLE001
            log.warning("не удалось убрать команду из переписки: %r", e)
            return False

    async def notice(self, final: str, *, icon: str = "🔇") -> None:
        """Заменяет команду уведомлением в самой переписке, с анимацией."""
        removed = await self.drop_command()
        first = f"{icon} {anim.bar(0, 3, width=6)}"
        try:
            sent = await outgoing.message(
                self.api, self.owner_id, self.chat_id, first, self.connection_id,
                reply_to=None if removed else self.message_id)
        except Exception as e:                               # noqa: BLE001
            log.warning("не удалось отправить уведомление: %r", e)
            await self.private(final)
            return
        style = "off" if {"q", "quiet"} & self.flags else None
        await anim.play(BizMessage(self.api, self.chat_id, sent["message_id"],
                                   self.connection_id, self.owner_id),
                        final, icon=icon, style=style)

    async def private(self, text: str) -> None:
        """Отвечает в личку с ботом — собеседник ничего не видит."""
        await self.drop_command()
        target = state.chat_of(self.owner_id)
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


@bizcmd("mute", args="[reply|id] [10m|2h|1d] [причина] [-all]", visible=True, cat="Мут",
        desc="замутить собеседника: его новые сообщения удаляются у всех")
async def cmd_mute(ctx: BizCtx) -> None:
    user_id, who, rest = await ctx.target()
    if user_id is None:
        await ctx.fail("Кого мутим? Ответьте на сообщение или укажите id.")
        return
    if user_id == ctx.owner_id:
        await ctx.fail("Себя мутить бессмысленно.")
        return
    if not ctx.can("can_delete_all_messages"):
        await ctx.fail("У бота нет права удалять сообщения собеседника. "
                       "Настройки → Telegram для бизнеса → Чат-боты → включите его.")
        return

    everywhere = bool({"all", "everywhere"} & ctx.flags)
    seconds = fmt.parse_duration(rest[0]) if rest else None
    reason = " ".join(rest[1:] if (rest and seconds is not None) else rest)
    seconds = seconds or 0
    until = int(time.time()) + seconds if seconds else 0
    await state.mute_user(ctx.owner_id, 0 if everywhere else ctx.chat_id,
                          user_id, until, reason)

    is_peer = user_id == ctx.chat_id
    if seconds:
        period = fmt.human_delta(seconds)
        text = (MUTED_SELF if is_peer else MUTED_OTHER).format(who=who, period=period)
    else:
        text = (MUTED_FOREVER_SELF if is_peer else MUTED_FOREVER_OTHER).format(who=who)
    if everywhere:
        text += "\n_(во всех чатах)_"
    if reason:
        text += f"\n_Причина: {reason}_"
    await ctx.notice(text)


@bizcmd("unmute", args="[reply|id]", visible=True, cat="Мут", desc="снять мут в этом чате")
async def cmd_unmute(ctx: BizCtx) -> None:
    user_id, who, _ = await ctx.target()
    if user_id is None:
        await ctx.fail("Кого размутить?")
        return

    mute = (await db.get_mute(ctx.owner_id, ctx.chat_id, user_id)
            or await db.get_mute(ctx.owner_id, 0, user_id))
    since = mute["created_at"] if mute else 0
    existed = await state.unmute_user(ctx.owner_id, ctx.chat_id, user_id)
    existed = await state.unmute_user(ctx.owner_id, 0, user_id) or existed
    if not existed:
        await ctx.fail("Этот пользователь и не был замучен.")
        return

    text = ("🔊 **С вас снят мут. Можете писать.**" if user_id == ctx.chat_id
            else f"🔊 **С {who} снят мут.**")
    await ctx.notice(text, icon="🔊")
    await mute_digest(ctx.owner_id, user_id, who, since)


async def mute_digest(owner_id: int, user_id: int, who: str, since: int) -> None:
    """Что человек написал, пока был замучен."""
    rows = await db.intercepted(owner_id, user_id, since=since, reason="mute",
                                limit=500)
    if rows:
        await digest.deliver(owner_id, rows, empty="",
                             title=f"🔇 **Пока {who} был(а) замучен(а)**",
                             chat_title=who)


@bizcmd("mutelist", desc="список активных мутов", aliases=["mutes"], cat="Мут")
async def cmd_mutelist(ctx: BizCtx) -> None:
    await ctx.private(await mute_list_text(ctx.owner_id))


async def mute_list_text(owner_id: int) -> str:
    rows = await db.all_mutes(owner_id)
    now = int(time.time())
    lines = ["🔇 **Активные муты**", ""]
    for row in rows:
        if row["until"] and row["until"] <= now:
            continue
        scope = "везде" if row["chat_id"] == 0 else f"чат `{row['chat_id']}`"
        left = ("бессрочно" if not row["until"]
                else f"ещё {fmt.human_delta(row['until'] - now)}")
        lines.append(f"• `{row['user_id']}` — {scope}, {left}")
    return "🔇 Список мутов пуст." if len(lines) == 2 else fmt.truncate(
        "\n".join(lines), 3500)


# ------------------------------------------------------- не беспокоить -----

@bizcmd("gmute", args="", aliases=["dnd"], cat="Не беспокоить",
        desc="режим «не беспокоить»: удалять сообщения всех подряд")
async def cmd_gmute(ctx: BizCtx) -> None:
    if not ctx.can("can_delete_all_messages"):
        await ctx.fail("У бота нет права удалять сообщения собеседника — "
                       "режим «не беспокоить» работать не будет.")
        return
    await state.set_dnd(ctx.owner_id)
    await ctx.private(dnd_enabled_text())


@bizcmd("ungmute", args="", aliases=["undnd"], cat="Не беспокоить",
        desc="выключить «не беспокоить»")
async def cmd_ungmute(ctx: BizCtx) -> None:
    if not state.dnd_active(ctx.owner_id):
        await ctx.fail("Режим «не беспокоить» и так выключен.")
        return
    since = state.dnd_since(ctx.owner_id)
    await state.clear_dnd(ctx.owner_id)
    await ctx.private(dnd_disabled_text(since))
    await dnd_digest(ctx.owner_id, since)


def dnd_enabled_text() -> str:
    answer = config.DND_TEXT + urgent.hint()
    quoted = "\n".join(f"> {line}" for line in answer.splitlines())
    text = (f"🌙 **Режим «не беспокоить» включён.**\n\n"
            f"Сообщения от всех будут удаляться сразу, а отправитель получит "
            f"от вашего имени:\n\n{quoted}\n\n")
    if config.URGENT_ENABLED and config.URGENT_WORDS:
        text += ("Срочный вызов приходит вам отдельным уведомлением, "
                 "по одному в сутки от человека — /urgent.\n")
    text += (f"Исключения — белый список (`{config.PREFIX}allow` реплаем или "
             f"/allow <id>).\n"
             f"Выключить: `{config.PREFIX}ungmute` или /ungmute.")
    return text


def dnd_disabled_text(since: int) -> str:
    spent = f" Провели в нём {fmt.uptime(time.time() - since)}." if since else ""
    return f"☀️ **Режим «не беспокоить» выключен.** Сообщения снова доходят.{spent}"


async def dnd_digest(owner_id: int, since: int) -> None:
    rows = await db.intercepted(owner_id, since=since, reason="dnd", limit=500)
    await digest.deliver(owner_id, rows, title="🌙 **Пока вас не беспокоили**",
                         empty="🌙 За это время вам никто не писал.")


@bizcmd("urgent", args="[N]", aliases=["calls"], cat="Не беспокоить",
        desc="срочные вызовы от собеседников")
async def cmd_urgent(ctx: BizCtx) -> None:
    limit = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    await ctx.private(await urgent.recent_text(ctx.owner_id, max(1, min(limit, 50))))


@bizcmd("allow", args="[reply|id]", cat="Не беспокоить",
        desc="пропускать этого человека в «не беспокоить»")
async def cmd_allow(ctx: BizCtx) -> None:
    user_id, who, _ = await ctx.target()
    if user_id is None:
        await ctx.fail("Кого пропускать? Ответьте на сообщение или укажите id.")
        return
    await state.allow_user(ctx.owner_id, user_id, who)
    await ctx.private(f"✅ {who} (`{user_id}`) теперь проходит сквозь режим "
                      f"«не беспокоить».")


@bizcmd("deny", args="[reply|id]", cat="Не беспокоить", desc="убрать из белого списка")
async def cmd_deny(ctx: BizCtx) -> None:
    user_id, who, _ = await ctx.target()
    if user_id is None:
        await ctx.fail("Кого убрать?")
        return
    removed = await state.deny_user(ctx.owner_id, user_id)
    await ctx.private(f"🚫 {who} убран(а) из белого списка." if removed
                      else "Его и не было в белом списке.")


@bizcmd("allowed", desc="белый список «не беспокоить»", cat="Не беспокоить")
async def cmd_allowed(ctx: BizCtx) -> None:
    await ctx.private(await allowed_text(ctx.owner_id))


async def allowed_text(owner_id: int) -> str:
    rows = await db.allowed_users(owner_id)
    if not rows:
        return "✅ Белый список пуст — в режиме «не беспокоить» удаляются все."
    lines = ["✅ **Белый список**", ""]
    lines += [f"• {row['name'] or '—'} (`{row['user_id']}`)" for row in rows]
    return fmt.truncate("\n".join(lines), 3500)


@bizcmd("muted", args="[N]", cat="Не беспокоить", desc="что удалили мут и «не беспокоить»")
async def cmd_muted(ctx: BizCtx) -> None:
    limit = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 20
    rows = await db.intercepted(ctx.owner_id, limit=max(1, min(limit, 200)))
    await ctx.drop_command()
    await digest.deliver(ctx.owner_id, rows,
                         title="🔇 **Перехваченные сообщения**",
                         empty="🔇 Пока ничего не перехвачено.")


# ---------------------------------------------------------------- чистка ----

@bizcmd("clear", args="[что]", cat="Чистка", aliases=["clearlog"],
        desc="почистить накопленное по этому чату")
async def cmd_clear(ctx: BizCtx) -> None:
    """Без аргумента — всё по этому чату, иначе одна область: `.clear urgent`."""
    from bot import cleanup
    from core import reporter

    await ctx.drop_command()
    wanted = (ctx.args[0].lower() if ctx.args else "")
    code = cleanup.CODES.get(wanted, cleanup.ALL)
    text, keyboard = await cleanup.offer(ctx.owner_id, code, "c", ctx.chat_id)
    await reporter.send_report(ctx.owner_id, text, reply_markup=keyboard)


@bizcmd("del", args="(реплаем)", cat="Чистка", desc="удалить сообщение у всех", aliases=["d"])
async def cmd_del(ctx: BizCtx) -> None:
    if not ctx.reply:
        await ctx.fail("Ответьте на сообщение, которое нужно удалить.")
        return
    ids = [ctx.reply["message_id"], ctx.message_id]
    try:
        await ctx.api.delete_business_messages(ctx.connection_id, ids)
        state.mark_own_deletion(ctx.owner_id, ctx.chat_id, *ids)
    except Exception as e:                                   # noqa: BLE001
        await ctx.fail(f"Не удалось удалить: `{type(e).__name__}`")


@bizcmd("purge", args="(реплаем)", cat="Чистка", desc="удалить всё от реплая до текущего сообщения")
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
            state.mark_own_deletion(ctx.owner_id, ctx.chat_id, *batch)
            removed += len(batch)
        except Exception as e:                               # noqa: BLE001
            log.warning("пачка не удалилась: %r", e)
    target = state.chat_of(ctx.owner_id)
    if target:
        await ctx.api.send_message(target, f"🧹 Удалено сообщений: **{removed}**")


# ------------------------------------------------------------ антиудаление --

async def _toggle(ctx: BizCtx, key: str, label: str) -> None:
    current = (await chatprefs.flags(ctx.owner_id, ctx.chat_id, True))[key]
    value = (not current) if not ctx.args else ctx.args[0].lower() in {
        "on", "вкл", "1", "да", "yes", "true"}
    title = parse.display_name(ctx.message.get("chat"))
    await chatprefs.toggle(ctx.owner_id, ctx.chat_id, key, value, title=title)

    text = (f"{'✅' if value else '🚫'} {label}: "
            f"**{'включено' if value else 'выключено'}** для чата «{title}».")
    if value and key == "ignored":
        text += ("\n\nЯ больше ничего не сохраняю и не присылаю из этого чата. "
                 "Ничего не удаляется — просто молчу.\n"
                 f"Вернуть как было: `{config.PREFIX}ignore off` здесь же "
                 "или /chats в чате со мной.")
    await ctx.private(text)


@bizcmd("antidelete", args="[on|off]", cat="Чат",
        desc="сохранять удалённые сообщения этого чата",
        aliases=["ad"])
async def cmd_antidelete(ctx: BizCtx) -> None:
    await _toggle(ctx, "antidelete", "Антиудаление")


@bizcmd("ignore", args="[on|off]", cat="Чат", desc="полностью игнорировать этот чат")
async def cmd_ignore(ctx: BizCtx) -> None:
    await _toggle(ctx, "ignored", "Игнорирование чата")


@bizcmd("chats", desc="чаты, где вы меняли настройки, и как вернуть как было", cat="Чат")
async def cmd_chats(ctx: BizCtx) -> None:
    await ctx.private(await chats_text(ctx.owner_id))


async def chats_text(owner_id: int) -> str:
    rows = await db.tuned_chats(owner_id)
    if not rows:
        return ("⚙️ Особых настроек нет — во всех личных чатах я сохраняю "
                "удалённые сообщения.")
    lines = ["⚙️ **Чаты с изменёнными настройками**", ""]
    for row in rows:
        title = row["title"] or f"чат `{row['chat_id']}`"
        if row["ignored"]:
            lines.append(f"🙈 **{title}** — игнорирую полностью")
        elif row["antidelete"] == 0:
            lines.append(f"🔕 **{title}** — не сохраняю удалённые")
        else:
            lines.append(f"⚙️ **{title}** — настройки изменены")
    lines.append(f"\nВернуть как было: `{config.PREFIX}ignore off` или "
                 f"`{config.PREFIX}antidelete on` в нужной переписке.")
    return fmt.truncate("\n".join(lines), 3500)


@bizcmd("deleted", args="[N]", cat="Чат", desc="последние удалённые в этом чате", aliases=["dels"])
async def cmd_deleted(ctx: BizCtx) -> None:
    limit = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    rows = await db.last_deleted(ctx.owner_id, ctx.chat_id, max(1, min(limit, 30)))
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


@bizcmd("export", desc="выгрузить журнал удалённых этого чата файлом", cat="Чат")
async def cmd_export(ctx: BizCtx) -> None:
    from core import reporter

    rows = await db.last_deleted(ctx.owner_id, ctx.chat_id, 5000)
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
        ctx.owner_id, payload, transcript.filename(ctx.chat_id, when),
        f"🗑 **Журнал удалённых**\n💬 {parse.display_name(ctx.message.get('chat'))}\n"
        f"записей: **{len(ordered)}**")


# ------------------------------------------------------------------ инфо ----

@bizcmd("id", desc="id чата и собеседника", cat="Инфо")
async def cmd_id(ctx: BizCtx) -> None:
    lines = [f"💬 Чат: `{ctx.chat_id}`"]
    if ctx.reply:
        sender = ctx.reply.get("from") or {}
        lines.append(f"👤 Отправитель: `{sender.get('id')}`")
        lines.append(f"✉️ Сообщение: `{ctx.reply['message_id']}`")
    await ctx.private("\n".join(lines))


@bizcmd("dox", args="[reply|id]", cat="Инфо", aliases=["whois", "досье"],
        desc="досье: всё, что я записал про человека")
async def cmd_dox(ctx: BizCtx) -> None:
    from bot import dossier

    user_id, _, _ = await ctx.target()
    if user_id is None:
        await ctx.fail("На кого? Ответьте на сообщение или `.dox <id>`.")
        return
    await ctx.private(await dossier.card(ctx.api, ctx.owner_id, user_id,
                                         chat_id=ctx.chat_id))


@bizcmd("ping", desc="проверить, что бот жив", cat="Инфо")
async def cmd_ping(ctx: BizCtx) -> None:
    started = time.perf_counter()
    await ctx.drop_command()
    delay = (time.perf_counter() - started) * 1000
    target = state.chat_of(ctx.owner_id)
    if target:
        await ctx.api.send_message(
            target, f"🏓 **На связи.** Отклик {delay:.0f} мс, "
                    f"работаю без перерыва {fmt.uptime(time.time() - state.start_time)}.")


@bizcmd("stats", desc="сколько всего сохранено", cat="Инфо")
async def cmd_stats(ctx: BizCtx) -> None:
    s = await db.stats(ctx.owner_id)
    lines = ["📊 **Что у меня сохранено**", "",
             f"🗑 удалённых сообщений: **{s['deleted']}**"]
    if s["edits"]:
        lines.append(f"✏️ изменённых: **{s['edits']}**")
    if s["intercepted"]:
        lines.append(f"🔒 перехвачено: **{s['intercepted']}**")
    if s["mutes"]:
        lines.append(f"🔇 замучено: **{s['mutes']}**")
    await ctx.private("\n".join(lines))


@bizcmd("help", args="[команда]", cat="Инфо", desc="список команд")
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
    by_cat: dict[str, list[BizCommand]] = {}
    for cmd in COMMANDS:
        by_cat.setdefault(cmd.cat, []).append(cmd)
    order = sorted(by_cat, key=lambda c: (CAT_ORDER.index(c) if c in CAT_ORDER
                                          else len(CAT_ORDER), c))

    lines = [f"🛡 **Команды в личных чатах** (префикс `{config.PREFIX}`)"]
    for cat in order:
        lines.append(f"\n**{cat}**")
        for cmd in by_cat[cat]:
            mark = "💬" if cmd.visible else "🔒"
            lines.append(f"{mark} `{config.PREFIX}{cmd.name}` — {cmd.desc}")
    lines += ["", "💬 — результат виден собеседнику, 🔒 — ответ приходит сюда, "
              "в личку с ботом."]
    return fmt.truncate("\n".join(lines), 4000)


# ------------------------------------------------------------- диспетчер ----

PATTERN = re.compile(rf"^{re.escape(config.PREFIX)}(\w+)(?:\s+([\s\S]*))?$")
FLAG_RE = re.compile(r"^-[^\W\d_]")


async def handle(api, message: dict, connection_id: str, owner_id: int) -> None:
    match = PATTERN.match(parse.text_of(message))
    if match is None:
        return
    cmd = REGISTRY.get(match.group(1).lower())
    if cmd is None:
        return

    raw = (match.group(2) or "").strip()
    tokens = raw.split()
    ctx = BizCtx(
        api=api, message=message, connection_id=connection_id, owner_id=owner_id,
        args=[t for t in tokens if not FLAG_RE.match(t)],
        flags={t.lstrip("-").lower() for t in tokens if FLAG_RE.match(t)},
        raw=raw,
    )
    try:
        await cmd.handler(ctx)
    except Exception:                                   # noqa: BLE001
        log.exception("ошибка команды %s", cmd.name)
        await ctx.fail(f"Не получилось выполнить `{config.PREFIX}{cmd.name}`. "
                       "Попробуйте ещё раз.")
