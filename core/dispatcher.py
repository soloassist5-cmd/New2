"""Реестр и диспетчер команд с префиксом (по умолчанию «.»)."""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from telethon import events

import config
from core import fmt, reporter, state

log = logging.getLogger("dispatcher")


@dataclass
class Command:
    name: str
    handler: Callable
    args: str = ""
    desc: str = ""
    cat: str = "Прочее"
    aliases: tuple[str, ...] = ()

    @property
    def usage(self) -> str:
        return f"{config.PREFIX}{self.name} {self.args}".strip()


REGISTRY: dict[str, Command] = {}     # имя/алиас -> Command
COMMANDS: list[Command] = []          # уникальные, в порядке регистрации

# Порядок разделов в .help — фиксированный, чтобы не зависеть от порядка импортов.
CAT_ORDER = ("Мут", "Антиудаление", "Чистка", "Админ (группы)", "AFK",
             "Заметки", "Инфо", "Система")


def command(name: str, *, args: str = "", desc: str = "", cat: str = "Прочее",
            aliases: tuple[str, ...] | list[str] = ()):
    def wrapper(func: Callable) -> Callable:
        cmd = Command(name=name, handler=func, args=args, desc=desc, cat=cat,
                      aliases=tuple(aliases))
        COMMANDS.append(cmd)
        for key in (name, *cmd.aliases):
            if key in REGISTRY:
                raise RuntimeError(f"Команда {key!r} уже зарегистрирована")
            REGISTRY[key] = cmd
        return func
    return wrapper


@dataclass
class Ctx:
    event: Any
    args: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    raw: str = ""

    # ------------------------------------------------------------- ярлыки --
    @property
    def client(self):
        return state.client

    @property
    def msg(self):
        return self.event.message

    @property
    def chat_id(self) -> int:
        return self.event.chat_id

    @property
    def is_private(self) -> bool:
        return bool(self.event.is_private)

    async def reply_msg(self):
        return await self.event.get_reply_message()

    async def edit(self, text: str, *, link_preview: bool = False):
        try:
            return await self.msg.edit(text, link_preview=link_preview)
        except Exception as e:                       # noqa: BLE001
            log.warning("не удалось отредактировать ответ: %r", e)
            return self.msg

    async def done(self, text: str, *, delete_after: float | None = None):
        msg = await self.edit(text)
        if delete_after:
            await asyncio.sleep(delete_after)
            try:
                await msg.delete()
            except Exception:
                pass
        return msg

    async def fail(self, text: str, *, delete_after: float = 6.0):
        return await self.done(f"⚠️ {text}", delete_after=delete_after)

    # ------------------------------------------------------------- цели ----
    async def resolve_target(self) -> tuple[int | None, object, list[str]]:
        """Возвращает (user_id, entity, оставшиеся аргументы).

        Порядок: реплай -> первый аргумент (@username / id) -> собеседник в личке.
        """
        rest = list(self.args)

        reply = await self.reply_msg()
        if reply is not None and reply.sender_id:
            try:
                sender = await reply.get_sender()
            except Exception:
                sender = None
            return reply.sender_id, sender, rest

        if rest:
            token = rest[0]
            if token.startswith("@") or re.fullmatch(r"-?\d+", token):
                try:
                    entity = await self.client.get_entity(
                        int(token) if re.fullmatch(r"-?\d+", token) else token)
                    return entity.id, entity, rest[1:]
                except Exception:
                    if re.fullmatch(r"-?\d+", token):
                        return int(token), None, rest[1:]
                    return None, None, rest[1:]

        if self.is_private:
            try:
                entity = await self.event.get_chat()
            except Exception:
                entity = None
            return self.chat_id, entity, rest

        return None, None, rest


TRACE_LIMIT = 1500


async def _report_error(ctx: Ctx, cmd: Command, exc: Exception) -> None:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.exception("ошибка в команде %s", cmd.name)
    await ctx.fail(f"`{type(exc).__name__}: {exc}`", delete_after=10)
    try:
        await reporter.send_report(
            f"❗️ **Ошибка команды** `{cmd.name}`\n\n```\n{tb[-TRACE_LIMIT:]}\n```")
    except Exception:
        pass


FLAG_RE = re.compile(r"^-[^\W\d_]")     # -q, -all; но не -100500 (id чата)


def split_args(raw: str) -> tuple[list[str], set[str]]:
    """Делит хвост команды на позиционные аргументы и флаги."""
    tokens = raw.split()
    flags = {t.lstrip("-").lower() for t in tokens if FLAG_RE.match(t)}
    args = [t for t in tokens if not FLAG_RE.match(t)]
    return args, flags


def build_pattern() -> re.Pattern:
    return re.compile(rf"^{re.escape(config.PREFIX)}(\w+)(?:\s+([\s\S]*))?$")


async def _handle(event) -> None:
    match = event.pattern_match
    name = match.group(1).lower()
    cmd = REGISTRY.get(name)
    if cmd is None:
        return

    raw = (match.group(2) or "").strip()
    args, flags = split_args(raw)

    ctx = Ctx(event=event, args=args, flags=flags, raw=raw)
    try:
        result = cmd.handler(ctx)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:                          # noqa: BLE001
        await _report_error(ctx, cmd, exc)


def setup(client) -> None:
    client.add_event_handler(
        _handle,
        events.NewMessage(outgoing=True, forwards=False, pattern=build_pattern()),
    )


def help_text(name: str | None = None) -> str:
    if name:
        cmd = REGISTRY.get(name.lstrip(config.PREFIX).lower())
        if cmd is None:
            return f"⚠️ Команда `{name}` не найдена."
        lines = [f"**{config.PREFIX}{cmd.name}**", f"`{cmd.usage}`", "", cmd.desc or "—"]
        if cmd.aliases:
            lines.append("Алиасы: " + ", ".join(f"`{config.PREFIX}{a}`" for a in cmd.aliases))
        return "\n".join(lines)

    by_cat: dict[str, list[Command]] = {}
    for cmd in COMMANDS:
        by_cat.setdefault(cmd.cat, []).append(cmd)
    order = sorted(by_cat, key=lambda c: (CAT_ORDER.index(c) if c in CAT_ORDER
                                          else len(CAT_ORDER), c))
    out = [f"🛡 **Команды** (префикс `{config.PREFIX}`)"]
    for cat in order:
        cmds = by_cat[cat]
        out.append(f"\n**{cat}**")
        for cmd in cmds:
            out.append(f"  `{config.PREFIX}{cmd.name}` — {cmd.desc}")
    out.append(f"\n`{config.PREFIX}help <команда>` — подробности.")
    return fmt.truncate("\n".join(out), 4000)
