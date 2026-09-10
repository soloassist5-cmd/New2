"""Диагностика и информация о чатах/пользователях."""
from __future__ import annotations

import time

from telethon.tl.types import Channel, Chat, User

import config
import db
from core import fmt, state
from core.dispatcher import Ctx, command

CAT = "Инфо"


@command("ping", args="", cat=CAT, desc="проверить отклик")
async def cmd_ping(ctx: Ctx) -> None:
    started = time.perf_counter()
    await ctx.edit("🏓 …")
    delay = (time.perf_counter() - started) * 1000
    await ctx.edit(f"🏓 **{delay:.0f} мс**\n⏱ аптайм: {fmt.uptime(time.time() - state.start_time)}")


@command("id", args="[reply]", cat=CAT, desc="показать id чата и пользователя")
async def cmd_id(ctx: Ctx) -> None:
    lines = [f"💬 Чат: `{ctx.chat_id}`"]
    reply = await ctx.reply_msg()
    if reply is not None:
        lines.append(f"👤 Отправитель: `{reply.sender_id}`")
        lines.append(f"✉️ Сообщение: `{reply.id}`")
        if reply.forward and reply.forward.sender_id:
            lines.append(f"↪️ Источник пересылки: `{reply.forward.sender_id}`")
    elif state.me is not None:
        lines.append(f"👤 Вы: `{state.me.id}`")
    await ctx.done("\n".join(lines))


@command("info", args="[@user|reply]", cat=CAT, desc="информация о пользователе")
async def cmd_info(ctx: Ctx) -> None:
    user_id, entity, _ = await ctx.resolve_target()
    if user_id is None:
        await ctx.fail("Кого смотрим?")
        return
    if entity is None:
        try:
            entity = await ctx.client.get_entity(user_id)
        except Exception:
            await ctx.done(f"👤 `{user_id}` — подробностей нет.")
            return

    lines = [f"👤 **{fmt.name_of(entity)}**", f"🆔 `{user_id}`"]
    if isinstance(entity, User):
        if entity.username:
            lines.append(f"🔗 @{entity.username}")
        if entity.phone:
            lines.append(f"📞 `{entity.phone}`")
        flags = [f for f, on in (("бот", entity.bot), ("premium", entity.premium),
                                 ("verified", entity.verified),
                                 ("scam", entity.scam)) if on]
        if flags:
            lines.append("🏷 " + ", ".join(flags))
    elif isinstance(entity, (Chat, Channel)):
        lines.append("👥 участников: "
                     f"{getattr(entity, 'participants_count', None) or 'неизвестно'}")

    owner = state.userbot_owner()
    muted_here = (owner, ctx.chat_id, user_id) in state.mutes
    muted_global = (owner, 0, user_id) in state.mutes
    if muted_here or muted_global:
        lines.append("🔇 замучен: " + ("везде" if muted_global else "в этом чате"))

    cached = await db.scalar(
        "SELECT COUNT(*) FROM messages WHERE owner_id=? AND user_id=?",
        (owner, user_id))
    deleted = await db.scalar(
        "SELECT COUNT(*) FROM deleted WHERE owner_id=? AND user_id=?",
        (owner, user_id))
    lines.append(f"🗂 в кэше: {cached} · удалено: {deleted}")
    await ctx.done("\n".join(lines))


@command("stats", args="", cat=CAT, desc="статистика базы и бота")
async def cmd_stats(ctx: Ctx) -> None:
    s = await db.stats(state.userbot_owner())
    await ctx.done(
        "📊 **Статистика**\n"
        f"🗂 в кэше: **{s['cached']}**\n"
        f"🗑 удалённых сохранено: **{s['deleted']}**\n"
        f"✏️ правок: **{s['edits']}**\n"
        f"🔇 мутов: **{s['mutes']}**\n"
        f"📒 заметок: **{s['notes']}**\n"
        f"💾 размер базы: **{fmt.size(s['size'])}**\n"
        f"⏱ аптайм: **{fmt.uptime(time.time() - state.start_time)}**\n"
        f"🧹 TTL кэша: {config.CACHE_TTL_HOURS} ч · журнала: {config.DELETED_TTL_DAYS} дн"
    )


@command("json", args="(реплаем)", cat=CAT, desc="сырой объект сообщения")
async def cmd_json(ctx: Ctx) -> None:
    reply = await ctx.reply_msg()
    target = reply if reply is not None else ctx.msg
    await ctx.done(f"```\n{fmt.truncate(target.stringify(), 3500)}\n```")
